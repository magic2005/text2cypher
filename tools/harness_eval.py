"""Run the installed DeepSeek Harness headless profile on one synthetic case."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import re
import subprocess
import tempfile
from threading import Lock, Thread
import time

from agent import opencode_go


MODEL_ROUTE = opencode_go.PROVIDER
REASONING_EFFORT = opencode_go.REASONING_EFFORT
MAX_PROBES = 4
MAX_SAMPLE_ROWS = 5
TOOL_NAME = "mcp__cypher__check"
DISABLED_PLUGINS = (
    "tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search",
    "tool-working-directory", "tool-skill", "skill-filesystem", "agent-instructions",
    "tool-subagent-control", "tool-subagent", "tool-subagent-fork", "tool-agent-team",
    "tool-workflow", "tool-todo", "tool-goal", "tool-web", "mcp-resources",
    "web-search-deepseek", "web-deepseek-api", "web-fetch-http", "remote-access", "typert-gateway",
    "session-title-llm", "session-log-deepseek", "session-telemetry-otel",
    "llm-deepseek", "llm-deepseek-account",
)
TOOL_DEFINITION = {
    "name": "check",
    "description": "Run one bounded read-only Cypher query on the synthetic graph. Return its row count and a small sample.",
    "inputSchema": {"type": "object", "properties": {"cypher": {"type": "string", "maxLength": 8000}},
                    "required": ["cypher"], "additionalProperties": False},
}
USAGE_FIELDS = {
    "inputTokens": "prompt_tokens",
    "outputTokens": "completion_tokens",
    "totalTokens": "total_tokens",
    "cacheReadTokens": "cached_tokens",
    "cacheWriteTokens": "cache_write_tokens",
    "reasoningTokens": "reasoning_tokens",
}
PROVIDER_ERRORS = {"MISSING_CREDENTIAL", "INVALID_CREDENTIAL", "QUOTA", "RATE_LIMIT", "RATE_LIMITED",
                   "INVALID_REQUEST", "UNKNOWN_MODEL", "MALFORMED_RESPONSE", "NETWORK_ERROR",
                   "PROVIDER_ERROR", "TRANSPORT", "TIMEOUT"}


def _bounded(graph, question, text, schemas):
    from agent import core

    candidate, _ = core.clamp_generated_limit(text)
    candidate, _ = core.clamp_generated_paths(candidate)
    query = core.validate(candidate, schemas)
    execution, cap = core.bounded_query(query, question)
    rows = graph.run(execution)
    return query, rows[:cap], len(rows) > cap


class _Probe:
    def __init__(self, item, graph, cfg):
        self.item = item
        self.graph = graph
        self.schema = cfg["schema_info"]
        self.profile = cfg["profile"]
        self.calls = 0
        self.lock = Lock()

    def run(self, arguments):
        with self.lock:
            self.calls += 1
            if self.calls > MAX_PROBES:
                return {"error": "Cypher probe budget exhausted"}
            text = arguments.get("cypher") if isinstance(arguments, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                return {"error": "Pass one Cypher string of at most 8000 characters"}
            return self._run_query(text)

    def _run_query(self, text):
        from agent import core

        start = time.perf_counter()
        status = "error"
        try:
            with core.graph_stage(self.graph, "harness_probe"):
                _, rows, truncated = _bounded(self.graph, self.item["question"], text, self.schema)
            sample = []
            for row in rows[:MAX_SAMPLE_ROWS]:
                if len(json.dumps([*sample, row], ensure_ascii=False, default=str)) > 4000:
                    break
                sample.append(row)
            status = "ok"
            return {"row_count": len(rows), "truncated": truncated, "sample_rows": sample}
        except ValueError as exc:
            return {"error": str(exc)[:500]}
        except Exception:
            return {"error": "Cypher execution failed. Check the graph schema and simplify the query."}
        finally:
            self.profile.append({"kind": "tool", "stage": TOOL_NAME, "status": status,
                                 "elapsed_seconds": time.perf_counter() - start})


def _handler(probe, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_error(405)

        def do_DELETE(self):
            self.send_error(405)

        def do_POST(self):
            if self.path != "/mcp" or not secrets.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    self.send_error(400)
                    return
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise ValueError("Invalid request")
            except ValueError:
                self.send_error(400)
                return
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            result = self.rpc(request.get("method"), request.get("params") or {})
            response = {"jsonrpc": "2.0", "id": request["id"]}
            if result is None:
                response["error"] = {"code": -32601, "message": "Method not found"}
            else:
                response["result"] = result
            data = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def rpc(self, method, params):
            if method == "initialize":
                return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "text2cypher-read-only", "version": "1"}}
            if method == "ping":
                return {}
            if method == "tools/list":
                return {"tools": [TOOL_DEFINITION]}
            if method == "tools/call" and isinstance(params, dict) and params.get("name") == "check":
                result = probe.run(params.get("arguments"))
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                        "isError": "error" in result}
            return None

    return Handler


def _route_patch(cfg):
    model = {"id": opencode_go.MODEL, "reasoningEfforts": {REASONING_EFFORT: REASONING_EFFORT}}
    if cfg.get("max_tokens") is not None:
        model["maxTokens"] = cfg["max_tokens"]
    provider = {
        "apiKeyEnv": "OPENCODE_GO_API_KEY",
        "baseURL": opencode_go.BASE_URL,
        "reasoning": REASONING_EFFORT,
        "headers": {"User-Agent": "Text2Cypher/1.0",
                    "x-opencode-session": cfg.get("session_id") or opencode_go.session_id()},
        "models": [model],
        "retryPolicy": {"mode": "normal", "maxRetries": 2},
    }
    return [
        {"id": "llm-pi-ai", "config": {"providers": {MODEL_ROUTE: provider}}},
        {"id": "agent-default-model", "config": {"provider": MODEL_ROUTE,
            "model": opencode_go.MODEL, "reasoningEffort": REASONING_EFFORT}},
    ]


def _isolation_patch(port, token):
    return [
        *({"id": plugin, "disabled": True} for plugin in DISABLED_PLUGINS),
        {"insert": [{"id": "benchmark-cypher-tool", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "cypher", "transport": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": f"Bearer {token}"}, "startupTimeoutMs": 6000,
            "failOnStartupError": True, "reconnect": {"enabled": False},
        }}]},
    ]


def resolve_harness(executable, installation):
    if installation:
        desktop = installation.resolve()
        app = desktop / "DeepSeek Harness.exe"
        bundle = desktop / "resources" / "app.asar"
        if not app.is_file() or not bundle.is_file():
            raise ValueError("The specified directory does not contain DeepSeek Harness Desktop")
        cli = bundle / "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
        return [str(app), "--expose-internals", str(cli)], {"ELECTRON_RUN_AS_NODE": "1"}
    path = shutil.which(executable or "dsh")
    if not path:
        raise ValueError("Put dsh on PATH or specify --harness-executable / --harness-installation")
    return [str(Path(path).resolve())], {}


def harness_version(command, extra_env):
    with tempfile.TemporaryDirectory(prefix="text2cypher-dsh-version-") as directory:
        proc = subprocess.run(
            [*command, "--version"], cwd=directory, check=False, capture_output=True,
            env=_launch_env(directory, None, extra_env), timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    output = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", output):
        raise ValueError("Could not verify the DeepSeek Harness version")
    return output


def _launch_env(home, key, extra_env):
    inherited = ("SystemRoot", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "WINDIR",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
    env = {name: os.environ[name] for name in inherited if name in os.environ}
    if env.get("NO_PROXY"):
        env["NO_PROXY"] += ",127.0.0.1,localhost"
    else:
        env["NO_PROXY"] = "127.0.0.1,localhost"
    env.update(extra_env, DSH_HOME=str(home))
    if key:
        env["OPENCODE_GO_API_KEY"] = key
    return env


def _model_usage(event):
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return {}
    normalized = {
        target: usage[name] for name, target in USAGE_FIELDS.items() if type(usage.get(name)) is int
    }
    if {"prompt_tokens", "completion_tokens", "total_tokens"} <= normalized.keys():
        cache = normalized.get("cached_tokens", 0) + normalized.get("cache_write_tokens", 0)
        if normalized["prompt_tokens"] + normalized["completion_tokens"] + cache == normalized["total_tokens"]:
            normalized["prompt_tokens"] += cache
    return normalized


def _read_events(stream, profile, outcome):
    starts = {}
    tool_calls = 0
    for line in stream:
        observed = time.perf_counter()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "status":
            key = (event.get("turn"), event.get("step"))
            if event.get("phase") == "step_start":
                starts[key] = observed
            if event.get("phase") == "step_end" and key in starts:
                profile.append({"kind": "model", "stage": "harness_step",
                                "timing_source": "native_headless_step_boundary",
                                "provider": MODEL_ROUTE, "model": opencode_go.MODEL,
                                "reasoning_effort": REASONING_EFFORT,
                                "elapsed_seconds": observed - starts.pop(key), "usage": _model_usage(event)})
        if kind == "tool_call" and event.get("tool") == TOOL_NAME:
            tool_calls += 1
        if kind == "final":
            outcome.update(reason=event.get("reason"), text=event.get("text"), tool_calls=tool_calls)


def _headless(command, env, home, prompt, timeout, profile):
    outcome = {}
    start = time.perf_counter()
    status = "error"
    try:
        with subprocess.Popen(command, cwd=home, env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            reader = Thread(target=_read_events, args=(process.stdout, profile, outcome), daemon=True)
            reader.start()
            try:
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                error = RuntimeError("DeepSeek Harness headless 超时")
                error.code = "harness_timeout"
                raise error from None
            finally:
                reader.join(timeout=5)
            reason = outcome.get("reason")
            if process.returncode != 0 or not isinstance(reason, dict) or reason.get("kind") != "completed":
                code = reason.get("code") if isinstance(reason, dict) else None
                suffix = f", {code}" if isinstance(code, str) and code in PROVIDER_ERRORS else ""
                error = RuntimeError(f"DeepSeek Harness headless 未完成（exit={process.returncode}{suffix}）")
                error.code = code.lower() if suffix else "harness_incomplete"
                raise error
            status = "ok"
            return outcome
    finally:
        profile.append({"kind": "harness", "stage": "headless", "status": status,
                        "elapsed_seconds": time.perf_counter() - start})


def _prompt(item, info):
    schema = {name: info.get(name, []) for name in ("labels", "properties", "relationship_properties", "patterns")}
    return (
        "你在解决一个合成 Neo4j 图谱的 Text2Cypher 评测问题。"
        "使用下方 schema。可调用 mcp__cypher__check 检查你生成的只读 Cypher 和少量结果。"
        "返回问题要求的字段，列举唯一实体用 DISTINCT。需要时可拆为 1–4 条只读查询。"
        "禁止 CALL、注释和写操作。路径必须有有限的 1–5 跳上界。"
        "最终只输出 JSON 对象 {\"queries\":[\"MATCH ... RETURN ... LIMIT 100\"]}，不要附加说明。\n"
        + json.dumps({"question": item["question"], "level": item["level"], "schema": schema}, ensure_ascii=False)
    )


def run_harness(item, graph, cfg):
    """Run a fresh native Agent session and execute its final queries locally."""
    from agent import core

    result = {"queries": [], "results": [], "answer_complete": False, "error": None, "stages": []}
    if cfg.get("synthetic_workload") is not True:
        result["error"] = "DeepSeek Harness 评测当前仅启用合成图谱"
        return result
    try:
        probe = _Probe(item, graph, cfg)
        token = secrets.token_hex(24)
        with tempfile.TemporaryDirectory(prefix="text2cypher-dsh-") as directory:
            home = Path(directory)
            with ThreadingHTTPServer(("127.0.0.1", 0), _handler(probe, token)) as server:
                server.daemon_threads = False
                thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                thread.start()
                try:
                    route_patch = home / "route.yml"
                    route_patch.write_text(json.dumps(_route_patch(cfg)), encoding="utf-8")
                    tools_patch = home / "tools.yml"
                    tools_patch.write_text(json.dumps(_isolation_patch(server.server_port, token)), encoding="utf-8")
                    command = list(cfg["harness_command"])
                    command.extend(["--profile", "headless", "--patch", str(route_patch)])
                    command.extend(["--patch", str(tools_patch), "--json",
                                    "--permission-mode", "read-only", "--approval-policy", "never", "-"])
                    outcome = _headless(command, _launch_env(home, cfg["key"], cfg["harness_env"]), home,
                                        _prompt(item, cfg["schema_info"]), cfg.get("harness_timeout", 180), cfg["profile"])
                finally:
                    server.shutdown()
                    thread.join()
        if not isinstance(outcome.get("text"), str) or not outcome["text"].strip():
            raise ValueError("DeepSeek Harness 未返回 Cypher")
        queries = core.parse_queries(outcome["text"])
        if not 1 <= len(queries) <= 4 or any(not isinstance(text, str) for text in queries):
            raise ValueError("DeepSeek Harness 应返回 1–4 条 Cypher")
        result["stages"].append({"stage": "DeepSeek Harness", "provider": MODEL_ROUTE,
                                 "model": opencode_go.MODEL, "reasoning_effort": REASONING_EFFORT,
                                 "native_tool_calls": outcome["tool_calls"], "received_graph_probes": probe.calls})
        complete = True
        rows_by_key = {}
        with core.graph_stage(graph, "execution"):
            for text in queries:
                query, rows, truncated = _bounded(graph, item["question"], text, cfg["schema_info"])
                result["queries"].append(query)
                complete = complete and not truncated
                for row in rows:
                    rows_by_key.setdefault(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str), row)
        result.update(results=list(rows_by_key.values()), answer_complete=complete)
    except (RuntimeError, ValueError) as exc:
        result["error"] = str(exc)
        result["failure_kind"] = getattr(exc, "code", None) or "validation"
    except OSError as exc:
        result["error"] = f"DeepSeek Harness 未能启动（{type(exc).__name__}）"
        result["failure_kind"] = "harness_startup"
    except Exception as exc:
        result["error"] = f"DeepSeek Harness 评测失败（{type(exc).__name__}）"
        result["failure_kind"] = "graph_execution"
    return result
