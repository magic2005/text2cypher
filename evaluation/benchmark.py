"""Pure helpers for the isolated 30-question benchmark.

This module deliberately uses only the standard library.  Neo4j, Streamlit
and the model SDKs are imported by the entry points in ``tools/`` after
argument parsing, so the question set can be checked and unit-tested on an
offline machine.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "evaluation" / "questions.json"
MOCK_TEACHER_DIR = ROOT / "evaluation" / "mock_teacher_2026-10-14"
MOCK_QUESTIONS_PATH = MOCK_TEACHER_DIR / "questions.json"

EXPECTED_IDS = tuple(
    [f"S{index:02d}" for index in range(1, 11)]
    + [f"M{index:02d}" for index in range(1, 13)]
    + [f"H{index:02d}" for index in range(1, 9)]
)
LEVEL_CATEGORY = {"简单": "简单", "中等": "中等", "困难": "复杂", "复杂": "复杂"}
LEVEL_COUNTS = {"简单": 10, "中等": 12, "复杂": 8}
PREFIX_CATEGORY = {"S": "简单", "M": "中等", "H": "复杂"}
COMBINED_IDS = frozenset({"H01", "H02", "H07", "H08"})
COMBINED_EVIDENCE_KEYS = frozenset(
    {"rest_rows", "service_dependency_rows", "witnessed_chain_rows", "mq_targets"}
)
REQUIRED_ITEM_KEYS = frozenset(
    {
        "id",
        "level",
        "question",
        "reference_cypher",
        "reference_rows",
        "reference_nonempty",
        "reference_error",
        "reference_complete",
        "reference_result_count",
        "reference_unique",
        "reference_ordered",
        "graph_versions",
        "graph_version_counts",
        "graph_fingerprint",
        "reference_scope",
        "reference_evidence_counts",
    }
)

FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL|"
    r"SHOW|USE|GRANT|DENY|REVOKE|ALTER|START|STOP|APOC|DBMS)\b",
    re.I,
)

SCORE_RULE = (
    "仅当 30 题全部尝试、每题均有自动或人工结论、且运行前后图谱指纹未漂移时计分；"
    "否则 score 为 null。"
)


# ---------------------------------------------------------------------------
# file and JSON helpers
# ---------------------------------------------------------------------------


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def dataset_sha256(path: Path | None = None) -> str:
    """Checksum of the exact question file used by a run."""
    return file_sha256(path or QUESTIONS_PATH)


def load_questions(path: Path | None = None) -> list:
    value = json.loads((path or QUESTIONS_PATH).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("questions.json 顶层必须是数组")
    return value


def atomic_write_json(path: Path, value) -> None:
    """Write JSON through a sibling temporary file, then replace the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)


def safe_output_path(directory: Path, name: str, reserved=("questions.json",)) -> Path:
    """Resolve a user supplied result filename without allowing path escapes.

    Result files must stay next to the question set; otherwise a relative
    ``--output`` could overwrite repository files such as ``agent/core.py``.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("输出文件名不能为空")
    if "/" in name or "\\" in name:
        raise ValueError("--output 必须是当前目录内的纯文件名，不能包含路径")
    candidate = Path(name)
    if candidate.name != name or candidate.suffix.lower() != ".json":
        raise ValueError("--output 必须是以 .json 结尾的纯文件名")
    blocked = {item.lower() for item in reserved}
    if name.lower() in blocked:
        raise ValueError(f"不能覆盖保留文件：{name}")
    return Path(directory) / name


# ---------------------------------------------------------------------------
# result comparison and judging
# ---------------------------------------------------------------------------


def canonical(rows) -> set:
    """Compare scalar result values while ignoring aliases and column order.

    This set semantics is intentional and matches the published scoring rule:
    target values are deduplicated and equivalent column names are accepted.
    Duplicate rows are reported separately as a diagnostic instead of being
    silently treated as a different answer.
    """
    return {
        json.dumps(sorted(str(value) for value in row.values()), ensure_ascii=False)
        for row in rows
    }


def canonical_multiset(rows) -> Counter:
    """Full row multiset, used by the local-oracle comparison tool."""
    return Counter(row_key(row) for row in rows)


def row_key(row) -> str:
    return json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)


def rows_equal(left, right) -> bool:
    return canonical_multiset(left) == canonical_multiset(right)


def duplicate_diagnostics(rows) -> dict:
    keys = [row_key(row) for row in rows]
    unique = len(set(keys))
    return {
        "actual_row_count": len(keys),
        "actual_unique_row_count": unique,
        "duplicate_rows_collapsed": len(keys) - unique,
    }


def judge(reference_rows, actual) -> dict:
    """Apply the published three-state judgement to one model answer."""
    error = actual.get("error")
    complete = actual.get("answer_complete")
    results = actual.get("results") or []
    automatic_match = not error and canonical(reference_rows) == canonical(results)
    if error:
        correct = False
        reason = str(error)
    elif not complete:
        correct = False
        reason = "结果已截断或完整链路无法验证"
    elif automatic_match:
        correct = True
        reason = None
    else:
        correct = None
        reason = "自动比对不一致；需人工复核语义、结果范围与返回列"
    return {
        "correct": correct,
        "automatic_match": bool(automatic_match),
        "failure_reason": reason,
        "answer_complete": bool(complete),
    }


def pending_record(item, reason="尚未运行") -> dict:
    return {
        "id": item["id"],
        "level": item["level"],
        "question": item["question"],
        "attempted": False,
        "actual_cypher": None,
        "execution_result": None,
        "correct": None,
        "automatic_match": None,
        "failure_reason": reason,
        "actual_row_count": None,
        "actual_unique_row_count": None,
        "duplicate_rows_collapsed": None,
    }


def build_record(item, actual) -> dict:
    """Build the stored evidence for one run without copying reference rows."""
    if not isinstance(actual, dict):
        raise TypeError("ask() 必须返回字典")
    verdict = judge(item["reference_rows"], actual)
    record = {
        "id": item["id"],
        "level": item["level"],
        "question": item["question"],
        "attempted": True,
        "actual_cypher": actual.get("queries"),
        "execution_result": actual.get("results"),
        "correct": verdict["correct"],
        "automatic_match": verdict["automatic_match"],
        "failure_reason": verdict["failure_reason"],
        "summary": actual.get("summary"),
        "stages": actual.get("stages"),
        "answer_complete": actual.get("answer_complete"),
        "truncated_subqueries": actual.get("truncated_subqueries", 0),
        "discarded_mismatched_route_rows": actual.get(
            "discarded_mismatched_route_rows", 0
        ),
        "discarded_partial_chain_rows": actual.get(
            "discarded_partial_chain_rows", 0
        ),
        **duplicate_diagnostics(actual.get("results") or []),
    }
    return record


def with_pending(questions, records, reason="尚未运行") -> dict:
    merged = dict(records)
    for item in questions:
        merged.setdefault(item["id"], pending_record(item, reason))
    return merged


def summarize(records, questions) -> dict:
    merged = with_pending(questions, records)
    items = [merged[item["id"]] for item in questions]
    attempted = sum(1 for item in items if item.get("attempted"))
    correct = sum(1 for item in items if item.get("correct") is True)
    needs_review = sum(
        1 for item in items if item.get("attempted") and item.get("correct") is None
    )
    return {
        "items": items,
        "total": len(items),
        "evaluated": attempted,
        "verified_correct": correct,
        "needs_review": needs_review,
        "pending": len(items) - attempted,
    }


def score_value(items, graph_drift: bool):
    if graph_drift or not items:
        return None
    if any(not item.get("attempted") or item.get("correct") is None for item in items):
        return None
    return sum(1 for item in items if item.get("correct") is True)


# ---------------------------------------------------------------------------
# static checks for the reference queries
# ---------------------------------------------------------------------------


def _mask_quotes(query: str, identifiers: bool = False) -> str:
    """Hide string literals (and optionally quoted identifiers) in place."""
    hidden = list(query)
    index = 0
    while index < len(query):
        quote = query[index]
        if quote not in ("'", '"') and not (identifiers and quote == "`"):
            index += 1
            continue
        start = index
        index += 1
        while index < len(query):
            if query[index] == "\\" and quote != "`":
                index += 2
                continue
            if query[index] == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    index += 2
                    continue
                index += 1
                break
            index += 1
        else:
            raise ValueError("Cypher 字符串或标识符未闭合")
        hidden[start:index] = " " * (index - start)
    return "".join(hidden)


def read_only_problems(
    query: str, *, require_limit: bool = True, max_limit: int | None = 5001
) -> list[str]:
    """Static, schema-free checks for reference and gold queries.

    Interactive answers are additionally validated against ``data/schema.json``
    by ``agent.core.validate``.  Reference queries are executed directly and
    may legitimately use ``LIMIT 5001`` to collect a whole answer, so the
    bound here is the benchmark cap rather than the interactive cap of 100.
    """
    if not isinstance(query, str) or not query.strip():
        return ["Cypher 为空"]
    problems: list[str] = []
    q = query.strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    try:
        outside = _mask_quotes(q)
    except ValueError as error:
        return [str(error)]
    if ";" in outside:
        problems.append("只能包含一条语句")
    if "//" in outside or "/*" in outside or "*/" in outside:
        problems.append("不能包含注释")
    try:
        keywords = _mask_quotes(q, identifiers=True)
    except ValueError as error:
        return [str(error)]
    match = FORBIDDEN.search(keywords)
    if match:
        problems.append(f"包含写操作或管理关键字：{match.group(0)}")
    if not re.match(r"^(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND)\b", keywords, re.I):
        problems.append("必须以只读的 MATCH/WITH/UNWIND 开头")
    if not re.search(r"\bRETURN\b", keywords, re.I):
        problems.append("必须返回结果")
    if require_limit:
        trailing = re.search(r"\bLIMIT\s+(\d+)\s*$", outside, re.I)
        if not trailing:
            problems.append("参考查询必须以 LIMIT 结束，避免收集不完整的答案")
        else:
            bound = int(trailing.group(1))
            if bound < 1 or (max_limit is not None and bound > max_limit):
                problems.append(f"LIMIT 必须在 1..{max_limit} 之间")
    for relation in re.findall(r"\[[^\]]*\]", outside):
        star = re.search(r"\*(\d*)(?:\.\.(\d*))?", relation)
        if not star:
            continue
        low, high = star.groups()
        if not low or (high is not None and not high):
            problems.append("变长路径必须写成有界跳数，例如 *1..3")
            continue
        upper = int(high if high is not None else low)
        if upper > 5:
            problems.append("变长路径不能超过 5 跳")
    return problems


# ---------------------------------------------------------------------------
# dataset validation
# ---------------------------------------------------------------------------


def fingerprint_from_counts(counts: dict) -> str:
    return sha256_bytes(
        json.dumps(counts, sort_keys=True).encode("utf-8")
    )


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_questions(items, expected_ids=EXPECTED_IDS) -> list[str]:
    """Return every integrity problem found in the independent question set."""
    problems: list[str] = []
    if not isinstance(items, list):
        return ["题集必须是 JSON 数组"]
    ids = [item.get("id") if isinstance(item, dict) else None for item in items]
    if ids != list(expected_ids):
        problems.append(
            "题号必须是 " + ", ".join(expected_ids) + "，实际为 "
            + ", ".join(str(value) for value in ids)
        )
    category_counts: Counter = Counter()
    fingerprints = set()
    questions = set()
    for item in items:
        if not isinstance(item, dict):
            problems.append(f"题目项必须是对象：{item!r}")
            continue
        item_id = str(item.get("id"))
        label = f"题 {item_id}"
        missing = REQUIRED_ITEM_KEYS - set(item)
        if missing:
            problems.append(label + " 缺少字段：" + ", ".join(sorted(missing)))
        level = item.get("level")
        category = LEVEL_CATEGORY.get(level)
        if category is None:
            problems.append(label + f" 的难度未知：{level!r}")
        else:
            category_counts[category] += 1
            expected_category = PREFIX_CATEGORY.get(item_id[:1])
            if expected_category and category != expected_category:
                problems.append(label + f" 的难度应为 {expected_category}，实际为 {level}")

        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            problems.append(label + " 的问题为空")
        elif question in questions:
            problems.append(label + " 的问题与前面的题目重复")
        else:
            questions.add(question)

        counts = item.get("graph_version_counts")
        fingerprint = item.get("graph_fingerprint")
        if not isinstance(counts, dict) or not counts:
            problems.append(label + " 的 graph_version_counts 必须是非空对象")
        elif not all(isinstance(key, str) for key in counts):
            problems.append(label + " 的图谱版本键必须是字符串")
        elif not all(_is_int(value) and value > 0 for value in counts.values()):
            problems.append(label + " 的图谱版本计数必须是正整数")
        else:
            expected_fingerprint = fingerprint_from_counts(counts)
            if fingerprint != expected_fingerprint:
                problems.append(label + " 的 graph_fingerprint 与版本计数不一致")
            versions = item.get("graph_versions")
            if versions != sorted(counts):
                problems.append(label + " 的 graph_versions 与版本计数不一致")
        if isinstance(fingerprint, str):
            fingerprints.add(fingerprint)
        else:
            problems.append(label + " 的 graph_fingerprint 缺失")

        rows = item.get("reference_rows")
        if not isinstance(rows, list) or not rows:
            problems.append(label + " 的 reference_rows 必须是非空数组")
            rows = []
        else:
            first_columns = None
            row_keys = []
            for row in rows:
                if not isinstance(row, dict) or not row:
                    problems.append(label + " 的参考行必须是非空对象")
                    continue
                if not all(isinstance(key, str) and key for key in row):
                    problems.append(label + " 的参考行列名必须是非空字符串")
                columns = tuple(sorted(row))
                if first_columns is None:
                    first_columns = columns
                elif columns != first_columns:
                    problems.append(label + " 的参考行列结构不一致")
                row_keys.append(row_key(row))
            if len(set(row_keys)) != len(row_keys):
                problems.append(label + " 的参考行包含重复行")
        if item.get("reference_result_count") != len(rows):
            problems.append(label + " 的 reference_result_count 与参考行数不一致")
        if not _is_int(item.get("reference_result_count")) or item.get(
            "reference_result_count", 0
        ) < 1:
            problems.append(label + " 的 reference_result_count 必须是正整数")
        if item.get("reference_nonempty") is not True:
            problems.append(label + " 的 reference_nonempty 必须为 true")
        if item.get("reference_complete") is not True:
            problems.append(label + " 的 reference_complete 必须为 true")
        if item.get("reference_unique") is not True:
            problems.append(label + " 的 reference_unique 必须为 true")
        if item.get("reference_error") is not None:
            problems.append(label + " 的 reference_error 必须为 null")
        if not isinstance(item.get("reference_ordered"), bool):
            problems.append(label + " 的 reference_ordered 必须是布尔值")
        scope = item.get("reference_scope")
        if not isinstance(scope, str) or not scope.strip():
            problems.append(label + " 的 reference_scope 不能为空")

        cypher = item.get("reference_cypher")
        queries = cypher if isinstance(cypher, list) else [cypher]
        if not isinstance(cypher, (str, list)) or not all(
            isinstance(query, str) for query in queries
        ):
            problems.append(label + " 的 reference_cypher 必须是字符串或字符串数组")
            queries = []
        if isinstance(cypher, list):
            if not 2 <= len(cypher) <= 4:
                problems.append(label + " 的组合查询应包含 2–4 条 Cypher")
            if item_id not in COMBINED_IDS:
                problems.append(label + " 不是组合题，却给出了多条 Cypher")
        elif item_id in COMBINED_IDS:
            problems.append(label + " 是组合题，reference_cypher 必须是数组")
        for index, query in enumerate(queries, start=1):
            for issue in read_only_problems(query):
                problems.append(f"{label} 第 {index} 条 Cypher：{issue}")

        evidence = item.get("reference_evidence_counts")
        if isinstance(cypher, list) and len(cypher) > 1:
            if not isinstance(evidence, dict):
                problems.append(label + " 的组合题缺少 reference_evidence_counts")
            else:
                if set(evidence) != COMBINED_EVIDENCE_KEYS:
                    problems.append(
                        label + " 的 reference_evidence_counts 字段不完整"
                    )
                for key in COMBINED_EVIDENCE_KEYS - {"mq_targets"}:
                    if not _is_int(evidence.get(key)) or evidence.get(key, -1) < 0:
                        problems.append(
                            label + f" 的 reference_evidence_counts.{key} 必须是非负整数"
                        )
                targets = evidence.get("mq_targets")
                if not isinstance(targets, list) or not targets or not all(
                    isinstance(target, str) and target for target in targets
                ):
                    problems.append(
                        label + " 的 reference_evidence_counts.mq_targets 必须是非空字符串数组"
                    )
                if rows and any(len(row) != 2 for row in rows):
                    problems.append(label + " 的组合题参考行应恰好有两列")
        elif evidence is not None:
            problems.append(label + " 不是组合题，reference_evidence_counts 应为 null")

    if len(fingerprints) > 1:
        problems.append("全部题目必须来自同一次图谱快照（graph_fingerprint 不一致）")
    if category_counts and dict(category_counts) != LEVEL_COUNTS:
        problems.append(
            "难度分布必须是 简单10 / 中等12 / 复杂8，实际为 "
            + json.dumps(dict(category_counts), ensure_ascii=False, sort_keys=True)
        )
    return problems


def dataset_info(items, path: Path | None = None) -> dict:
    return {
        "dataset": str((path or QUESTIONS_PATH).relative_to(ROOT)),
        "sha256": dataset_sha256(path),
        "total": len(items),
        "levels": dict(
            Counter(LEVEL_CATEGORY.get(item.get("level"), "未知") for item in items)
        ),
        "reference_rows": sum(
            len(item.get("reference_rows") or []) for item in items
        ),
        "graph_fingerprint": next(
            (item.get("graph_fingerprint") for item in items if item.get("graph_fingerprint")),
            None,
        ),
    }


# ---------------------------------------------------------------------------
# mock teacher questions
# ---------------------------------------------------------------------------


def mock_questions_payload(cases) -> list[dict]:
    return [
        {"id": case[0], "level": case[1], "question": case[2]}
        for case in cases
    ]


def validate_mock_cases(cases, committed=None) -> list[str]:
    """Check the inline mock-teacher questions and the committed JSON copy."""
    problems: list[str] = []
    if not isinstance(cases, (list, tuple)):
        return ["CASES 必须是列表"]
    ids = [case[0] if isinstance(case, (list, tuple)) and len(case) >= 4 else None for case in cases]
    if ids != list(EXPECTED_IDS):
        problems.append(
            "模拟教师题号必须是 " + ", ".join(EXPECTED_IDS) + "，实际为 "
            + ", ".join(str(value) for value in ids)
        )
    category_counts: Counter = Counter()
    questions = set()
    for case in cases:
        if not isinstance(case, (list, tuple)) or len(case) != 4:
            problems.append(f"CASES 项必须是四元组：{case!r}")
            continue
        item_id, level, question, gold = case
        label = f"模拟题 {item_id}"
        category = LEVEL_CATEGORY.get(level)
        if category is None:
            problems.append(label + f" 的难度未知：{level!r}")
        else:
            category_counts[category] += 1
            expected_category = PREFIX_CATEGORY.get(str(item_id)[:1])
            if expected_category and category != expected_category:
                problems.append(label + f" 的难度应为 {expected_category}，实际为 {level}")
        if not isinstance(question, str) or not question.strip():
            problems.append(label + " 的问题为空")
        elif question in questions:
            problems.append(label + " 的问题与前面的题目重复")
        else:
            questions.add(question)
        if not isinstance(gold, str) or not gold.strip():
            problems.append(label + " 的 gold_query 为空")
        else:
            for issue in read_only_problems(gold, require_limit=False):
                problems.append(label + " 的 gold_query：" + issue)
    if category_counts and dict(category_counts) != LEVEL_COUNTS:
        problems.append(
            "模拟题难度分布必须是 简单10 / 中等12 / 复杂8，实际为 "
            + json.dumps(dict(category_counts), ensure_ascii=False, sort_keys=True)
        )

    if committed is None:
        if MOCK_QUESTIONS_PATH.exists():
            committed = json.loads(MOCK_QUESTIONS_PATH.read_text(encoding="utf-8"))
        else:
            problems.append("缺少已提交的模拟题 questions.json")
    if committed is not None:
        expected_payload = mock_questions_payload(cases)
        if isinstance(committed, list) and len(committed) == len(expected_payload):
            for index, (stored, expected) in enumerate(
                zip(committed, expected_payload), start=1
            ):
                if stored != expected:
                    problems.append(
                        f"模拟题 questions.json 第 {index} 项与工具内 CASES 不一致"
                    )
                    break
        else:
            problems.append("模拟题 questions.json 必须与 CASES 数量一致")
    return problems
