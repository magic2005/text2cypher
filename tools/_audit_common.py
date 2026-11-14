"""Shared, offline-safe helpers for reference and regression audit tools."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from evaluation.benchmark import atomic_write_json, safe_output_path


EVALUATION_DIR = Path(__file__).resolve().parents[1] / "evaluation"


def resolve_input(name: str | Path, base: Path = EVALUATION_DIR) -> Path:
    """Resolve an input path; relative paths are relative to the evaluation dir."""
    path = Path(name).expanduser()
    return path if path.is_absolute() else base / path


def read_json(path: str | Path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 格式错误：{path}:{error.lineno}:{error.colno}: {error.msg}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"JSON 文件不是 UTF-8 编码：{path}") from error


def report_items(report, label: str) -> dict[str, dict]:
    """Index report items by ID and reject malformed or duplicated entries."""
    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        raise ValueError(f"{label} 必须包含 items 数组")
    mapped = {}
    for index, item in enumerate(report["items"], 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError(f"{label} 第 {index} 项缺少字符串 id")
        item_id = item["id"]
        if item_id in mapped:
            raise ValueError(f"{label} 存在重复题号：{item_id}")
        mapped[item_id] = item
    return mapped


def semantic_row_key(row) -> str:
    """Row identity used by the scoring rule: ignore aliases and column order."""
    if not isinstance(row, dict):
        raise ValueError("结果行必须是对象")
    values = [json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) for value in row.values()]
    return json.dumps(sorted(values), ensure_ascii=False)


def semantic_row_counter(rows) -> Counter:
    return Counter(semantic_row_key(row) for row in (rows or []))


def semantic_value_set(rows) -> set[str]:
    return {
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        for row in (rows or []) if isinstance(row, dict)
        for value in row.values()
    }


def row_delta(expected, actual, *, preserve_duplicates=False):
    """Return row-set or row-multiset differences without changing either input."""
    if preserve_duplicates:
        left = semantic_row_counter(expected)
        right = semantic_row_counter(actual)
        missing = left - right
        extra = right - left
        return {
            "matched_rows": sum((left & right).values()),
            "missing_row_kinds": len(missing),
            "missing_row_count": sum(missing.values()),
            "extra_row_kinds": len(extra),
            "extra_row_count": sum(extra.values()),
            "missing_row_samples": list(missing)[:10],
            "extra_row_samples": list(extra)[:10],
        }
    left = {semantic_row_key(row) for row in (expected or [])}
    right = {semantic_row_key(row) for row in (actual or [])}
    missing = sorted(left - right)
    extra = sorted(right - left)
    return {
        "matched_rows": len(left & right),
        "missing_row_kinds": len(missing),
        "missing_row_count": len(missing),
        "extra_row_kinds": len(extra),
        "extra_row_count": len(extra),
        "missing_row_samples": missing[:10],
        "extra_row_samples": extra[:10],
    }


def graph_check(report: dict) -> dict:
    """Read graph stability fields from current and archived report formats."""
    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    before = (
        report.get("observed_graph_fingerprint_start")
        or report.get("graph_fingerprint_before")
        or run.get("graph_fingerprint_start")
    )
    after = (
        report.get("observed_graph_fingerprint_end")
        or report.get("graph_fingerprint_after")
        or run.get("graph_fingerprint_end")
    )
    if report.get("graph_drift") is True or report.get("graph_changed") is True:
        stable = False
    elif isinstance(report.get("graph_stable"), bool):
        stable = report["graph_stable"]
    elif before is not None and after is not None:
        stable = before == after
    elif report.get("graph_drift") is False:
        stable = True
    else:
        stable = None
    return {"before": before, "after": after, "stable": stable}


def output_path(directory: Path, name: str, reserved=("questions.json",)) -> Path:
    return safe_output_path(directory, name, reserved=reserved)


def ensure_distinct_output(destination: Path, inputs) -> None:
    destination = Path(destination).resolve()
    collisions = [str(Path(item).resolve()) for item in inputs if Path(item).resolve() == destination]
    if collisions:
        raise ValueError("输出路径会覆盖输入证据：" + ", ".join(collisions))


def write_json(path: Path, value) -> None:
    atomic_write_json(path, value)
