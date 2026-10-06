"""Provider-switch regressions without Hermes, credentials or network requests."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "emuru_vault_profile", REPO / "scripts/hermes-vault-profile.py"
)
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


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
        root_patch = patch.object(profile, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def select(self, provider, model):
        self.write_json("model-selection.json", {"provider": provider, "model": model})

    def write_json(self, name, data):
        (self.root / "infra/hermes" / name).write_text(json.dumps(data))

    def cli(self, command, **kwargs):
        self.assertEqual(command[0], str(self.root / "scripts/hermes-emuru.sh"))
        self.assertEqual(command[1], "config")
        if command[2] == "get":
            key = command[3]
            self.assertEqual(command[4], "--json")
            if key == "model":
                value = {
                    name.removeprefix("model."): value
                    for name, value in self.state.items()
                    if name.startswith("model.")
                }
            else:
                value = self.state[key]
            return subprocess.CompletedProcess(command, 0, json.dumps(value))
        self.assertEqual(command[2], "set")
        forced = command[3] == "--force"
        key, raw = command[4:] if forced else command[3:]
        self.writes.append((key, raw, forced))
        # Hermes parses structured JSON but stores quoted JSON strings literally.
        if (
            raw.startswith(("[", "{"))
            or raw in ("true", "false", "null")
            or raw.isdigit()
        ):
            value = json.loads(raw)
        else:
            value = raw
        if key == "mcp_servers.vault":
            self.state["mcp_servers"]["vault"] = value
        else:
            if key == "model.provider" and value != self.state[key]:
                # Mimic the CLI's clearing of a previous provider's route.
                self.state.pop("model.base_url", None)
                self.state.pop("model.api_mode", None)
            self.state[key] = value
        return subprocess.CompletedProcess(command, 0, "")

    def run_profile(self, *arguments):
        with (
            patch.object(sys, "argv", ["hermes-vault-profile.py", *arguments]),
            patch.object(profile.subprocess, "run", side_effect=self.cli),
            patch.object(profile.shutil, "which", return_value="/mock/bin/uv"),
            patch.object(profile, "check_provider"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            profile.main()
        return output.getvalue()

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
        self.assertIn(str(self.root / "scripts/hermes-vault.py"), server["args"])
