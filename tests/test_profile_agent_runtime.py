import json

import pytest

pytest.importorskip("neo4j")
from agent import core, opencode_go


def test_profiled_native_agent_keeps_repair_and_summary_in_one_session(monkeypatch, tmp_path):
    calls = []
    schema = {
        "labels": ["微服务"], "relationships": [], "properties": {"微服务": []}, "patterns": [],
    }
    examples = [{
        "id": "KB-SYNTHETIC", "category": "计数", "question": "有哪些节点",
        "cypher": "MATCH (n:微服务) RETURN n",
    }]
    answers = iter([
        json.dumps({"queries": ["MATCH (n:微服务) DELETE n RETURN n"]}),
        json.dumps({"queries": ["MATCH (n:微服务) RETURN count(n) AS 数量 LIMIT 1"]}),
        "找到八个节点。",
    ])

    def generate(messages, cfg):
        calls.append((cfg["model_stage"], cfg["session_id"], json.loads(messages[-1]["content"])))
        return next(answers)

    class Graph:
        def run(self, query):
            assert query == "MATCH (n:微服务) RETURN count(n) AS 数量 LIMIT 2"
            return [{"数量": 8}]

    monkeypatch.setattr(opencode_go, "generate", generate)
    monkeypatch.setattr(core, "DATA", tmp_path / "isolated")
    cfg = {
        "provider": opencode_go.PROVIDER, "key": "fixture", "schema_info": schema, "knowledge": examples,
        "profile": [], "record_history": False,
    }
    result = core.ask("有多少服务？", Graph(), cfg, level="简单")
    assert result["error"] is None
    assert result["results"] == [{"数量": 8}]
    assert result["summary"] == "找到八个节点。"
    assert [stage for stage, _, _ in calls] == ["generation", "repair", "summary"]
    assert len({session for _, session, _ in calls}) == 1
    assert calls[0][2]["examples"][0]["question"] == examples[0]["question"]
    assert not core.DATA.exists()


def test_graph_executes_only_a_query_explained_as_read_only(monkeypatch):
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, query, params):
            calls.append(str(query))
            return self

        def consume(self):
            return type("Summary", (), {"query_type": "w"})

    class Driver:
        def session(self, **kwargs):
            assert kwargs["default_access_mode"] == core.READ_ACCESS
            return Session()

    monkeypatch.setattr(core.GraphDatabase, "driver", lambda *args, **kwargs: Driver())
    profile = []
    graph = core.Graph({"uri": "bolt://localhost", "user": "fixture", "password": "fixture",
                        "timeout": 5, "profile": profile})
    with pytest.raises(ValueError, match="只读"):
        graph.run("CREATE (a) RETURN a")
    assert calls == ["EXPLAIN CREATE (a) RETURN a"]
    assert profile[0]["status"] == "error"
    assert profile[0]["execute_seconds"] is None
