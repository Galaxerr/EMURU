"""Legacy inventory selection cannot bypass agent gateway inference."""

from unittest.mock import patch

from provider_profile_case import ProfileCase


class OllamaProfileTests(ProfileCase):
    def test_all_legacy_routes_stay_gateway_only_without_daemon_preflight(self):
        with patch(
            "emuru.models.ollama.urlopen", side_effect=RuntimeError("daemon stopped")
        ) as http:
            for model in ("qwen3:4b", "gpt-oss:20b-cloud"):
                self.select("ollama", model)
                self.run_profile("--apply")
                self.assertEqual(self.state["model.provider"], "custom:emuru")
                self.assertEqual(self.state["model.default"], "emuru")
                self.assertEqual(
                    self.state["providers.emuru"]["key_env"], "EMURU_GATEWAY_KEY"
                )
                self.assert_vault_only()
            http.assert_not_called()
