"""Validate, apply, or audit the Phase 1 Hermes configuration contract."""

import argparse
import json
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    settings = json.loads(
        (root / "infra/hermes/settings.json").read_text(encoding="utf-8")
    )
    required = {
        "model.provider": "gemini",
        "model.default": "gemini-3.8-flash",
        "model.base_url": "",
        "agent.api_max_retries": 0,
        "agent.auto_recovery_cycles": 0,
        "platform_toolsets.cli": [],
        "platform_toolsets.telegram": [],
        "fallback_providers": [],
        "mcp_servers": {},
        "memory.memory_enabled": False,
        "memory.user_profile_enabled": False,
        "auxiliary.background_review.enabled": False,
        "auxiliary.title_generation.enabled": False,
        "security.redact_secrets": True,
        "providers.gemini.request_timeout_seconds": 60,
        "providers.gemini.stale_timeout_seconds": 60,
    }
    for key, expected in required.items():
        if key not in settings or type(settings[key]) is not type(expected):
            raise SystemExit(f"Missing or incorrectly typed setting: {key}")
        if settings[key] != expected:
            raise SystemExit(f"Unsafe Phase 1 value: {key}")

    turns = settings.get("agent.max_turns")
    if type(turns) is not int or not 1 <= turns <= 12:
        raise SystemExit("agent.max_turns must be an integer between 1 and 12")

    disabled = settings.get("agent.disabled_toolsets")
    if not isinstance(disabled, list) or not all(
        isinstance(value, str) for value in disabled
    ):
        raise SystemExit("agent.disabled_toolsets must be a string list")
    forbidden = {
        "terminal",
        "file",
        "code_execution",
        "browser",
        "web",
        "delegation",
        "skills",
        "cronjob",
        "memory",
        "session_search",
    }
    if not forbidden.issubset(disabled):
        raise SystemExit("A required disabled toolset is missing")

    allowed_keys = set(required) | {"agent.max_turns", "agent.disabled_toolsets"}
    if set(settings) != allowed_keys:
        raise SystemExit("Unexpected setting; review changes to this contract")

    launcher = str(root / "scripts/hermes/emuru.sh")
    if args.apply:
        registered = subprocess.run(
            [launcher, "config", "get", "mcp_servers", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if json.loads(registered.stdout):
            raise SystemExit(
                "Phase 1 reset refused: an MCP server is configured. "
                "Use scripts/hermes/vault-profile.py for Phase 2."
            )

        for key, value in settings.items():
            serialized = value if isinstance(value, str) else json.dumps(value)
            command = [launcher, "config", "set"]

            if key == "mcp_servers":
                command.append("--force")

            command.extend([key, serialized])

            subprocess.run(
                command,
                check=True,
                timeout=30,
            )

    if args.apply or args.live:
        for key, expected in settings.items():
            result = subprocess.run(
                [launcher, "config", "get", key, "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            actual = json.loads(result.stdout)
            if type(actual) is not type(expected) or actual != expected:
                raise SystemExit(f"Live configuration mismatch: {key}")

    print("Hermes configuration contract: PASS")


if __name__ == "__main__":
    main()
