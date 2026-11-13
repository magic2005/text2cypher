"""Compare two preserved local-oracle runs without changing either record.

The tool does not assume that the historical filenames exist in every clone.
Pass ``--before`` / ``--after`` to compare a different pair, for example after
rerunning ``tools.mock_teacher_eval --local-oracle --force``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluation.benchmark import (
    EXPECTED_IDS,
    atomic_write_json,
    rows_equal,
    safe_output_path,
)

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "evaluation" / "mock_teacher_2026-10-14"
DEFAULT_BEFORE = "results_before_validation_and_chain_fix.json"
DEFAULT_AFTER = "results.json"


def read_run(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到运行记录：{path}；请先用 tools.mock_teacher_eval "
            "生成两份 local_oracle 记录，或用 --before/--after 指定路径"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def inspect(item):
    """Apply the local-oracle pass rule to one stored item."""
    if not item.get("gold_nonempty"):
        return False, "参考查询返回空结果"
    if item.get("error"):
        return False, item["error"]
    if item.get("answer_complete") is not True:
        return False, "流水线将答案标记为不完整"
    if not rows_equal(item.get("gold_rows") or [], item.get("actual_rows") or []):
        return False, "实际结果与参考结果的行及重复次数不一致"
    return True, None


def load_run_items(run, label):
    if run.get("evaluation_mode") != "local_oracle":
        raise RuntimeError(f"{label} 不是 local_oracle 运行，不能与本地替身结果比较")
    items = run.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"{label} 缺少 items 数组")
    mapped = {}
    for item in items:
        item_id = item.get("id")
        if item_id in mapped:
            raise RuntimeError(f"{label} 存在重复题号：{item_id}")
        mapped[item_id] = item
    return mapped


def _graph_stable(run):
    before = run.get("graph_fingerprint_before")
    after = run.get("graph_fingerprint_after")
    if before is None or after is None:
        return None
    return before == after


def build_comparison(before, after, expected_ids=EXPECTED_IDS):
    """Pure comparison of two loaded local-oracle runs."""
    expected = list(expected_ids)
    before_items = load_run_items(before, "before")
    after_items = load_run_items(after, "after")
    if (
        len(before_items) != len(expected)
        or set(before_items) != set(expected)
        or set(after_items) != set(expected)
    ):
        raise RuntimeError("前后试题不完整或题号不一致")

    rows = []
    for item_id in expected:
        old = before_items[item_id]
        new = after_items[item_id]
        if old.get("question") != new.get("question") or old.get(
            "gold_cypher"
        ) != new.get("gold_cypher"):
            raise RuntimeError(f"{item_id} 的问题或参考查询被改变")
        old_pass, old_reason = inspect(old)
        new_pass, new_reason = inspect(new)
        rows.append(
            {
                "id": item_id,
                "question": new.get("question"),
                "before_pass": old_pass,
                "after_pass": new_pass,
                "reference_rows_same": rows_equal(
                    old.get("gold_rows") or [], new.get("gold_rows") or []
                ),
                "before_rows": len(old.get("actual_rows") or []),
                "after_rows": len(new.get("actual_rows") or []),
                "reference_rows": len(new.get("gold_rows") or []),
                "before_reason": old_reason,
                "after_reason": new_reason,
            }
        )

    graph_stable = _graph_stable(after)
    old_graph_stable = _graph_stable(before)
    if old_graph_stable is None or graph_stable is None:
        same_graph = None
    else:
        same_graph = (
            old_graph_stable
            and graph_stable
            and before.get("graph_fingerprint_after")
            == after.get("graph_fingerprint_before")
        )
    return {
        "mode": "local_oracle",
        "graph_stable": graph_stable,
        "old_graph_stable": old_graph_stable,
        "same_graph_between_runs": same_graph,
        "all_reference_rows_same": all(
            row["reference_rows_same"] for row in rows
        ),
        "before_passed": sum(1 for row in rows if row["before_pass"]),
        "after_passed": (
            sum(1 for row in rows if row["after_pass"])
            if graph_stable is True
            else None
        ),
        "items": rows,
    }


def _input_path(name):
    path = Path(name)
    if path.is_absolute():
        return path
    return FOLDER / path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before", default=DEFAULT_BEFORE, help="修复前的 local_oracle 结果文件"
    )
    parser.add_argument(
        "--after", default=DEFAULT_AFTER, help="修复后的 local_oracle 结果文件"
    )
    parser.add_argument(
        "--output", default="comparison.json", help="输出文件名，必须留在模拟题目录内"
    )
    args = parser.parse_args(argv)

    try:
        output_path = safe_output_path(
            FOLDER, args.output, reserved=("questions.json",)
        )
    except ValueError as error:
        parser.error(str(error))
    try:
        before = read_run(_input_path(args.before))
        after = read_run(_input_path(args.after))
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        comparison = build_comparison(before, after)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    atomic_write_json(output_path, comparison)
    print(
        json.dumps(
            {
                "before_passed": comparison["before_passed"],
                "after_passed": comparison["after_passed"],
                "graph_stable": comparison["graph_stable"],
                "same_graph_between_runs": comparison["same_graph_between_runs"],
                "all_reference_rows_same": comparison["all_reference_rows_same"],
                "remaining_failures": [
                    row["id"] for row in comparison["items"] if not row["after_pass"]
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
