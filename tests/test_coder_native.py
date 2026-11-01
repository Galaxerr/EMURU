import io
import json
import os
from pathlib import Path

import pytest

from emuru.projects.coder_native import CoderProtocol


def test_read_uses_bounded_correlated_rpc():
    incoming = io.StringIO('{"id":1,"result":{"content":"return 0"}}\n')
    outgoing = io.StringIO()
    protocol = CoderProtocol(incoming, outgoing)
    assert json.loads(protocol.call("coder_read", {"path": "sum_prices.py"})) == {
        "content": "return 0"
    }
    assert json.loads(outgoing.getvalue()) == {
        "type": "tool",
        "id": 1,
        "name": "coder_read",
        "arguments": {"path": "sum_prices.py"},
    }


def test_protocol_refuses_extra_authority_and_mismatched_reply():
    protocol = CoderProtocol(io.StringIO('{"id":9,"result":{}}\n'), io.StringIO())
    for name, arguments in [
        ("terminal", {"command": "id"}),
        ("coder_check", {"check_id": "python_unittest", "command": "id"}),
        ("coder_check", {"check_id": "shell"}),
        ("coder_write", {"path": "a.py", "content": "x", "expected_sha256": 1}),
    ]:
        with pytest.raises((ValueError, TypeError)):
            protocol.call(name, arguments)
    assert protocol.outgoing.getvalue() == ""
    with pytest.raises(ValueError, match="reply_mismatch"):
        protocol.call("coder_read", {"path": "a.py"})


def test_native_driver_rejects_route_and_budget_expansion_before_loading_runtime():
    from emuru.projects.coder_native import run_native

    request = {
        "prompt": "Fix sum_prices.py",
        "max_iterations": 12,
        "deadline_seconds": 600,
        "model": "emuru",
    }
    for update in [
        {"endpoint": "https://evil.invalid/v1"},
        {"model": "other"},
        {"max_iterations": 13},
        {"deadline_seconds": 601},
        {"max_iterations": True},
        {"command": "id"},
    ]:
        with pytest.raises(ValueError):
            run_native({**request, **update}, lambda *_: "{}")


def test_native_turn_reports_completion_without_exporting_model_prose(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from emuru.projects.coder_native import ENDPOINT, run_native, tool_definitions

    registry = SimpleNamespace(
        register=lambda **kwargs: None, dispatch=lambda *a, **k: "{}"
    )
    registry_module = ModuleType("tools.registry")
    registry_module.registry = registry
    tool_executor = ModuleType("agent.tool_executor")
    tool_executor._dispatch_authorized_once = lambda *a, execute, **k: execute({})
    agent_module = ModuleType("agent")
    agent_module.tool_executor = tool_executor
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.tool_executor", tool_executor)
    monkeypatch.setenv("EMURU_GATEWAY_KEY", "synthetic-test-key")
    monkeypatch.setenv("HERMES_HOME", "/tmp/original-test-home")

    class NativeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.tools = tool_definitions()
            config = json.loads(
                Path(os.environ["HERMES_HOME"], "config.yaml").read_text()
            )
            assert config["tools"]["tool_search"] is False
            assert kwargs["skip_memory"] and kwargs["skip_context_files"]
            assert kwargs["max_iterations"] == 12

        def _create_openai_client(self, kwargs, **kw):
            return SimpleNamespace(
                max_retries=kwargs["max_retries"],
                base_url=kwargs["base_url"],
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **kw: "response")
                ),
            )

        def _invoke_tool(self, *args, **kwargs):
            raise AssertionError("forbidden tool executed")

        def run_conversation(self, prompt, **kwargs):
            assert self._use_prompt_caching is False
            with pytest.raises(ValueError):
                self._invoke_tool("terminal", {"command": "id"}, "task")
            self.client = self._create_openai_client(
                {
                    "api_key": "synthetic-test-key",
                    "base_url": ENDPOINT,
                }
            )
            create = self.client.chat.completions.create
            assert create(model="emuru", tools=tool_definitions()) == "response"
            assert (
                create(
                    model="emuru", tools=[], extra_body={"tools": tool_definitions()}
                )
                == "response"
            )
            assert (
                create(model="emuru", tools=list(reversed(tool_definitions())))
                == "response"
            )
            for update in (
                {"model": "other"},
                {"tools": []},
                {"tools": [{"type": "function", "function": {"name": "terminal"}}]},
                {"extra_body": {"tools": []}},
                {"extra_body": {"tools": None}},
                {
                    "extra_body": {
                        "tools": [
                            {"type": "function", "function": {"name": "terminal"}}
                        ]
                    }
                },
                {"extra_body": {"model": "other"}},
                {"extra_body": []},
            ):
                with pytest.raises((ValueError, TypeError)):
                    create(**{"model": "emuru", "tools": tool_definitions(), **update})
            for update in (
                {"api_key": "wrong"},
                {"base_url": "https://evil.invalid"},
                {"max_retries": 1},
            ):
                with pytest.raises(ValueError):
                    self._create_openai_client(
                        {
                            "api_key": "synthetic-test-key",
                            "base_url": ENDPOINT,
                            **update,
                        }
                    )
            assert not self._try_activate_fallback()
            assert not self._try_recover_primary_transport()
            return {"completed": True, "final_response": "sensitive source and key"}

        def close(self):
            with pytest.raises(ValueError, match="inference_refused"):
                self.client.chat.completions.create(
                    model="emuru", tools=tool_definitions()
                )

    assert run_native(
        {"prompt": "Fix sum_prices.py"}, lambda *_: "{}", NativeAgent
    ) == {"success": True}


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "{}",
        "[]\n",
        '{"id":1,"result":{}}',
        '{"id":1,"result":"' + "x" * (2 * 1024 * 1024) + '"}\n',
    ],
)
def test_protocol_rejects_incomplete_or_oversized_replies(reply):
    protocol = CoderProtocol(io.StringIO(reply), io.StringIO())
    with pytest.raises((ValueError, TypeError)):
        protocol.call("coder_read", {"path": "sum_prices.py"})


def test_missing_model_access_fails_without_exporting_input(monkeypatch):
    from emuru.projects.coder_native import main

    monkeypatch.delenv("EMURU_GATEWAY_KEY", raising=False)
    incoming = io.StringIO('{"prompt":"private source text"}\n')
    outgoing = io.StringIO()
    assert main(incoming, outgoing) == 1
    assert json.loads(outgoing.getvalue()) == {
        "type": "final",
        "success": False,
        "error": "coder_model_access_missing",
        "tool_calls": 0,
    }


def test_write_schema_uses_pinned_native_nullable_shape():
    from emuru.projects.coder_native import tool_definitions

    write = next(
        t["function"]
        for t in tool_definitions()
        if t["function"]["name"] == "coder_write"
    )
    assert write["parameters"]["properties"]["expected_sha256"] == {
        "type": "string",
        "nullable": True,
    }
