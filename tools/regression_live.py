"""Replay archived Cypher through the current agent on the read-only graph."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from evaluation import benchmark
from tools._audit_common import (
    EVALUATION_DIR,
    output_path,
    read_json,
    report_items,
    resolve_input,
    write_json,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(graph) -> tuple[str, dict]:
    rows = graph.run(
        "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
    )
    counts = {str(row["version"]): row["node_count"] for row in rows}
    return benchmark.fingerprint_from_counts(counts), counts


def _queries(value):
    if isinstance(value, str):
        result = [value]
    elif isinstance(value, list):
        result = value
    else:
        result = []
    if not result or not all(isinstance(query, str) and query.strip() for query in result):
        raise ValueError("原评测项没有可回放的 actual_cypher")
    return result


def _item_record(prior: dict, replay: dict) -> dict:
    return {
        "id": prior["id"],
        "question": prior.get("question"),
        "level": prior.get("level"),
        "source_cypher": prior.get("actual_cypher"),
        "source_correct": prior.get("correct"),
        "source_failure_reason": prior.get("failure_reason"),
        "replayed_cypher": replay.get("queries"),
        "replayed_rows": replay.get("results"),
        "replayed_row_count": len(replay.get("results") or []),
        "answer_complete": replay.get("answer_complete"),
        "error": replay.get("error"),
        "summary": replay.get("summary"),
        "stages": replay.get("stages"),
        "discarded_mismatched_route_rows": replay.get("discarded_mismatched_route_rows", 0),
        "discarded_partial_chain_rows": replay.get("discarded_partial_chain_rows", 0),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="latest_run.json", help="包含旧 actual_cypher 的评测报告")
    parser.add_argument("--only", nargs="+", help="只回放指定题号")
    parser.add_argument("--output", default="regression_live.json", help="回放结果文件名")
    parser.add_argument("--resume", action="store_true", help="按输入报告哈希续跑")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有回放文件")
    parser.add_argument("--retry-failed", action="store_true", help="续跑时重新执行上次失败的题")
    args = parser.parse_args(argv)
    try:
        input_path = resolve_input(args.input)
        destination = output_path(EVALUATION_DIR, args.output)
        source = read_json(input_path)
        source_items = report_items(source, "输入评测报告")
        selected = set(args.only or source_items.keys())
        unknown = selected - source_items.keys()
        if unknown:
            raise ValueError("输入报告没有题号：" + ", ".join(sorted(unknown)))
        if input_path.resolve() == destination.resolve():
            raise ValueError("回放输出不能覆盖输入评测报告")
        source_hash = benchmark.file_sha256(input_path)
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if destination.exists() and not args.resume and not args.force:
        print(f"输出已存在：{destination.name}；请使用 --resume 或 --force", file=sys.stderr)
        return 2
    if args.resume:
        try:
            report = read_json(destination)
        except (FileNotFoundError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        if report.get("source_sha256") != source_hash:
            print("输入评测报告已变化，拒绝续跑。", file=sys.stderr)
            return 1
        if set(report.get("selected_ids") or []) != selected:
            print("--only 与已有回放文件的题号范围不同，拒绝续跑。", file=sys.stderr)
            return 1
        prior_items = list(report_items(report, "回放结果").values())
        completed = {
            item["id"]: item
            for item in prior_items
            if isinstance(item.get("id"), str)
            and item.get("finished") is True
            and not (args.retry_failed and item.get("error"))
        }
        if args.retry_failed:
            previous_failures = [item for item in prior_items if item.get("finished") is True and item.get("error")]
            previous_attempts = report.get("previous_failed_attempts")
            if not isinstance(previous_attempts, list):
                previous_attempts = []
            report["previous_failed_attempts"] = previous_attempts + previous_failures
    else:
        completed = {}
        report = {
            "source_file": str(input_path),
            "source_sha256": source_hash,
            "selected_ids": sorted(selected),
            "external_model_calls": 0,
            "created_at_utc": _utc_now(),
            "items": [],
        }
    report["items"] = list(completed.values())

    from agent import core

    try:
        cfg = core.configuration()
        graph = core.Graph(cfg)
    except Exception as error:
        print(f"无法连接 Neo4j：{error}", file=sys.stderr)
        return 1
    before = None
    before_counts = {}
    after = None
    after_counts = {}
    caught = None
    try:
        before, before_counts = _fingerprint(graph)
        report.update(
            graph_fingerprint_start=before,
            graph_version_counts_start=before_counts,
            reference_graph_fingerprint=source.get("reference_graph_fingerprint"),
            graph_matches_reference=(
                before == source.get("reference_graph_fingerprint")
                if source.get("reference_graph_fingerprint")
                else None
            ),
            started_at_utc=report.get("started_at_utc") or _utc_now(),
            status="running",
        )
        write_json(destination, report)
        for item_id in source_items:
            if item_id not in selected or item_id in completed:
                continue
            prior = source_items[item_id]
            try:
                cypher = _queries(prior.get("actual_cypher"))
                serialized = json.dumps({"queries": cypher}, ensure_ascii=False)

                def replay_model(_messages, _cfg, payload=serialized):
                    return payload

                replay = core.ask(
                    prior.get("question") or "",
                    graph,
                    cfg,
                    model=replay_model,
                    level=prior.get("level") or "自动",
                )
                row = _item_record(prior, replay)
                row["error"] = row["error"] or None
            except Exception as error:
                row = {
                    "id": item_id,
                    "question": prior.get("question"),
                    "level": prior.get("level"),
                    "source_cypher": prior.get("actual_cypher"),
                    "source_correct": prior.get("correct"),
                    "error": f"{type(error).__name__}: {error}",
                    "finished": True,
                }
            row.update(finished=True, replayed_at_utc=_utc_now())
            completed[item_id] = row
            report["items"] = [completed[key] for key in source_items if key in completed]
            report["updated_at_utc"] = _utc_now()
            write_json(destination, report)
        after, after_counts = _fingerprint(graph)
        report.update(
            graph_fingerprint_end=after,
            graph_version_counts_end=after_counts,
            graph_stable=before == after,
            graph_matches_reference=(
                before == source.get("reference_graph_fingerprint") == after
                if source.get("reference_graph_fingerprint")
                else None
            ),
            status="finished" if selected <= completed.keys() else "partial",
            finished_at_utc=_utc_now(),
            updated_at_utc=_utc_now(),
        )
    except Exception as error:
        caught = error
        report.update(
            status="interrupted",
            error=f"{type(error).__name__}: {error}",
            updated_at_utc=_utc_now(),
            finished_at_utc=_utc_now(),
        )
    finally:
        graph.close()
        if caught:
            try:
                write_json(destination, report)
            except Exception as write_error:
                print(f"无法保存中断状态：{write_error}", file=sys.stderr)
    write_json(destination, report)
    print(
        json.dumps(
            {
                "output": str(destination),
                "status": report.get("status"),
                "replayed": sum(item_id in completed for item_id in selected),
                "selected": len(selected),
                "graph_stable": report.get("graph_stable"),
                "failed": [item["id"] for item in report["items"] if item.get("error")],
            },
            ensure_ascii=False,
        )
    )
    return 1 if caught else 0


if __name__ == "__main__":
    raise SystemExit(main())
