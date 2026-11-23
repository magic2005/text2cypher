import json

import pytest

from tools.harness_eval import _model_usage
from tools.profile_agents import WORKLOAD, check_fixture, connection_config, failure_kind, judge, projection


def test_checked_in_fixture_has_independent_answers():
    fixture = json.loads(WORKLOAD.read_bytes())
    check_fixture(fixture)
    fixture["knowledge"][0]["cypher"] = fixture["items"][0]["reference_cypher"]
    with pytest.raises(ValueError, match="knowledge base"):
        check_fixture(fixture)


def test_projection_preserves_roles_and_scalar_types():
    columns = [
        {"name": "入口", "aliases": ["upstream"]},
        {"name": "下游", "aliases": ["downstream"]},
    ]
    correct = [{"upstream": "gateway", "downstream": "inventory", "evidence": []}]
    swapped = [{"upstream": "inventory", "downstream": "gateway"}]
    item = {"columns": columns, "reference_projection": projection(correct, columns)}
    assert judge(item, {"results": correct, "answer_complete": True}) == (True, None)
    assert judge(item, {"results": swapped, "answer_complete": True}) == (False, "result_mismatch")
    result = {"results": [{"x": "gateway", "y": "inventory"}], "answer_complete": True}
    assert judge(item, result) == (None, "ambiguous_columns")
    numeric = {"columns": [{"name": "总数"}], "reference_projection": ["[1]"]}
    assert judge(numeric, {"results": [{"count": "1"}], "answer_complete": True}) == (False, "result_mismatch")


def test_empty_answer_requires_successful_execution():
    item = {"columns": [{"name": "接口"}], "reference_projection": []}
    assert judge(item, {"results": [], "answer_complete": True}) == (True, None)
    assert judge(item, {"results": [], "answer_complete": False}) == (False, "incomplete_answer")
    errored = {"results": [], "answer_complete": True, "error": "timeout"}
    assert judge(item, errored) == (False, "execution_error")
    assert failure_kind(errored, [{"kind": "model", "error_code": "timeout"}]) == "timeout"
    stages = [{"stage": "校验", "passed": False}]
    assert failure_kind({"error": "rejected", "stages": stages}, []) == "validation"


def test_profile_rejects_remote_databases_and_requires_configured_password(monkeypatch):
    monkeypatch.delenv("T2C_PROFILE_NEO4J_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="loopback"):
        connection_config("bolt://example.com:7687")
    with pytest.raises(ValueError, match="PASSWORD"):
        connection_config("bolt://127.0.0.1:17687")
    monkeypatch.setenv("T2C_PROFILE_NEO4J_PASSWORD", "fixture")
    assert connection_config("bolt://127.0.0.1:17687")["password"] == "fixture"


@pytest.mark.parametrize("reported_input", [404, 1556])
def test_harness_usage_includes_cached_input_once(reported_input):
    usage = _model_usage({"usage": {
        "inputTokens": reported_input, "outputTokens": 28, "totalTokens": 1584,
        "cacheReadTokens": 1152,
    }})
    assert usage == {
        "prompt_tokens": 1556, "completion_tokens": 28, "total_tokens": 1584,
        "cached_tokens": 1152,
    }
