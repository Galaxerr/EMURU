"""Hermes profile policy, composition and verification; stdlib-only in either runtime.

Config transports supply reads and optional writes. Native audits accept the
already-read config mapping and never contact a provider or mutate live state.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from emuru.models.providers import check_provider, provider_settings
from emuru.telegram.queue import load_telegram_settings


class ProfileError(ValueError):
    """Safe CLI explanation with a content-free native diagnostic code."""

    def __init__(self, message, *, code="profile_settings_mismatch"):
        super().__init__(message)
        self.code = code


class HealthError(RuntimeError):
    """Only explicit, content-free diagnostic codes may be printed."""


TOOLS = ["vault_map", "vault_search", "vault_open", "vault_neighbors", "vault_write"]
DISABLED_TOOLSETS = {
    "web",
    "browser",
    "terminal",
    "file",
    "code_execution",
    "vision",
    "video",
    "image_gen",
    "video_gen",
    "x_search",
    "tts",
    "stt",
    "skills",
    "todo",
    "kanban",
    "memory",
    "context_engine",
    "session_search",
    "connections",
    "clarify",
    "delegation",
    "cronjob",
    "spotify",
    "yuanbao",
    "computer_use",
    "a2a",
}


def expected_settings(root: Path, *, model: dict | None = None):
    try:
        load_telegram_settings(root / "infra/hermes/telegram-settings.json")
    except ValueError as error:
        raise ProfileError(str(error)) from error
    settings = json.loads(
        (root / "infra/hermes/runtime-settings.json").read_text(encoding="utf-8")
    )
    selection = json.loads(
        (root / "infra/hermes/model-selection.json").read_text(encoding="utf-8")
    )
    if not isinstance(settings, dict):
        raise ProfileError("runtime-settings.json must be a settings mapping")
    try:
        cloud = None
        if (
            model is not None
            and isinstance(selection, dict)
            and selection.get("provider") == "ollama"
        ):
            context = model.get("ollama_num_ctx")
            if type(context) is not int or context not in (0, 65536):
                raise ProfileError(
                    "Invalid Ollama context", code="profile_model_route_mismatch"
                )
            # Native audits use the reviewed, persisted route without contacting Ollama.
            cloud = context == 0
        if os.environ.get("EMURU_CONTAINER_ROUTE"):
            from emuru.models.gateway import read_route
            from emuru.models.gateway import settings as gateway_settings

            route = gateway_settings(read_route(os.environ["EMURU_CONTAINER_ROUTE"]))
        else:
            route = provider_settings(selection, cloud=cloud)
    except ProfileError:
        raise
    except ValueError as error:
        raise ProfileError(str(error)) from error
    if any(
        not isinstance(key, str)
        or key.startswith(("model.", "mcp_servers", "providers."))
        or key.isupper()
        or "api_key" in key.lower()
        for key in settings
    ):
        raise ProfileError(
            "Keep provider configuration, MCP registration and secrets separate"
        )
    turns = settings.get("agent.max_turns")
    if type(turns) is not int or not 1 <= turns <= 12:
        raise ProfileError("Invalid turn bound")
    disabled = settings.get("agent.disabled_toolsets")
    if (
        not isinstance(disabled, list)
        or not all(isinstance(name, str) for name in disabled)
        or not DISABLED_TOOLSETS.issubset(disabled)
    ):
        raise ProfileError("Required disabled toolsets are missing")
    for key in ("agent.api_max_retries", "agent.auto_recovery_cycles"):
        if type(settings.get(key)) is not int or settings[key] != 0:
            raise ProfileError(f"Unsafe retry setting: {key}")
    for key in (
        "memory.memory_enabled",
        "memory.user_profile_enabled",
        "auxiliary.background_review.enabled",
        "auxiliary.title_generation.enabled",
    ):
        if settings.get(key) is not False:
            raise ProfileError(f"Unexpected automatic capability: {key}")
    if (
        settings.get("fallback_providers") != []
        or settings.get("security.redact_secrets") is not True
        or settings.get("platform_toolsets.cli") != ["mcp-vault"]
        or settings.get("platform_toolsets.telegram") != ["mcp-vault"]
        or settings.get("tools.tool_search.enabled") != "off"
        or settings.get("platforms.telegram.extra.drop_pending_on_cold_boot")
        is not False
        or type(settings.get("platforms.telegram.extra.max_concurrent_updates"))
        is not int
        or settings.get("platforms.telegram.extra.max_concurrent_updates") != 1
        or settings.get("gateway.standalone") is not True
    ):
        raise ProfileError("Fallback, tool exposure or secret-redaction policy changed")
    settings.update(route)
    return settings


def expected_server(root: Path):
    uv = shutil.which("uv")
    if uv is None:
        raise ProfileError("uv is not on PATH")
    return {
        "command": uv,
        "args": [
            "--directory",
            str(root),
            "run",
            "--frozen",
            "--no-sync",
            "python",
            str(root / "scripts/hermes/vault.py"),
            "serve",
        ],
        "cwd": str(root),
        "enabled": True,
        "lazy": False,
        "timeout": 15,
        "connect_timeout": 15,
        "supports_parallel_tool_calls": False,
        "sampling": {"enabled": False},
        "elicitation": {"enabled": False},
        "tools": {"include": TOOLS, "resources": False, "prompts": False},
    }


def _check_model(model):
    if not isinstance(model, dict):
        raise ProfileError(
            "The model configuration must be a mapping", code="inline_model_credentials"
        )
    if model.get("api_key") or model.get("key_env"):
        raise ProfileError(
            "Clear inline model.api_key/model.key_env overrides; "
            "use the profile's standard provider environment variables",
            code="inline_model_credentials",
        )


def verify_profile(settings, server, get):
    """Verify through the same read seam used by the config transport."""
    for key, expected in settings.items():
        actual = get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise ProfileError(f"EMURU live configuration mismatch: {key}")
    if get("mcp_servers") != {"vault": server}:
        raise ProfileError(
            "EMURU MCP registration mismatch", code="profile_mcp_registration_mismatch"
        )
    return server


def configure_profile(root: Path, get, set_value=None):
    """Preflight, optionally apply, then audit; omitted writer means zero writes."""
    settings, server = expected_settings(root), expected_server(root)
    registered = get("mcp_servers")
    if not isinstance(registered, dict) or set(registered) - {"vault"}:
        raise ProfileError(
            "Unexpected MCP server configured; inspect the profile",
            code="profile_mcp_registration_mismatch",
        )
    _check_model(get("model"))
    try:
        check_provider(settings)
    except (ValueError, RuntimeError) as error:
        raise ProfileError(str(error)) from error
    if set_value is not None:
        if not (root / ".runtime/vault").is_dir():
            raise ProfileError("Prepare the synthetic runtime vault first")
        for key, value in settings.items():
            set_value(key, value)
        set_value("mcp_servers.vault", server, force=True)
    return verify_profile(settings, server, get)


def audit_native_profile(root: Path, actual: dict):
    """Audit a native nested config snapshot without network or config writes."""
    if not isinstance(actual, dict):
        raise ProfileError("The profile configuration must be a mapping")
    model = actual.get("model", {})
    _check_model(model)
    settings, server = expected_settings(root, model=model), expected_server(root)

    def get(key):
        cursor = actual
        for part in key.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        return cursor

    return verify_profile(settings, server, get)


def public_contract(root: Path):
    settings, server = expected_settings(root), expected_server(root)
    lock = json.loads((root / "infra/hermes/runtime-lock.json").read_text())
    if (
        lock.get("source_repository")
        != "https://github.com/NousResearch/hermes-agent.git"
        or len(lock.get("commit", "")) != 40
        or any(c not in "0123456789abcdef" for c in lock["commit"])
    ):
        raise HealthError("runtime_record_invalid")
    contract = json.loads(
        (root / "infra/hermes/telegram-native-contract.json").read_text()
    )
    if (
        contract.get("telegram_version") != "22.8"
        or not contract.get("source_sha256")
        or not contract.get("methods")
        or not contract.get("functions")
    ):
        raise HealthError("bridge_contract_invalid")
    return settings, server


def installed_runtime(root: Path):
    """Owner environment chooses the location; update/message data never reaches it."""
    raw = os.environ.get("EMURU_HERMES_ROOT", str(Path.home() / ".hermes/hermes-agent"))
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise HealthError("runtime_location_invalid")
    path = path.resolve()
    lock = json.loads((root / "infra/hermes/runtime-lock.json").read_text())
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
        if commit != lock["commit"]:
            raise HealthError("runtime_commit_mismatch")
        subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD", "--"],
            check=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as error:
        raise HealthError("runtime_source_unverified") from error
    contract = json.loads(
        (root / "infra/hermes/telegram-native-contract.json").read_text()
    )
    for relative, expected in contract["source_sha256"].items():
        source = path / relative
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != expected
        ):
            raise HealthError("bridge_source_contract_mismatch")
    return path


def profile_home() -> Path:
    raw = os.environ.get(
        "EMURU_HERMES_PROFILE_HOME",
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/emuru")),
    )
    path = Path(raw)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name != "emuru"
        or path.parent.name != "profiles"
    ):
        raise ProfileError("profile_location_invalid", code="profile_location_invalid")
    no_symlinks(path)
    return path


def no_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ProfileError("target_symlink_refused", code="target_symlink_refused")
