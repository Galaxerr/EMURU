"""Ollama protocol regressions; live calls are explicitly opt-in."""

import io
import json
import os
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from emuru.ollama import DEFAULT_BASE_URL, OllamaConnection, ollama_base_url


def response(value):
    return io.BytesIO(json.dumps(value).encode())


@pytest.mark.parametrize("thinking", [False, True])
def test_chat_tool_round_trip_uses_local_endpoint_without_credentials(thinking):
    connection = OllamaConnection("qwen2.5-coder:3b")
    tool = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "connection_probe",
                    "arguments": '{"message":"ping"}',
                },
            }
        ],
    }
    replies = [
        {"models": [{"name": connection.model}]},
        {"capabilities": ["completion", "tools", *(["thinking"] if thinking else [])]},
        {"choices": [{"message": tool}]},
        {"choices": [{"message": {"content": "EMURU_OLLAMA_OK"}}]},
    ]
    with patch(
        "emuru.ollama.urlopen", side_effect=[response(x) for x in replies]
    ) as http:
        assert connection.smoke() == "EMURU_OLLAMA_OK"
    requests = [call.args[0] for call in http.call_args_list]
    assert [r.full_url for r in requests] == [
        connection.root + path
        for path in (
            "/api/tags",
            "/api/show",
            "/v1/chat/completions",
            "/v1/chat/completions",
        )
    ]
    assert all("Authorization" not in r.headers for r in requests)
    body = json.loads(requests[-1].data)
    assert body["model"] == connection.model
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 65536}
    assert ("reasoning_effort" in body) is thinking
    assert body["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "EMURU_OLLAMA_OK",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://ollama.com.attacker/v1",
        "http://example.com/v1",
        "http://localhost.attacker/v1",
        "http://user:secret@localhost:11434/v1",
        "http://localhost:11434/v1?key=secret",
        "http://localhost:11434/api",
        "http://localhost:bad/v1",
        "http://localhost:0",
    ],
)
def test_nonlocal_or_malformed_url_is_rejected(url):
    with pytest.raises(ValueError):
        ollama_base_url(url)


def test_ipv6_loopback_url():
    assert ollama_base_url("http://[::1]:11434/") == "http://[::1]:11434/v1"


def test_cloud_proxy_uses_remote_manifest_without_downloading_weights():
    connection = OllamaConnection("gpt-oss:20b-cloud")
    replies = [
        {
            "models": [
                {
                    "name": connection.model,
                    "remote_host": "https://ollama.com",
                    "remote_model": "gpt-oss:20b",
                    "size": 306,
                }
            ]
        },
        {"capabilities": ["completion", "tools", "thinking"]},
    ]
    with patch(
        "emuru.ollama.urlopen", side_effect=[response(x) for x in replies]
    ) as http:
        assert "tools" in connection.check()["capabilities"]
    assert [call.args[0].full_url for call in http.call_args_list] == [
        connection.root + "/api/tags",
        connection.root + "/api/show",
    ]
    assert connection.cloud


@pytest.mark.parametrize("catalog_name", ["custom", "custom:latest"])
def test_preflight_accepts_exact_name_and_latest_alias(catalog_name):
    replies = [
        {"models": [{"name": catalog_name, "remote_host": "https://ollama.com"}]},
        {"capabilities": ["tools"]},
    ]
    with patch(
        "emuru.ollama.urlopen", side_effect=[response(x) for x in replies]
    ) as http:
        connection = OllamaConnection("custom")
        connection.check()
    assert connection.cloud
    assert json.loads(http.call_args_list[1].args[0].data) == {"model": "custom"}


def test_model_suffix_does_not_override_actual_downloaded_route():
    replies = [{"models": [{"name": "custom-cloud"}]}, {"capabilities": ["tools"]}]
    with patch("emuru.ollama.urlopen", side_effect=[response(x) for x in replies]):
        connection = OllamaConnection("custom-cloud")
        connection.check()
        assert not connection.cloud


def test_direct_cloud_authorization_and_no_local_key_leak():
    with (
        patch.dict(os.environ, {"OLLAMA_API_KEY": "SYNTHETIC_TEST_KEY"}),
        patch(
            "emuru.ollama.urlopen",
            side_effect=[response({"models": [{"name": "gpt-oss:20b"}]}), response({})],
        ) as http,
    ):
        assert OllamaConnection("gpt-oss:20b", "https://ollama.com/v1").check() == {}
        OllamaConnection("qwen3:4b").request("/api/tags")
    assert (
        http.call_args_list[0].args[0].headers["Authorization"]
        == "Bearer SYNTHETIC_TEST_KEY"
    )
    assert "Authorization" not in http.call_args_list[1].args[0].headers


def test_direct_cloud_missing_key_does_not_send_request():
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("emuru.ollama.urlopen") as http,
        pytest.raises(RuntimeError, match="OLLAMA_API_KEY"),
    ):
        OllamaConnection("gpt-oss:20b", "https://ollama.com/v1").check()
    http.assert_not_called()


@pytest.mark.parametrize(
    ("status", "message"), [(401, "authentication"), (429, "quota/rate")]
)
def test_cloud_auth_and_quota_failures(status, message):
    with (
        patch(
            "emuru.ollama.urlopen",
            side_effect=HTTPError("url", status, "error", {}, None),
        ),
        pytest.raises(RuntimeError, match=message),
    ):
        OllamaConnection("gpt-oss:20b-cloud").request("/v1/chat/completions", {})


@pytest.mark.parametrize(
    ("replies", "message"),
    [
        ([{"models": []}], "not installed"),
        (
            [{"models": [{"name": "qwen3:4b"}]}, {"capabilities": ["completion"]}],
            "lacks tool",
        ),
    ],
)
def test_model_preflight_errors(replies, message):
    with (
        patch("emuru.ollama.urlopen", side_effect=[response(x) for x in replies]),
        pytest.raises(RuntimeError, match=message),
    ):
        OllamaConnection("qwen3:4b").check()


@pytest.mark.parametrize(
    "error",
    [URLError("offline"), TimeoutError(), HTTPError("url", 500, "error", {}, None)],
)
def test_connection_errors_are_actionable(error):
    with (
        patch("emuru.ollama.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match="Ollama"),
    ):
        OllamaConnection("qwen3:4b").check()


def test_plain_chat_instead_of_tool_call_fails_smoke():
    with (
        patch("emuru.ollama.OllamaConnection.check"),
        patch(
            "emuru.ollama.urlopen",
            return_value=response({"choices": [{"message": {"content": "hello"}}]}),
        ),
        pytest.raises(RuntimeError, match="round trip failed"),
    ):
        OllamaConnection("qwen3:4b").smoke()


@pytest.mark.skipif(
    os.environ.get("EMURU_TEST_OLLAMA") != "1",
    reason="Set EMURU_TEST_OLLAMA=1 for live Ollama inference",
)
def test_live_ollama_chat_and_tools():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    selection = json.loads((root / "infra/hermes/model-selection.json").read_text())
    assert selection["provider"] == "ollama"
    connection = OllamaConnection(
        selection["model"], selection.get("base_url", DEFAULT_BASE_URL)
    )
    assert "EMURU_OLLAMA_OK" in connection.smoke()
