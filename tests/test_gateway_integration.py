"""Private route contract and optional pinned-proxy integration, no cloud calls."""

import copy
import json
import os
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from emuru.hermes import profile
from emuru.models import gateway
from emuru.models.ollama import OllamaClient

ROOT = Path(__file__).resolve().parents[1]
ROUTE = {
    "schema_version": 1,
    "primary": {"provider": "ollama", "model": "upstream"},
    "fallback": {
        "provider": "ollama",
        "model": "local",
        "digest": "a" * 64,
        "context_tokens": 65536,
        "output_reserve_tokens": 4096,
        "qualification": None,
    },
}
CATALOG = [
    {"name": "cloud-alias", "digest": "b" * 64, "size": 100},
    {"name": "local", "digest": "a" * 64, "size": 100},
]


def responses(catalog=None, cloud=None, local=None):
    def request(self, path, body=None, timeout=60):
        assert path in {"/api/tags", "/api/show"}  # no pull or inference
        if path == "/api/tags":
            return {"models": CATALOG if catalog is None else catalog}
        return (
            (
                {
                    "capabilities": ["tools"],
                    "remote_model": "upstream",
                    "remote_host": "https://ollama.com",
                }
                if cloud is None
                else cloud
            )
            if body["model"] == "cloud-alias"
            else (
                {
                    "capabilities": ["tools"],
                    "model_info": {"general.architecture": "test"},
                }
                if local is None
                else local
            )
        )

    return request


def test_valid_pair_and_primary_only_render():
    with patch.object(OllamaClient, "request", responses()):
        assert (
            gateway.select_pair("cloud-alias", "local", "http://localhost:11434")
            == ROUTE
        )
    config = yaml.safe_load(gateway.render(ROUTE))
    assert config["model_list"][0]["litellm_params"]["model"] == "emuru_guard/emuru"
    assert config["litellm_settings"]["drop_params"] is True
    assert gateway.upstream(ROUTE["primary"])["api_base"] == "https://ollama.com"
    assert "local" not in gateway.render(ROUTE)
    assert config["router_settings"]["fallbacks"] == []
    assert gateway.render(ROUTE) == gateway.render(copy.deepcopy(ROUTE))


@pytest.mark.parametrize(
    "changes",
    [
        {"cloud": {"capabilities": ["tools"]}},
        {"local": {"capabilities": ["tools"], "remote_model": "deceptive"}},
        {"catalog": CATALOG[:1]},
        {"local": {"capabilities": ["tools"]}},
        {"local": {"capabilities": [], "model_info": {"x": 1}}},
        {"cloud": {"remote_host": "https://ollama.com"}},
        {"catalog": [CATALOG[0], dict(CATALOG[1], size=0)]},
        {"catalog": [dict(CATALOG[0], remote_model="inconsistent"), CATALOG[1]]},
    ],
)
def test_failed_pair_preserves_selection(tmp_path, changes):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    gateway.write_private(path, json.dumps(ROUTE))
    before = path.read_bytes()
    with (
        patch.object(OllamaClient, "request", responses(**changes)),
        pytest.raises((ValueError, RuntimeError)),
    ):
        route = gateway.select_pair("cloud-alias", "local", "http://localhost:11434")
        gateway.write_private(path, json.dumps(route))
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_remote_model_preserves_selection(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    gateway.write_private(path, json.dumps(ROUTE))
    before = path.read_bytes()
    with (
        patch.object(
            OllamaClient,
            "request",
            responses(cloud={"capabilities": ["tools"], "remote_model": "../bad"}),
        ),
        pytest.raises(ValueError, match="model ID"),
    ):
        route = gateway.select_pair("cloud-alias", "local", "http://localhost:11434")
        gateway.write_private(path, json.dumps(route))
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("qualification", "invalid"),
        ("digest", "bad"),
        ("context_tokens", True),
        ("output_reserve_tokens", 65536),
        ("model", "../bad"),
        ("provider", "openai-api"),
    ],
)
def test_invalid_candidate(field, value):
    route = copy.deepcopy(ROUTE)
    route["fallback"][field] = value
    with pytest.raises(ValueError):
        gateway.validate(route)


@pytest.mark.parametrize(
    "route",
    [
        {},
        dict(ROUTE, schema_version=True),
        dict(ROUTE, unexpected=1),
        dict(ROUTE, primary={}),
    ],
)
def test_invalid_shape(route):
    with pytest.raises(ValueError):
        gateway.validate(route)


def test_profile_opt_in_and_secret_free_mcp(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    gateway.write_private(path, json.dumps(ROUTE))
    monkeypatch.setenv("EMURU_CONTAINER_ROUTE", str(path))
    settings = profile.expected_settings(ROOT)
    assert settings["model.provider"] == "custom:emuru"
    assert settings["fallback_providers"] == []
    assert settings["agent.api_max_retries"] == 0
    assert settings["agent.max_turns"] == 12
    monkeypatch.setenv("EMURU_GATEWAY_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-upstream")
    backend = runpy.run_path(str(ROOT / "scripts/hermes/vault.py"))["backend"]
    _, environment = backend(tmp_path, "emuru-vault-mcp")
    assert (
        not {"EMURU_GATEWAY_KEY", "OPENAI_API_KEY", "EMURU_CONTAINER_ROUTE"}
        & environment.keys()
    )
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        gateway.read_route(path)
    monkeypatch.delenv("EMURU_CONTAINER_ROUTE")
    assert profile.expected_settings(ROOT)["model.provider"] == "custom:emuru"


@pytest.mark.parametrize(
    "provider,model,prefix,key",
    [
        ("gemini", "gemini-synthetic", "gemini", "GEMINI_API_KEY"),
        ("openai-api", "gpt-synthetic", "openai", "OPENAI_API_KEY"),
    ],
)
def test_optional_upstream(provider, model, prefix, key):
    route = copy.deepcopy(ROUTE)
    route["primary"] = {"provider": provider, "model": model}
    params = gateway.upstream(route["primary"])
    assert params["model"] == prefix + "/" + model
    assert params["api_key"] == "os.environ/" + key


def test_service_url_requires_explicit_container_opt_in(monkeypatch):
    monkeypatch.delenv("EMURU_CONTAINER_ROUTE", raising=False)
    with pytest.raises(ValueError):
        OllamaClient("http://ollama:11434/v1")
    monkeypatch.setenv("EMURU_CONTAINER_ROUTE", "/private/route.json")
    assert OllamaClient("http://ollama:11434/v1").root == "http://ollama:11434"
    with pytest.raises(ValueError):
        OllamaClient("http://arbitrary:11434/v1")


@pytest.mark.skipif(
    not os.environ.get("EMURU_TEST_CONTAINERS"),
    reason="explicit disposable Docker verification required",
)
def test_pinned_container_boundaries():
    import subprocess

    subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "--no-sync",
            "python",
            "scripts/hermes/container-check.py",
        ],
        check=True,
        cwd=ROOT,
    )


def test_empty_infrastructure_cannot_route():
    config = yaml.safe_load(gateway.render())
    assert config["model_list"] == []
    assert config["general_settings"]["master_key"] == "os.environ/EMURU_GATEWAY_KEY"


def test_route_file_refuses_symlink(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    gateway.write_private(path, json.dumps(ROUTE))
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        gateway.read_route(link)


def test_same_picker_pair_cli(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    main = runpy.run_path(str(ROOT / "scripts/hermes/ollama.py"))["main"]
    monkeypatch.setattr(
        "sys.argv",
        [
            "ollama.py",
            "--route",
            str(path),
            "--primary",
            "cloud-alias",
            "--local-candidate",
            "local",
        ],
    )
    with patch.object(OllamaClient, "request", responses()):
        main()
    assert gateway.read_route(path) == ROUTE
    before = path.read_bytes()
    with (
        patch.object(OllamaClient, "request", responses(local={"capabilities": []})),
        pytest.raises(SystemExit),
    ):
        main()
    assert path.read_bytes() == before
