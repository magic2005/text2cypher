import json
import pytest
from agent.core import PROVIDERS, bounded_query, clamp_generated_limit, clamp_generated_paths, examples, filter_routed_messages, lexical_score, parse_queries, result_summary, retrieve, retrieve_ranked, schema, topology, validate, verify_complete_chains
from tools.evaluate import canonical
from tools.build_evaluation import ITEMS, COMBINED_COLUMNS, complete_reference


def test_safe_real_query():
    assert validate("MATCH (s:微服务) RETURN s.服务名称",schema()).endswith("LIMIT 100")


def test_real_relationship_property_is_allowed():
    query = "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) WHERE pub.路由键=route.路由键 RETURN p LIMIT 20"
    assert validate(query, schema()) == query


@pytest.mark.parametrize("query",[
    "MATCH (n) DELETE n RETURN n", "MATCH (n) SET n.x=1 RETURN n",
    "MATCH (n:不存在) RETURN n", "MATCH (n) RETURN n; MATCH (m) RETURN m",
    "CALL db.labels() YIELD label RETURN label", "MATCH (n) RETURN n // comment",
    "MATCH (n) RETURN n LIMIT 100000", "MATCH (a)-[:调用*]->(b) RETURN a",
    "MATCH (a)-[:调用*1..20]->(b) RETURN a"])
def test_reject_unsafe(query):
    with pytest.raises(ValueError):validate(query,schema())


@pytest.mark.parametrize("word", ["DELETE", "CREATE", "CALL"])
def test_write_keywords_inside_string_values_are_read_only(word):
    query = f"MATCH (a:上游API) WHERE a.HTTP方法='{word}' RETURN a.API路径"
    assert validate(query, schema()).endswith("LIMIT 100")


def test_comment_markers_and_semicolons_inside_strings_are_not_syntax():
    query = "MATCH (a:上游API) WHERE a.API路径='http://host/a;/*safe*/' RETURN a.API路径"
    assert validate(query, schema()).endswith("LIMIT 100")
    for unsafe in (
        "MATCH (a:上游API) DELETE a RETURN a",
        "MATCH (a:上游API) CREATE (b:上游API) RETURN a",
        "MATCH (a:上游API) CALL db.labels() RETURN a",
        "MATCH (a:上游API) RETURN a; MATCH (b:上游API) RETURN b",
        "MATCH (a:上游API) RETURN a // comment",
        "MATCH (a:上游API) RETURN a /* comment */",
        "MATCH (a:上游API) WHERE a.API路径='RETURN'",
    ):
        with pytest.raises(ValueError):
            validate(unsafe, schema())


def test_parse_and_retrieve():
    assert parse_queries('```json\n{"queries":["MATCH (n) RETURN n"]}\n```')==["MATCH (n) RETURN n"]
    assert retrieve("food_delivery 消息队列")


def test_allowed_model_providers():
    assert set(PROVIDERS)=={"deepseek","glm","kimi"}
    assert PROVIDERS["deepseek"][1]=="deepseek-flash"


def test_result_summary_uses_data():
    text = result_summary([{"服务":"ts-food-service"}], 2)
    assert "2 个只读查询" in text and "ts-food-service" in text


def test_evaluation_ignores_alias_and_column_order():
    assert canonical([{"a":"x","b":"y"}]) == canonical([{"other":"y","name":"x"}])


def test_topology_never_connects_nodes_just_because_they_share_a_row():
    a = {"nodeId": "svc:a", "服务名称": "a"}
    b = {"nodeId": "svc:b", "服务名称": "b"}
    unrelated = topology([{"left": a, "right": b}])
    assert len(unrelated["nodes"]) == 2
    assert unrelated["edges"] == []
    witnessed = topology([{"left": a, "right": b, "rel": {
        "__graph_edge__": True, "source": "svc:b", "target": "svc:a", "type": "消息依赖"}}])
    assert witnessed["edges"] == [{"source": "svc:b", "target": "svc:a", "label": "消息依赖"}]


def test_open_ended_query_probes_past_generated_limit():
    query = "MATCH (d:下游API)-[:外部调用]->(a:上游API) RETURN d,a LIMIT 100"
    effective, cap = bounded_query(query, "哪些下游 API 能映射到入口 API？")
    assert cap == 500 and effective.endswith("LIMIT 501")
    explicit, cap = bounded_query("MATCH (s:微服务) RETURN s LIMIT 10", "列出十个微服务")
    assert cap == 10 and explicit.endswith("LIMIT 11")


def test_oversized_model_limit_is_clamped_before_strict_validation():
    original = "MATCH (s:微服务) RETURN s LIMIT 200"
    repaired, changed = clamp_generated_limit(original)
    assert changed and repaired.endswith("LIMIT 100")
    assert validate(repaired, schema()) == repaired


def test_generated_reverse_path_keeps_zero_hop_and_bounded_upper_limit():
    original = "MATCH p=(m:方法)-[:调用|接口调用*0..6]->(t:方法) RETURN p LIMIT 100"
    repaired, changed = clamp_generated_paths(original)
    assert changed and "*0..5" in repaired
    assert validate(repaired, schema()) == repaired


def test_full_chain_filters_partial_rows_without_banning_optional_match():
    rows = [
        {"发布者方法": {"nodeId": "mtd:p"}, "交换机": {"nodeId": "exchange:e"},
         "队列": {"nodeId": "queue:q"}, "消费者方法": {"nodeId": "mtd:c"}},
        {"发布者方法": {"nodeId": "mtd:p"}, "交换机": {"nodeId": "exchange:e"},
         "队列": {"nodeId": "queue:other"}, "消费者方法": None},
    ]
    class GraphStub:
        def run(self, query, params):
            assert "发布至" in query and "路由至" in query and "消费自" in query
            assert "pub.路由键=route.路由键" in query
            assert len(params["candidates"]) == 1
            return [{"i": 0}]
    complete, stage = verify_complete_chains("email 的消息发布到消费全链路是什么？", rows, GraphStub())
    assert complete == rows[:1] and stage["discarded"] == 1
    partial, stage = verify_complete_chains("email 有哪些消息队列？", rows, GraphStub())
    assert partial == rows and stage["required"] is False


def test_full_chain_accepts_actual_returned_path_evidence():
    edges = [{"__graph_edge__": True, "type": "发布至", "source": "mtd:p", "target": "exchange:e", "properties": {"路由键": "notice"}},
             {"__graph_edge__": True, "type": "路由至", "source": "exchange:e", "target": "queue:q", "properties": {"路由键": "notice"}},
             {"__graph_edge__": True, "type": "消费自", "source": "queue:q", "target": "mtd:c"}]
    class GraphStub:
        def run(self, *_):
            raise AssertionError("returned path already proves the chain")
    row = {"path": {"__graph_path__": True, "relationships": edges}}
    verified, stage = verify_complete_chains("查看消息完整链路", [row], GraphStub())
    assert verified == [row] and stage["verified"] == 1


def test_shared_exchange_does_not_make_mismatched_routing_keys_a_chain():
    edges = [{"__graph_edge__": True, "type": "发布至", "source": "mtd:p", "target": "exchange:e", "properties": {"路由键": "food_delivery"}},
             {"__graph_edge__": True, "type": "路由至", "source": "exchange:e", "target": "queue:q", "properties": {"路由键": "email"}},
             {"__graph_edge__": True, "type": "消费自", "source": "queue:q", "target": "mtd:c"}]
    class GraphStub:
        def run(self, *_):
            return []
    row = {"path": {"__graph_path__": True, "relationships": edges}}
    verified, stage = verify_complete_chains("查看消息完整链路", [row], GraphStub())
    assert verified == [] and stage["discarded"] == 1


def test_mq_target_filter_drops_mismatched_path_but_preserves_rest_evidence():
    invalid = {"path": {"__graph_path__": True, "relationships": [
        {"__graph_edge__": True, "type": "发布至", "source": "mtd:p", "target": "exchange:e", "properties": {"路由键": "food_delivery"}},
        {"__graph_edge__": True, "type": "路由至", "source": "exchange:e", "target": "queue:q", "properties": {"路由键": "email"}}]}}
    rest = {"REST路径": "/api/example"}
    class GraphStub:
        def run(self, *_):
            raise AssertionError("path evidence should be sufficient")
    filtered, stage = filter_routed_messages("REST 路径和 MQ 目标有哪些？", [invalid, rest], GraphStub())
    assert filtered == [rest] and stage["discarded"] == 1


def test_mq_target_filter_checks_scalar_node_ids_with_graph():
    row = {"发布方法节点": "mtd:p", "交换机节点": "exchange:e", "队列节点": "queue:q"}
    class GraphStub:
        def run(self, query, params):
            assert "pub.路由键=route.路由键" in query
            assert params["candidates"][0]["queue"] == "queue:q"
            return []
    filtered, stage = filter_routed_messages("消息从哪个方法发出、由谁接收？", [row], GraphStub())
    assert filtered == [] and stage["discarded"] == 1


def test_full_chain_scalar_projection_is_checked_against_graph():
    row = {"发布者": "p.send", "交换机": "default", "队列": "notice", "消费者": "c.receive"}
    class GraphStub:
        def run(self, query, params):
            assert "p.全限定名" in query and params["candidates"][0]["publisher"] == "p.send"
            return [{"i": 0}]
    verified, stage = verify_complete_chains("消息发布到消费全链路", [row], GraphStub())
    assert verified == [row] and stage["discarded"] == 0


def test_scalar_chain_uses_question_queue_service_and_returned_route_key():
    row = {"发布方法": "p.send", "消费方法": "c.receive", "路由键": "food_delivery"}
    entities = [{"label": "消息队列", "value": "food_delivery"},
                {"label": "微服务", "value": "ts-delivery-service"}]
    class GraphStub:
        def run(self, query, params):
            assert "q.所属服务名=candidate.queue_service" in query
            assert "pub.路由键=candidate.routing_key" in query
            assert "pub.路由键=route.路由键" in query and "消费自" in query
            candidate = params["candidates"][0]
            assert candidate["queue"] == "food_delivery"
            assert candidate["queue_service"] == "ts-delivery-service"
            assert candidate["routing_key"] == "food_delivery"
            return [{"i": 0}]
    checked, stage = verify_complete_chains(
        "food_delivery 消息到 ts-delivery-service 的完整链路", [row], GraphStub(), entities)
    assert checked == [row] and stage["verified"] == 1


def test_scalar_chain_rejects_false_route_or_service_and_marks_missing_evidence():
    row = {"发布方法": "p.send", "队列服务": "ts-wrong-service", "消费方法": "c.receive"}
    entities = [{"label": "消息队列", "value": "email"}]
    class GraphStub:
        def run(self, query, params):
            assert params["candidates"][0]["queue"] == "email"
            assert params["candidates"][0]["queue_service"] == "ts-wrong-service"
            return []
    checked, stage = verify_complete_chains("email 消息完整链路", [row], GraphStub(), entities)
    assert checked == [] and stage["rejected"] == 1
    missing = {"发布方法": "p.send", "消费方法": "c.receive"}
    checked, stage = verify_complete_chains("消息完整链路", [missing], GraphStub())
    assert checked == [] and stage["insufficient_evidence"] == 1


def test_scalar_chain_rejects_conflicting_question_and_result_constraints():
    row = {"发布方法": "p.send", "队列": "email", "队列服务": "ts-notification-service",
           "消费方法": "c.receive", "发布路由键": "email", "绑定路由键": "food_delivery"}
    entities = [{"label": "消息队列", "value": "email"},
                {"label": "微服务", "value": "ts-delivery-service"}]
    class GraphStub:
        def run(self, *_):
            raise AssertionError("conflicting constraints must never reach a graph query")
    verified, stage = verify_complete_chains("email 消息完整链路", [row], GraphStub(), entities)
    assert verified == [] and stage["rejected"] == 1
    assert stage["insufficient_evidence"] == 0


def test_unverifiable_chain_is_reported_as_incomplete(monkeypatch, tmp_path):
    import agent.core as core
    info = schema()
    monkeypatch.setattr(core, "schema", lambda: info)
    monkeypatch.setattr(core, "recognize", lambda *_: [])
    monkeypatch.setattr(core, "DATA", tmp_path)
    class GraphStub:
        def run(self, query, params=None):
            return [{"发布方法": "p.send", "消费方法": "c.receive"}]
    query = "MATCH (p:方法) RETURN p.全限定名 AS 发布方法,p.方法名 AS 消费方法 LIMIT 1"
    model = lambda *_: json.dumps({"queries": [query]}, ensure_ascii=False)
    result = core.ask("消息发布到消费完整链路", GraphStub(), {"key": ""}, model=model)
    assert result["results"] == [] and result["answer_complete"] is False
    assert len(result["unverified_chain_rows"]) == 1
    assert "证据不足" in result["summary"]


def test_semantic_reranking_handles_paraphrases_with_explanations():
    paraphrases = [
        ("哪个方法接收 email 发来的消息？", "K26"),
        ("ts-food-service 的同步和异步依赖都流向哪里？", "K28"),
        ("food_delivery 的消息全程从生产端到消费端怎么走？", "K24"),
    ]
    old_winners = []
    for question, expected in paraphrases:
        old_winners.append(sorted(examples(), key=lambda item: lexical_score(question, item), reverse=True)[0]["id"])
        winner = retrieve_ranked(question, 1)[0]
        assert winner["example"]["id"] == expected
        assert winner["reasons"]["shared_intents"]
    assert old_winners[0] != paraphrases[0][1]


def test_reference_builder_removes_illustrative_limits_and_orders_rows():
    assert len(ITEMS) == 30
    assert len(COMBINED_COLUMNS) == 4
    for id_, _, _, query in ITEMS:
        if id_ in COMBINED_COLUMNS:
            assert query is None
            continue
        safe = validate(query, schema())
        complete, cap = complete_reference(safe)
        if cap > 1:
            assert "ORDER BY" in complete and complete.endswith("LIMIT 5001")
        else:
            assert complete == safe


def test_invalid_model_subquery_gets_one_revalidated_repair(monkeypatch, tmp_path):
    import agent.core as core
    info = schema()
    calls = []
    def model(messages, cfg):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({"queries": ["CALL db.labels() YIELD label RETURN label"]})
        if len(calls) == 2:
            return json.dumps({"queries": ["MATCH (s:微服务) RETURN s.服务名称 AS 服务 LIMIT 1"]})
        return "找到 ts-food-service。"
    class GraphStub:
        def run(self, query, params=None):
            assert query.startswith("MATCH") and "CALL" not in query
            return [{"服务": "ts-food-service"}]
    monkeypatch.setattr(core, "generate", model)
    monkeypatch.setattr(core, "schema", lambda: info)
    monkeypatch.setattr(core, "recognize", lambda *_: {})
    monkeypatch.setattr(core, "DATA", tmp_path)
    result = core.ask("列出一个微服务", GraphStub(), {"key": "test"}, model=model)
    assert result["error"] is None and result["results"] == [{"服务": "ts-food-service"}]
    assert sum(stage["stage"] == "生成修正" for stage in result["stages"]) == 1
    assert any(stage.get("after_repair") for stage in result["stages"])
