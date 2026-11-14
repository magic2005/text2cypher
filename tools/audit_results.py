"""Explain failed, incomplete and review-pending answers in an evaluation run."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from evaluation import benchmark
from tools._audit_common import (
    EVALUATION_DIR,
    ensure_distinct_output,
    output_path,
    read_json,
    report_items,
    resolve_input,
    semantic_value_set,
    row_delta,
    write_json,
)


def _actual_rows(item: dict) -> list:
    value = (
        item.get("execution_result")
        if item.get("execution_result") is not None
        else item.get("results")
        if item.get("results") is not None
        else item.get("actual_rows") or []
    )
    return value if isinstance(value, list) else []


def _stage_failures(stages) -> list[dict]:
    failures = []
    if not isinstance(stages, list):
        return failures
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if stage.get("passed") is False or stage.get("error"):
            failures.append(
                {
                    "stage": stage.get("stage"),
                    "subquery": stage.get("subquery"),
                    "error": stage.get("error"),
                    "message": stage.get("message"),
                }
            )
    return failures


def _failure_class(item: dict, attempted: bool, match: bool | None) -> str:
    if not attempted:
        return "pending"
    if item.get("error"):
        return "execution_error"
    if item.get("answer_complete") is False:
        return "incomplete_answer"
    if item.get("correct") is False:
        return "recorded_incorrect"
    if item.get("correct") is True and match is False:
        return "human_override_correct"
    if match is True and item.get("correct") is True:
        return "matched"
    if item.get("correct") is None:
        return "automatic_match_unconfirmed" if match is True else "needs_human_review"
    if match is False:
        return "answer_mismatch"
    return "record_inconsistent"


def audit_run(run: dict, questions: list[dict], *, include_all=False, question_path=None) -> dict:
    refs = {item["id"]: item for item in questions}
    records = report_items(run, "评测报告")
    items = []
    counts = Counter()
    for item_id, record in records.items():
        reference = refs.get(item_id)
        attempted = record.get("attempted") is True
        actual = _actual_rows(record)
        expected = reference.get("reference_rows", []) if reference else []
        match = None if reference is None or not attempted else (
            benchmark.canonical(expected) == benchmark.canonical(actual)
        )
        classification = _failure_class(record, attempted, match)
        counts[classification] += 1
        if not include_all and classification == "matched" and record.get("correct") is True:
            continue
        expected_values = semantic_value_set(expected)
        actual_values = semantic_value_set(actual)
        expected_rows = {next(iter(benchmark.canonical([row]))) for row in expected} if expected else set()
        actual_rows = {next(iter(benchmark.canonical([row]))) for row in actual} if actual else set()
        matched_rows = len(expected_rows & actual_rows)
        queries = record.get("actual_cypher")
        query_count = 1 if isinstance(queries, str) else len(queries) if isinstance(queries, list) else 0
        items.append(
            {
                "id": item_id,
                "level": record.get("level") or (reference or {}).get("level"),
                "question": record.get("question") or (reference or {}).get("question"),
                "attempted": attempted,
                "recorded_correct": record.get("correct"),
                "automatic_match": match,
                "classification": classification,
                "failure_reason": record.get("failure_reason"),
                "answer_complete": record.get("answer_complete"),
                "query_count": query_count,
                "actual_cypher": record.get("actual_cypher"),
                "reference_row_count": len(expected),
                "actual_row_count": len(actual),
                "expected_unique_value_count": len(expected_values),
                "actual_unique_value_count": len(actual_values),
                "matched_reference_rows": matched_rows,
                "reference_row_coverage": (
                    round(matched_rows / len(expected_rows), 4) if expected_rows else None
                ),
                "row_differences": row_delta(expected, actual),
                "missing_reference_values": sorted(expected_values - actual_values)[:20],
                "unexpected_values": sorted(actual_values - expected_values)[:20],
                "stage_failures": _stage_failures(record.get("stages")),
                "duplicate_rows_collapsed": record.get("duplicate_rows_collapsed"),
                "truncated_subqueries": record.get("truncated_subqueries", 0),
                "discarded_mismatched_route_rows": record.get(
                    "discarded_mismatched_route_rows", 0
                ),
                "discarded_partial_chain_rows": record.get(
                    "discarded_partial_chain_rows", 0
                ),
                "reference_missing": reference is None,
            }
        )
    for item_id in benchmark.EXPECTED_IDS:
        if item_id not in refs or item_id in records:
            continue
        counts["missing_record"] += 1
        if include_all:
            reference = refs[item_id]
            items.append(
                {
                    "id": item_id,
                    "level": reference.get("level"),
                    "question": reference.get("question"),
                    "attempted": False,
                    "recorded_correct": None,
                    "automatic_match": None,
                    "classification": "missing_record",
                    "reference_row_count": len(reference.get("reference_rows") or []),
                    "actual_row_count": 0,
                }
            )
    report_hash = run.get("dataset_sha256")
    current_hash = benchmark.dataset_sha256(question_path)
    return {
        "dataset_sha256": report_hash,
        "current_dataset_sha256": current_hash,
        "dataset_hash_matches": report_hash == current_hash,
        "run_score": run.get("score"),
        "graph_drift": run.get("graph_drift"),
        "graph_fingerprints": {
            "start": run.get("observed_graph_fingerprint_start")
            or run.get("graph_fingerprint_before"),
            "end": run.get("observed_graph_fingerprint_end")
            or run.get("graph_fingerprint_after"),
        },
        "counts": dict(sorted(counts.items())),
        "audited_item_count": len(items),
        "items": items,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="latest_run.json", help="评测报告路径")
    parser.add_argument("--questions", default="questions.json", help="对应题集路径")
    parser.add_argument("--output", help="可选审计 JSON 文件名")
    parser.add_argument("--all", action="store_true", help="也输出正确题与未执行题")
    args = parser.parse_args(argv)
    try:
        run_path = resolve_input(args.run)
        question_path = resolve_input(args.questions)
        run = read_json(run_path)
        questions = read_json(question_path)
        problems = benchmark.validate_questions(questions)
        if problems:
            raise ValueError("题集校验失败：" + "; ".join(problems[:8]))
        result = audit_run(run, questions, include_all=args.all, question_path=question_path)
        if args.output:
            destination = output_path(EVALUATION_DIR, args.output)
            ensure_distinct_output(destination, [run_path, question_path])
            write_json(destination, result)
        else:
            destination = None
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": str(destination) if destination else None,
                "dataset_hash_matches": result["dataset_hash_matches"],
                "graph_drift": result["graph_drift"],
                "counts": result["counts"],
                "items": result["items"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
