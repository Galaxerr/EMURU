"""Synthetic provider and gateway probes; no live network or bot."""

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from emuru.models import gateway

ROOT = Path(__file__).resolve().parents[1]


def test_startup_requires_primary_tool_call_before_gateway(
    tmp_path, monkeypatch, capsys
):
    route = {
        "schema_version": 1,
        "primary": {"provider": "ollama", "model": "synthetic-cloud"},
        "fallback": {
            "provider": "ollama",
            "model": "local",
            "digest": "a" * 64,
            "context_tokens": 65536,
            "output_reserve_tokens": 4096,
            "qualification": None,
        },
    }
    path = tmp_path / "route.json"
    gateway.write_private(path, json.dumps(route))
    monkeypatch.setenv("EMURU_CONTAINER_ROUTE", str(path))
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-key")
    monkeypatch.setenv("EMURU_GATEWAY_KEY", "synthetic-gateway")
    calls = []
    tool = SimpleNamespace(
        id="call",
        type="function",
        function=SimpleNamespace(name="connection_probe", arguments="{}"),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(tool_calls=[tool], content=None),
            )
        ]
    )

    response_box = [response]

    def router(**kwargs):
        calls.append(("router", kwargs))
        return object()

    async def collect(*args):
        calls.append(("primary", args))
        return response_box[0]

    async def qualified(*args):
        calls.append(("qualification", args))
        return False

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(Router=router))
    monkeypatch.setitem(
        sys.modules,
        "emuru.models.gateway_guard",
        SimpleNamespace(
            guard=SimpleNamespace(collect=collect),
            qualified=qualified,
            valid_response=lambda *args: None,
        ),
    )
    module = runpy.run_path(str(ROOT / "src/emuru/models/startup.py"))

    class GatewayResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {"tool_calls": [{"id": "call"}]},
                        }
                    ]
                }
            ).encode()

    def open_gateway(request, timeout):
        calls.append(("gateway", request))
        assert request.get_header("Authorization") == "Bearer synthetic-gateway"
        assert timeout == 75
        return GatewayResponse()

    monkeypatch.setitem(
        module["check"].__globals__,
        "build_opener",
        lambda handler: SimpleNamespace(open=open_gateway),
    )
    module["check"]()
    assert [name for name, _ in calls] == ["router", "primary", "gateway"]
    assert calls[0][1]["model_list"][0]["litellm_params"]["api_key"] == "synthetic-key"
    assert "LOCAL_UNQUALIFIED" in capsys.readouterr().out

    calls.clear()
    monkeypatch.setitem(
        module["check"].__globals__, "valid_qualification", lambda route: True
    )
    module["check"]()
    assert [name for name, _ in calls] == [
        "router",
        "primary",
        "gateway",
        "qualification",
    ]

    calls.clear()
    response_box[0] = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(tool_calls=[], content="hello"),
            )
        ]
    )
    with pytest.raises(RuntimeError, match="lacks required tool calls"):
        module["check"]()
    assert [name for name, _ in calls] == ["router", "primary"]
