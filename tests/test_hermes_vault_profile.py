"""Shared vault policy regressions with synthetic Hermes configuration."""

import json
from copy import deepcopy
from unittest.mock import patch

from provider_profile_case import REPO, ProfileCase, profile


class VaultProfileTests(ProfileCase):
    def test_live_audit_does_not_mutate(self):
        self.run_profile("--apply")
        before = list(self.writes)
        self.assertEqual(self.run_profile(), self.state["mcp_servers"]["vault"])
        self.assertEqual(self.writes, before)

    def test_offline_check_does_not_preflight(self):
        with patch("emuru.models.ollama.urlopen") as preflight:
            profile.expected_settings(self.root)
        preflight.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_legacy_selection_never_changes_agent_route(self):
        self.write_json(
            "model-selection.json", {"provider": "custom", "model": "ignored"}
        )
        self.run_profile("--apply")
        self.assertEqual(self.state["model.provider"], "custom:emuru")
        self.assertEqual(self.state["model.default"], "emuru")

    def test_extra_server_is_rejected_before_any_changes(self):
        self.state["mcp_servers"]["unexpected"] = {}
        with self.assertRaisesRegex(profile.ProfileError, "Unexpected MCP server"):
            self.run_profile("--apply")
        self.assertEqual(self.writes, [])

    def test_inline_key_is_rejected_without_printing_it(self):
        self.state["model.api_key"] = "SYNTHETIC_SECRET"
        with self.assertRaisesRegex(profile.ProfileError, "Clear inline") as error:
            self.run_profile("--apply")
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.assertEqual(self.writes, [])

    def test_increased_retries_and_broader_tools_are_rejected(self):
        original = json.loads((REPO / "infra/hermes/runtime-settings.json").read_text())
        for key, value in (
            ("agent.api_max_retries", 1),
            ("agent.auto_recovery_cycles", 1),
            ("platform_toolsets.cli", ["mcp-vault", "terminal"]),
            ("tools.tool_search.enabled", "on"),
            ("fallback_providers", [{"provider": "gemini"}]),
            ("auxiliary.compression", {"provider": "gemini", "model": "other"}),
            ("mcp_servers.other", {}),
        ):
            with self.subTest(key=key):
                self.write_json("runtime-settings.json", {**original, key: value})
                with self.assertRaises(profile.ProfileError):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])

    def test_all_generic_toolsets_must_stay_disabled(self):
        original = json.loads((REPO / "infra/hermes/runtime-settings.json").read_text())
        for name in profile.DISABLED_TOOLSETS:
            with self.subTest(toolset=name):
                settings = {
                    **original,
                    "agent.disabled_toolsets": [
                        item
                        for item in original["agent.disabled_toolsets"]
                        if item != name
                    ],
                }
                self.write_json("runtime-settings.json", settings)
                with self.assertRaises(profile.ProfileError):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])

    def test_broader_live_mcp_registration_fails_audit(self):
        self.run_profile("--apply")
        server = self.state["mcp_servers"]["vault"]
        original = json.loads(json.dumps(server))
        for key, value in (
            (
                "tools",
                {
                    "include": profile.TOOLS + ["terminal"],
                    "resources": False,
                    "prompts": False,
                },
            ),
            ("sampling", {"enabled": True}),
            ("elicitation", {"enabled": True}),
            ("timeout", 30),
            ("connect_timeout", 30),
        ):
            with self.subTest(key=key):
                self.state["mcp_servers"]["vault"] = {**original, key: value}
                with self.assertRaisesRegex(
                    profile.ProfileError, "MCP registration mismatch"
                ):
                    self.run_profile()


class NativeProfileTests(ProfileCase):
    def nested_config(self):
        # Native Hermes reads a nested mapping; CLI transport reads dotted keys.
        actual = {}
        for key, value in self.state.items():
            cursor = actual
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
        return json.loads(json.dumps(actual))

    def audit_native(self, actual):
        before = deepcopy(actual)
        with (
            patch.object(profile.shutil, "which", return_value="/mock/bin/uv"),
            patch("emuru.models.ollama.urlopen") as http,
            patch.object(profile.subprocess, "run") as transport,
        ):
            server = profile.audit_native_profile(self.root, actual)
        http.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(actual, before)
        return server

    def test_native_audit_all_provider_routes_without_mutation_or_network(self):
        for provider, model in (
            ("gemini", "gemini-3.8-flash"),
            ("openai-api", "gpt-6-astra"),
            ("ollama", "qwen3:4b"),
            ("ollama", "gpt-oss:20b-cloud"),
        ):
            with self.subTest(provider=provider, model=model):
                self.select(provider, model)
                self.run_profile("--apply")
                actual = self.nested_config()
                before = list(self.writes)
                self.assertEqual(
                    self.audit_native(actual), actual["mcp_servers"]["vault"]
                )
                self.assertEqual(self.writes, before)

    def test_native_route_and_timeout_overrides_fail_closed(self):
        self.run_profile("--apply")
        for key, value in (
            ("base_url", "https://ollama.com/v1"),
            ("provider", "ollama"),
            ("default", "other"),
            ("ollama_num_ctx", 65536),
        ):
            actual = self.nested_config()
            actual["model"][key] = value
            with self.assertRaises(profile.ProfileError):
                self.audit_native(actual)
        actual = self.nested_config()
        actual["providers"]["emuru"]["request_timeout_seconds"] = 180
        with self.assertRaisesRegex(profile.ProfileError, "providers.emuru"):
            self.audit_native(actual)

    def test_native_compression_overrides_fail_closed(self):
        self.run_profile("--apply")
        for override in (
            {"provider": "gemini"},
            {"model": "other"},
            {"base_url": "https://other/v1"},
            {"api_key": "SYNTHETIC_SECRET"},
        ):
            actual = self.nested_config()
            actual["auxiliary"]["compression"].update(override)
            with self.assertRaisesRegex(
                profile.ProfileError, "auxiliary.compression"
            ) as error:
                self.audit_native(actual)
            self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))

    def test_native_compression_accepts_pinned_hermes_defaults(self):
        self.run_profile("--apply")
        actual = self.nested_config()
        actual["auxiliary"]["compression"] = {
            "provider": "custom:emuru",
            "model": "emuru",
            "base_url": "",
            "api_key": "",
            "timeout": 120,
            "extra_body": {},
            "reasoning_effort": "",
            "no_progress_timeout": None,
        }
        self.audit_native(actual)

    def test_native_secrets_types_and_registration_fail_closed(self):
        self.run_profile("--apply")
        for model in (
            {"api_key": "SYNTHETIC_SECRET"},
            {"key_env": "SYNTHETIC_SECRET"},
            None,
            "SYNTHETIC_SECRET",
            [],
        ):
            with self.subTest(model=model):
                actual = self.nested_config()
                actual["model"] = model
                with self.assertRaises(profile.ProfileError) as error:
                    self.audit_native(actual)
                self.assertEqual(error.exception.code, "inline_model_credentials")
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        actual = self.nested_config()
        actual["agent"]["api_max_retries"] = False
        with self.assertRaisesRegex(profile.ProfileError, "api_max_retries"):
            self.audit_native(actual)
        actual = self.nested_config()
        actual["mcp_servers"]["unexpected"] = {"token": "SYNTHETIC_SECRET"}
        with self.assertRaises(profile.ProfileError) as error:
            self.audit_native(actual)
        self.assertEqual(error.exception.code, "profile_mcp_registration_mismatch")
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
