"""Provider-specific regressions with synthetic Hermes configuration."""

import pytest
from provider_profile_case import ProfileCase

from emuru.models.providers import provider_settings


class OpenAIProfileTests(ProfileCase):
    def test_openai_switch_uses_raw_strings_and_restores_route_after_provider_change(
        self,
    ):
        self.run_profile("--apply")
        self.assertEqual(self.state["model.provider"], "openai-api")
        self.assertEqual(self.state["model.default"], "gpt-6-astra")
        self.assertEqual(self.state["model.base_url"], "https://api.openai.com/v1")
        self.assertEqual(self.state["model.api_mode"], "codex_responses")
        self.assert_vault_only()


def test_openai_rejects_another_providers_model():
    with pytest.raises(ValueError, match="OpenAI"):
        provider_settings({"provider": "openai-api", "model": "gemini-3.8-flash"})


def test_openai_does_not_accept_an_endpoint_override():
    with pytest.raises(ValueError, match="only configurable for Ollama"):
        provider_settings(
            {
                "provider": "openai-api",
                "model": "gpt-6-astra",
                "base_url": "http://localhost:11434",
            }
        )
