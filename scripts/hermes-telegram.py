"""Owner-operated Telegram diagnostics and guarded native-runtime launch."""

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH_CODES = {
    "turn_timeout",
    "queue_database_error",
    "turn_failed",
    "provider_failed",
    "tool_failed",
    "native_failure",
    "native_cancelled",
    "dispatch_admission",
    "restart_interrupted",
    "turn_interrupted",
    "consumer_failed",
    "consumer_exited",
    "consumer_not_running",
    "status_reply_failed",
}


class HealthError(RuntimeError):
    """Only explicit, content-free diagnostic codes may be printed."""


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_contract():
    # Only EMURU's stdlib routing/policy modules; never its MCP SDK in Hermes.
    sys.path.insert(0, str(ROOT / "src"))
    try:
        profile = load_module(
            "emuru_vault_profile", ROOT / "scripts/hermes-vault-profile.py"
        )
        settings, server = profile.expected_settings(), profile.expected_server()
    finally:
        sys.path.pop(0)
    lock = json.loads((ROOT / "infra/hermes/runtime-lock.json").read_text())
    if (
        lock.get("source_repository")
        != "https://github.com/NousResearch/hermes-agent.git"
        or len(lock.get("commit", "")) != 40
        or any(c not in "0123456789abcdef" for c in lock["commit"])
    ):
        raise HealthError("runtime_record_invalid")
    contract = json.loads(
        (ROOT / "infra/hermes/telegram-native-contract.json").read_text()
    )
    if (
        contract.get("telegram_version") != "22.8"
        or not contract.get("source_sha256")
        or not contract.get("methods")
        or not contract.get("functions")
    ):
        raise HealthError("bridge_contract_invalid")
    return settings, server


def offline():
    public_contract()
    queue_module = load_module(
        "emuru_queue_check", ROOT / "src/emuru/telegram_queue.py"
    )
    policy = queue_module.load_telegram_settings(
        ROOT / "infra/hermes/telegram-settings.json"
    )
    with tempfile.TemporaryDirectory(prefix="emuru-telegram-offline-") as directory:
        queue = queue_module.TelegramQueue(
            Path(directory) / "queue.sqlite3", policy, 42, clock=lambda: 100000
        )
        try:
            envelope = {
                "bot_id": 7,
                "update_id": 1,
                "chat_id": 42,
                "chat_type": "private",
                "user_id": 42,
                "is_bot": False,
                "message_id": 1,
                "message_date": 100000,
                "text": "synthetic queue check",
            }
            if queue.stage(7, [envelope, envelope]) != [
                "queued",
                "queued",
            ] or queue.status() != {"queued": 1}:
                raise HealthError("queue_contract_invalid")
        finally:
            queue.close()
    print("Telegram public policy / pure queue contract: PASS (offline)")


def installed_runtime():
    """Owner environment chooses the location; update/message data never reaches it."""
    raw = os.environ.get("EMURU_HERMES_ROOT", str(Path.home() / ".hermes/hermes-agent"))
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise HealthError("runtime_location_invalid")
    path = path.resolve()
    lock = json.loads((ROOT / "infra/hermes/runtime-lock.json").read_text())
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
        (ROOT / "infra/hermes/telegram-native-contract.json").read_text()
    )
    for relative, expected in contract["source_sha256"].items():
        source = path / relative
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != expected
        ):
            raise HealthError("bridge_source_contract_mismatch")
    return path


def profile_home():
    raw = os.environ.get(
        "EMURU_HERMES_PROFILE_HOME",
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/emuru")),
    )
    path = Path(raw)
    if not path.is_absolute() or path.name != "emuru" or path.parent.name != "profiles":
        raise HealthError("profile_location_invalid")
    return path.resolve()


def status(home):
    path = home / "emuru-telegram/queue.sqlite3"
    if not path.exists():
        print(json.dumps({"queue": "not_started", "health": ["not_started"]}))
        return 0
    try:
        if home.joinpath("emuru-telegram").stat().st_mode & 0o777 != 0o700 or any(
            candidate.is_symlink() or candidate.stat().st_mode & 0o777 != 0o600
            for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
            if candidate.exists() or candidate.is_symlink()
        ):
            raise HealthError("queue_permissions_invalid")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise HealthError("queue_schema_unsupported")
            counts = dict(
                db.execute("SELECT state,count(*) FROM updates GROUP BY state")
            )
            if set(counts) - {
                "queued",
                "started",
                "completed",
                "failed",
                "interrupted",
                "expired",
                "rejected",
            }:
                raise HealthError("queue_schema_unsupported")
            health = {
                code if code in HEALTH_CODES else "turn_failed"
                for (raw,) in db.execute(
                    "SELECT DISTINCT error_code FROM updates WHERE error_code IS NOT NULL"
                )
                for code in [raw.removesuffix(":reported")]
            }
        health_file = path.parent / "health.json"
        if health_file.exists():
            if health_file.is_symlink() or health_file.stat().st_mode & 0o777 != 0o600:
                raise HealthError("queue_permissions_invalid")
            health.update(
                code if code in HEALTH_CODES else "consumer_failed"
                for code in json.loads(health_file.read_text())["health"]
            )
        print(json.dumps({"queue": counts, "health": sorted(health)}))
        return 0
    except (
        sqlite3.Error,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        HealthError,
    ) as error:
        code = str(error) if isinstance(error, HealthError) else "queue_unavailable"
        print(json.dumps({"queue": {}, "health": [code]}))
        return 1


def launch(mode):
    public_contract()
    native = installed_runtime()
    os.environ["EMURU_HERMES_ROOT"] = str(native)
    os.environ["HERMES_HOME"] = str(profile_home())
    # Dependency bootstrapping must not fetch/install anything on the launch path.
    os.environ["UV_OFFLINE"] = "1"
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    workspace = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "emuru/workspace"
    )
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(workspace)
    arguments = [str(ROOT / "infra/hermes/telegram-runtime.py")]
    if mode != "--launch":
        arguments.append(mode)
    python = native / ".venv/bin/python"
    if python.is_file() and os.access(python, os.X_OK):
        command = [
            str(python),
            "-I",
            "-c",
            "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_path(sys.argv.pop(1),run_name='__main__')",
            str(native),
            *arguments,
        ]
    else:
        # Current Hermes uses its package manager rather than a checkout .venv.
        hermes = native / ".hermes/bin/hermes"
        if not hermes.is_file() or not os.access(hermes, os.X_OK):
            raise HealthError("runtime_python_missing")
        result = subprocess.run(
            [
                str(hermes),
                "--print-runtime-command",
                "--module",
                "runpy",
                "--",
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        command = json.loads(result.stdout)
        entry = "runpy.run_module('runpy', run_name='__main__', alter_sys=True)"
        if (
            not isinstance(command, list)
            or len(command) < 4
            or not all(type(x) is str for x in command)
            or command[1:3] != ["-I", "-c"]
            or not command[3].endswith(entry)
            or not Path(command[0]).is_absolute()
        ):
            raise HealthError("runtime_launcher_contract_changed")
        command[3] = (
            command[3].removesuffix(entry)
            + "runpy.run_path(sys.argv.pop(1), run_name='__main__')"
        )
    # Exec preserves the lifetime lock and delivers shutdown signals directly to Hermes.
    sys.stdout.flush()
    os.execv(command[0], command)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ("offline", "check", "status", "runtime-check", "launch", "preflight"):
        modes.add_argument("--" + name, action="store_const", const=name, dest="mode")
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "offline":
        offline()
    elif args.mode == "status":
        raise SystemExit(status(profile_home()))
    elif args.mode == "preflight":
        public_contract()
        installed_runtime()
        profile_home()
        print("Pinned Hermes source / bridge source contract: PASS (local)")
    else:
        launch("--" + args.mode)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- never expose private SDK exception text
        code = str(error) if isinstance(error, HealthError) else "diagnostic_failed"
        raise SystemExit("EMURU Telegram: " + code) from None
