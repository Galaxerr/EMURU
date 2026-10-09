"""Provider-specific regressions with synthetic Hermes configuration."""

import pytest
from provider_profile_case import ProfileCase

from emuru.models.providers import provider_settings


class GeminiProfileTests(ProfileCase):
    def test_switch_back_to_gemini_clears_openai_route_and_keeps_permissions(self):
        self.run_profile("--apply")
        self.select("gemini", "gemini-3.8-flash")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.provider"], "custom:emuru")
        self.assertEqual(self.state["model.default"], "emuru")
        self.assertEqual(self.state["model.api_mode"], "chat_completions")
        self.assert_vault_only()


def test_gemini_rejects_another_providers_model():
    with pytest.raises(ValueError, match="Gemini"):
        provider_settings({"provider": "gemini", "model": "gpt-6-astra"})


def test_gemini_does_not_accept_an_endpoint_override():
    with pytest.raises(ValueError, match="only configurable for Ollama"):
        provider_settings(
            {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "base_url": "http://localhost:11434",
            }
        )
