"""Independent helpers for the synthetic profiling runner."""

from collections import Counter, defaultdict
import json
import statistics


DIRECT_SYSTEM = (
    '你是只读 Neo4j Cypher 生成器。根据问题和提供的真实 Schema 生成恰好一条查询，'
    '只输出 JSON {"queries":["MATCH ... RETURN ... LIMIT 100"]}。'
    '使用真实标签、关系方向和属性，并返回问题要求的字段。列举唯一实体使用 DISTINCT。'
    '不要把 LIMIT 当作语义过滤条件。路径限制在 1–5 跳。禁止 CALL、注释和写操作。'
)


def direct(item, graph, cfg):
    from agent import core, opencode_go

    info = cfg["schema_info"]
    compact = {name: info[name] for name in ("labels", "properties", "relationship_properties", "patterns")}
    request = json.dumps(
        {"question": item["question"], "level": item["level"], "schema": compact},
        ensure_ascii=False,
    )
    result = {"queries": [], "results": [], "answer_complete": False, "error": None}
    try:
        raw = core.generate(
            [{"role": "system", "content": DIRECT_SYSTEM}, {"role": "user", "content": request}],
            {**cfg, "model_stage": "generation"},
        )
        queries = core.parse_queries(raw)
        if len(queries) != 1 or not isinstance(queries[0], str):
            raise ValueError("无 Agent 对照只接受一条 Cypher 查询")
        candidate, _ = core.clamp_generated_limit(queries[0])
        candidate, _ = core.clamp_generated_paths(candidate)
        query = core.validate(candidate, info)
        result["queries"].append(query)
        execution_query, cap = core.bounded_query(query, item["question"])
        graph.profile_stage = "execution"
        rows = graph.run(execution_query)
        result["results"] = rows[:cap]
        result["answer_complete"] = len(rows) <= cap
    except opencode_go.ModelRequestError as exc:
        result["error"] = str(exc)
        result["failure_kind"] = exc.code
    except (TypeError, ValueError) as exc:
        result["error"] = str(exc)
        result["failure_kind"] = "validation" if not result["queries"] else "graph_execution"
    except Exception as exc:
        result["error"] = str(exc)
        result["failure_kind"] = "graph_execution"
    return result


def summary_for(records):
    events = [event for item in records for event in item["profile"]]
    model = [event for event in events if event["kind"] == "model"]
    graph = [event for event in events if event["kind"] == "graph"]
    wall = sorted(item["wall_seconds"] for item in records)
    by_stage = defaultdict(float)
    for event in events:
        by_stage[f'{event["kind"]}/{event.get("stage") or "other"}'] += event["elapsed_seconds"]
    usage = Counter()
    for event in model:
        model_usage = event.get("usage") or {}
        for name, value in model_usage.items():
            if type(value) is int:
                usage[name] += value
        for group, name in (("prompt_tokens_details", "cached_tokens"),
                            ("completion_tokens_details", "reasoning_tokens")):
            value = model_usage.get(group, {}).get(name)
            if name not in model_usage and type(value) is int:
                usage[name] += value
    return {
        "completed": len(records),
        "automatic_correct": sum(item["correct"] is True for item in records),
        "requires_review": sum(item["correct"] is None for item in records),
        "known_incorrect": sum(item["correct"] is False for item in records),
        "execution_errors": sum(bool(item["runtime_error"]) for item in records),
        "wall_median_seconds": statistics.median(wall) if wall else None,
        "wall_p95_seconds": wall[(95 * len(wall) - 1) // 100] if wall else None,
        "model_calls": len(model),
        "http_attempts": sum(len(event.get("attempts", ())) for event in model),
        "graph_calls": len(graph),
        "graph_explain_seconds": round(sum(event.get("explain_seconds") or 0 for event in graph), 4),
        "graph_execute_seconds": round(sum(event.get("execute_seconds") or 0 for event in graph), 4),
        "stage_seconds": {key: round(value, 4) for key, value in sorted(by_stage.items())},
        "token_usage": dict(usage),
    }


def live_schema(cfg):
    """Read label, property and relationship names without samples or data values."""
    from agent import core

    graph = core.Graph(cfg)
    try:
        nodes = graph.run(
            "MATCH (n) UNWIND labels(n) AS label "
            "RETURN label, collect(DISTINCT keys(n)) AS properties ORDER BY label"
        )
        relationships = graph.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, "
            "collect(DISTINCT keys(r)) AS properties ORDER BY type"
        )
        patterns = graph.run(
            "MATCH (a)-[r]->(b) RETURN labels(a) AS source, type(r) AS type, "
            "labels(b) AS target, count(*) AS count ORDER BY count DESC, source, type, target"
        )
    finally:
        graph.close()
    return {
        "labels": [row["label"] for row in nodes],
        "relationships": [row["type"] for row in relationships],
        "properties": {row["label"]: property_names(row) for row in nodes},
        "relationship_properties": [{"type": row["type"], "properties": property_names(row)}
                                    for row in relationships],
        "patterns": patterns,
    }


def property_names(row):
    return sorted({name for names in row["properties"] for name in names})


def entity_key(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
