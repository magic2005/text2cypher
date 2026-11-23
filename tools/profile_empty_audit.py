"""Replay H08 after adding a consumer in a rolled-back Neo4j transaction."""

from tools.profile_agents import MODES, expected_snapshot, fixture_hash, graph_snapshot, projection
from tools.profile_common import live_schema


def _read_rows(tx, query, schema):
    from agent.core import validate

    query = validate(query, schema)
    if tx.run("EXPLAIN " + query).consume().query_type != "r":
        raise ValueError("Neo4j did not confirm a read-only audit query")
    return [row.data() for row in tx.run(query)]


def audit_empty(fixture, cfg, report):
    from neo4j import GraphDatabase

    from agent import core

    digest = fixture_hash(fixture)
    if report.get("graph_verified") is not True or report.get("meta", {}).get("workload_sha256") != digest:
        raise ValueError("Provide a completed report for the same synthetic fixture")
    runs = report.get("records", {}).get("H08", {})
    if set(runs) != set(MODES):
        raise ValueError("The completed report must include H08 in all three arms")
    case = next(item for item in fixture["items"] if item["id"] == "H08")
    schema = live_schema(cfg)
    expected_graph = expected_snapshot(fixture)
    queue = "queue:lantern_orphan"
    consumer = next(edge["target"] for edge in fixture["edges"] if edge["type"] == "消费自")
    graph = core.Graph(cfg)
    try:
        if graph_snapshot(graph) != expected_graph:
            raise ValueError("The local database does not match the synthetic fixture")
        with GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"])) as driver:
            with driver.session() as session, session.begin_transaction() as tx:
                count = tx.run(
                    "MATCH (q:消息队列 {nodeId: $queue}), (m:方法 {nodeId: $consumer}) "
                    "CREATE (q)-[:消费自]->(m) RETURN count(*) AS created",
                    queue=queue, consumer=consumer,
                ).single(strict=True)["created"]
                if count != 1:
                    raise ValueError("The intervention must add exactly one edge")
                expected = projection(_read_rows(tx, case["reference_cypher"], schema), case["columns"])
                if expected is None or len(expected) != 1 or "publishUnclaimed" not in expected[0]:
                    raise ValueError("The H08 intervention no longer produces the expected publisher")
                outcomes = {}
                for mode, case_run in runs.items():
                    queries = case_run["actual"]["queries"]
                    if case_run["runtime_error"] or not queries:
                        outcomes[mode] = {"correct": None, "rows": None, "fault": "no_completed_query"}
                        continue
                    rows = [row for query in queries for row in _read_rows(tx, query, schema)]
                    values = projection(rows, case["columns"])
                    correct = None if values is None else set(values) == set(expected)
                    fault = "ambiguous_columns" if correct is None else (None if correct else "result_mismatch")
                    outcomes[mode] = {"correct": correct, "rows": len(rows), "fault": fault,
                                      "projected_rows": values}
                tx.rollback()
        if graph_snapshot(graph) != expected_graph:
            raise ValueError("The graph differs from the fixture after rollback")
    finally:
        graph.close()
    return {"case": "H08", "workload_sha256": digest, "expected": expected,
            "intervention": {"source": queue, "type": "消费自", "target": consumer},
            "transaction_rolled_back_and_fixture_verified": True, "outcomes": outcomes}
