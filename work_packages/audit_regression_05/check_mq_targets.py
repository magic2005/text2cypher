"""Compare service-level MQ edges with routing-key-verified delivery chains."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from evaluation import benchmark
from work_packages.audit_regression_05._audit_common import EVALUATION_DIR, output_path, read_json, resolve_input, write_json


SERVICE_NAME = re.compile(r"\bts-[a-z0-9]+(?:-[a-z0-9]+)*\b", re.I)
ROW_CAP = 5000


def _question_sources(questions: list[dict]):
    sources: dict[str, list[str]] = {}
    for item in questions:
        text = item.get("question", "")
        if item.get("level") not in {"困难", "复杂"}:
            continue
        if "MQ" not in text and "异步" not in text and "消息" not in text:
            continue
        for source in dict.fromkeys(SERVICE_NAME.findall(text)):
            sources.setdefault(source, []).append(item["id"])
    return {name: sorted(ids) for name, ids in sorted(sources.items())}


def _bounded(rows):
    return {"rows": rows[:ROW_CAP], "fetched": len(rows), "truncated": len(rows) > ROW_CAP}


def audit(graph, questions: list[dict], before: str, before_counts: dict) -> dict:
    source_questions = _question_sources(questions)
    names = list(source_questions)
    if not names:
        raise ValueError("困难题中没有可用于 MQ 检查的微服务名称")
    params = {"names": names}
    dependency_query = (
        "MATCH (s:微服务)-[:消息依赖]->(t:微服务) "
        "WHERE s.服务名称 IN $names "
        "RETURN DISTINCT s.服务名称 AS source,t.服务名称 AS target "
        "ORDER BY source,target LIMIT 5001"
    )
    chain_query = (
        "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) "
        "MATCH (p)-[:归属于]->(:类)-[:归属于]->(s:微服务) "
        "MATCH (c)-[:归属于]->(:类)-[:归属于]->(t:微服务) "
        "WHERE s.服务名称 IN $names AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
        "RETURN DISTINCT s.服务名称 AS source,t.服务名称 AS target,p.全限定名 AS publisher, "
        "e.交换机名称 AS exchange,q.队列名称 AS queue,q.所属服务名 AS queue_service, "
        "c.全限定名 AS consumer,pub.路由键 AS routing_key "
        "ORDER BY source,target,queue,publisher,consumer LIMIT 5001"
    )
    mismatch_query = (
        "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) "
        "MATCH (p)-[:归属于]->(:类)-[:归属于]->(s:微服务) "
        "OPTIONAL MATCH (q)-[:消费自]->(c:方法) "
        "OPTIONAL MATCH (c)-[:归属于]->(:类)-[:归属于]->(t:微服务) "
        "WHERE s.服务名称 IN $names AND "
        "(pub.路由键 IS NULL OR route.路由键 IS NULL OR pub.路由键<>route.路由键) "
        "RETURN DISTINCT s.服务名称 AS source,p.全限定名 AS publisher,e.交换机名称 AS exchange, "
        "q.队列名称 AS queue,q.所属服务名 AS queue_service,c.全限定名 AS consumer, "
        "t.服务名称 AS consumer_service,pub.路由键 AS publish_key,route.路由键 AS binding_key "
        "ORDER BY source,queue,publisher LIMIT 5001"
    )
    unconsumed_query = (
        "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) "
        "MATCH (p)-[:归属于]->(:类)-[:归属于]->(s:微服务) "
        "WHERE s.服务名称 IN $names AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
        "OPTIONAL MATCH (q)-[:消费自]->(c:方法) "
        "WITH s,p,e,route,q,pub,c WHERE c IS NULL "
        "RETURN DISTINCT s.服务名称 AS source,p.全限定名 AS publisher,e.交换机名称 AS exchange, "
        "q.队列名称 AS queue,q.所属服务名 AS queue_service,pub.路由键 AS routing_key "
        "ORDER BY source,queue,publisher LIMIT 5001"
    )
    dependency = _bounded(graph.run(dependency_query, params))
    chains = _bounded(graph.run(chain_query, params))
    mismatches = _bounded(graph.run(mismatch_query, params))
    unconsumed = _bounded(graph.run(unconsumed_query, params))
    dependency_pairs = {
        (row.get("source"), row.get("target")) for row in dependency["rows"]
    }
    witnessed_pairs = {
        (row.get("source"), row.get("target")) for row in chains["rows"]
    }
    after_rows = graph.run(
        "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
    )
    after_counts = {str(row["version"]): row["node_count"] for row in after_rows}
    after = benchmark.fingerprint_from_counts(after_counts)
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "question_scope": "题目提及的困难级 MQ/异步/消息问题中的服务名；未推断其调用方或目标身份",
        "sources": names,
        "source_questions": source_questions,
        "graph_fingerprint_start": before,
        "graph_fingerprint_end": after,
        "graph_version_counts_start": before_counts,
        "graph_version_counts_end": after_counts,
        "graph_stable": before == after,
        "service_dependencies": dependency["rows"],
        "witnessed_full_chains": chains["rows"],
        "route_key_mismatches_or_missing_keys": mismatches["rows"],
        "matched_routes_without_consumers": unconsumed["rows"],
        "dependency_pairs_without_witnessed_chain": [
            {"source": source, "target": target}
            for source, target in sorted(dependency_pairs - witnessed_pairs)
        ],
        "witnessed_chain_pairs_without_service_dependency": [
            {"source": source, "target": target}
            for source, target in sorted(witnessed_pairs - dependency_pairs)
        ],
        "truncated_sections": [
            name
            for name, value in (
                ("service_dependencies", dependency),
                ("witnessed_full_chains", chains),
                ("route_key_mismatches_or_missing_keys", mismatches),
                ("matched_routes_without_consumers", unconsumed),
            )
            if value["truncated"]
        ],
        "query_row_counts": {
            "service_dependencies": dependency["fetched"],
            "witnessed_full_chains": chains["fetched"],
            "route_key_mismatches_or_missing_keys": mismatches["fetched"],
            "matched_routes_without_consumers": unconsumed["fetched"],
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", default="questions.json", help="评测题集路径")
    parser.add_argument("--output", default="mq_targets_audit.json", help="审计 JSON 文件名")
    args = parser.parse_args(argv)
    try:
        question_path = resolve_input(args.questions)
        questions = read_json(question_path)
        problems = benchmark.validate_questions(questions)
        if problems:
            raise ValueError("题集校验失败：" + "; ".join(problems[:8]))
        destination = output_path(EVALUATION_DIR, args.output)
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    from agent import core

    try:
        cfg = core.configuration()
        graph = core.Graph(cfg)
    except Exception as error:
        print(f"无法连接 Neo4j：{error}", file=sys.stderr)
        return 1
    try:
        rows = graph.run(
            "MATCH (n) RETURN n.图谱版本 AS version, count(*) AS node_count LIMIT 100"
        )
        before_counts = {str(row["version"]): row["node_count"] for row in rows}
        before = benchmark.fingerprint_from_counts(before_counts)
        result = audit(graph, questions, before, before_counts)
    except Exception as error:
        print(f"MQ 目标检查失败：{error}", file=sys.stderr)
        return 1
    finally:
        graph.close()
    result["question_file"] = str(question_path)
    write_json(destination, result)
    print(
        json.dumps(
            {
                "output": str(destination),
                "sources": result["sources"],
                "service_dependency_rows": len(result["service_dependencies"]),
                "witnessed_chain_rows": len(result["witnessed_full_chains"]),
                "route_mismatch_rows": len(result["route_key_mismatches_or_missing_keys"]),
                "unconsumed_rows": len(result["matched_routes_without_consumers"]),
                "graph_stable": result["graph_stable"],
                "truncated_sections": result["truncated_sections"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["graph_stable"] and not result["truncated_sections"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
