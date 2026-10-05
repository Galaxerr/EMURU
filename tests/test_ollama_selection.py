"""Discover and choose all locally registered models without a model allowlist."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from emuru.ollama import OllamaClient
from emuru.providers import check_provider, provider_settings

spec = importlib.util.spec_from_file_location(
    "ollama_cli", Path(__file__).resolve().parents[1] / "scripts/hermes-ollama.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
CATALOG = [
    {"name": "my-custom-model:v7"},
    {"name": "a-cloud-alias", "remote_host": "https://ollama.com"},
    {"name": "embedding-only:latest"},
]


def test_catalog_returns_all_models_including_custom_and_non_chat_models():
    with patch(
        "emuru.ollama.urlopen",
        return_value=io.BytesIO(json.dumps({"models": CATALOG}).encode()),
    ) as http:
        assert OllamaClient().list_models() == CATALOG
    assert http.call_args.args[0].full_url == "http://127.0.0.1:11434/api/tags"


@pytest.mark.parametrize(
    "catalog", [{}, {"models": None}, {"models": [None]}, {"models": [{"name": 2}]}]
)
def test_invalid_catalog_is_actionable(catalog):
    with (
        patch(
            "emuru.ollama.urlopen",
            return_value=io.BytesIO(json.dumps(catalog).encode()),
        ),
        pytest.raises(RuntimeError, match="model catalog"),
    ):
        OllamaClient().list_models()


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("2", "a-cloud-alias"),
        ("my-custom-model:v7", "my-custom-model:v7"),
        ("3", "embedding-only:latest"),
    ],
)
def test_interactive_selection_uses_discovered_models(answer, expected):
    with patch("builtins.input", return_value=answer):
        assert cli.choose_model(CATALOG) == expected


@pytest.mark.parametrize("answer", ["", "99", "not-installed"])
def test_invalid_or_cancelled_selection(answer):
    with patch("builtins.input", return_value=answer), pytest.raises(ValueError):
        cli.choose_model(CATALOG)


def test_empty_catalog_does_not_prompt():
    with (
        patch("builtins.input") as prompt,
        pytest.raises(ValueError, match="No Ollama models"),
    ):
        cli.choose_model([])
    prompt.assert_not_called()


@pytest.mark.parametrize("choice", ["my-custom-model:v7", "embedding-only:latest"])
def test_cli_persists_any_discovered_model(tmp_path, choice):
    path = tmp_path / "infra/hermes/model-selection.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider": "gemini", "model": "gemini-test"}))
    with (
        patch.object(cli, "ROOT", tmp_path),
        patch.object(sys, "argv", ["hermes-ollama.py", "--select", choice]),
        patch.object(OllamaClient, "list_models", return_value=CATALOG),
    ):
        cli.main()
    assert json.loads(path.read_text()) == {
        "provider": "ollama",
        "model": choice,
        "base_url": "http://127.0.0.1:11434/v1",
    }


def test_cli_rejects_missing_model_without_changing_selection(tmp_path):
    path = tmp_path / "infra/hermes/model-selection.json"
    path.parent.mkdir(parents=True)
    before = '{"provider":"ollama","model":"my-custom-model:v7"}'
    path.write_text(before)
    with (
        patch.object(cli, "ROOT", tmp_path),
        patch.object(sys, "argv", ["hermes-ollama.py", "--select", "missing"]),
        patch.object(OllamaClient, "list_models", return_value=CATALOG),
        pytest.raises(SystemExit, match="not available"),
    ):
        cli.main()
    assert path.read_text() == before


def test_custom_cloud_alias_preflight_updates_context_and_timeouts():
    settings = provider_settings({"provider": "ollama", "model": "a-cloud-alias"})
    replies = [{"models": CATALOG}, {"capabilities": ["tools"]}]
    with patch(
        "emuru.ollama.urlopen",
        side_effect=[io.BytesIO(json.dumps(reply).encode()) for reply in replies],
    ):
        check_provider(settings)
    assert settings["model.ollama_num_ctx"] == 0
    assert settings["providers.ollama.request_timeout_seconds"] == 60
