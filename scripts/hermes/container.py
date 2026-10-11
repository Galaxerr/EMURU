"""Initialize and operate the isolated EMURU Docker deployment."""

import argparse
import json
import os
import secrets
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

from emuru.environment import load_env, write_env
from emuru.models.gateway import private_path, read_route, render, write_private

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "infra/docker/compose.yaml"
DEFAULT_HOME = Path.home() / ".local/state/emuru/container"
DEPLOYMENT_ENV = ROOT / "infra/docker/deployment.env"
CONTAINER_VAULT_PATH = Path("/state/vault")

UPSTREAM_KEYS = {
    "ollama": "OLLAMA_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai-api": "OPENAI_API_KEY",
}
DEPLOYMENT_PATHS = {
    "EMURU_PROFILE": "profiles/emuru",
    "EMURU_STATE": "state",
    "EMURU_RUNTIME": "runtime",
    "EMURU_ROUTE": "route.json",
    "EMURU_GATEWAY_CONFIG": "litellm.yaml",
    "EMURU_GATEWAY_KEY_FILE": "gateway.key",
    "EMURU_OLLAMA_STATE": "ollama",
    **{
        f"EMURU_{name}_FILE": name.removesuffix("_API_KEY").lower() + "-api.key"
        for name in UPSTREAM_KEYS.values()
    },
}
PRIVATE_DIRECTORIES = (
    "EMURU_PROFILE",
    "EMURU_STATE",
    "EMURU_RUNTIME",
    "EMURU_OLLAMA_STATE",
)


def deployment_home(value=None):
    path = Path(value or os.environ.get("EMURU_CONTAINER_HOME", DEFAULT_HOME))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Container home must be an absolute path without '..'")
    return path


def _make_private_directory(path):
    path = private_path(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o777 != 0o700:
        raise ValueError(f"Private directory must be owner-owned and mode 0700: {path}")


def _write_if_missing(path, value):
    if path.exists():
        info = path.stat()
        if (
            not info.st_mode & 0o100000
            or info.st_uid != os.getuid()
            or info.st_mode & 0o777 != 0o600
        ):
            raise ValueError(f"Private file must be owner-owned and mode 0600: {path}")
        return
    write_private(path, value)


def _deployment_paths(home):
    home = private_path(home)
    paths = {
        key: private_path(home / relative) for key, relative in DEPLOYMENT_PATHS.items()
    }
    for name in ("config.yaml", "vault-target.json", ".env"):
        private_path(paths["EMURU_PROFILE"] / name)
    return paths


def prepare_deployment(home, vault, owner_id):
    """Prepare private directories and Compose values without reading live state."""
    paths = _deployment_paths(home)
    _make_private_directory(home)
    for key in PRIVATE_DIRECTORIES:
        _make_private_directory(paths[key])
    return {
        "EMURU_UID": os.getuid(),
        "EMURU_GID": os.getgid(),
        "EMURU_TELEGRAM_OWNER_ID": owner_id,
        "EMURU_VAULT": vault,
        **paths,
    }


def _saved_owner_id():
    path = DEPLOYMENT_ENV
    if not path.is_file():
        return ""
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if key == "EMURU_TELEGRAM_OWNER_ID" and separator:
            return value.strip("'\"")
    return ""


def _vault_path():
    raw = os.environ.get("EMURU_VAULT_PATH", "")
    path = Path(raw)
    if (
        not raw
        or not path.is_absolute()
        or ".." in path.parts
        or not path.is_dir()
        or path.is_symlink()
        or not (path / ".git").exists()
        or not (path / "00_Inbox").is_dir()
    ):
        raise ValueError("EMURU_VAULT_PATH must be an absolute Git vault with 00_Inbox")
    return path.resolve()


def initialize(
    home=None, route_source=None, telegram_owner_id=None, workspace_env=None
):
    """Create one private deployment and return its Compose environment path."""
    if workspace_env is not None:
        workspace_env = private_path(workspace_env)
        load_env(workspace_env)
    home = private_path(deployment_home(home))
    env_path = private_path(DEPLOYMENT_ENV)
    # Validate every destination before mkdir/chmod or replacing private state.
    _deployment_paths(home)
    source = private_path(route_source or home / "route.json")
    vault = _vault_path()
    route = read_route(source)
    owner_id = str(
        telegram_owner_id
        or os.environ.get("EMURU_TELEGRAM_OWNER_ID", "")
        or _saved_owner_id()
    )
    if not owner_id.isascii() or not owner_id.isdecimal() or int(owner_id) <= 0:
        raise ValueError(
            "A positive Telegram owner ID is required; pass --telegram-owner-id"
        )
    values = prepare_deployment(home, vault, owner_id)
    _write_if_missing(
        home / "profiles/emuru/config.yaml", "model: {}\nmcp_servers: {}\n"
    )
    write_private(
        home / "profiles/emuru/vault-target.json",
        json.dumps({"mode": "real", "vault_path": str(CONTAINER_VAULT_PATH)}) + "\n",
    )

    key_path = values["EMURU_GATEWAY_KEY_FILE"]
    gateway_key = os.environ.get("EMURU_GATEWAY_KEY", "")
    if not gateway_key and key_path.exists():
        _write_if_missing(key_path, "")
        gateway_key = key_path.read_text().strip()
    gateway_key = gateway_key or "sk-" + secrets.token_hex(32)
    write_private(key_path, gateway_key + "\n")
    if workspace_env is not None:
        write_env(workspace_env, {"EMURU_GATEWAY_KEY": gateway_key})

    selected_key = UPSTREAM_KEYS[route["primary"]["provider"]]
    for name in UPSTREAM_KEYS.values():
        secret_path = values[f"EMURU_{name}_FILE"]
        value = os.environ.get(name, "") if name == selected_key else ""
        if name == "GEMINI_API_KEY" and not value and name == selected_key:
            value = os.environ.get("GOOGLE_API_KEY", "")
        write_private(secret_path, value + "\n")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    write_private(
        home / "profiles/emuru/.env",
        f"TELEGRAM_BOT_TOKEN={shlex.quote(telegram_token)}\n",
    )
    route_path = values["EMURU_ROUTE"]
    write_private(route_path, json.dumps(route, indent=2) + "\n")
    write_private(values["EMURU_GATEWAY_CONFIG"], render(route))

    # Deployment metadata belongs in the repo; its parent need not be private.
    fd, temporary = tempfile.mkstemp(dir=env_path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(
                "".join(
                    f"{key}={shlex.quote(str(value))}\n"
                    for key, value in values.items()
                )
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return env_path


def compose(
    home, *arguments, route_source=None, telegram_owner_id=None, workspace_env=None
):
    if arguments[:1] in {("down",), ("ps",)}:
        env_path = private_path(DEPLOYMENT_ENV)
        info = env_path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Deployment metadata must be owner-owned and mode 0600")
        if home is not None or os.environ.get("EMURU_CONTAINER_HOME"):
            expected = str(deployment_home(home) / "profiles/emuru")
            saved = next(
                (
                    line.partition("=")[2]
                    for line in env_path.read_text().splitlines()
                    if line.startswith("EMURU_PROFILE=")
                ),
                "",
            )
            if shlex.split(saved) != [expected]:
                raise ValueError("Saved deployment does not match the requested home")
    else:
        env_path = initialize(home, route_source, telegram_owner_id, workspace_env)
    command = [
        "docker",
        "compose",
        "--project-name",
        "emuru",
        "--env-file",
        str(env_path),
        "-f",
        str(COMPOSE_FILE),
        *arguments,
    ]
    return subprocess.run(command, check=True).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        help=f"Private deployment directory (default: {DEFAULT_HOME})",
    )
    parser.add_argument(
        "--route-source",
        help="Owner-only route source (default: route.json in the private directory)",
    )
    parser.add_argument(
        "--telegram-owner-id",
        help="Positive numeric Telegram owner ID (or EMURU_TELEGRAM_OWNER_ID)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create private files and Compose environment")
    subparsers.add_parser("config", help="Validate the generated Compose configuration")
    subparsers.add_parser("up", help="Build and start isolated infrastructure")
    subparsers.add_parser("down", help="Stop and remove the isolated deployment")
    subparsers.add_parser("status", help="Show isolated deployment status")
    args = parser.parse_args()
    try:
        workspace_env = ROOT / ".env"
        if args.command == "init":
            initialize(
                args.home, args.route_source, args.telegram_owner_id, workspace_env
            )
            home = deployment_home(args.home)
            print(f"Private container deployment initialized: {home}")
            print(
                "Start it with: "
                f"uv run --frozen python scripts/hermes/container.py"
                f" --home {shlex.quote(str(home))} up"
            )
            return
        command = {
            "config": ("config", "--quiet"),
            "up": ("up", "-d", "--build"),
            "down": ("down", "--remove-orphans"),
            "status": ("ps",),
        }[args.command]
        compose(
            args.home,
            *command,
            route_source=args.route_source,
            telegram_owner_id=args.telegram_owner_id,
            workspace_env=workspace_env,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
