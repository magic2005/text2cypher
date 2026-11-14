"""Record one human verdict and recalculate the run's three-state score."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from evaluation import benchmark
from tools._audit_common import EVALUATION_DIR, graph_check, output_path, read_json, report_items, resolve_input, write_json


def _graph_drift(report: dict, questions: list[dict]) -> bool:
    if report.get("graph_drift") is True:
        return True
    expected = {item.get("graph_fingerprint") for item in questions if item.get("graph_fingerprint")}
    observed = graph_check(report)
    if observed["stable"] is False:
        return True
    return bool(expected and any(value not in expected for value in (observed["before"], observed["after"]) if value))


def _graph_evidence_complete(report: dict) -> bool:
    observed = graph_check(report)
    return observed["before"] is not None and observed["after"] is not None and observed["stable"] is True


def review(report: dict, questions: list[dict], item_id: str, judgment: str, reason: str, *, replace=False, dataset_matches=True) -> dict:
    records = report_items(report, "评测报告")
    question_by_id = {item["id"]: item for item in questions}
    unknown = records.keys() - question_by_id.keys()
    if unknown:
        raise ValueError("评测报告含有当前题集不存在的题号：" + ", ".join(sorted(unknown)))
    item = records.get(item_id)
    if item_id not in question_by_id:
        raise ValueError(f"题集不存在题号：{item_id}")
    if item is None or item.get("attempted") is not True:
        raise ValueError("只能复核已经实际执行的题目")
    if item.get("reviewed_at_utc") and not replace:
        raise ValueError("该题已有人工复核；如需改判，请传 --replace-review")
    if judgment not in {"correct", "incorrect"}:
        raise ValueError("judgment 必须是 correct 或 incorrect")
    reason = reason.strip()
    if not reason:
        raise ValueError("人工复核依据不能为空")
    is_correct = judgment == "correct"
    item.update(
        correct=is_correct,
        failure_reason=None if is_correct else reason,
        review_reason=reason,
        reviewed_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    summary = benchmark.summarize(records, questions)
    drift = _graph_drift(report, questions)
    graph_evidence_complete = _graph_evidence_complete(report)
    report.update(
        items=summary["items"],
        total=summary["total"],
        evaluated=summary["evaluated"],
        verified_correct=summary["verified_correct"],
        needs_review=summary["needs_review"],
        pending=summary["pending"],
        graph_drift=drift,
        score=benchmark.score_value(summary["items"], drift or not graph_evidence_complete)
        if dataset_matches
        else None,
        score_rule=benchmark.SCORE_RULE,
        dataset_matches_current=dataset_matches,
        graph_evidence_complete=graph_evidence_complete,
        last_review={
            "id": item_id,
            "judgment": judgment,
            "reason": reason,
            "reviewed_at_utc": item["reviewed_at_utc"],
        },
    )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", help="题号，如 M03")
    parser.add_argument("judgment", choices=("correct", "incorrect"))
    parser.add_argument("--reason", required=True, help="人工核对依据")
    parser.add_argument("--input", default="latest_run.json", help="评测报告路径")
    parser.add_argument("--output", help="输出文件名，默认覆盖输入报告")
    parser.add_argument("--questions", default="questions.json", help="报告对应题集")
    parser.add_argument("--replace-review", action="store_true", help="允许替换已有人工结论")
    parser.add_argument("--allow-dataset-mismatch", action="store_true", help="允许复核旧题，但保持总分 null")
    args = parser.parse_args(argv)
    try:
        input_path = resolve_input(args.input)
        question_path = resolve_input(args.questions)
        destination = output_path(EVALUATION_DIR, args.output or input_path.name)
        report = read_json(input_path)
        questions = read_json(question_path)
        problems = benchmark.validate_questions(questions)
        if problems:
            raise ValueError("题集校验失败：" + "; ".join(problems[:8]))
        current_hash = benchmark.dataset_sha256(question_path)
        stored_hash = report.get("dataset_sha256")
        matches = bool(stored_hash and stored_hash == current_hash)
        if not matches and not args.allow_dataset_mismatch:
            raise ValueError(
                "评测报告与当前题集的 SHA-256 不一致；请指定匹配的 --questions，"
                "或用 --allow-dataset-mismatch 只记录复核并保持总分 null"
            )
        result = review(
            report,
            questions,
            args.id,
            args.judgment,
            args.reason,
            replace=args.replace_review,
            dataset_matches=matches,
        )
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    write_json(destination, result)
    print(
        json.dumps(
            {
                "output": str(destination),
                "id": args.id,
                "correct": result["last_review"]["judgment"] == "correct",
                "evaluated": result.get("evaluated"),
                "verified_correct": result.get("verified_correct"),
                "needs_review": result.get("needs_review"),
                "pending": result.get("pending"),
                "dataset_matches_current": result["dataset_matches_current"],
                "graph_drift": result["graph_drift"],
                "graph_evidence_complete": result["graph_evidence_complete"],
                "score": result["score"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
