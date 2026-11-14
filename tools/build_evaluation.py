"""Rebuild the isolated reference set from its read-only Cypher queries.

The default output is an ignored candidate file. Replacing the committed
question set requires the explicit ``--in-place --force`` flags.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone

from evaluation import benchmark
from tools._audit_common import EVALUATION_DIR, output_path, write_json


QUESTIONS_PATH = benchmark.QUESTIONS_PATH
ROW_CAP = 5000
COMBINED_COLUMNS = {
    "H01": ("rest_service", "REST", "MQ"),
    "H02": ("rest_service", "同步", "异步"),
    "H07": ("rest_path", "REST路径", "MQ目标"),
    "H08": ("rest_service", "REST目标", "MQ目标"),
}
SERVICE_NAME = re.compile(r"\bts-[a-z0-9]+(?:-[a-z0-9]+)*\b", re.I)
TRAILING_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.I)


def _query_parameters(item: dict, queries: list[str]) -> dict:
    """Bind the service parameters used by the four reference compositions."""
    used = set()
    for query in queries:
        used.update(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", query))
    if not used:
        return {}
    names = list(dict.fromkeys(SERVICE_NAME.findall(item["question"])))
    parameters = {}
    if "source" in used:
        if not names:
            raise ValueError("参考查询需要 $source，但题目中没有服务名")
        parameters["source"] = names[0]
    if "target" in used:
        parameters["target"] = names[1] if item["id"] == "H07" and len(names) > 1 else None
    unknown = used - {"source", "target"}
    if unknown:
        raise ValueError("无法从题目推导查询参数：" + ", ".join(sorted(unknown)))
    return parameters


def complete_reference(query: str) -> tuple[str, bool]:
    """Replace illustrative row limits with a stable, completeness-probing cap."""
    query = query.strip()
    if query.endswith(";"):
        query = query[:-1].rstrip()
    match = TRAILING_LIMIT.search(query)
    if not match:
        raise ValueError("参考查询缺少末尾 LIMIT")
    if int(match.group(1)) == 1 and re.search(r"\bcount\s*\(", query, re.I):
        return query, True
    base = query[: match.start()].rstrip()
    returned = re.search(r"\bRETURN\b", query, re.I)
    projection = query[returned.end() : match.start()] if returned else ""
    aliases = re.findall(r"\bAS\s+([\w\u4e00-\u9fff]+)", projection, re.I)
    if not aliases:
        raise ValueError("参考查询需要显式列别名，才能稳定排序并检查完整性")
    if not re.search(r"\bORDER\s+BY\b", base, re.I):
        base += " ORDER BY " + ", ".join(aliases)
    return base + f" LIMIT {ROW_CAP + 1}", True


def schema_problems(query: str, info: dict, core) -> list[str]:
    """Check labels, relationship types and properties against the local snapshot."""
    problems = benchmark.read_only_problems(query, max_limit=ROW_CAP + 1)
    if problems:
        return problems
    without_literals = core.mask_cypher_quotes(query)
    allowed = set(info.get("labels", [])) | set(info.get("relationships", []))
    for token in core.LABEL.findall(without_literals):
        if token not in allowed:
            problems.append("Schema 中不存在标签或关系：" + token)
    properties = {
        value for values in info.get("properties", {}).values() for value in values
    }
    properties.update(
        value
        for relation in info.get("relationship_properties", [])
        for value in relation.get("properties", [])
    )
    for token in core.PROPERTY.findall(without_literals):
        prop = token.strip("`")
        if prop not in properties:
            problems.append("Schema 中不存在属性：" + prop)
    return problems


def _row_id(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def _run_combined(item: dict, queries: list[str], graph, params: dict):
    if len(queries) != 3:
        raise ValueError("组合参考题必须提供 REST、服务级依赖、闭合链路三条查询")
    rows = []
    fetched = []
    for query in queries:
        effective, _ = complete_reference(query)
        values = graph.run(effective, params)
        fetched.append(values)
        rows.append(values[:ROW_CAP])
    rest_field, rest_column, mq_column = COMBINED_COLUMNS[item["id"]]
    rest_values = {row.get(rest_field) for row in rows[0] if row.get(rest_field) is not None}
    mq_values = {
        row.get("mq_service")
        for group in rows[1:]
        for row in group
        if row.get("mq_service") is not None
    }
    ordered_rest = sorted(rest_values, key=str)
    ordered_mq = sorted(mq_values, key=str)
    pair_limit_hit = len(ordered_rest) * len(ordered_mq) > ROW_CAP
    combined = [
        {rest_column: rest_value, mq_column: mq_value}
        for rest_value in ordered_rest
        for mq_value in ordered_mq
    ][:ROW_CAP]
    evidence = {
        "rest_rows": len(fetched[0]),
        "service_dependency_rows": len(fetched[1]),
        "witnessed_chain_rows": len(fetched[2]),
        "mq_targets": ordered_mq,
    }
    complete = all(len(group) <= ROW_CAP for group in fetched) and not pair_limit_hit
    return combined, complete, evidence, len(fetched[0]) == 0 or not ordered_mq


def rebuild(items: list[dict], graph, graph_counts: dict, info: dict, core):
    """Execute every stored reference and return a rebuilt candidate plus errors."""
    fingerprint = benchmark.fingerprint_from_counts(graph_counts)
    versions = sorted(graph_counts)
    rebuilt = deepcopy(items)
    errors = []
    for item in rebuilt:
        item_id = item["id"]
        cypher = item["reference_cypher"]
        queries = cypher if isinstance(cypher, list) else [cypher]
        rows = []
        evidence = None
        complete = False
        error = None
        try:
            params = _query_parameters(item, queries)
            issues = [
                f"第 {index} 条：{problem}"
                for index, query in enumerate(queries, 1)
                for problem in schema_problems(query, info, core)
            ]
            if issues:
                raise ValueError("; ".join(issues))
            if isinstance(cypher, list):
                rows, complete, evidence, empty = _run_combined(item, queries, graph, params)
                if empty:
                    error = "组合参考查询缺少 REST 结果或有效 MQ 目标"
                    complete = False
            else:
                effective, _ = complete_reference(queries[0])
                fetched = graph.run(effective, params)
                rows = fetched[:ROW_CAP]
                complete = len(fetched) <= ROW_CAP
                if not complete:
                    error = f"参考结果超过 {ROW_CAP} 行，已截断"
        except Exception as exc:
            rows = []
            complete = False
            error = f"{type(exc).__name__}: {exc}"
        unique = len({_row_id(row) for row in rows}) == len(rows)
        if rows and not unique:
            error = error or "参考查询返回重复行"
        if not rows:
            complete = False
            error = error or "参考查询返回空结果"
        if not complete and not error:
            error = f"至少一条组合参考查询达到 {ROW_CAP + 1} 行，无法确认答案全集"
        item.update(
            reference_rows=rows,
            reference_nonempty=bool(rows),
            reference_error=error,
            reference_complete=complete,
            reference_result_count=len(rows),
            reference_unique=unique,
            reference_ordered=True,
            graph_versions=versions,
            graph_version_counts=graph_counts,
            graph_fingerprint=fingerprint,
            reference_evidence_counts=evidence,
        )
        if error:
            errors.append({"id": item_id, "error": error})
    return rebuilt, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="questions_candidate.json", help="候选 JSON 文件名")
    parser.add_argument("--in-place", action="store_true", help="明确写回 evaluation/questions.json")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有输出；配合 --in-place 才能替换题集")
    parser.add_argument("--check-only", action="store_true", help="校验当前题集，不连接 Neo4j")
    args = parser.parse_args(argv)
    if args.in_place and args.output != "questions_candidate.json":
        parser.error("--in-place 不能与自定义 --output 同时使用")
    if args.check_only:
        problems = benchmark.validate_questions(benchmark.load_questions())
        print(json.dumps({"valid": not problems, "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 1
    try:
        destination = (
            QUESTIONS_PATH
            if args.in_place
            else output_path(EVALUATION_DIR, args.output, reserved=("questions.json",))
        )
    except ValueError as error:
        parser.error(str(error))
    if destination.exists() and not args.force:
        parser.error(f"输出已存在：{destination.name}；请使用 --force")

    items = benchmark.load_questions()
    problems = benchmark.validate_questions(items)
    if problems:
        print(json.dumps({"valid": False, "problems": problems}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    from agent import core

    try:
        info = core.schema()
        cfg = core.configuration()
        graph = core.Graph(cfg)
    except Exception as error:
        print(f"无法初始化 Schema 或 Neo4j：{error}", file=sys.stderr)
        return 1
    started = datetime.now(timezone.utc).isoformat()
    before_counts = {}
    after_counts = {}
    try:
        before_rows = graph.run(
            "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
        )
        before_counts = {str(row["version"]): row["node_count"] for row in before_rows}
        rebuilt, errors = rebuild(items, graph, before_counts, info, core)
        after_rows = graph.run(
            "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
        )
        after_counts = {str(row["version"]): row["node_count"] for row in after_rows}
        stable = before_counts == after_counts
        if not stable:
            errors.append({"id": "*", "error": "运行期间图谱版本计数发生变化"})
        post_problems = benchmark.validate_questions(rebuilt)
        payload = {
            "built_at_utc": started,
            "source_dataset_sha256": benchmark.dataset_sha256(),
            "graph_fingerprint_start": benchmark.fingerprint_from_counts(before_counts),
            "graph_fingerprint_end": benchmark.fingerprint_from_counts(after_counts),
            "graph_version_counts_start": before_counts,
            "graph_version_counts_end": after_counts,
            "graph_stable": stable,
            "complete": not errors and not post_problems,
            "errors": errors,
            "validation_problems": post_problems,
            "items": rebuilt,
        }
        if args.in_place and (errors or post_problems or not stable):
            print("拒绝写回：候选题集不完整或图谱发生漂移。", file=sys.stderr)
            return 1
        if args.in_place and not args.force:
            print("写回题集必须同时指定 --in-place --force。", file=sys.stderr)
            return 2
        write_json(destination, rebuilt if args.in_place else payload)
        print(
            json.dumps(
                {
                    "output": str(destination),
                    "questions": len(rebuilt),
                    "graph_stable": stable,
                    "complete": payload["complete"],
                    "failed_ids": [item["id"] for item in errors],
                    "validation_problem_count": len(post_problems),
                },
                ensure_ascii=False,
            )
        )
        return 0 if payload["complete"] else 1
    finally:
        graph.close()


if __name__ == "__main__":
    raise SystemExit(main())
