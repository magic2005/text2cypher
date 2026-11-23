import io
import json

import pytest

from agent import opencode_go


def test_go_request_uses_max_effort_session_and_unredirected_key(monkeypatch):
    requests = []

    def send(request, timeout):
        requests.append(request)
        reply = {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 4}},
        }
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(opencode_go.urllib.request, "urlopen", send)
    cfg = {"key": "fixture", "session_id": "ses_test", "profile": [], "max_tokens": 123}
    assert opencode_go.generate([{"role": "user", "content": "synthetic"}], cfg) == "OK"
    request = requests[0]
    assert request.full_url == opencode_go.ENDPOINT
    assert request.get_header("X-opencode-session") == "ses_test"
    assert "Authorization" not in request.headers
    payload = json.loads(request.data)
    assert payload["model"] == opencode_go.MODEL
    assert payload["reasoning_effort"] == "max"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["max_tokens"] == 123
    assert cfg["profile"][0]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 4


def test_truncated_go_reply_is_an_output_limit(monkeypatch):
    response = {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
    monkeypatch.setattr(
        opencode_go.urllib.request, "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(response).encode()),
    )
    cfg = {"key": "fixture", "profile": []}
    with pytest.raises(opencode_go.ModelRequestError) as error:
        opencode_go.generate([{"role": "user", "content": "synthetic"}], cfg)
    assert error.value.code == "output_limit"
    assert cfg["profile"][0]["error_code"] == "output_limit"
    assert len(cfg["profile"][0]["attempts"]) == 1
