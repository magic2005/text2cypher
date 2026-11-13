"""Run the isolated 30-question set with checkpointed, reviewable evidence.

The Neo4j driver and the model configuration are imported lazily, only after
argument parsing.  ``--check-dataset`` and the offline unit tests therefore
work on a machine without Neo4j, Streamlit or model credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from evaluation.benchmark import (
    QUESTIONS_PATH,
    SCORE_RULE,
    atomic_write_json,
    build_record,
    canonical,
    dataset_info,
    dataset_sha256,
    load_questions,
    pending_record,
    safe_output_path,
    score_value,
    summarize,
    validate_questions,
)

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "evaluation" / "latest_run.json"
QUESTIONS = load_questions()
DATASET_SHA256 = dataset_sha256()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending(item, reason="尚未运行"):
    """Keep the historical helper name used by older review scripts."""
    return pending_record(item, reason)


def snapshot_fingerprint(graph):
    """Fingerprint the dataset version counts without touching any properties."""
    import hashlib

    rows = graph.run(
        "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
    )
    counts = {str(row["version"]): row["node_count"] for row in rows}
    return hashlib.sha256(json.dumps(counts, sort_keys=True).encode("utf-8")).hexdigest()


def _model_label(cfg):
    if cfg.get("model"):
        return cfg["model"]
    from agent.core import PROVIDERS

    return PROVIDERS[cfg["provider"]][1]


def build_report(
    records,
    cfg,
    graph_before=None,
    graph_after=None,
    *,
    metadata=None,
    questions=None,
    destination=None,
):
    """Build the report dict without writing it (useful for review tools)."""
    questions = QUESTIONS if questions is None else questions
    summary = summarize(records, questions)
    expected = questions[0].get("graph_fingerprint") if questions else None
    graph_drift = bool(
        expected
        and (
            (graph_before and graph_before != expected)
            or (graph_after and graph_after != expected)
        )
    )
    report = {
        "date_utc": _utc_now(),
        "dataset": "independent local 30",
        "provider": cfg.get("provider"),
        "model": _model_label(cfg),
        "total": summary["total"],
        "reference_graph_fingerprint": expected,
        "observed_graph_fingerprint_start": graph_before,
        "observed_graph_fingerprint_end": graph_after,
        "graph_drift": graph_drift,
        "evaluated": summary["evaluated"],
        "verified_correct": summary["verified_correct"],
        "needs_review": summary["needs_review"],
        "pending": summary["pending"],
        "score": score_value(summary["items"], graph_drift),
        "score_rule": SCORE_RULE,
        "dataset_sha256": DATASET_SHA256,
        "dataset_file": str(QUESTIONS_PATH.relative_to(ROOT)),
        "run": dict(metadata or {}),
        "items": summary["items"],
    }
    return report


def save_report(
    records,
    cfg,
    graph_before=None,
    graph_after=None,
    *,
    metadata=None,
    questions=None,
    destination=None,
):
    """Atomically write a report; existing positional callers stay compatible."""
    report = build_report(
        records,
        cfg,
        graph_before,
        graph_after,
        metadata=metadata,
        questions=questions,
        destination=destination,
    )
    atomic_write_json(Path(destination) if destination else DEST, report)
    return report


def resolve_output(name):
    return safe_output_path(ROOT / "evaluation", name, reserved=("questions.json",))


def load_resume_records(path):
    path = Path(path)
    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    stored = report.get("dataset_sha256")
    if stored and stored != DATASET_SHA256:
        raise RuntimeError(
            "题目集已变化（dataset_sha256 不一致），不能沿用旧报告；"
            "请使用新的 --output 或从头运行"
        )
    known = {item["id"] for item in QUESTIONS}
    return {
        item["id"]: item
        for item in report.get("items", [])
        if item.get("attempted") and item.get("id") in known
    }


def evaluate_one(item, graph, cfg, ask_fn=None):
    """Run one question through the agent; injectable for isolation tests."""
    if ask_fn is None:
        from agent.core import ask as ask_fn
    actual = ask_fn(item["question"], graph, cfg, level=item["level"])
    return build_record(item, actual)


def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def _print_progress(report, item_id):
    print(
        json.dumps(
            {
                "id": item_id,
                "attempted": report["evaluated"],
                "verified_correct": report["verified_correct"],
                "needs_review": report["needs_review"],
            }
        ),
        flush=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=_positive_int, default=30, help="最多执行多少道尚未运行的题"
    )
    parser.add_argument(
        "--resume", action="store_true", help="保留已有模型记录，仅执行剩余题"
    )
    parser.add_argument(
        "--only", nargs="+", help="只重跑指定题号；与 --resume 合用可保留其余逐题记录"
    )
    parser.add_argument(
        "--output",
        default=DEST.name,
        help="结果文件名，必须留在 evaluation/ 内（默认 latest_run.json）",
    )
    parser.add_argument(
        "--force", action="store_true", help="允许覆盖 --output 指定的已有结果文件"
    )
    parser.add_argument(
        "--check-dataset", action="store_true", help="只校验题集，不连接图谱"
    )
    args = parser.parse_args(argv)

    if args.check_dataset:
        problems = validate_questions(QUESTIONS)
        payload = dataset_info(QUESTIONS)
        payload["problems"] = problems
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    try:
        output = resolve_output(args.output)
    except ValueError as error:
        parser.error(str(error))
    if args.resume and not output.exists():
        parser.error(f"--resume 需要已有报告：{output}")
    if output.exists() and not args.resume and not args.force and output != DEST:
        parser.error(
            f"输出文件已存在：{output.name}；请使用 --resume 续跑或 --force 覆盖"
        )
    selected = set(args.only or [])
    unknown = selected - {item["id"] for item in QUESTIONS}
    if unknown:
        parser.error("未知题号：" + ", ".join(sorted(unknown)))

    from agent.core import Graph, configuration

    cfg = configuration()
    try:
        records = load_resume_records(output) if args.resume else {}
    except RuntimeError as error:
        parser.error(str(error))
    if selected:
        for item_id in selected:
            records.pop(item_id, None)

    previous = json.loads(output.read_text(encoding="utf-8")) if args.resume else {}
    started_at = (
        (previous.get("run") or {}).get("started_at_utc")
        or previous.get("date_utc")
        or _utc_now()
    )
    metadata = {
        "mode": "resume" if args.resume else "fresh",
        "status": "running",
        "started_at_utc": started_at,
        "updated_at_utc": _utc_now(),
        "finished_at_utc": None,
        "dataset_file": str(QUESTIONS_PATH.relative_to(ROOT)),
        "dataset_sha256": DATASET_SHA256,
        "output_file": output.name,
        "limit": args.limit,
        "only": sorted(selected) or None,
    }

    if not cfg.get("key"):
        for item in QUESTIONS:
            records.setdefault(
                item["id"],
                pending_record(
                    item, "未配置 DeepSeek/GLM/Kimi 模型密钥；未进行模型评测"
                ),
            )
        metadata.update(
            status="no_key",
            updated_at_utc=_utc_now(),
            finished_at_utc=_utc_now(),
        )
        report = save_report(
            records, cfg, metadata=metadata, destination=output
        )
        print(
            json.dumps(
                {
                    "evaluated": report["evaluated"],
                    "pending": report["pending"],
                    "score": report["score"],
                    "total": report["total"],
                }
            )
        )
        return 0

    graph = Graph(cfg)
    before = None
    after = None
    completed = False
    try:
        before = snapshot_fingerprint(graph)
        metadata["graph_fingerprint_start"] = before
        expected = QUESTIONS[0].get("graph_fingerprint")
        if expected and before != expected:
            raise RuntimeError(
                "图谱版本计数与参考答案不同；请先重建参考答案，不能沿用旧标准"
            )
        count = 0
        for item in QUESTIONS:
            if selected and item["id"] not in selected:
                continue
            if item["id"] in records:
                continue
            if count >= args.limit:
                break
            record = evaluate_one(item, graph, cfg)
            records[item["id"]] = record
            count += 1
            metadata["updated_at_utc"] = _utc_now()
            report = save_report(
                records, cfg, before, metadata=metadata, destination=output
            )
            _print_progress(report, item["id"])
        after = snapshot_fingerprint(graph)
        metadata.update(
            status="finished" if len(records) == len(QUESTIONS) else "partial",
            updated_at_utc=_utc_now(),
            finished_at_utc=_utc_now(),
            graph_fingerprint_end=after,
        )
        completed = True
    except RuntimeError as error:
        print(f"无法开始评测：{error}", file=sys.stderr)
        return 1
    finally:
        graph.close()
        if not completed:
            metadata.update(
                status="interrupted",
                updated_at_utc=_utc_now(),
                finished_at_utc=_utc_now(),
                graph_fingerprint_end=None,
            )
            try:
                save_report(
                    records,
                    cfg,
                    before,
                    None,
                    metadata=metadata,
                    destination=output,
                )
            except Exception as error:  # pragma: no cover - best effort only
                print(f"警告：中断报告未写入：{error}", file=sys.stderr)

    report = save_report(
        records, cfg, before, after, metadata=metadata, destination=output
    )
    print(
        json.dumps(
            {
                "evaluated": report["evaluated"],
                "verified_correct": report["verified_correct"],
                "needs_review": report["needs_review"],
                "pending": report["pending"],
                "score": report["score"],
                "total": report["total"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
