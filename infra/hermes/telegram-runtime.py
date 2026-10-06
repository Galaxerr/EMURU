"""Run in Hermes's own environment; never import the project's MCP SDK."""

import argparse
import asyncio
import contextvars
import importlib.util
import inspect
import json
import logging
import os
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
spec = importlib.util.spec_from_file_location(
    "emuru_telegram_queue", ROOT / "src/emuru/telegram_queue.py"
)
queue_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_module)


def contract_signature(function):
    signature = inspect.signature(function, follow_wrapped=False)
    signature = signature.replace(
        parameters=[
            p.replace(annotation=p.empty) for p in signature.parameters.values()
        ],
        return_annotation=signature.empty,
    )
    return [inspect.iscoroutinefunction(function), str(signature)]


def verify_native_contract(
    Bot, Application, Updater, Adapter, Runner, *, adapter_only=False
):
    """Required guards fail startup if a dependency changes an inspected seam."""
    import telegram

    contract = json.loads(
        (ROOT / "infra/hermes/telegram-native-contract.json").read_text()
    )
    if telegram.__version__ != contract["telegram_version"]:
        raise RuntimeError("Pinned Telegram dependency version changed")
    classes = {
        cls.__name__: cls
        for cls in (Bot, Application, Updater, Adapter, Runner)
        if cls is not None
    }
    for qualified, expected in contract["methods"].items():
        name, method_name = qualified.split(".")
        if adapter_only and name != "TelegramAdapter":
            continue
        if name == "GatewayRunner" and Runner is None:
            continue  # Hermes source was already pinned by commit and source hashes.
        method = getattr(classes[name], method_name)
        if contract_signature(method) != expected:
            raise RuntimeError(f"Required native Telegram seam changed: {qualified}")


def tool_failed(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return False
    if isinstance(content, dict):
        if (
            content.get("error")
            or content.get("isError")
            or content.get("is_error")
            or content.get("success") is False
        ):
            return True
        if isinstance(content.get("content"), list):
            return any(
                tool_failed(block.get("text"))
                for block in content["content"]
                if isinstance(block, dict)
            )
    return False


class Bridge:
    """One durable worker. PTB polling and Hermes turns remain native."""

    def __init__(self, queue, dispatch, report):
        self.queue, self.dispatch, self.report = queue, dispatch, report
        self.current = contextvars.ContextVar("emuru_telegram_turn", default=None)
        self.wake = asyncio.Event()
        self.app = None
        self.worker = None
        self.retired = False
        self.native_runner = None
        self.route = None
        self.intake_failed = False
        self.health_error = None
        self.stopping = False
        self.running = asyncio.Event()
        self.bot_id = None

    def bind(self, app):
        if self.intake_failed:
            raise RuntimeError("Telegram intake stopped after a durable queue failure")
        if self.retired:
            raise RuntimeError("An interrupted native turn requires a gateway restart")
        self.app = app
        if self.bot_id is not None and self.bot_id != app.bot.id:
            raise load_diagnostics().HealthError("consumer_bot_scope_changed")
        self.bot_id = app.bot.id
        if self.worker is not None and self.worker.done():
            raise load_diagnostics().HealthError(self.health_error or "consumer_exited")
        if self.worker is None:
            self.stopping = False
            self.running.clear()
            self.worker = asyncio.create_task(self.run(), name="emuru-telegram-worker")
            self.worker.add_done_callback(self._worker_done)
        self.wake.set()

    def _worker_done(self, task):
        error = None if task.cancelled() else task.exception()
        if not self.stopping:
            # Observe exceptions without logging credential-bearing exception text.
            self.health_error = "consumer_failed" if error else "consumer_exited"
            self.intake_failed = True
            logger.error("EMURU Telegram health: %s", self.health_error)
            try:
                path = self.queue.path.parent / "health.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"health": [self.health_error]}))
                temporary.chmod(0o600)
                temporary.replace(path)
            except OSError:
                logger.error("EMURU Telegram health: consumer_health_unavailable")
            finally:
                os.kill(os.getpid(), signal.SIGTERM)

    async def require_consumer(self):
        await asyncio.sleep(0)
        if (
            self.worker is None
            or self.worker.done()
            or not self.running.is_set()
            or self.app is None
            or self.bot_id != self.app.bot.id
            or self.intake_failed
        ):
            raise load_diagnostics().HealthError(
                self.health_error or "consumer_not_running"
            )
        (self.queue.path.parent / "health.json").unlink(missing_ok=True)

    async def local_status(self, app):
        while row := self.queue.claim(
            app.bot.id, app.bot.username or "", local_status=True
        ):
            token = self.current.set(
                {"uid": row["update_id"], "notice": True, "open": True, "failed": False}
            )
            try:
                health = self.health_error or (
                    "consumer_running"
                    if self.worker and not self.worker.done() and self.running.is_set()
                    else "consumer_not_running"
                )
                await app.bot.send_message(
                    chat_id=self.queue.owner_id,
                    text="EMURU Telegram: "
                    + json.dumps(
                        {"queue": self.queue.status(), "health": [health]},
                        sort_keys=True,
                    ),
                )
            except asyncio.CancelledError:
                self.queue.mark_interrupted(row["sequence"])
                raise
            except Exception:  # noqa: BLE001 -- never expose private SDK exception text
                self.queue.finish(row["sequence"], False, "status_reply_failed")
            else:
                self.queue.finish(row["sequence"], True)
            finally:
                self.current.get()["open"] = False
                self.current.reset(token)

    async def once(self, app):
        if self.retired:
            raise RuntimeError("An interrupted native turn requires a gateway restart")
        await self.local_status(app)
        # Native startup recovery has its own inbound buffer. Wait before claim
        # so our input can never be requeued/merged there or lost on a boot crash.
        while getattr(self.native_runner, "_startup_restore_in_progress", False):
            await asyncio.sleep(0.1)
        bot, username = app.bot.id, app.bot.username or ""
        row = self.queue.claim_report(bot)
        if row:
            token = self.current.set(
                {"uid": row["update_id"], "failed": False, "notice": True, "open": True}
            )
            try:
                await self.report(app, row["update_id"])
            except Exception:  # noqa: BLE001 -- one notice attempt, no credential-bearing errors
                logger.warning("EMURU failure notice could not be delivered")
            finally:
                self.current.get()["open"] = False
                self.current.reset(token)
            return True
        route = (lambda payload: self.route(app, payload)) if self.route else None
        row = self.queue.claim(bot, username, session_key=route)
        if row is None:
            return False
        turn = {
            "uid": row["update_id"],
            "key": (bot, row["update_id"]),
            "session_key": row["session_key"],
            "failed": False,
            "notice": False,
            "open": True,
            "completed": False,
            "agent_entered": False,
            "error_code": None,
        }
        token = self.current.set(turn)
        try:
            outcome = await self.dispatch(app, queue_module.as_update(row))
        except asyncio.CancelledError:
            self.retired = True
            self.queue.mark_interrupted(row["sequence"])
            raise
        except Exception as error:  # noqa: BLE001 -- errors become terminal without replay
            logger.warning("EMURU Telegram turn failed; it will not be replayed")
            if "--runtime-check" in sys.argv:
                frame = traceback.extract_tb(error.__traceback__)[-1]
                logger.warning(
                    "Native turn check: %s at %s:%d",
                    type(error).__name__,
                    Path(frame.filename).name,
                    frame.lineno,
                )
            self.queue.finish(
                row["sequence"], False, queue_module.safe_error_code(error)
            )
        else:
            if turn["error_code"] == "native_cancelled":
                self.retired = True
                self.queue.mark_interrupted(row["sequence"])
            else:
                self.queue.finish(
                    row["sequence"],
                    outcome is True and not turn["failed"],
                    turn["error_code"],
                )
        finally:
            turn["open"] = False
            self.current.reset(token)
        return True

    async def run(self):
        self.running.set()
        while True:
            self.wake.clear()
            while await self.once(self.app):
                pass
            await self.wake.wait()

    async def stop(self):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        self.worker = None
        self.running.clear()


def install_guards(
    bridge, *, Bot, Application, Updater, Adapter, Runner, Update, MessageType, receipts
):
    """Pinned seams, also exercised with fake PTB/Hermes classes in tests."""
    if getattr(Application, "_emuru_guarded", False):
        raise RuntimeError("Telegram bridge already installed")
    policy, owner = bridge.queue.settings, bridge.queue.owner_id
    get_updates, post = Bot.get_updates, Bot._post
    native_update = Application.process_update
    start_polling = Updater.start_polling
    runner_handle, agent_turn = (
        Runner._handle_message,
        Runner._handle_message_with_agent,
    )
    resume_candidates = Runner._resume_pending_candidates
    runner_init = Runner.__init__
    native_run_agent = Runner._run_agent
    native_agent_error = Runner._hmwa_agent_error_reply
    native_reset = Runner._handle_reset_command
    polling_request = contextvars.ContextVar("emuru_native_poll_request", default=False)
    poll_after = 0

    def turn_for(event=None):
        turn = bridge.current.get()
        if not turn or not turn["open"] or turn["notice"]:
            raise RuntimeError("Telegram dispatch requires a durable claim")
        if event is not None:
            source = event.source
            if (
                getattr(event, "internal", False)
                or event.platform_update_id != turn["uid"]
                or str(source.chat_id) != str(owner)
                or str(source.user_id) != str(owner)
                or source.chat_type != "dm"
                or getattr(source.platform, "value", source.platform) != "telegram"
            ):
                raise RuntimeError("Unclaimed Telegram event rejected")
        return turn

    def owned_runner(runner, *args, **kwargs):
        runner_init(runner, *args, **kwargs)
        bridge.native_runner = runner

    async def staged_poll(bot, *args, **kwargs):
        nonlocal poll_after
        await asyncio.sleep(max(0, poll_after - time.monotonic()))
        # ponytail: one synchronous FULL SQLite commit per <=100-update poll batch;
        # move this connection to a dedicated thread only if fsync latency matters.
        if bridge.intake_failed:
            raise RuntimeError("Telegram intake stopped after a durable queue failure")
        try:
            token = polling_request.set(True)
            try:
                updates = await get_updates(bot, *args, **kwargs)
            finally:
                polling_request.reset(token)
        except Exception:
            poll_after = time.monotonic() + policy["reconnect_delay_seconds"]
            raise
        try:
            if bridge.bot_id is not None and bridge.bot_id != bot.id:
                raise load_diagnostics().HealthError("consumer_bot_scope_changed")
            bridge.queue.stage(
                bot.id, [json.loads(u.to_json()) for u in updates], bot.username or ""
            )
        except Exception:  # noqa: BLE001 -- fail closed before the native offset can advance
            bridge.intake_failed = True
            logger.error("EMURU Telegram queue persistence failed; intake stopped")
            os.kill(os.getpid(), signal.SIGTERM)
            raise RuntimeError(
                "Telegram queue persistence failed; intake stopped"
            ) from None
        bridge.wake.set()
        return updates

    async def guarded_update(app, update):
        if not isinstance(update, Update):
            raise TypeError("Non-Telegram update rejected")
        turn = bridge.current.get()
        if not turn or not turn["open"] or turn["notice"]:
            # Polling has already staged this input; only the durable worker can
            # enter native admission and handlers. Rejections never reach PTB groups.
            admitted = queue_module.admit_update(
                json.loads(update.to_json()),
                owner,
                policy,
                bridge.queue.now(),
                app.bot.username or "",
            )
            if admitted is not None:
                await bridge.local_status(app)
            return
        if update.update_id != turn["uid"]:
            raise RuntimeError("Unclaimed Telegram update rejected")
        raw = json.loads(update.to_json())
        admitted = queue_module.admit_update(
            raw, owner, policy, bridge.queue.now(), app.bot.username or ""
        )
        if admitted is None:
            raise RuntimeError("Telegram dispatch admission changed")
        # Native PTB receipt claims, auth checks, handler groups and context scopes
        # remain underneath the stricter durable gate, with reference media stripped.
        return await native_update(app, Update.de_json(admitted, app.bot))

    async def polling(updater, *args, **kwargs):
        if args:
            raise RuntimeError("Unexpected positional polling options")
        kwargs.update(
            timeout=policy["poll_timeout_seconds"],
            poll_interval=0,
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
        return await start_polling(updater, **kwargs)

    def guard_adapter(cls):
        if cls.__dict__.get("_emuru_adapter_guarded", False):
            return
        connect, disconnect, handle = cls.connect, cls.disconnect, cls.handle_message
        processing_hook = cls._run_processing_hook

        async def guarded_connect(adapter, *args, **kwargs):
            if bridge.retired:
                raise RuntimeError(
                    "An interrupted native turn requires a gateway restart"
                )
            extra = adapter.config.extra
            if (
                extra.get("drop_pending_on_cold_boot") is not False
                or type(extra.get("max_concurrent_updates")) is not int
                or extra["max_concurrent_updates"] != 1
            ):
                raise RuntimeError("Native Telegram admission settings changed")
            result = await connect(adapter, *args, **kwargs)
            if result:
                bridge.bind(adapter._app)
            return result

        async def guarded_disconnect(adapter, *args, **kwargs):
            await bridge.stop()
            return await disconnect(adapter, *args, **kwargs)

        async def awaited_handle(adapter, event):
            turn = turn_for(event)
            key = adapter._event_session_key(event)
            if turn.get("session_key") is not None and key != turn["session_key"]:
                raise RuntimeError("Native Telegram session route changed after claim")
            event.metadata = {
                **(getattr(event, "metadata", None) or {}),
                "gateway_session_key": key,
            }
            event.auto_skill = event.channel_prompt = event.reply_to_text = None
            if adapter._active_sessions:
                raise RuntimeError("Unexpected active native Telegram turn")
            await handle(adapter, event)
            key = adapter._event_session_key(event)
            task = adapter._session_tasks.get(key)
            if task is not None:
                await task
            if not turn["completed"] or adapter._pending_messages:
                raise RuntimeError("Native turn did not complete inline")

        async def hook(adapter, name, *args, **kwargs):
            turn = bridge.current.get()
            if name == "on_processing_complete" and turn and turn["open"]:
                outcome = getattr(args[1], "value", args[1])
                turn["completed"] = True
                turn["failed"] |= outcome != "success"
                if outcome != "success":
                    turn["error_code"] = turn.get("error_code") or "native_" + (
                        outcome if outcome in {"failure", "cancelled"} else "failure"
                    )
            return await processing_hook(adapter, name, *args, **kwargs)

        cls.connect, cls.disconnect = guarded_connect, guarded_disconnect
        cls.handle_message, cls._run_processing_hook = awaited_handle, hook
        cls._emuru_adapter_guarded = True

    async def guarded_runner(runner, event):
        turn_for(event)
        return await runner_handle(runner, event)

    async def guarded_agent(runner, event, *args, **kwargs):
        turn = turn_for(event)
        if turn["agent_entered"]:
            raise RuntimeError("A Telegram update cannot start a second agent turn")
        turn["agent_entered"] = True
        return await agent_turn(runner, event, *args, **kwargs)

    async def control_new(runner, event, source, _quick_key):
        turn = turn_for(event)
        key = runner._session_key_for_source(source)
        if key != turn["session_key"]:
            raise RuntimeError("Native session reset route changed after claim")
        old = runner.session_store._entries.get(key)
        old_id = old.session_id if old else None
        # Native auth and command access checks run before this handler. The
        # durable FIFO already waits for active work; retain native reset/cache
        # cleanup, but skip its callback confirmation on this text-only surface.
        await native_reset(runner, event)
        new = runner.session_store._entries.get(key)
        if new is None or new.session_id == old_id:
            raise load_diagnostics().HealthError("session_transition_failed")
        return True, "New session started. Previous conversation preserved."

    async def no_onboarding(runner, source, history, turn_sidecar_notes):
        turn_for()
        # This owner-only deployment has no sethome/profile-build workflow.

    async def checked_result(runner, *args, **kwargs):
        turn = turn_for()
        result = await native_run_agent(runner, *args, **kwargs)
        if not isinstance(result, dict) or result.get("failed") or result.get("error"):
            turn["failed"], turn["error_code"] = True, "provider_failed"
        elif any(
            message.get("role") == "tool" and tool_failed(message.get("content"))
            for message in result.get("messages", [])[result.get("history_offset", 0) :]
            if isinstance(message, dict)
        ):
            turn["failed"], turn["error_code"] = True, "tool_failed"
        return result

    async def checked_agent_error(runner, error, *args, **kwargs):
        turn = turn_for()
        turn["failed"], turn["error_code"] = True, queue_module.safe_error_code(error)
        return await native_agent_error(runner, error, *args, **kwargs)

    def guarded_resume(runner, *args, **kwargs):
        candidates = resume_candidates(runner, *args, **kwargs)
        if candidates is None:
            return None
        # Retain native history/recovery, but never synthesize another Telegram
        # model turn after a crash that could have committed a vault write.
        return [
            entry
            for entry in candidates
            if getattr(entry.origin.platform, "value", entry.origin.platform)
            != "telegram"
        ]

    async def guarded_post(bot, endpoint, data=None, *args, **kwargs):
        data = data or {}
        if endpoint == "getUpdates" and (
            not polling_request.get() or bridge.intake_failed
        ):
            raise RuntimeError("Telegram polling must pass through durable staging")
        safe_global = {
            "getMe",
            "getUpdates",
            "getWebhookInfo",
            "getMyCommands",
            "getMyDescription",
            "getMyShortDescription",
            "setMyDescription",
            "setMyShortDescription",
            "setMyCommands",
            "deleteMyCommands",
            "deleteWebhook",
        }
        if endpoint == "deleteWebhook" and data.get("drop_pending_updates"):
            raise RuntimeError("Dropping pending Telegram input is forbidden")
        if endpoint == "setMyCommands":
            data = dict(data)
            data["commands"] = [
                cmd
                for cmd in data.get("commands", [])
                if (cmd.get("command") if isinstance(cmd, dict) else cmd.command)
                in policy["allowed_commands"]
            ]
        if endpoint not in safe_global:
            turn = bridge.current.get()
            if (
                endpoint
                not in {
                    "sendMessage",
                    "editMessageText",
                    "sendChatAction",
                    "setMessageReaction",
                }
                or str(data.get("chat_id")) != str(owner)
                or not turn
                or not turn["open"]
                or turn["failed"]
            ):
                raise RuntimeError("Telegram egress rejected")
            try:
                return await post(bot, endpoint, data, *args, **kwargs)
            except asyncio.CancelledError:
                turn["failed"] = True
                raise
            except Exception:  # noqa: BLE001 -- suppress credential-bearing SDK errors
                # Native formatting/thread/network fallbacks may try again. An
                # unknown send outcome latches the whole turn against resends.
                turn["failed"] = True
                raise RuntimeError(
                    "Telegram egress outcome is uncertain; no retry"
                ) from None
        return await post(bot, endpoint, data, *args, **kwargs)

    def route(app, payload):
        update = Update.de_json(payload, app.bot)
        kind = (
            MessageType.COMMAND
            if payload["message"]["text"].startswith("/")
            else MessageType.TEXT
        )
        event = app.adapter._build_message_event(
            update.message, kind, update_id=update.update_id
        )
        return app.adapter._event_session_key(event)

    async def dispatch(app, payload):
        adapter = app.adapter
        receipts._load_receipts(adapter, app.bot.id)
        receipt_key = f"{app.bot.id}:{payload['update_id']}"
        if receipt_key in adapter._seen_update_ids:
            return True
        await app.process_update(Update.de_json(payload, app.bot))
        # Native text handlers detach a batch-flush task. With one admitted update
        # at a time it cannot merge a second request; await that task and its turn.
        batches = list(adapter._pending_text_batch_tasks.values())
        if batches:
            await asyncio.gather(*batches)
        turn = bridge.current.get()
        if not turn["completed"]:
            raise RuntimeError("Native Telegram handler did not complete a turn")
        return not turn["failed"]

    native_factory = Runner._create_adapter

    def guarded_factory(runner, platform, config):
        adapter = native_factory(runner, platform, config)
        if getattr(platform, "value", platform) == "telegram" and adapter is not None:
            cls = type(adapter)
            if not cls.__dict__.get("_emuru_adapter_guarded", False):
                # The plugin registry loads the same pinned source in its own namespace.
                if Path(inspect.getfile(cls)).resolve() != (
                    Path(inspect.getfile(Adapter)).resolve()
                ):
                    raise load_diagnostics().HealthError(
                        "telegram_adapter_contract_changed"
                    )
                verify_native_contract(
                    Bot, Application, Updater, cls, None, adapter_only=True
                )
                guard_adapter(cls)
        return adapter

    Runner._create_adapter = guarded_factory
    bridge.route = route
    bridge.dispatch = dispatch
    Application._emuru_guarded = True
    Bot.get_updates, Bot._post = staged_poll, guarded_post
    Application.process_update, Updater.start_polling = guarded_update, polling
    guard_adapter(Adapter)
    Runner._handle_message, Runner._handle_message_with_agent = (
        guarded_runner,
        guarded_agent,
    )
    Runner._resume_pending_candidates = guarded_resume
    Runner.__init__ = owned_runner
    Runner._run_agent = checked_result
    Runner._hmwa_agent_error_reply = checked_agent_error
    Runner._hm_cmd_new = control_new
    Runner._hmwa_first_contact_notes = no_onboarding


def runtime_identity():
    import gateway

    diagnostic = load_diagnostics()
    expected = diagnostic.installed_runtime()
    native = Path(gateway.__file__).resolve().parents[1]
    if native != expected:
        raise diagnostic.HealthError("runtime_import_location_mismatch")
    return native


def load_diagnostics():
    if "emuru_telegram_diagnostics" in sys.modules:
        return sys.modules["emuru_telegram_diagnostics"]
    spec = importlib.util.spec_from_file_location(
        "emuru_telegram_diagnostics", ROOT / "scripts/hermes-telegram.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_functions(run, gateway_cli):
    contract = json.loads(
        (ROOT / "infra/hermes/telegram-native-contract.json").read_text()
    )
    for qualified, expected in contract["functions"].items():
        if qualified.startswith("gateway.run."):
            module = run
        elif qualified.startswith("agent.system_prompt."):
            from agent import system_prompt as module
        else:
            module = gateway_cli
        function = getattr(module, qualified.rsplit(".", 1)[1])
        if contract_signature(function) != expected:
            raise RuntimeError("Required native Telegram launch seam changed")


def restrict_prompt(system_prompt):
    # Native system guidance teaches a marker used only by mid-turn /steer.
    # FIFO admission never steers a running agent; omit that unused example at
    # prompt construction, leaving user messages and generated replies intact.
    system_prompt.STEER_CHANNEL_NOTE = ""


def audit_live_profile():
    """Read the native profile without model requests, MCP calls or config writes."""
    from hermes_cli.config import read_user_config_raw

    diagnostic = load_diagnostics()
    expected, server = diagnostic.public_contract()
    actual = read_user_config_raw()
    model = actual.get("model", {})
    if not isinstance(model, dict) or model.get("api_key") or model.get("key_env"):
        raise diagnostic.HealthError("inline_model_credentials")
    # Metadata-based cloud aliases have the same reviewed route with cloud bounds.
    if expected["model.provider"] == "ollama":
        from emuru.ollama import provider_settings

        context = model.get("ollama_num_ctx")
        if type(context) is not int or context not in (0, 65536):
            raise diagnostic.HealthError("profile_model_route_mismatch")
        expected.update(
            provider_settings(
                expected["model.default"],
                expected["model.base_url"],
                cloud=context == 0,
            )
        )
    for key, value in expected.items():
        cursor = actual
        for part in key.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if type(cursor) is not type(value) or cursor != value:
            raise diagnostic.HealthError("profile_settings_mismatch")
    if actual.get("mcp_servers") != {"vault": server}:
        raise diagnostic.HealthError("profile_mcp_registration_mismatch")
    return server


def vault_tools_ready():
    from tools import mcp_tool
    from tools.mcp_tool_scope import _server_key

    expected = {
        "vault_map",
        "vault_search",
        "vault_open",
        "vault_neighbors",
        "vault_write",
    }
    with mcp_tool._lock:
        server = mcp_tool._servers.get(_server_key("vault"))
        return bool(
            server is not None
            and server.session is not None
            and server._error is None
            and server._task is not None
            and not server._task.done()
            and {tool.name for tool in server._tools} == expected
            and set(server._registered_tool_names)
            == {f"mcp__vault__{name}" for name in expected}
        )


def install_launch_guards(run, gateway_cli, queue, config, bridge):
    """Native discovery is best-effort; the required MCP gate must fail closed."""
    native_start, native_exit = run.start_gateway, run._exit_after_graceful_shutdown
    runner_start = run.GatewayRunner.start
    native_attach = gateway_cli._attach_to_host_gateway_or_guard

    def refuse_host():
        from gateway.host_attach import host_gateway

        if host_gateway() is not None:
            raise load_diagnostics().HealthError("unguarded_host_gateway_running")

    async def guarded_host(replace, force=False):
        refuse_host()

    def guarded_attach(*args, **kwargs):
        refuse_host()
        return native_attach(*args, **kwargs)

    async def guarded_start(**kwargs):
        # CLI imports may reload dotenv; keep native auth narrowed to the owner.
        os.environ["TELEGRAM_ALLOWED_USERS"] = str(queue.owner_id)
        os.environ["TELEGRAM_ALLOW_ALL_USERS"] = "false"
        kwargs["config"] = config
        return await native_start(**kwargs)

    async def ready_start(runner):
        if not vault_tools_ready():
            raise load_diagnostics().HealthError("vault_mcp_not_ready")
        result = await runner_start(runner)
        if not result:
            raise load_diagnostics().HealthError("native_start_failed")
        await bridge.require_consumer()
        print(
            "EMURU Telegram checkpoint: required guards and five MCP tools READY",
            flush=True,
        )
        return result

    def closed_exit(code):
        try:
            queue.close()
        finally:
            native_exit(code)

    run._host_attach_or_none = guarded_host
    gateway_cli._attach_to_host_gateway_or_guard = guarded_attach
    run.start_gateway = guarded_start
    run.GatewayRunner.start = ready_start
    run._exit_after_graceful_shutdown = closed_exit


def private_config():
    from gateway.config import Platform, load_gateway_config

    health_error = load_diagnostics().HealthError
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
    load_diagnostics().public_contract()
    runtime_identity()

    settings = queue_module.load_telegram_settings(
        ROOT / "infra/hermes/telegram-settings.json"
    )
    from gateway.platforms.event import MessageType
    from plugins.platforms.telegram import update_admission
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot, Update
    from telegram.ext import Updater

    verify_native_contract(
        Bot, update_admission.TelegramApplication, Updater, TelegramAdapter, None
    )
    # Only after local source/SDK checks: native gateway.run uses Hermes's dotenv
    # loader for this profile. Credentials are never executable shell input.
    from gateway import run
    from hermes_cli import gateway as gateway_cli

    verify_native_contract(
        Bot,
        update_admission.TelegramApplication,
        Updater,
        TelegramAdapter,
        run.GatewayRunner,
    )
    verify_functions(run, gateway_cli)
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

    restrict_prompt(system_prompt)

    if args.runtime_check:
        # Resolve/install every seam against the actual installed dependency
        # generation, without Telegram credentials, polling, or model calls.
        with tempfile.TemporaryDirectory(prefix="emuru-telegram-native-") as directory:
            queue = queue_module.TelegramQueue(
                Path(directory) / "queue.sqlite3", settings, 42
            )
            bridge = Bridge(queue, None, None)
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
            install_guards(bridge, **guard_classes)
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
                await bridge.require_consumer()
                while queue.status() != {"completed": 2}:
                    if bridge.worker.done():
                        await bridge.require_consumer()
                    await asyncio.sleep(0.01)
                await bridge.stop()
                if (
                    calls != ["first", "second"]
                    or len(sent) != 2
                    or queue.status() != {"completed": 2}
                ):
                    raise RuntimeError(
                        "Native background turn/delivery contract changed"
                    )
                case = load_diagnostics().load_module(
                    "emuru_native_session_case",
                    ROOT / "tests/telegram/native_session_case.py",
                )
                await case.check(run, adapter, bridge, directory)
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
        bridge = Bridge(queue, None, None)
        install_guards(bridge, **guard_classes)

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
        raise RuntimeError("Start Telegram through scripts/hermes-telegram.sh")
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

    async def report(app, uid):
        await app.bot.send_message(
            chat_id=owner,
            text=f"Telegram update {uid} failed or was interrupted. Its effects may be uncertain; it was not replayed. Check the vault before sending it again.",
        )

    bridge = Bridge(queue, None, report)
    install_guards(bridge, **guard_classes)
    try:
        sys.argv = ["hermes", "-p", "emuru", "gateway", "run"]
        # Keep the CLI in this process: a supervisor exec would discard the guards.
        os.environ["HERMES_GATEWAY_NO_SUPERVISE"] = "1"
        profile = Path(os.environ["HERMES_HOME"]).resolve()
        from hermes_cli import main as native_cli

        if Path(os.environ["HERMES_HOME"]).resolve() != profile:
            raise load_diagnostics().HealthError("native_profile_changed")
        install_launch_guards(run, gateway_cli, queue, config, bridge)
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
        diagnostic = load_diagnostics()
        code = (
            str(error)
            if isinstance(error, diagnostic.HealthError)
            else "guarded_runtime_failed"
        )
        raise SystemExit("EMURU Telegram: " + code) from None
