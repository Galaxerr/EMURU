"""Apply or audit the EMURU vault profile for the explicitly selected provider."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from emuru.providers import check_provider, provider_settings

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["vault_map", "vault_search", "vault_open", "vault_neighbors", "vault_write"]


def expected_settings():
    settings = json.loads(
        (ROOT / "infra/hermes/runtime-settings.json").read_text(encoding="utf-8")
    )
    selection = json.loads(
        (ROOT / "infra/hermes/model-selection.json").read_text(encoding="utf-8")
    )
    if not isinstance(settings, dict):
        raise SystemExit("runtime-settings.json must be a settings mapping")
    try:
        route = provider_settings(selection)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if any(
        not isinstance(key, str)
        or key.startswith(("model.", "mcp_servers", "providers."))
        or key.isupper()
        or "api_key" in key.lower()
        for key in settings
    ):
        raise SystemExit(
            "Keep provider configuration, MCP registration and secrets separate"
        )
    turns = settings.get("agent.max_turns")
    if type(turns) is not int or not 1 <= turns <= 12:
        raise SystemExit("Invalid turn bound")
    disabled = settings.get("agent.disabled_toolsets")
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
        "computer_use",
    }
    if (
        not isinstance(disabled, list)
        or not all(isinstance(name, str) for name in disabled)
        or not forbidden.issubset(disabled)
    ):
        raise SystemExit("Required disabled toolsets are missing")
    for key in ("agent.api_max_retries", "agent.auto_recovery_cycles"):
        if type(settings.get(key)) is not int or settings[key] != 0:
            raise SystemExit(f"Unsafe retry setting: {key}")
    for key in (
        "memory.memory_enabled",
        "memory.user_profile_enabled",
        "auxiliary.background_review.enabled",
        "auxiliary.title_generation.enabled",
    ):
        if settings.get(key) is not False:
            raise SystemExit(f"Unexpected automatic capability: {key}")
    if (
        settings.get("fallback_providers") != []
        or settings.get("security.redact_secrets") is not True
        or settings.get("platform_toolsets.cli") != ["mcp-vault"]
        or settings.get("platform_toolsets.telegram") != []
    ):
        raise SystemExit("Fallback, tool exposure or secret-redaction policy changed")
    settings.update(route)
    return settings


def expected_server():
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is not on PATH")
    return {
        "command": uv,
        "args": [
            "--directory",
            str(ROOT),
            "run",
            "--frozen",
            "--no-sync",
            "python",
            str(ROOT / "scripts/hermes-vault.py"),
            "serve",
        ],
        "cwd": str(ROOT),
        "enabled": True,
        "lazy": False,
        "timeout": 15,
        "connect_timeout": 15,
        "supports_parallel_tool_calls": False,
        "sampling": {"enabled": False},
        "elicitation": {"enabled": False},
        "tools": {"include": TOOLS, "resources": False, "prompts": False},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Validate version-controlled settings without launching Hermes",
    )
    args = parser.parse_args()
    if args.apply and args.offline:
        parser.error("--apply and --offline cannot be combined")
    settings = expected_settings()
    if args.offline:
        print("EMURU provider and vault policy configuration: PASS (offline)")
        return
    server = expected_server()
    launcher = str(ROOT / "scripts/hermes-emuru.sh")

    def get(key):
        response = subprocess.run(
            [launcher, "config", "get", key, "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(response.stdout)

    def set_value(key, value, force=False):
        command = [launcher, "config", "set"]
        if force:
            command.append("--force")
        command.extend([key, value if isinstance(value, str) else json.dumps(value)])
        subprocess.run(command, check=True, timeout=30)

    registered = get("mcp_servers")
    if not isinstance(registered, dict) or set(registered) - {"vault"}:
        raise SystemExit("Unexpected MCP server configured; inspect the profile")
    # Do not print inline credentials or silently reuse a custom provider's key.
    current_model = get("model")
    if not isinstance(current_model, dict):
        raise SystemExit("The model configuration must be a mapping")
    if current_model.get("api_key") or current_model.get("key_env"):
        raise SystemExit(
            "Clear inline model.api_key/model.key_env overrides; "
            "use the profile's standard provider environment variables"
        )
    try:
        check_provider(settings)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error
    if args.apply:
        if not (ROOT / ".runtime/vault").is_dir():
            raise SystemExit("Prepare the synthetic runtime vault first")
        for key, value in settings.items():
            set_value(key, value)
        set_value("mcp_servers.vault", server, force=True)
    for key, expected in settings.items():
        actual = get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise SystemExit(f"EMURU live configuration mismatch: {key}")
    registered = get("mcp_servers")
    if registered != {"vault": server}:
        raise SystemExit("EMURU MCP registration mismatch")
    print("EMURU provider and vault profile configuration: PASS")


if __name__ == "__main__":
    main()
