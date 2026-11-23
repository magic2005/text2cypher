"""OpenCode Go transport and per-call telemetry for DeepSeek V4.1 Flash."""

import json
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from uuid import uuid4


PROVIDER = "opencode-go"
MODEL = "deepseek-v4.1-flash"
BASE_URL = "https://opencode.ai/inference/go/openai/v1"
ENDPOINT = f"{BASE_URL}/chat/completions"
REASONING_EFFORT = "max"


def session_id():
    return "ses_" + uuid4().hex


class ModelRequestError(RuntimeError):
    def __init__(self, code, status_code=None):
        self.code = code
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else code
        super().__init__(f"模型请求失败（{detail}）")


def _model_usage(payload):
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens", "cached_tokens", "reasoning_tokens")
    result = {key: usage[key] for key in keys if type(usage.get(key)) is int}
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            result[key] = {name: details[name] for name in ("cached_tokens", "reasoning_tokens")
                           if type(details.get(name)) is int}
    return result


def _completion_text(payload):
    try:
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ModelRequestError("output_limit")
        content = choice["message"]["content"]
    except (AttributeError, IndexError, KeyError, TypeError):
        raise ModelRequestError("invalid_response") from None
    if isinstance(content, str) and content.strip():
        return content
    raise ModelRequestError("empty_content")


def generate(messages, cfg):
    if not cfg["key"]:
        raise RuntimeError("未配置允许的模型密钥 LLM_API_KEY")
    started = time.perf_counter()
    model = cfg.get("model") or MODEL
    if model != MODEL:
        raise ValueError("OpenCode Go 仅配置为运行 DeepSeek V4.1 Flash")
    payload = {"model": model, "temperature": 0, "messages": messages}
    payload.update(thinking={"type": "enabled"}, reasoning_effort=REASONING_EFFORT)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Text2Cypher/1.0",
        "x-opencode-session": cfg.get("session_id") or session_id(),
    }
    if cfg.get("max_tokens") is not None:
        payload["max_tokens"] = cfg["max_tokens"]
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), headers=headers)
    req.add_unredirected_header("Authorization", "Bearer " + cfg["key"])
    profile = cfg.get("profile")
    event = {"kind": "model", "stage": cfg.get("model_stage", "generation"),
             "status": "error", "usage": {}, "attempts": []}
    try:
        for attempt in range(3):
            attempt_started = time.perf_counter()
            details = {"attempt": attempt + 1, "status": "error"}
            try:
                with urllib.request.urlopen(req, timeout=cfg.get("model_timeout", 60)) as response:
                    response_payload = json.load(response)
                if not isinstance(response_payload, dict):
                    raise ModelRequestError("invalid_response")
                event["usage"] = _model_usage(response_payload)
                content = _completion_text(response_payload)
                event["status"] = details["status"] = "ok"
                return content
            except urllib.error.HTTPError as exc:
                exc.close()
                error = ModelRequestError("http_error", exc.code)
                details["http_status"] = exc.code
                retryable = exc.code in {429, 500, 502, 503, 504}
            except TimeoutError:
                error = ModelRequestError("timeout")
                retryable = True
            except urllib.error.URLError as exc:
                error = ModelRequestError("timeout" if isinstance(exc.reason, TimeoutError) else "transport")
                retryable = True
            except (ConnectionError, HTTPException):
                error = ModelRequestError("transport")
                retryable = True
            except ValueError:
                error = ModelRequestError("invalid_response")
                retryable = False
            except ModelRequestError as exc:
                error = exc
                retryable = False
            finally:
                details["elapsed_seconds"] = time.perf_counter() - attempt_started
                event["attempts"].append(details)
            details["error_code"] = error.code
            if not retryable or attempt == 2:
                event["error_code"] = error.code
                if error.status_code is not None:
                    event["http_status"] = error.status_code
                raise error from None
            time.sleep(attempt + 1)
    finally:
        if profile is not None:
            event["elapsed_seconds"] = time.perf_counter() - started
            profile.append(event)
