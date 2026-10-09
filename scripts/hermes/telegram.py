"""Owner-operated Telegram diagnostics and guarded native-runtime launch."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Import only stdlib EMURU modules; Hermes keeps its own interpreter and MCP SDK.
sys.path.insert(0, str(ROOT / "src"))
from emuru.environment import load_env
from emuru.hermes import profile
from emuru.hermes.launch import launch_native
from emuru.hermes.profile import HealthError
from emuru.telegram import queue as queue_module


def offline():
    profile.public_contract(ROOT)
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


def status(home):
    path = home / "emuru-telegram/queue.sqlite3"
    try:
        report = queue_module.diagnostics(path, health_file=path.parent / "health.json")
    except queue_module.QueueDiagnosticsError as error:
        print(json.dumps({"queue": {}, "health": [str(error)]}))
        return 1
    print(json.dumps(report))
    return 0


def launch(mode):
    profile.public_contract(ROOT)
    native = profile.installed_runtime(ROOT)
    os.environ["EMURU_HERMES_ROOT"] = str(native)
    os.environ["HERMES_HOME"] = str(profile.profile_home())
    workspace = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "emuru/workspace"
    )
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(workspace)
    arguments = [str(ROOT / "infra/hermes/telegram-runtime.py")]
    if mode != "--launch":
        arguments.append(mode)
    launch_native(native, arguments)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ("offline", "check", "status", "runtime-check", "launch", "preflight"):
        modes.add_argument("--" + name, action="store_const", const=name, dest="mode")
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "offline":
        offline()
    elif args.mode == "status":
        raise SystemExit(status(profile.profile_home()))
    elif args.mode == "preflight":
        profile.public_contract(ROOT)
        profile.installed_runtime(ROOT)
        profile.profile_home()
        print("Pinned Hermes source / bridge source contract: PASS (local)")
    else:
        launch("--" + args.mode)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- never expose private SDK exception text
        code = (
            error.code
            if isinstance(error, profile.ProfileError)
            else (str(error) if isinstance(error, HealthError) else "diagnostic_failed")
        )
        raise SystemExit("EMURU Telegram: " + code) from None
