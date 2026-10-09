"""Provider-switch regressions without Hermes, credentials or network requests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuru.hermes import profile

REPO = Path(__file__).resolve().parents[1]


class ProfileCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "infra/hermes").mkdir(parents=True)
        (self.root / ".runtime/vault").mkdir(parents=True)
        for name in (
            "runtime-settings.json",
            "model-selection.json",
            "telegram-settings.json",
        ):
            source = REPO / "infra/hermes" / name
            (self.root / "infra/hermes" / name).write_bytes(source.read_bytes())
        # Regression scenarios are independent of the owner's active selection.
        self.select("openai-api", "gpt-6-astra")
        self.state = {
            "mcp_servers": {},
            "model.provider": "gemini",
            "model.default": "gemini-3.8-flash",
            "model.base_url": "",
            "model.api_mode": "chat_completions",
        }
        self.writes = []

    def select(self, provider, model):
        self.write_json("model-selection.json", {"provider": provider, "model": model})

    def write_json(self, name, data):
        (self.root / "infra/hermes" / name).write_text(json.dumps(data))

    def get(self, key):
        if key == "model":
            return {
                name.removeprefix("model."): value
                for name, value in self.state.items()
                if name.startswith("model.")
            }
        return self.state[key]

    def set_value(self, key, value, force=False):
        self.writes.append((key, value, force))
        if key == "mcp_servers.vault":
            self.state["mcp_servers"]["vault"] = value
        else:
            if key == "model.provider" and value != self.state[key]:
                # Native CLI clears the previous route when changing providers.
                self.state.pop("model.base_url", None)
                self.state.pop("model.api_mode", None)
            self.state[key] = value

    def run_profile(self, *arguments):
        with (
            patch.object(profile.shutil, "which", return_value="/mock/bin/uv"),
        ):
            if "--offline" in arguments:
                return profile.expected_settings(self.root)
            return profile.configure_profile(
                self.root, self.get, self.set_value if "--apply" in arguments else None
            )

    def assert_vault_only(self):
        self.assertEqual(set(self.state["mcp_servers"]), {"vault"})
        server = self.state["mcp_servers"]["vault"]
        self.assertEqual(server["tools"]["include"], profile.TOOLS)
        self.assertFalse(server["sampling"]["enabled"])
        self.assertFalse(server["elicitation"]["enabled"])
        self.assertEqual(server["timeout"], 15)
        self.assertEqual(server["connect_timeout"], 15)
        self.assertEqual(self.state["platform_toolsets.cli"], ["mcp-vault"])
        self.assertEqual(self.state["platform_toolsets.telegram"], ["mcp-vault"])
        self.assertEqual(self.state["tools.tool_search.enabled"], "off")
        self.assertIs(
            self.state["platforms.telegram.extra.drop_pending_on_cold_boot"], False
        )
        self.assertEqual(
            self.state["platforms.telegram.extra.max_concurrent_updates"], 1
        )
        self.assertEqual(self.state["agent.api_max_retries"], 0)
        self.assertEqual(self.state["agent.auto_recovery_cycles"], 0)
        self.assertEqual(self.state["fallback_providers"], [])
        self.assertEqual(
            [key for key, _, forced in self.writes if forced],
            ["mcp_servers.vault"]
            * sum(key == "model.provider" for key, _, _ in self.writes),
        )
        self.assertNotIn("mcp_servers", [key for key, _, _ in self.writes])
        self.assertIn(str(self.root / "scripts/hermes/vault.py"), server["args"])
