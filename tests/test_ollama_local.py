"""Downloaded Ollama models use a local route and explicit model selection."""

import io
import json
from unittest.mock import patch

import pytest
from provider_profile_case import ProfileCase

from emuru.models.ollama import OllamaConnection
from emuru.models.providers import provider_settings


@pytest.mark.parametrize("model", ["gemma4:e4b", "qwen3:4b"])
def test_downloaded_model_route(model):
    settings = provider_settings({"provider": "ollama", "model": model})
    assert settings["model.default"] == model
    assert settings["model.base_url"] == "http://127.0.0.1:11434/v1"
    assert settings["providers.ollama.key_env"] == ""
    assert settings["model.ollama_num_ctx"] == 65536
    assert settings["providers.ollama.request_timeout_seconds"] == 180


def test_custom_named_cloud_alias_is_detected_from_metadata():
    replies = [
        {"models": [{"name": "gemma4:e4b", "remote_host": "https://ollama.com"}]},
        {"capabilities": ["tools"]},
    ]
    with patch(
        "emuru.models.ollama.urlopen",
        side_effect=[io.BytesIO(json.dumps(reply).encode()) for reply in replies],
    ):
        connection = OllamaConnection("gemma4:e4b")
        connection.check()
        assert connection.cloud


class DownloadedModelProfileTests(ProfileCase):
    def test_explicit_switch_between_gemma_cloud_and_downloaded_model(self):
        self.select("ollama", "gemma4:31b-cloud")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.ollama_num_ctx"], 0)
        self.assert_vault_only()
        self.select("ollama", "gemma4:e4b")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.default"], "gemma4:e4b")
        self.assertEqual(self.state["model.ollama_num_ctx"], 65536)
        self.assert_vault_only()
        self.select("ollama", "gemma4:31b-cloud")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.ollama_num_ctx"], 0)
        self.assert_vault_only()
