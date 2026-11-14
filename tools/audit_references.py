"""Audit old and rebuilt reference answers without changing either dataset."""

from __future__ import annotations

import argparse
import json
import sys

from evaluation import benchmark
from tools._audit_common import (
    EVALUATION_DIR,
    ensure_distinct_output,
    output_path,
    read_json,
    report_items,
    resolve_input,
    write_json,
)


DEFAULT_BEFORE = "questions_before_semantic_audit.json"


def _dataset_items(document, label: str) -> list[dict]:
    if isinstance(document, list):
        items = document
    elif isinstance(document, dict) and isinstance(document.get("items"), list):
        items = document["items"]
    else:
        raise ValueError(f"{label} 必须是题目数组或包含 items 数组的候选报告")
    mapped = report_items({"items": items}, label)
    for item_id, item in mapped.items():
        if not isinstance(item.get("question"), str):
            raise ValueError(f"{label} 的 {item_id} 缺少 question")
        if not isinstance(item.get("reference_rows"), list):
            raise ValueError(f"{label} 的 {item_id} 缺少 reference_rows 数组")
    return items


def _query_signature(value) -> str:
    queries = value if isinstance(value, list) else [value]
    return "\n---\n".join(" ".join(str(query).split()) for query in queries)


def audit(before_items: list[dict], after_items: list[dict], judgments=None) -> dict:
    before = report_items({"items": before_items}, "旧参考集")
    after = report_items({"items": after_items}, "新参考集")
    judgments = judgments or {}
    all_ids = list(dict.fromkeys([item["id"] for item in after_items] + [item["id"] for item in before_items]))
    rows = []
    for item_id in all_ids:
        old = before.get(item_id)
        new = after.get(item_id)
        if old is None or new is None:
            rows.append(
                {
                    "id": item_id,
                    "status": "added" if old is None else "removed",
                    "old_rows": len((old or {}).get("reference_rows") or []),
                    "new_rows": len((new or {}).get("reference_rows") or []),
                    "prior_judgment_basis": (judgments.get(item_id) or {}).get("review_reason"),
                }
            )
            continue
        old_rows = old.get("reference_rows") or []
        new_rows = new.get("reference_rows") or []
        old_values = benchmark.canonical(old_rows)
        new_values = benchmark.canonical(new_rows)
        old_fp = old.get("graph_fingerprint")
        new_fp = new.get("graph_fingerprint")
        query_changed = _query_signature(old.get("reference_cypher")) != _query_signature(
            new.get("reference_cypher")
        )
        scope_changed = old.get("reference_scope") != new.get("reference_scope")
        values_changed = old_values != new_values
        row_count_changed = len(old_rows) != len(new_rows)
        question_changed = old.get("question") != new.get("question")
        status = (
            "changed"
            if question_changed or query_changed or scope_changed or values_changed or row_count_changed
            else "unchanged"
        )
        rows.append(
            {
                "id": item_id,
                "question": new["question"],
                "status": status,
                "question_changed": question_changed,
                "reference_cypher_changed": query_changed,
                "reference_scope_changed": scope_changed,
                "reference_values_changed": values_changed,
                "old_rows": len(old_rows),
                "new_rows": len(new_rows),
                "old_unique_rows": len({benchmark.row_key(row) for row in old_rows}),
                "new_unique_rows": len({benchmark.row_key(row) for row in new_rows}),
                "old_values_missing": len(old_values - new_values),
                "new_values_added": len(new_values - old_values),
                "old_values_missing_sample": sorted(old_values - new_values)[:10],
                "new_values_added_sample": sorted(new_values - old_values)[:10],
                "old_values_subset_of_new": old_values <= new_values,
                "row_count_changed": row_count_changed,
                "exact_reference_rows_changed": benchmark.canonical_multiset(old_rows)
                != benchmark.canonical_multiset(new_rows),
                "same_graph_fingerprint": old_fp == new_fp if old_fp and new_fp else None,
                "old_graph_fingerprint": old_fp,
                "new_graph_fingerprint": new_fp,
                "new_reference_complete": new.get("reference_complete"),
                "new_reference_unique": new.get("reference_unique"),
                "new_reference_error": new.get("reference_error"),
                "prior_judgment_basis": (judgments.get(item_id) or {}).get("review_reason")
                or (judgments.get(item_id) or {}).get("failure_reason"),
            }
        )
    fingerprints_before = {
        item.get("graph_fingerprint") for item in before_items if item.get("graph_fingerprint")
    }
    fingerprints_after = {
        item.get("graph_fingerprint") for item in after_items if item.get("graph_fingerprint")
    }
    validation_problems = benchmark.validate_questions(after_items)
    all_before_have_fingerprint = all(item.get("graph_fingerprint") for item in before_items)
    all_after_have_fingerprint = all(item.get("graph_fingerprint") for item in after_items)
    return {
        "total_before": len(before_items),
        "total_after": len(after_items),
        "same_question_ids": set(before) == set(after),
        "same_question_text": all(
            item_id in before and item_id in after
            and before[item_id].get("question") == after[item_id].get("question")
            for item_id in set(before) & set(after)
        ),
        "old_graph_fingerprints": sorted(value for value in fingerprints_before if value),
        "new_graph_fingerprints": sorted(value for value in fingerprints_after if value),
        "same_graph_snapshot": (
            fingerprints_before == fingerprints_after
            if before_items and after_items and all_before_have_fingerprint and all_after_have_fingerprint
            else None
        ),
        "valid_new_dataset": not validation_problems,
        "new_dataset_validation_problems": validation_problems,
        "changed_ids": [row["id"] for row in rows if row.get("status") == "changed"],
        "added_ids": [row["id"] for row in rows if row.get("status") == "added"],
        "removed_ids": [row["id"] for row in rows if row.get("status") == "removed"],
        "items": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default=DEFAULT_BEFORE, help="旧题集路径，默认 evaluation/questions_before_semantic_audit.json")
    parser.add_argument("--after", default="questions.json", help="新题集或 build_evaluation 候选报告")
    parser.add_argument("--prior-run", help="可选旧评测报告，用于附上逐题人工复核依据")
    parser.add_argument("--output", default="reference_audit.json", help="审计报告文件名")
    args = parser.parse_args(argv)
    try:
        destination = output_path(EVALUATION_DIR, args.output)
        before_path = resolve_input(args.before)
        after_path = resolve_input(args.after)
        before = _dataset_items(read_json(before_path), "旧参考集")
        after = _dataset_items(read_json(after_path), "新参考集")
        judgments = {}
        if args.prior_run:
            judgments = report_items(read_json(resolve_input(args.prior_run)), "旧评测报告")
        result = audit(before, after, judgments)
        ensure_distinct_output(
            destination,
            [before_path, after_path] + ([resolve_input(args.prior_run)] if args.prior_run else []),
        )
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    result["inputs"] = {"before": str(before_path), "after": str(after_path)}
    if args.prior_run:
        result["inputs"]["prior_run"] = str(resolve_input(args.prior_run))
    write_json(destination, result)
    print(
        json.dumps(
            {
                "output": str(destination),
                "same_question_ids": result["same_question_ids"],
                "same_question_text": result["same_question_text"],
                "same_graph_snapshot": result["same_graph_snapshot"],
                "valid_new_dataset": result["valid_new_dataset"],
                "changed": result["changed_ids"],
                "added": result["added_ids"],
                "removed": result["removed_ids"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["valid_new_dataset"] and result["same_question_ids"] and result["same_question_text"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
