"""Compare two model-evaluation reports while preserving score boundaries."""

from __future__ import annotations

import argparse
import json
import sys

from evaluation.benchmark import EXPECTED_IDS, canonical, dataset_sha256, validate_questions
from tools._audit_common import (
    EVALUATION_DIR,
    ensure_distinct_output,
    graph_check,
    output_path,
    read_json,
    report_items,
    resolve_input,
    row_delta,
    semantic_row_counter,
    write_json,
)


def _rows(item: dict):
    if item.get("execution_result") is not None:
        return item.get("execution_result") or []
    return item.get("results") or item.get("actual_rows") or []


def _report_score(records: dict[str, dict]):
    if set(records) != set(EXPECTED_IDS) or any(
        not item.get("attempted")
        or (item.get("correct") is not True and item.get("correct") is not False)
        for item in records.values()
    ):
        return None
    return sum(item.get("correct") is True for item in records.values())


def build_comparison(before: dict, after: dict, *, reference_audit=None, questions=None) -> dict:
    old = report_items(before, "旧评测报告")
    new = report_items(after, "新评测报告")
    common = sorted(old.keys() & new.keys(), key=lambda item_id: (EXPECTED_IDS.index(item_id) if item_id in EXPECTED_IDS else len(EXPECTED_IDS), item_id))
    old_only = sorted(old.keys() - new.keys())
    new_only = sorted(new.keys() - old.keys())
    same_questions = all(
        old[item_id].get("question") == new[item_id].get("question")
        and old[item_id].get("level") == new[item_id].get("level")
        for item_id in common
    )
    old_hash = before.get("dataset_sha256")
    new_hash = after.get("dataset_sha256")
    current_hash = dataset_sha256()
    same_dataset_hash = old_hash == new_hash if old_hash and new_hash else None
    dataset_compatible = (
        same_dataset_hash is True
        and same_questions
        and not old_only
        and not new_only
        and set(old) == set(EXPECTED_IDS)
        and set(new) == set(EXPECTED_IDS)
    )
    old_graph = graph_check(before)
    new_graph = graph_check(after)
    same_graph = (
        old_graph["stable"] is True
        and new_graph["stable"] is True
        and old_graph["after"] is not None
        and old_graph["after"] == new_graph["before"]
    )
    audit_items = {}
    if reference_audit:
        if isinstance(reference_audit, dict) and isinstance(reference_audit.get("items"), list):
            audit_items = {item.get("id"): item for item in reference_audit["items"] if isinstance(item, dict)}
    question_items = {item["id"]: item for item in (questions or []) if isinstance(item, dict)}
    references_available = bool(
        question_items
        and old_hash == current_hash
        and new_hash == current_hash
        and set(question_items) == set(EXPECTED_IDS)
    )

    rows = []
    for item_id in common:
        old_item, new_item = old[item_id], new[item_id]
        old_actual, new_actual = _rows(old_item), _rows(new_item)
        old_reference = old_item.get("reference_rows") or (
            question_items.get(item_id, {}).get("reference_rows") if references_available else []
        )
        new_reference = new_item.get("reference_rows") or (
            question_items.get(item_id, {}).get("reference_rows") if references_available else []
        )
        reference = audit_items.get(item_id, {})
        rows.append(
            {
                "id": item_id,
                "question": new_item.get("question"),
                "level": new_item.get("level"),
                "before_correct": old_item.get("correct"),
                "after_correct": new_item.get("correct"),
                "judgment_changed": old_item.get("correct") != new_item.get("correct"),
                "before_failure_reason": old_item.get("failure_reason"),
                "after_failure_reason": new_item.get("failure_reason"),
                "before_answer_complete": old_item.get("answer_complete"),
                "after_answer_complete": new_item.get("answer_complete"),
                "before_query_count": len(old_item.get("actual_cypher") or []) if not isinstance(old_item.get("actual_cypher"), str) else 1,
                "after_query_count": len(new_item.get("actual_cypher") or []) if not isinstance(new_item.get("actual_cypher"), str) else 1,
                "before_actual_rows": len(old_actual),
                "after_actual_rows": len(new_actual),
                "actual_values_match_scoring_rule": canonical(old_actual) == canonical(new_actual),
                "actual_rows_match_with_multiplicity": semantic_row_counter(old_actual) == semantic_row_counter(new_actual),
                "actual_row_differences": row_delta(old_actual, new_actual, preserve_duplicates=True),
                "before_actual_vs_reference": row_delta(old_reference, old_actual, preserve_duplicates=True) if old_reference else None,
                "after_actual_vs_reference": row_delta(new_reference, new_actual, preserve_duplicates=True) if new_reference else None,
                "reference_rows_changed": reference.get("reference_values_changed"),
                "reference_query_changed": reference.get("reference_cypher_changed"),
                "reference_scope_changed": reference.get("reference_scope_changed"),
            }
        )
    old_score = _report_score(old)
    new_score = _report_score(new)
    score_comparable = bool(
        dataset_compatible
        and same_graph
        and old_score is not None
        and new_score is not None
        and len(old) == len(EXPECTED_IDS)
        and len(new) == len(EXPECTED_IDS)
    )
    return {
        "before_dataset_sha256": old_hash,
        "after_dataset_sha256": new_hash,
        "current_dataset_sha256": current_hash,
        "same_dataset_hash": same_dataset_hash,
        "same_question_text_and_level": same_questions,
        "before_only_ids": old_only,
        "after_only_ids": new_only,
        "dataset_compatible": dataset_compatible,
        "before_graph": old_graph,
        "after_graph": new_graph,
        "same_graph_between_runs": same_graph,
        "before_score": old_score,
        "after_score": new_score,
        "before_reported_score": before.get("score"),
        "after_reported_score": after.get("score"),
        "reference_answers_available": references_available,
        "score_comparable": score_comparable,
        "score_delta": new_score - old_score if score_comparable else None,
        "before_needs_review": before.get("needs_review"),
        "after_needs_review": after.get("needs_review"),
        "judgments_changed": [row["id"] for row in rows if row["judgment_changed"]],
        "items": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default="run_before_semantic_fixes.json", help="旧评测报告")
    parser.add_argument("--after", default="latest_run.json", help="新评测报告")
    parser.add_argument("--reference-audit", help="可选 audit_references.py 输出")
    parser.add_argument("--questions", default="questions.json", help="题集，用于结果与参考答案差异")
    parser.add_argument("--output", default="run_comparison.json", help="对比报告文件名")
    parser.add_argument("--require-comparable", action="store_true", help="数据集或图谱不可比时返回失败状态")
    args = parser.parse_args(argv)
    try:
        before_path = resolve_input(args.before)
        after_path = resolve_input(args.after)
        destination = output_path(EVALUATION_DIR, args.output)
        before = read_json(before_path)
        after = read_json(after_path)
        audit = read_json(resolve_input(args.reference_audit)) if args.reference_audit else None
        questions = read_json(resolve_input(args.questions))
        problems = validate_questions(questions)
        if problems:
            raise ValueError("题集校验失败：" + "; ".join(problems[:8]))
        result = build_comparison(before, after, reference_audit=audit, questions=questions)
        ensure_distinct_output(
            destination,
            [before_path, after_path, resolve_input(args.questions)]
            + ([resolve_input(args.reference_audit)] if args.reference_audit else []),
        )
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    result["inputs"] = {"before": str(before_path), "after": str(after_path)}
    if args.reference_audit:
        result["inputs"]["reference_audit"] = str(resolve_input(args.reference_audit))
    write_json(destination, result)
    print(
        json.dumps(
            {
                "output": str(destination),
                "before_score": result["before_score"],
                "after_score": result["after_score"],
                "score_comparable": result["score_comparable"],
                "score_delta": result["score_delta"],
                "same_graph_between_runs": result["same_graph_between_runs"],
                "judgments_changed": result["judgments_changed"],
            },
            ensure_ascii=False,
        )
    )
    if args.require_comparable and not result["score_comparable"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
