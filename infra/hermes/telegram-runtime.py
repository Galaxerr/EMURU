"""Run in Hermes's own environment; never import the project's MCP SDK."""

import argparse
import asyncio
import importlib.util
import inspect
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Only stdlib EMURU modules enter Hermes's independently installed environment.
sys.path.insert(0, str(ROOT / "src"))
from emuru.hermes import profile
from emuru.hermes.profile import HealthError
from emuru.telegram import native
from emuru.telegram import queue as queue_module


def runtime_identity():
    import gateway

    expected = profile.installed_runtime(ROOT)
    native = Path(gateway.__file__).resolve().parents[1]
    if native != expected:
        raise HealthError("runtime_import_location_mismatch")
    return native


def audit_live_profile():
    """Native config access only; the shared module owns profile verification."""
    from hermes_cli.config import read_user_config_raw

    try:
        return profile.audit_native_profile(ROOT, read_user_config_raw())
    except profile.ProfileError as error:
        raise HealthError(error.code) from None


def private_config():
    from gateway.config import Platform, load_gateway_config

    health_error = HealthError
    config = load_gateway_config()
    telegram = config.platforms.get(Platform.TELEGRAM)
    owner = os.environ.get("EMURU_TELEGRAM_OWNER_ID", "")
    if not owner.isascii() or not owner.isdecimal() or int(owner) <= 0:
        raise health_error("owner_identity_missing")
    if telegram is None or not telegram.token:
        raise health_error("telegram_credentials_missing")
    if (
        os.environ.get("TELEGRAM_WEBHOOK_URL")
        or telegram.extra.get("webhook_url")
        or telegram.extra.get("base_url")
        or config.multiplex_profiles
        or any(
            platform != Platform.TELEGRAM and value.enabled
            for platform, value in config.platforms.items()
        )
    ):
        raise health_error("transport_policy_mismatch")
    if (
        telegram.extra.get("drop_pending_on_cold_boot") is not False
        or type(telegram.extra.get("max_concurrent_updates")) is not int
        or telegram.extra.get("max_concurrent_updates") != 1
    ):
        raise health_error("native_admission_policy_mismatch")
    # Model routing and authorization still use Hermes's native machinery; the
    # admission boundary has the same single private owner, never a public ID.
    os.environ["TELEGRAM_ALLOWED_USERS"] = owner
    os.environ["TELEGRAM_ALLOW_ALL_USERS"] = "false"
    telegram.enabled = True
    config.multiplex_profiles = False
    config.platforms = {Platform.TELEGRAM: telegram}
    return config, int(owner)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--runtime-check", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    profile.public_contract(ROOT)
    runtime_identity()

    settings = queue_module.load_telegram_settings(
        ROOT / "infra/hermes/telegram-settings.json"
    )
    from gateway.platforms.event import MessageType
    from plugins.platforms.telegram import update_admission
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot, Update
    from telegram.ext import Updater

    native.verify_native_contract(
        ROOT, Bot, update_admission.TelegramApplication, Updater, TelegramAdapter, None
    )
    # Only after local source/SDK checks: native gateway.run uses Hermes's dotenv
    # loader for this profile. Credentials are never executable shell input.
    from gateway import run
    from hermes_cli import gateway as gateway_cli

    native.verify_native_contract(
        ROOT,
        Bot,
        update_admission.TelegramApplication,
        Updater,
        TelegramAdapter,
        run.GatewayRunner,
    )
    native.verify_functions(ROOT, run, gateway_cli)
    guard_classes = {
        "Bot": Bot,
        "Application": update_admission.TelegramApplication,
        "Updater": Updater,
        "Adapter": TelegramAdapter,
        "Runner": run.GatewayRunner,
        "Update": Update,
        "MessageType": MessageType,
        "receipts": update_admission,
    }
    from agent import system_prompt

    native.restrict_prompt(system_prompt)

    if args.runtime_check:
        # Resolve/install every seam against the actual installed dependency
        # generation, without Telegram credentials, polling, or model calls.
        with tempfile.TemporaryDirectory(prefix="emuru-telegram-native-") as directory:
            queue = queue_module.TelegramQueue(
                Path(directory) / "queue.sqlite3", settings, 42
            )
            from gateway.config import PlatformConfig
            from telegram import User
            from telegram.ext import Application

            calls, sent = [], []

            async def fake_post(bot, endpoint, data=None, **kwargs):
                if endpoint == "sendMessage":
                    sent.append(data["text"])
                    return {
                        "message_id": len(sent),
                        "date": 100000,
                        "chat": {"id": 42, "type": "private"},
                        "text": data["text"],
                    }
                return True

            # Exercise real PTB encoding and the native adapter's detached turn
            # lifecycle, replacing only HTTP and the model-facing callback.
            Bot._post = fake_post
            # Native plugins load their adapter under a second module namespace.
            alias = importlib.util.spec_from_file_location(
                "plugins.platforms.telegram.emuru_replay_adapter",
                inspect.getfile(TelegramAdapter),
            )
            plugin = importlib.util.module_from_spec(alias)
            sys.modules[alias.name] = plugin
            alias.loader.exec_module(plugin)

            async def synthetic_connect(self, *, is_reconnect=False):
                return True

            plugin.TelegramAdapter.connect = synthetic_connect
            adapter = plugin.TelegramAdapter(
                PlatformConfig(
                    token="7:SYNTHETIC",
                    extra={
                        "drop_pending_on_cold_boot": False,
                        "max_concurrent_updates": 1,
                    },
                )
            )
            bot = Bot("7:SYNTHETIC")
            bot._bot_user = User(7, "Synthetic", True, username="synthetic")
            adapter._bot = bot
            app = (
                Application.builder()
                .bot(bot)
                .application_class(
                    update_admission.TelegramApplication, {"adapter": adapter}
                )
                .build()
            )
            app._initialized = True  # Synthetic Bot already has its getMe identity.
            adapter._app = app
            adapter._register_handlers(app)
            os.environ["TELEGRAM_ALLOWED_USERS"] = "42"
            os.environ["TELEGRAM_ALLOW_ALL_USERS"] = "false"

            async def fake_agent(event):
                calls.append(event.text)
                await asyncio.sleep(0)
                return "synthetic reply " + event.text

            adapter._message_handler = fake_agent
            adapter._update_receipt_dir = Path(directory) / "native-receipts"
            execution = native.NativeTelegram(queue, ROOT, app=app, **guard_classes)
            worker = execution.worker
            runner = object.__new__(run.GatewayRunner)
            runner._instantiate_adapter = lambda platform, config: adapter
            adapter = runner._create_adapter(run.Platform.TELEGRAM, adapter.config)

            async def native_replay():
                now = int(time.time())
                updates = [
                    {
                        "update_id": uid,
                        "message": {
                            "message_id": uid,
                            "date": now,
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 42, "is_bot": False},
                            "text": text,
                        },
                    }
                    for uid, text in ((1, "first"), (2, "second"))
                ]
                queue.stage(7, updates, "synthetic")
                await adapter.connect()
                await worker.require_consumer()
                while queue.status() != {"completed": 2}:
                    await worker.require_consumer()
                    await asyncio.sleep(0.01)
                await worker.stop()
                if (
                    calls != ["first", "second"]
                    or len(sent) != 2
                    or queue.status() != {"completed": 2}
                ):
                    raise RuntimeError(
                        "Native background turn/delivery contract changed"
                    )
                sys.path.insert(0, str(ROOT / "tests/telegram"))
                import native_session_case as case

                await case.check(run, adapter, worker, directory)
                case.check_tools(execution, directory)
                if len(sent) != 5 or sent[3].replace("\\.", ".") != (
                    "New session started. Previous conversation preserved."
                ):
                    raise RuntimeError(
                        "Native session reset/onboarding contract changed"
                    )
                await adapter.disconnect()

            asyncio.run(asyncio.wait_for(native_replay(), timeout=15))
            queue.close()
        print("Pinned Hermes runtime / native Telegram guard seams: PASS (no network)")
        return
    config, owner = private_config()
    audit_live_profile()
    if args.check:
        # Install the same required guards in a transient queue; do not recover,
        # open or modify the profile queue, start adapters, or connect MCP.
        temporary = tempfile.TemporaryDirectory(prefix="emuru-telegram-check-")
        queue = queue_module.TelegramQueue(
            Path(temporary.name) / "queue.sqlite3", settings, owner
        )
        execution = native.NativeTelegram(queue, ROOT, **guard_classes)

        async def diagnostic():
            from telegram import Bot

            async with Bot(config.platforms[run.Platform.TELEGRAM].token) as bot:
                me = bot.bot
                webhook = await bot.get_webhook_info()
                if (
                    me.is_bot is not True
                    or type(me.id) is not int
                    or me.id <= 0
                    or webhook.url
                ):
                    raise RuntimeError(
                        "Telegram bot identity or polling mode is invalid"
                    )
            print(
                "Telegram guarded compatibility / live profile / bot identity / webhook status: PASS (no inference or polling)"
            )

        try:
            asyncio.run(diagnostic())
        finally:
            queue.close()
            temporary.cleanup()
        return
    # Launcher lock must survive exec into this installed runtime.
    import fcntl

    fd = int(os.environ.get("EMURU_TELEGRAM_LOCK_FD", "-1"))
    if fd < 0:
        raise RuntimeError("Start Telegram through scripts/hermes/telegram.sh")
    expected_lock = (
        Path(os.environ["HERMES_HOME"]) / "emuru-telegram/instance.lock"
    ).stat()
    actual_lock = os.fstat(fd)
    if (actual_lock.st_dev, actual_lock.st_ino) != (
        expected_lock.st_dev,
        expected_lock.st_ino,
    ):
        raise RuntimeError(
            "The native gateway must inherit the launcher's lifetime lock"
        )
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    queue = queue_module.TelegramQueue(
        Path(os.environ["HERMES_HOME"]) / "emuru-telegram/queue.sqlite3",
        settings,
        owner,
        lock_fd=fd,
    )
    queue.recover()

    execution = native.NativeTelegram(queue, ROOT, **guard_classes)
    from agent import tool_executor
    from run_agent import AIAgent

    from emuru.hermes.inference import install_guard

    install_guard(AIAgent, permitted=execution.inference_permitted)
    execution.install_tool_guard(tool_executor)
    try:
        sys.argv = ["hermes", "-p", "emuru", "gateway", "run"]
        # Keep the CLI in this process: a supervisor exec would discard the guards.
        os.environ["HERMES_GATEWAY_NO_SUPERVISE"] = "1"
        profile_path = Path(os.environ["HERMES_HOME"]).resolve()
        from hermes_cli import main as native_cli

        if Path(os.environ["HERMES_HOME"]).resolve() != profile_path:
            raise HealthError("native_profile_changed")
        execution.install_launch_guards(run, gateway_cli, config)
        native_cli.main()

    finally:
        queue.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- private SDK errors can contain credentials
        # Telegram SDK exceptions may contain the token, URL, or owner's ID.
        if "--runtime-check" in sys.argv:
            frame = traceback.extract_tb(error.__traceback__)[-1]
            print(
                f"Native seam check failed: {type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}",
                file=sys.stderr,
            )
        code = (
            error.code
            if isinstance(error, profile.ProfileError)
            else (
                str(error)
                if isinstance(error, HealthError)
                else "guarded_runtime_failed"
            )
        )
        raise SystemExit("EMURU Telegram: " + code) from None
