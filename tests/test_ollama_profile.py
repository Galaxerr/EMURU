"""Provider-specific regressions with synthetic Hermes configuration."""

import sys
from unittest.mock import patch

from provider_profile_case import ProfileCase, profile


class OllamaProfileTests(ProfileCase):
    def test_ollama_switch_and_return_to_paid_provider(self):
        self.run_profile("--apply")
        self.select("ollama", "qwen2.5-coder:3b")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.provider"], "ollama")
        self.assertEqual(self.state["model.base_url"], "http://127.0.0.1:11434/v1")
        self.assertEqual(
            self.state["providers.ollama.base_url"], self.state["model.base_url"]
        )
        self.assertEqual(self.state["model.api_mode"], "chat_completions")
        self.assertEqual(self.state["model.ollama_num_ctx"], 65536)
        self.assertIs(self.state["agent.reasoning_effort"], False)
        self.assert_vault_only()
        self.select("openai-api", "gpt-6-astra")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.base_url"], "https://api.openai.com/v1")
        self.assertEqual(self.state["model.api_mode"], "codex_responses")
        self.assertEqual(self.state["model.ollama_num_ctx"], 0)
        self.assert_vault_only()

    def test_ollama_url_normalization(self):
        self.write_json(
            "model-selection.json",
            {
                "provider": "ollama",
                "model": "qwen3:4b",
                "base_url": "http://localhost:11435/",
            },
        )
        self.run_profile("--apply")
        self.assertEqual(self.state["model.base_url"], "http://localhost:11435/v1")

    def test_ollama_cloud_routes_preserve_tools_without_local_context_override(self):
        self.select("ollama", "gpt-oss:20b-cloud")
        self.run_profile("--apply")
        self.assertEqual(self.state["model.ollama_num_ctx"], 0)
        self.assertEqual(self.state["providers.ollama.key_env"], "")
        self.assert_vault_only()
        self.write_json(
            "model-selection.json",
            {
                "provider": "ollama",
                "model": "gpt-oss:20b",
                "base_url": "https://ollama.com/v1",
            },
        )
        self.run_profile("--apply")
        self.assertEqual(self.state["model.base_url"], "https://ollama.com/v1")
        self.assertEqual(self.state["providers.ollama.key_env"], "OLLAMA_API_KEY")
        self.assertEqual(self.state["model.ollama_num_ctx"], 0)
        self.assert_vault_only()

    def test_ollama_invalid_endpoint_rejected_before_changes(self):
        for selection in (
            {
                "provider": "ollama",
                "model": "qwen3:4b",
                "base_url": "https://ollama.com.attacker/v1",
            },
            {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "base_url": "http://localhost:11434",
            },
        ):
            self.write_json("model-selection.json", selection)
            with self.assertRaises(SystemExit):
                self.run_profile("--apply")
            self.assertEqual(self.writes, [])

    def test_unavailable_ollama_does_not_change_live_profile(self):
        self.select("ollama", "qwen3:4b")
        with (
            patch.object(sys, "argv", ["profile", "--apply"]),
            patch.object(profile.subprocess, "run", side_effect=self.cli),
            patch.object(profile.shutil, "which", return_value="/mock/bin/uv"),
            patch.object(
                profile, "check_provider", side_effect=RuntimeError("offline")
            ),
            self.assertRaisesRegex(SystemExit, "offline"),
        ):
            profile.main()
        self.assertEqual(self.writes, [])
