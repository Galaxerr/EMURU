"""Offline Groq SSE/error regression against the pinned LiteLLM image."""

import asyncio
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import litellm
from litellm.llms.custom_llm import CustomLLMError

from emuru.models.gateway import write_private
from emuru.models.gateway_guard import guard

TEXT = 'café 雪 \\n "literal"\nactual newline'
status = 200
calls = 0
json_finish = "stop"


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global calls
        calls += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/openai/v1/chat/completions"
        assert self.headers["Authorization"] == "Bearer synthetic-groq"
        assert body["model"] == "openai/gpt-oss-120b"
        assert body["reasoning_effort"] == "low"
        assert body["include_reasoning"] is False
        if body.get("response_format") == {"type": "json_object"}:
            assert not body.get("stream")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "json-one",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "service_tier": "on_demand",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": '{"verdict":"approve"}',
                                },
                                "finish_reason": json_finish,
                            }
                        ],
                    }
                ).encode()
            )
            return
        assert body["stream"] is True
        self.send_response(status)
        self.send_header(
            "Content-Type", "text/event-stream" if status == 200 else "application/json"
        )
        self.end_headers()
        if status != 200:
            self.wfile.write(
                b'{"error":{"message":"controlled failure","type":"api_error"}}'
            )
            return
        arguments = json.dumps({"text": TEXT}, ensure_ascii=False)
        for index, part in enumerate((arguments[:12], arguments[12:])):
            function = {"arguments": part}
            call = {"index": 0, "function": function}
            if index == 0:
                function["name"] = "echo_exact"
                call.update(id="call-one", type="function")
            chunk = {
                "id": "chat-one",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}
                ],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
        self.wfile.write(
            ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
        )


async def check():
    global status, json_finish
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo_exact",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    for streaming in (False, True):
        result = await guard.run(
            [{"role": "user", "content": "synthetic"}],
            {"tools": tools, "max_tokens": 256},
            streaming=streaming,
        )
        assert json.loads(
            result.choices[0].message.tool_calls[0].function.arguments
        ) == {"text": TEXT}
    for streaming in (False, True):
        result = await guard.run(
            [{"role": "user", "content": "Return JSON"}],
            {"response_format": {"type": "json_object"}},
            streaming=streaming,
        )
        assert json.loads(result.choices[0].message.content) == {"verdict": "approve"}
    json_finish = "length"
    try:
        await guard.run(
            [{"role": "user", "content": "Return JSON"}],
            {"response_format": {"type": "json_object"}},
        )
    except CustomLLMError as error:
        assert error.status_code == 502
    else:
        raise AssertionError("Incomplete JSON response was accepted")
    for status in (400, 401, 402, 429, 503):
        before = calls
        try:
            await guard.run([{"role": "user", "content": "synthetic"}], {})
        except (
            litellm.BadRequestError,
            litellm.AuthenticationError,
            litellm.RateLimitError,
            litellm.ServiceUnavailableError,
            litellm.APIError,
        ) as error:
            assert getattr(error, "status_code", None) == status
        else:
            raise AssertionError("Provider error was swallowed")
        assert calls == before + 1, "Provider retried or fell back"


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "route.json"
        write_private(
            path,
            json.dumps(
                {
                    "schema_version": 1,
                    "primary": {"provider": "groq", "model": "openai/gpt-oss-120b"},
                    "fallback": {
                        "provider": "ollama",
                        "model": "disabled",
                        "digest": "a" * 64,
                        "context_tokens": 65536,
                        "output_reserve_tokens": 4096,
                        "qualification": None,
                    },
                }
            ),
        )
        os.environ.update(
            EMURU_CONTAINER_ROUTE=str(path),
            EMURU_GATEWAY_TEST_FIXTURE="1",
            EMURU_TEST_PRIMARY_BASE=f"http://127.0.0.1:{server.server_port}/openai/v1",
            GROQ_API_KEY="synthetic-groq",
        )
        try:
            asyncio.run(check())
        finally:
            server.shutdown()
    print("Groq offline streaming/JSON/error checks: PASS (10 synthetic requests)")
