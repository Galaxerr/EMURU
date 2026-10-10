"""Disposable actual pinned-proxy HTTP/SSE checks; invoked by container checker."""

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from emuru.models.gateway import (
    WORKLOAD_REPETITIONS,
    qualification_record,
    render,
)

counts = {"primary": 0, "local": 0}
mode = "ok"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if mode == "local-daemon-down":
            self.send(503, {"error": "local daemon unavailable"})
            return
        if self.path == "/api/version":
            self.send(200, {"version": "fixture-runtime"})
        else:
            self.send(
                200, {"models": [{"name": "synthetic-local", "digest": "a" * 64}]}
            )

    def do_POST(self):
        request_mode = mode
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self.send(200, {"capabilities": ["tools"], "template": "fixture"})
            return
        backend = "primary" if self.server.server_port == 11435 else "local"
        counts[backend] += 1
        if backend == "primary" and request_mode == "connection-reset":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if backend == "local":
            assert body["options"]["num_ctx"] == 65536, body
            assert body["options"]["num_predict"] == 4096, body
        if backend == "primary" and request_mode.startswith("error-"):
            self.send(int(request_mode.split("-")[1]), {"error": "controlled failure"})
            return
        if request_mode in {"both-down", "local-oom"}:
            self.send(
                500 if request_mode == "local-oom" and backend == "local" else 503,
                {"error": "out of memory" if backend == "local" else "unavailable"},
            )
            return
        if request_mode in {"slow", "cancel"} and backend == "primary":
            time.sleep(2)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        data = {
            "model": body["model"],
            "created_at": "2026-10-08T00:00:00Z",
            "message": {"role": "assistant", "content": backend + "-answer"},
            "done": False,
        }
        if request_mode == "partial-tool":
            data["message"] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "probe", "arguments": '{"message":'}}
                ],
            }
        if request_mode in {"tool-valid", "tool-invalid"}:
            data["message"] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "probe",
                            "arguments": {
                                "message": "ping"
                                if request_mode == "tool-valid"
                                else 42
                            },
                        }
                    }
                ],
            }
        try:
            self.wfile.write((json.dumps(data) + "\n").encode())
            self.wfile.flush()
        except BrokenPipeError:
            return
        if request_mode == "interchunk":
            time.sleep(1)
        if request_mode in {"partial", "partial-tool"}:
            return
        data = {
            "model": body["model"],
            "created_at": "2026-10-08T00:00:00Z",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        try:
            self.wfile.write((json.dumps(data) + "\n").encode())
        except BrokenPipeError:
            return


def main():
    global mode
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from litellm.llms.custom_llm import CustomLLMError

    from emuru.models.gateway_guard import valid_response

    schema = [{"function": {"name": "probe", "parameters": {"type": "object"}}}]
    for arguments in ('{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}', '{"x":'):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call",
                                type="function",
                                function=SimpleNamespace(
                                    name="probe", arguments=arguments
                                ),
                            )
                        ],
                    ),
                )
            ]
        )
        try:
            valid_response(response, schema)
        except CustomLLMError:
            pass
        else:
            raise AssertionError("Unsafe tool JSON accepted: " + arguments)
    for port in (11434, 11435):
        server = ThreadingHTTPServer(("127.0.0.1", port), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        route = {
            "schema_version": 1,
            "primary": {"provider": "ollama", "model": "synthetic-cloud"},
            "fallback": {
                "provider": "ollama",
                "model": "synthetic-local",
                "digest": "a" * 64,
                "context_tokens": 65536,
                "output_reserve_tokens": 4096,
                "qualification": None,
            },
        }
        q = qualification_record(
            route,
            "fixture-runtime",
            hashlib.sha256(b"fixture").hexdigest(),
            {
                "offline_local": True,
                "workload_repetitions": WORKLOAD_REPETITIONS,
                "context_trials": 3,
                "latency_samples": 20,
                "cold_seconds": 1.0,
                "warm_p95_seconds": 0.5,
                "available_ram_bytes": 16 * 1024**3,
                "model_size_bytes": 4 * 1024**3,
                "gpu_present": False,
                "gpu_vram_bytes": 0,
                "oom_events": 0,
            },
            time.time() - 1,
        )
        q["expires_at"] = time.time() + 3600
        q["provenance"] = "deterministic-fixture"
        route["fallback"]["qualification"] = q
        route_path = path / "route.json"

        def save():
            route_path.write_text(json.dumps(route))
            route_path.chmod(0o600)

        save()
        config = path / "proxy.yaml"
        config.write_text(render(route))
        env = dict(
            os.environ,
            EMURU_GATEWAY_KEY="sk-fixture",
            EMURU_CONTAINER_ROUTE=str(route_path),
            EMURU_GATEWAY_TEST_FIXTURE="1",
            EMURU_TEST_PRIMARY_BASE="http://127.0.0.1:11435",
            EMURU_LOCAL_BASE="http://127.0.0.1:11434",
            EMURU_CLOUD_SECONDS="1",
            EMURU_LOCAL_SECONDS="2",
            EMURU_ROUTED_SECONDS="4",
            EMURU_CLOUD_FIRST_TOKEN_SECONDS="0.5",
            EMURU_LOCAL_FIRST_TOKEN_SECONDS="1",
            EMURU_INTER_CHUNK_SECONDS="0.5",
            OLLAMA_API_KEY="synthetic-key",
            LITELLM_LOG="ERROR",
        )
        with open(path / "proxy.log", "w+") as log:
            proxy = subprocess.Popen(
                ["litellm", "--config", str(config), "--port", "4000"],
                env=env,
                stdout=log,
                stderr=log,
            )
            for _ in range(100):
                try:
                    urlopen("http://127.0.0.1:4000/health/liveliness", timeout=1)
                    break
                except (OSError, URLError):
                    if proxy.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read()[-5000:])
                    time.sleep(0.2)

            def request(stream=False, content="hello", tools=None, **params):
                body = {
                    "model": "emuru",
                    "messages": [{"role": "user", "content": content}],
                    "stream": stream,
                }
                if tools is not None:
                    body["tools"] = tools
                body.update(params)
                req = Request(
                    "http://127.0.0.1:4000/v1/chat/completions",
                    json.dumps(body).encode(),
                    {
                        "Authorization": "Bearer sk-fixture",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urlopen(req, timeout=10) as response:
                        return response.status, response.read().decode()
                except HTTPError as error:
                    return error.code, error.read().decode()

            status, body = request()
            assert status == 200 and "primary-answer" in body, (status, body)
            assert counts == {"primary": 1, "local": 0}, counts
            mode = "local-daemon-down"
            before = dict(counts)
            status, body = request()
            assert status == 200 and "primary-answer" in body, (status, body)
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"],
            }, counts
            mode = "error-503"
            status, body = request(True)
            assert status == 200 and "local-answer" in body, (status, body)
            assert counts == {"primary": 3, "local": 1}, counts
            for code in (401, 400, 403):
                mode = "error-" + str(code)
                before = dict(counts)
                status, body = request()
                assert status >= 400, (status, body)
                assert counts == {
                    "primary": before["primary"] + 1,
                    "local": before["local"],
                }, counts
            for code in (429, 529):
                mode = "error-" + str(code)
                before = dict(counts)
                assert request()[0] == 200
                assert counts == {
                    "primary": before["primary"] + 1,
                    "local": before["local"] + 1,
                }
            mode = "connection-reset"
            before = dict(counts)
            assert request()[0] == 200
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"] + 1,
            }
            for stream_flag in (False, True):
                mode = "cancel"
                before = dict(counts)
                body = json.dumps(
                    {
                        "model": "emuru",
                        "messages": [{"role": "user", "content": "cancel"}],
                        "stream": stream_flag,
                    }
                ).encode()
                client = socket.create_connection(("127.0.0.1", 4000))
                client.sendall(
                    b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer sk-fixture\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                for _ in range(50):
                    if counts["primary"] > before["primary"]:
                        break
                    time.sleep(0.01)
                client.shutdown(socket.SHUT_RDWR)
                client.close()
                time.sleep(1.5)
                assert counts["local"] == before["local"], counts
            mode = "partial"
            before = dict(counts)
            status, body = request()
            assert status >= 400, (status, body)
            assert counts["local"] == before["local"], counts
            mode = "partial-tool"
            before = dict(counts)
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    },
                }
            ]
            status, body = request(tools=tools)
            assert status >= 400 and "primary-answer" not in body, (status, body)
            assert counts["local"] == before["local"], counts
            for tool_mode, success in (("tool-valid", True), ("tool-invalid", False)):
                mode = tool_mode
                before = dict(counts)
                status, body = request(True, tools=tools)
                assert (
                    (status == 200 and "ping" in body)
                    if success
                    else "Invalid tool arguments" in body
                ), (status, body)
                assert counts["local"] == before["local"], counts
            mode = "interchunk"
            before = dict(counts)
            started = time.monotonic()
            status, body = request(True)
            assert time.monotonic() - started < 4
            assert "primary-answer" not in body, (status, body)
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"] + 1,
            }, counts
            mode = "slow"
            status, body = request()
            assert status == 200 and "local-answer" in body, (status, body)
            mode = "ok"
            before = dict(counts)
            assert request()[0] == 200
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"],
            }
            mode = "error-503"
            route["fallback"]["qualification"] = None
            save()
            before = dict(counts)
            assert request()[0] >= 400
            assert counts["local"] == before["local"]
            route["fallback"]["qualification"] = q
            save()
            for field, value in (
                ("schema_version", 1),
                ("schema_version", True),
                ("expires_at", time.time() - 1),
                ("runtime_version", "wrong"),
                ("template_sha256", "b" * 64),
                ("effective_context_tokens", 32768),
                ("workload_policy_version", "wrong"),
            ):
                route["fallback"]["qualification"] = {**q, field: value}
                save()
                before = dict(counts)
                assert request()[0] >= 400, field
                assert counts["local"] == before["local"], (field, counts)
            route["fallback"]["qualification"] = q
            route["primary"] = {"provider": "ollama", "model": "changed-cloud"}
            save()
            before = dict(counts)
            assert request()[0] >= 400
            assert counts["local"] == before["local"], counts
            route["primary"] = q["primary"]
            save()
            before = dict(counts)
            assert request(content="x" * 70000)[0] >= 400
            assert counts["local"] == before["local"]
            before = dict(counts)
            assert (
                request(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "oversized",
                            "schema": {"type": "object", "description": "x" * 70000},
                        },
                    }
                )[0]
                >= 400
            )
            assert counts["local"] == before["local"]
            mode = "both-down"
            before = dict(counts)
            assert request()[0] >= 400
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"] + 1,
            }
            mode = "local-oom"
            before = dict(counts)
            assert request()[0] >= 400
            assert counts == {
                "primary": before["primary"] + 1,
                "local": before["local"] + 1,
            }
            print(
                "Pinned proxy HTTP/SSE bounded fallback, retries, eligibility, deadlines, qualification, context, primary recovery: PASS"
            )
            proxy.terminate()
            proxy.wait(timeout=20)


if __name__ == "__main__":
    main()
