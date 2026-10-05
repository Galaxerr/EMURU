"""Shared vault policy regressions with synthetic Hermes configuration."""

import contextlib
import io
import json
import sys
from unittest.mock import patch

from provider_profile_case import REPO, ProfileCase, profile


class VaultProfileTests(ProfileCase):
    def test_live_audit_does_not_mutate(self):
        self.run_profile("--apply")
        before = list(self.writes)
        self.assertIn("PASS", self.run_profile())
        self.assertEqual(self.writes, before)

    def test_offline_check_does_not_launch_hermes(self):
        with (
            patch.object(sys, "argv", ["hermes-vault-profile.py", "--offline"]),
            patch.object(profile.subprocess, "run") as launch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            profile.main()
        launch.assert_not_called()

    def test_wrong_provider_unknown_fields_and_quoted_model_are_rejected(self):
        for selection in (
            {"provider": "custom", "model": "gpt-6-astra"},
            {"provider": "openai-api", "model": '"gpt-6-astra"'},
            {"provider": "openai-api", "model": "gpt-6-astra", "api_key": "FAKE"},
        ):
            with self.subTest(selection=selection):
                self.write_json("model-selection.json", selection)
                with self.assertRaises(SystemExit):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])

    def test_extra_server_is_rejected_before_any_changes(self):
        self.state["mcp_servers"]["unexpected"] = {}
        with self.assertRaisesRegex(SystemExit, "Unexpected MCP server"):
            self.run_profile("--apply")
        self.assertEqual(self.writes, [])

    def test_inline_key_is_rejected_without_printing_it(self):
        self.state["model.api_key"] = "SYNTHETIC_SECRET"
        with self.assertRaisesRegex(SystemExit, "Clear inline") as error:
            self.run_profile("--apply")
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.assertEqual(self.writes, [])

    def test_increased_retries_and_broader_tools_are_rejected(self):
        original = json.loads((REPO / "infra/hermes/runtime-settings.json").read_text())
        for key, value in (
            ("agent.api_max_retries", 1),
            ("agent.auto_recovery_cycles", 1),
            ("platform_toolsets.cli", ["mcp-vault", "terminal"]),
            ("fallback_providers", [{"provider": "gemini"}]),
        ):
            with self.subTest(key=key):
                self.write_json("runtime-settings.json", {**original, key: value})
                with self.assertRaises(SystemExit):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])
