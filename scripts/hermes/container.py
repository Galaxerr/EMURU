"""Initialize and operate the isolated EMURU Docker deployment."""

import argparse
import fcntl
import json
import os
import secrets
import shlex
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
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


@contextmanager
def lifecycle_lock():
    path = DEPLOYMENT_ENV.with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(fd)
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("Lifecycle lock permissions mismatch")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


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
    if DEPLOYMENT_ENV.exists():
        saved_deployment(home)
        if route_source is not None or telegram_owner_id is not None:
            raise ValueError(
                "Deployment already initialized; edit private state explicitly"
            )
        return DEPLOYMENT_ENV
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


def compose(home, *arguments):
    if arguments[:1] in {("config",), ("run",), ("build",), ("pull",), ("logs",)}:
        saved_deployment(home)
        env_path = DEPLOYMENT_ENV
    elif arguments[:1] in {("down",), ("ps",)}:
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
        raise ValueError("Unsupported Compose operation")
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


def saved_deployment(home=None):
    """Read the initialized deployment without changing private files."""
    env_path = private_path(DEPLOYMENT_ENV)
    info = env_path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Deployment metadata must be owner-owned and mode 0600")
    values = {}
    for line in env_path.read_text().splitlines():
        key, separator, raw = line.partition("=")
        if (
            not separator
            or key in values
            or key
            not in {
                "EMURU_UID",
                "EMURU_GID",
                "EMURU_TELEGRAM_OWNER_ID",
                "EMURU_VAULT",
                *DEPLOYMENT_PATHS,
            }
        ):
            raise ValueError("Invalid deployment metadata")
        parsed = shlex.split(raw)
        if len(parsed) != 1:
            raise ValueError("Invalid deployment metadata")
        values[key] = parsed[0]
    if set(values) != {
        "EMURU_UID",
        "EMURU_GID",
        "EMURU_TELEGRAM_OWNER_ID",
        "EMURU_VAULT",
        *DEPLOYMENT_PATHS,
    }:
        raise ValueError("Incomplete deployment metadata")
    if values["EMURU_UID"] != str(os.getuid()) or values["EMURU_GID"] != str(
        os.getgid()
    ):
        raise ValueError("Deployment identity mismatch")
    owner = values["EMURU_TELEGRAM_OWNER_ID"]
    if not owner.isascii() or not owner.isdecimal() or int(owner) <= 0:
        raise ValueError("Invalid Telegram owner")
    profile = private_path(values["EMURU_PROFILE"])
    if profile.name != "emuru" or profile.parent.name != "profiles":
        raise ValueError("Invalid deployment profile path")
    saved_home = profile.parents[1]
    if profile != saved_home / "profiles/emuru" or (
        home is not None and saved_home != deployment_home(home)
    ):
        raise ValueError("Saved deployment does not match the requested home")
    home_info = saved_home.stat()
    if home_info.st_uid != os.getuid() or stat.S_IMODE(home_info.st_mode) != 0o700:
        raise ValueError("Deployment home permissions mismatch")
    for key, relative in DEPLOYMENT_PATHS.items():
        path = private_path(values[key])
        if path != saved_home / relative:
            raise ValueError("Deployment path mismatch")
        info = path.stat()
        mode = 0o700 if key in PRIVATE_DIRECTORIES else 0o600
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or not (
                stat.S_ISDIR(info.st_mode)
                if key in PRIVATE_DIRECTORIES
                else stat.S_ISREG(info.st_mode)
            )
        ):
            raise ValueError("Deployment permissions mismatch")
    route = read_route(values["EMURU_ROUTE"])
    if Path(values["EMURU_GATEWAY_CONFIG"]).read_text() != render(route):
        raise ValueError("Gateway config does not match selected route")
    target = profile / "vault-target.json"
    info = target.stat()
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or json.loads(target.read_text())
        != {"mode": "real", "vault_path": str(CONTAINER_VAULT_PATH)}
    ):
        raise ValueError("Private vault target mismatch")
    vault = private_path(values["EMURU_VAULT"])
    if (
        not vault.is_dir()
        or not (vault / ".git").exists()
        or not (vault / "00_Inbox").is_dir()
    ):
        raise ValueError("Saved vault unavailable")
    return values


def wake(home=None):
    with lifecycle_lock():
        saved_deployment(home)
        service = subprocess.run(
            ["systemctl", "--user", "is-active", "emuru-telegram.service"],
            check=False,
            capture_output=True,
            text=True,
        )
        if service.returncode not in (0, 3) or service.stdout.strip() not in (
            "active",
            "inactive",
            "failed",
        ):
            raise ValueError("Cannot establish systemd poller state")
        if service.stdout.strip() == "active":
            raise ValueError("Systemd Telegram poller is active")
        command = [
            "docker",
            "compose",
            "--project-name",
            "emuru",
            "--env-file",
            str(DEPLOYMENT_ENV),
            "-f",
            str(COMPOSE_FILE),
        ]
        running = subprocess.run(
            [*command, "ps", "--status", "running", "--services", "emuru"],
            check=True,
            capture_output=True,
            text=True,
        )
        if running.stdout.strip() == "emuru":
            print("EMURU worker already running; no second poller started")
            return
        if running.stdout.strip():
            raise ValueError("Unexpected Compose worker state")
        subprocess.run(
            [
                *command,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "--pull",
                "never",
                "--no-build",
                "ollama",
                "litellm",
            ],
            check=True,
        )
        subprocess.run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "litellm",
                "python",
                "-m",
                "emuru.models.startup",
            ],
            check=True,
        )
        for args in (
            ("scripts/hermes/telegram.sh", "--check"),
            (
                "/opt/emuru/.venv/bin/python",
                "scripts/hermes/vault.py",
                "target",
                "--require-real",
            ),
        ):
            subprocess.run(
                [*command, "run", "--rm", "--no-deps", "emuru", *args], check=True
            )
        started = datetime.now(UTC).isoformat()
        ready = False
        try:
            subprocess.run(
                [
                    *command,
                    "--profile",
                    "agent",
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "emuru",
                ],
                check=True,
            )
            for _ in range(60):
                worker = subprocess.run(
                    [*command, "logs", "--no-color", "--since", started, "emuru"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if (
                    "EMURU Telegram checkpoint: required guards and five MCP tools READY"
                    in worker.stdout
                ):
                    break
                running = subprocess.run(
                    [*command, "ps", "--status", "running", "--services", "emuru"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if not running.stdout.strip():
                    break
                time.sleep(1)
            else:
                worker = None
            if (
                worker is None
                or "EMURU Telegram checkpoint: required guards and five MCP tools READY"
                not in worker.stdout
            ):
                raise ValueError("Guarded worker did not reach READY")
            running = subprocess.run(
                [*command, "ps", "--status", "running", "--services", "emuru"],
                check=True,
                capture_output=True,
                text=True,
            )
            if running.stdout.strip() != "emuru":
                raise ValueError("Guarded worker exited after READY")
            ready = True
        finally:
            if not ready:
                subprocess.run([*command, "stop", "emuru"], check=True)
        print("EMURU READY")


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
    subparsers.add_parser("up", help="Start with guarded readiness checks")
    subparsers.add_parser("down", help="Stop and remove the isolated deployment")
    subparsers.add_parser("status", help="Show isolated deployment status")
    subparsers.add_parser(
        "wake", help="Start infrastructure and guarded worker after readiness checks"
    )
    args = parser.parse_args()
    try:
        workspace_env = ROOT / ".env"
        if args.command == "init":
            with lifecycle_lock():
                initialize(
                    args.home, args.route_source, args.telegram_owner_id, workspace_env
                )
                compose(args.home, "pull", "--policy", "missing", "ollama", "litellm")
                compose(args.home, "build", "emuru")
                for arguments in (
                    (
                        "uv",
                        "run",
                        "--frozen",
                        "--no-sync",
                        "python",
                        "scripts/hermes/vault.py",
                        "prepare",
                    ),
                    (
                        "uv",
                        "run",
                        "--frozen",
                        "--no-sync",
                        "python",
                        "scripts/hermes/vault-profile.py",
                        "--apply",
                    ),
                ):
                    compose(args.home, "run", "--rm", "--no-deps", "emuru", *arguments)
            home = deployment_home(args.home)
            print(f"Private container deployment initialized: {home}")
            print(
                "Start it with: "
                f"uv run --frozen python scripts/hermes/container.py"
                f" --home {shlex.quote(str(home))} up"
            )
            return
        if args.command in {"wake", "up"}:
            if args.route_source is not None or args.telegram_owner_id is not None:
                raise ValueError("Route and owner selection belong to initialization")
            wake(args.home)
            return
        command = {
            "config": ("config", "--quiet"),
            "down": ("down", "--remove-orphans"),
            "status": ("ps",),
        }[args.command]
        with lifecycle_lock() if args.command == "down" else nullcontext():
            compose(args.home, *command)
    except (
        OSError,
        ValueError,
        BlockingIOError,
        subprocess.CalledProcessError,
    ) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
