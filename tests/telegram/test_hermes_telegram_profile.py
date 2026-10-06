"""Telegram-specific profile restrictions, kept with the transport tests."""

import json
import sys
from types import SimpleNamespace as NS

import pytest
from provider_profile_case import REPO, ProfileCase


class TelegramProfileTests(ProfileCase):
    def test_telegram_native_and_tool_exposure_restrictions(self):
        original = json.loads((REPO / "infra/hermes/runtime-settings.json").read_text())
        for key, value in (
            ("platform_toolsets.telegram", ["mcp-vault", "terminal"]),
            ("platform_toolsets.telegram", []),
            ("platforms.telegram.extra.drop_pending_on_cold_boot", True),
            ("platforms.telegram.extra.max_concurrent_updates", 2),
            ("platforms.telegram.extra.max_concurrent_updates", True),
            ("tools.tool_search.enabled", "on"),
        ):
            with self.subTest(key=key, value=value):
                self.write_json("runtime-settings.json", {**original, key: value})
                with self.assertRaises(SystemExit):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])

    def test_transport_policy_types_bounds_and_fields(self):
        original = json.loads(
            (REPO / "infra/hermes/telegram-settings.json").read_text()
        )
        changes = [(key, None) for key in original]
        for key in (
            "max_message_age_seconds",
            "max_input_chars",
            "max_pending_updates",
            "max_active_turns",
            "poll_timeout_seconds",
            "reconnect_delay_seconds",
        ):
            changes.extend(
                (key, value) for value in (True, 0, -1, original[key] + 1, "1", 1.5)
            )
        changes.extend(
            [
                ("private_chat_only", 1),
                ("text_only", False),
                ("transport", "webhook"),
                ("allowed_commands", ["start", "shell"]),
                ("ambiguous_turn_recovery", "replay"),
                ("owner_id", 123),
            ]
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.write_json("telegram-settings.json", {**original, key: value})
                with self.assertRaises(SystemExit):
                    self.run_profile("--apply")
                self.assertEqual(self.writes, [])


def test_private_credentials_and_native_transport_restrictions(runtime, monkeypatch):
    platform = NS(TELEGRAM="telegram")
    telegram = NS(
        token="SYNTHETIC_PRIVATE_TOKEN",
        enabled=False,
        extra={"drop_pending_on_cold_boot": False, "max_concurrent_updates": 1},
    )
    config = NS(platforms={"telegram": telegram}, multiplex_profiles=None)
    monkeypatch.setitem(
        sys.modules,
        "gateway.config",
        NS(load_gateway_config=lambda: config, Platform=platform),
    )
    monkeypatch.delenv("EMURU_TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    with pytest.raises(RuntimeError, match="owner_identity_missing"):
        runtime.private_config()
    for bad in ("all", "42,43", "0", "-42", "４２"):
        monkeypatch.setenv("EMURU_TELEGRAM_OWNER_ID", bad)
        with pytest.raises(RuntimeError):
            runtime.private_config()
    monkeypatch.setenv("EMURU_TELEGRAM_OWNER_ID", "42")
    for key, value in (
        ("webhook_url", "https://example.invalid"),
        ("base_url", "https://example.invalid"),
        ("drop_pending_on_cold_boot", True),
        ("max_concurrent_updates", True),
    ):
        original = dict(telegram.extra)
        telegram.extra[key] = value
        with pytest.raises(RuntimeError):
            runtime.private_config()
        telegram.extra = original
    config.multiplex_profiles = True
    with pytest.raises(RuntimeError):
        runtime.private_config()
    config.multiplex_profiles = None
    config.platforms["discord"] = NS(enabled=True)
    with pytest.raises(RuntimeError):
        runtime.private_config()
    config.platforms.pop("discord")
    telegram.token = ""
    with pytest.raises(RuntimeError, match="telegram_credentials_missing"):
        runtime.private_config()
    telegram.token = "SYNTHETIC_PRIVATE_TOKEN"
    checked, owner = runtime.private_config()
    assert checked is config and owner == 42
    assert config.platforms == {"telegram": telegram} and telegram.enabled
    assert config.multiplex_profiles is False
    assert runtime.os.environ["TELEGRAM_ALLOWED_USERS"] == "42"
    assert runtime.os.environ["TELEGRAM_ALLOW_ALL_USERS"] == "false"
