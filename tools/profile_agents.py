"""Profile direct generation, DeepSeek Harness and the Text2Cypher agent."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from agent import opencode_go
from evaluation.benchmark import LEVEL_CATEGORY, atomic_write_json
from tools.harness_eval import harness_version, resolve_harness
from tools.profile_common import direct, entity_key, live_schema, summary_for


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "evaluation" / "profile_workload.json"
MODES = ("direct", "harness", "text2cypher")


def connection_config(uri):
    address = urlsplit(uri)
    if (address.scheme != "bolt" or address.hostname not in ("127.0.0.1", "::1", "localhost")
            or address.username is not None):
        raise ValueError("The synthetic fixture requires a loopback-only Bolt server")
    if not os.getenv("T2C_PROFILE_NEO4J_PASSWORD"):
        raise ValueError("Set T2C_PROFILE_NEO4J_PASSWORD for the isolated fixture database")
    return {
        "uri": uri,
        "user": os.getenv("T2C_PROFILE_NEO4J_USER", "neo4j"),
        "password": os.getenv("T2C_PROFILE_NEO4J_PASSWORD"),
        "timeout": 10,
    }


def graph_snapshot(graph):
    nodes = graph.run("MATCH (n) RETURN n.nodeId AS id, labels(n) AS labels, properties(n) AS properties")
    edges = graph.run(
        "MATCH (a)-[r]->(b) RETURN a.nodeId AS source, b.nodeId AS target, "
        "type(r) AS type, properties(r) AS properties"
    )
    return (
        sorted(entity_key(row) for row in nodes),
        sorted(entity_key(row) for row in edges),
    )


def expected_snapshot(fixture):
    return (
        sorted(entity_key({
            "id": item["id"], "labels": [item["label"]],
            "properties": {**item["properties"], "nodeId": item["id"]},
        }) for item in fixture["nodes"]),
        sorted(entity_key(row) for row in fixture["edges"]),
    )


def seed_graph(cfg, fixture):
    from neo4j import GraphDatabase

    with GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"])) as driver:
        with driver.session() as session:
            with session.begin_transaction() as tx:
                if tx.run("MATCH (n) RETURN count(n) AS count").single()["count"]:
                    raise ValueError("Refusing to seed a nonempty Neo4j database")
                for item in fixture["nodes"]:
                    label = identifier(item["label"])
                    tx.run(
                        f"CREATE (n:{label}) SET n = $props",
                        props={**item["properties"], "nodeId": item["id"]},
                    ).consume()
                for item in fixture["edges"]:
                    relation = identifier(item["type"])
                    tx.run(
                        "MATCH (a {nodeId: $source}), (b {nodeId: $target}) "
                        f"CREATE (a)-[r:{relation}]->(b) SET r = $props",
                        source=item["source"], target=item["target"], props=item["properties"],
                    ).consume()
                tx.commit()


def identifier(value):
    if not value.isidentifier():
        raise ValueError(f"Invalid fixture identifier: {value!r}")
    return f"`{value}`"


def alias_key(name):
    return "".join(char for char in name.casefold() if char.isalnum())


def projection(rows, columns):
    projected = []
    for row in rows:
        values = []
        used = set()
        for column in columns:
            accepted = {alias_key(label) for label in [column["name"], *column.get("aliases", ())]}
            exact = [name for name in row if alias_key(name) == alias_key(column["name"])]
            matches = exact or [name for name in row if alias_key(name) in accepted]
            if not matches and len(row) == len(columns) == 1:
                matches = list(row)
            if len(matches) != 1 or matches[0] in used:
                return None
            used.add(matches[0])
            values.append(row[matches[0]])
        projected.append(entity_key(values))
    return projected


def judge(item, actual):
    if actual.get("error"):
        return False, "execution_error"
    if not actual.get("answer_complete"):
        return False, "incomplete_answer"
    scored = projection(actual.get("results") or [], item["columns"])
    if scored is None:
        return None, "ambiguous_columns"
    expected = item["reference_projection"]
    if item.get("ordered"):
        correct = scored == expected
    elif item.get("retain_duplicates"):
        correct = Counter(scored) == Counter(expected)
    else:
        correct = set(scored) == set(expected)
    return correct, None if correct else "result_mismatch"


def validated_items(fixture, graph, info):
    from agent import core

    items = fixture["items"]
    if len({item["id"] for item in items}) != len(items):
        raise ValueError("The workload contains duplicate question IDs")
    result = []
    for item in items:
        query = core.validate(item["reference_cypher"], info)
        names = [column["name"] for column in item["columns"]]
        rows = graph.run(query)
        if any(set(row) != set(names) for row in rows):
            raise ValueError(f"{item['id']}: reference projection differs from the column contract")
        values = projection(rows, item["columns"])
        if values is None or bool(values) == bool(item.get("expect_empty", False)):
            raise ValueError(f"{item['id']}: reference rows violate the fixture contract")
        declared = projection(item["expected_rows"], item["columns"])
        matches_declared = values == declared if item.get("ordered") else (
            declared is not None and Counter(values) == Counter(declared)
        )
        if not matches_declared:
            raise ValueError(f"{item['id']}: reference differs from the independently authored answer")
        if len(rows) >= 100:
            raise ValueError(f"{item['id']}: reference reached the validation row cap")
        result.append({**item, "reference_projection": values})
    return result


def run_case(item, mode, cfg):
    from agent import core

    profile = []
    case = {
        "id": item["id"],
        "level": LEVEL_CATEGORY[item["level"]],
        "question": item["question"] + " 请使用以下结果列别名：" +
        "、".join(column["name"] for column in item["columns"]) + "。",
    }
    case_cfg = {
        **cfg,
        "session_id": opencode_go.session_id(),
        "record_history": False,
        "profile": profile,
    }
    graph = core.Graph(case_cfg)
    start = time.perf_counter()
    try:
        if mode == "direct":
            actual = direct(case, graph, case_cfg)
        elif mode == "harness":
            from tools.harness_eval import run_harness

            actual = run_harness(case, graph, case_cfg)
        else:
            actual = core.ask(case["question"], graph, case_cfg, level=case["level"])
        wall = time.perf_counter() - start
    finally:
        graph.close()
    correct, reason = judge(item, actual)
    measured_seconds = sum(
        entry["elapsed_seconds"] for entry in profile
        if entry["kind"] == "model"
        or (entry["kind"] == "graph" and entry.get("stage") != "harness_probe")
    )
    return {
        "correct": correct, "reason": reason, "runtime_error": actual.get("error"),
        "failure_kind": failure_kind(actual, profile),
        "wall_seconds": round(wall, 6), "profile": profile,
        "other_seconds": round(max(0, wall - measured_seconds), 6),
        "actual": actual,
    }


def run_question(item, modes, cfg):
    return {mode: run_case(item, mode, cfg) for mode in modes}


def write_report(path, meta, items, records, graph_verified):
    summaries = {}
    for mode in MODES:
        measured = [cases[mode] for cases in records.values() if mode in cases]
        summary = summary_for(measured)
        events = [event for case in measured for event in case["profile"]]
        summary["model_with_usage"] = sum(
            bool(event.get("usage")) for event in events if event["kind"] == "model"
        )
        summary["tool_calls"] = sum(event["kind"] == "tool" for event in events)
        if any(event["kind"] == "model" and "attempts" not in event for event in events):
            summary["http_attempts"] = None
        summary["failure_reasons"] = dict(Counter(
            case["failure_kind"] or case["reason"] for case in measured if case["reason"]
        ))
        summary["other_seconds"] = round(sum(case["other_seconds"] for case in measured), 4)
        summary["by_level"] = {}
        for level in sorted({item["level"] for item in items}):
            subset = [
                records[item["id"]][mode] for item in items
                if item["level"] == level and mode in records.get(item["id"], {})
            ]
            summary["by_level"][level] = {
                "completed": len(subset), "correct": sum(case["correct"] is True for case in subset),
                "review": sum(case["correct"] is None for case in subset),
            }
        summaries[mode] = summary
    atomic_write_json(path, {
        "meta": meta, "graph_verified": graph_verified, "summaries": summaries, "records": records,
    })


def fixture_hash(fixture):
    return hashlib.sha256(entity_key(fixture).encode("utf-8")).hexdigest()


def check_fixture(fixture):
    if any(key not in fixture for key in ("nodes", "edges", "items", "knowledge")):
        raise ValueError("The profiling fixture is incomplete")
    nodes, items = fixture["nodes"], fixture["items"]
    node_ids = {node["id"] for node in nodes}
    if not nodes or len(node_ids) != len(nodes):
        raise ValueError("The fixture must have unique node IDs")
    for node in nodes:
        identifier(node["label"])
    groups = Counter(item["level"] for item in items)
    if groups != {"简单": 10, "中等": 12, "困难": 8} or len({i["id"] for i in items}) != len(items):
        raise ValueError("Expected 30 unique questions split 10/12/8 by difficulty")
    for edge in fixture["edges"]:
        identifier(edge["type"])
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            raise ValueError("The fixture contains an edge with a missing endpoint")
    for item in items:
        names = {column["name"] for column in item["columns"]}
        if len(names) != len(item["columns"]) or not names:
            raise ValueError(f"{item['id']}: invalid column roles")
        if any(set(row) != names for row in item["expected_rows"]):
            raise ValueError(f"{item['id']}: expected rows must preserve column roles")
        if bool(item["expected_rows"]) == bool(item.get("expect_empty", False)):
            raise ValueError(f"{item['id']}: invalid empty-result contract")
    question_set = {item["question"] for item in items}
    gold_queries = {item["reference_cypher"] for item in items}
    for example in fixture["knowledge"]:
        if example["question"] in question_set or example["cypher"] in gold_queries:
            raise ValueError("Evaluation questions and reference Cypher must stay out of the knowledge base")


def failure_kind(actual, profile):
    if not actual.get("error"):
        return None
    if actual.get("failure_kind"):
        return actual["failure_kind"]
    for event in reversed(profile):
        if event["kind"] == "model" and event.get("error_code"):
            return event["error_code"]
    for stage in reversed(actual.get("stages", ())):
        if stage.get("passed") is False:
            if stage.get("stage") == "校验":
                return "validation"
            if stage.get("stage") == "执行":
                return "graph_execution"
    if any(event["kind"] == "graph" and event.get("status") == "error" for event in profile):
        return "graph_execution"
    return "execution_error"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bolt", default="bolt://127.0.0.1:17687")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-fixture", action="store_true")
    parser.add_argument("--audit-empty", type=Path, metavar="COMPLETED_REPORT")
    parser.add_argument("--only", nargs="+")
    harness_location = parser.add_mutually_exclusive_group()
    harness_location.add_argument("--harness-executable")
    harness_location.add_argument("--harness-installation", type=Path)
    parser.add_argument("--harness-timeout", type=int, default=240)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if any(value < 1 for value in (args.workers, args.max_tokens, args.harness_timeout)):
        parser.error("Invalid timeout, workers or token cap")
    fixture = json.loads(WORKLOAD.read_bytes())
    check_fixture(fixture)
    if args.check_fixture:
        print(f"Verified fixture: {len(fixture['items'])} questions, "
              f"{len(fixture['nodes'])} nodes, {len(fixture['edges'])} relationships.")
        return
    if not args.output:
        parser.error("--output is required")
    if args.audit_empty and (args.resume or args.seed or args.only or args.verify_only):
        parser.error("--audit-empty uses a completed report and an already loaded fixture")
    if args.output.exists() and not args.resume:
        parser.error("Use --resume or choose a fresh output")
    from agent import core

    cfg = connection_config(args.bolt)
    if args.audit_empty:
        from tools.profile_empty_audit import audit_empty

        report = json.loads(args.audit_empty.read_bytes())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = audit_empty(fixture, cfg, report)
        atomic_write_json(args.output, result)
        print(json.dumps({mode: outcome["fault"] for mode, outcome in result["outcomes"].items()}))
        return
    if args.seed:
        seed_graph(cfg, fixture)
    info = live_schema(cfg)
    graph = core.Graph(cfg)
    try:
        snapshot = graph_snapshot(graph)
        if snapshot != expected_snapshot(fixture):
            parser.error("The local graph does not match the frozen synthetic fixture")
        items = validated_items(fixture, graph, info)
    finally:
        graph.close()
    if args.only:
        unknown = set(args.only) - {item["id"] for item in items}
        if unknown:
            parser.error(f"Unknown questions: {sorted(unknown)}")
        items = [item for item in items if item["id"] in args.only]
    meta = {
        "workload_sha256": fixture_hash(fixture),
        "schema_sha256": hashlib.sha256(entity_key(info).encode()).hexdigest(),
        "items": [item["id"] for item in items],
        "provider": opencode_go.PROVIDER, "model": opencode_go.MODEL,
        "reasoning_effort": opencode_go.REASONING_EFFORT,
        "modes": MODES, "workers": args.workers, "max_tokens": args.max_tokens,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.verify_only:
        atomic_write_json(args.output, {
            "meta": meta, "schema": info,
            "reference_projections": {item["id"]: item["reference_projection"] for item in items},
        })
        print(f"Verified {len(items)} references, {len(snapshot[0])} nodes, {len(snapshot[1])} relationships.")
        return
    key = os.getenv("OPENCODE_GO_API_KEY")
    if not key:
        parser.error("OPENCODE_GO_API_KEY is required for the three profiled arms")
    try:
        command, harness_env = resolve_harness(args.harness_executable, args.harness_installation)
        meta["harness_version"] = harness_version(command, harness_env)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    meta["harness_timeout_seconds"] = args.harness_timeout
    cfg.update(
        provider=opencode_go.PROVIDER, model=opencode_go.MODEL, key=key,
        max_tokens=args.max_tokens, schema_info=info, knowledge=fixture["knowledge"],
        harness_command=command, harness_env=harness_env,
        harness_timeout=args.harness_timeout, synthetic_workload=True,
    )
    records = {}
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        if previous["meta"] != json.loads(json.dumps(meta)):
            parser.error("Run configuration differs from the checkpoint")
        records = previous["records"]
    write_report(args.output, meta, items, records, None)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {}
        for index, item in enumerate(items):
            if item["id"] in records:
                continue
            offset = index % len(MODES)
            order = MODES[offset:] + MODES[:offset]
            pending[pool.submit(run_question, item, order, cfg)] = item["id"]
        for future in as_completed(pending):
            item_id = pending[future]
            cases = future.result()
            records[item_id] = cases
            write_report(args.output, meta, items, records, None)
            print(item_id, *(f"{mode}={cases[mode]['correct']}/{cases[mode]['wall_seconds']:.2f}s"
                             for mode in MODES), flush=True)
    graph = core.Graph(cfg)
    try:
        graph_verified = graph_snapshot(graph) == snapshot
    finally:
        graph.close()
    write_report(args.output, meta, items, records, graph_verified)
    if not graph_verified:
        parser.error("Graph changed during the comparison")


if __name__ == "__main__":
    main()
