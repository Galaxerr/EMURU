"""Pinned Hermes/PTB execution adapter; all native state stays behind this seam."""

import asyncio
import contextvars
import inspect
import json
import logging
import os
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from emuru.hermes.profile import HealthError
from emuru.telegram import queue as queue_module
from emuru.telegram.worker import TelegramWorker, TurnResult

logger = logging.getLogger(__name__)


@dataclass
class _Turn:
    uid: int
    session_key: str | None = None
    notice: bool = False
    open: bool = True
    failed: bool = False
    completed: bool = False
    agent_entered: bool = False
    error_code: str | None = None


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
    root, Bot, Application, Updater, Adapter, Runner, *, adapter_only=False
):
    """Required guards fail startup if a dependency changes an inspected seam."""
    import telegram

    contract = json.loads(
        (root / "infra/hermes/telegram-native-contract.json").read_text()
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


def verify_functions(root, run, gateway_cli):
    contract = json.loads(
        (root / "infra/hermes/telegram-native-contract.json").read_text()
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


class NativeTelegram:
    """Own native guards, turn scopes, task completion, routing and process health."""

    def __init__(self, queue, root, *, app=None, **classes):
        self.queue, self.root = queue, root
        self._app, self._runner = None, None
        self._current = contextvars.ContextVar("emuru_native_turn", default=None)
        self._update = classes["Update"]
        self._message_type = classes["MessageType"]
        self._receipts = classes["receipts"]
        self.worker = TelegramWorker(queue, self, health=self._health)
        if app is not None:
            self._attach(app)
        self._install_guards(**classes)

    def _attach(self, app):
        self._app = app
        runner = getattr(app.adapter, "gateway_runner", None)
        if runner is not None:
            self._runner = runner

    def identity(self):
        if self._app is None:
            raise HealthError("consumer_not_running")
        return self._app.bot.id, self._app.bot.username or ""

    async def wait_ready(self):
        # Native startup recovery has its own inbound buffer. Never claim into it.
        while getattr(self._runner, "_startup_restore_in_progress", False):
            await asyncio.sleep(0.1)

    def route(self, payload):
        app = self._app
        update = self._update.de_json(payload, app.bot)
        kind = (
            self._message_type.COMMAND
            if payload["message"]["text"].startswith("/")
            else self._message_type.TEXT
        )
        event = app.adapter._build_message_event(
            update.message, kind, update_id=update.update_id
        )
        return app.adapter._event_session_key(event)

    @contextmanager
    def _turn_scope(self, uid, session_key=None, *, notice=False):
        turn = _Turn(uid, session_key, notice)
        token = self._current.set(turn)
        try:
            yield turn
        finally:
            # Detached tasks inherit this object. Closing it prevents late egress.
            turn.open = False
            self._current.reset(token)

    async def execute(self, payload, session_key):
        app, adapter = self._app, self._app.adapter
        with self._turn_scope(payload["update_id"], session_key) as turn:
            try:
                self._receipts._load_receipts(adapter, app.bot.id)
                if f"{app.bot.id}:{payload['update_id']}" in adapter._seen_update_ids:
                    return TurnResult(True)
                await app.process_update(self._update.de_json(payload, app.bot))
                # One durable input cannot merge with a second native text batch.
                batches = list(adapter._pending_text_batch_tasks.values())
                if batches:
                    await asyncio.gather(*batches)
                if not turn.completed:
                    raise RuntimeError(
                        "Native Telegram handler did not complete a turn"
                    )
            except Exception as error:
                if "--runtime-check" in sys.argv:
                    frame = traceback.extract_tb(error.__traceback__)[-1]
                    logger.warning(
                        "Native turn check: %s at %s:%d",
                        type(error).__name__,
                        Path(frame.filename).name,
                        frame.lineno,
                    )
                raise
            return TurnResult(
                not turn.failed, turn.error_code, turn.error_code == "native_cancelled"
            )

    async def notice(self, uid, status=None):
        text = (
            ("EMURU Telegram: " + json.dumps(status, sort_keys=True))
            if status is not None
            else (
                f"Telegram update {uid} failed or was interrupted. Its effects may be uncertain; "
                "it was not replayed. Check the vault before sending it again."
            )
        )
        with self._turn_scope(uid, notice=True):
            await self._app.bot.send_message(chat_id=self.queue.owner_id, text=text)

    def _health(self, code):
        path = self.queue.path.parent / "health.json"
        if code is None:
            path.unlink(missing_ok=True)
            return
        logger.error("EMURU Telegram health: %s", code)
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"health": [code]}))
            temporary.chmod(queue_module.PRIVATE_FILE_MODE)
            temporary.replace(path)
        except OSError:
            logger.error("EMURU Telegram health: consumer_health_unavailable")
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    def _install_guards(
        self,
        *,
        Bot,
        Application,
        Updater,
        Adapter,
        Runner,
        Update,
        MessageType,
        receipts,
    ):
        """Pinned seams, also exercised with fake PTB/Hermes classes in tests."""
        if getattr(Application, "_emuru_guarded", False):
            raise RuntimeError("Telegram bridge already installed")
        policy, owner = self.queue.settings, self.queue.owner_id
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
        polling_request = contextvars.ContextVar(
            "emuru_native_poll_request", default=False
        )
        poll_after = 0

        def turn_for(event=None):
            turn = self._current.get()
            if not turn or not turn.open or turn.notice:
                raise RuntimeError("Telegram dispatch requires a durable claim")
            if event is not None:
                source = event.source
                if (
                    getattr(event, "internal", False)
                    or event.platform_update_id != turn.uid
                    or str(source.chat_id) != str(owner)
                    or str(source.user_id) != str(owner)
                    or source.chat_type != "dm"
                    or getattr(source.platform, "value", source.platform) != "telegram"
                ):
                    raise RuntimeError("Unclaimed Telegram event rejected")
            return turn

        def owned_runner(runner, *args, **kwargs):
            runner_init(runner, *args, **kwargs)
            self._runner = runner

        async def staged_poll(bot, *args, **kwargs):
            nonlocal poll_after
            await asyncio.sleep(max(0, poll_after - time.monotonic()))
            # ponytail: one synchronous FULL SQLite commit per <=100-update poll batch;
            # move this connection to a dedicated thread only if fsync latency matters.
            if self.worker.intake_failed:
                raise RuntimeError(
                    "Telegram intake stopped after a durable queue failure"
                )
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
                self.worker.stage(
                    bot.id,
                    (json.loads(u.to_json()) for u in updates),
                    bot.username or "",
                )
            except Exception:  # noqa: BLE001 -- fail closed before native acknowledgement
                logger.error("EMURU Telegram queue persistence failed; intake stopped")
                os.kill(os.getpid(), signal.SIGTERM)
                raise RuntimeError(
                    "Telegram queue persistence failed; intake stopped"
                ) from None
            return updates

        async def guarded_update(app, update):
            if not isinstance(update, Update):
                raise TypeError("Non-Telegram update rejected")
            turn = self._current.get()
            if not turn or not turn.open or turn.notice:
                # Polling has already staged this input; only the durable worker can
                # enter native admission and handlers. Rejections never reach PTB groups.
                admitted = queue_module.admit_update(
                    json.loads(update.to_json()),
                    owner,
                    policy,
                    self.queue.now(),
                    app.bot.username or "",
                )
                if admitted is not None:
                    self._attach(app)
                    await self.worker.local_status()
                return
            if update.update_id != turn.uid:
                raise RuntimeError("Unclaimed Telegram update rejected")
            raw = json.loads(update.to_json())
            admitted = queue_module.admit_update(
                raw, owner, policy, self.queue.now(), app.bot.username or ""
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
            connect, disconnect, handle = (
                cls.connect,
                cls.disconnect,
                cls.handle_message,
            )
            processing_hook = cls._run_processing_hook

            async def guarded_connect(adapter, *args, **kwargs):
                self.worker.ensure_accepting()
                extra = adapter.config.extra
                if (
                    extra.get("drop_pending_on_cold_boot") is not False
                    or type(extra.get("max_concurrent_updates")) is not int
                    or extra["max_concurrent_updates"] != 1
                ):
                    raise RuntimeError("Native Telegram admission settings changed")
                result = await connect(adapter, *args, **kwargs)
                if result:
                    self._attach(adapter._app)
                    self.worker.start()
                return result

            async def guarded_disconnect(adapter, *args, **kwargs):
                await self.worker.stop()
                return await disconnect(adapter, *args, **kwargs)

            async def awaited_handle(adapter, event):
                turn = turn_for(event)
                key = adapter._event_session_key(event)
                if turn.session_key is not None and key != turn.session_key:
                    raise RuntimeError(
                        "Native Telegram session route changed after claim"
                    )
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
                if not turn.completed or adapter._pending_messages:
                    raise RuntimeError("Native turn did not complete inline")

            async def hook(adapter, name, *args, **kwargs):
                turn = self._current.get()
                if name == "on_processing_complete" and turn and turn.open:
                    outcome = getattr(args[1], "value", args[1])
                    turn.completed = True
                    turn.failed |= outcome != "success"
                    if outcome != "success":
                        turn.error_code = turn.error_code or "native_" + (
                            outcome
                            if outcome in {"failure", "cancelled"}
                            else "failure"
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
            if turn.agent_entered:
                raise RuntimeError("A Telegram update cannot start a second agent turn")
            turn.agent_entered = True
            return await agent_turn(runner, event, *args, **kwargs)

        async def control_new(runner, event, source, _quick_key):
            turn = turn_for(event)
            key = runner._session_key_for_source(source)
            if key != turn.session_key:
                raise RuntimeError("Native session reset route changed after claim")
            old = runner.session_store._entries.get(key)
            old_id = old.session_id if old else None
            # Native auth and command access checks run before this handler. The
            # durable FIFO already waits for active work; retain native reset/cache
            # cleanup, but skip its callback confirmation on this text-only surface.
            await native_reset(runner, event)
            new = runner.session_store._entries.get(key)
            if new is None or new.session_id == old_id:
                raise HealthError("session_transition_failed")
            return True, "New session started. Previous conversation preserved."

        async def no_onboarding(runner, source, history, turn_sidecar_notes):
            turn_for()
            # This owner-only deployment has no sethome/profile-build workflow.

        async def checked_result(runner, *args, **kwargs):
            turn = turn_for()
            result = await native_run_agent(runner, *args, **kwargs)
            if (
                not isinstance(result, dict)
                or result.get("failed")
                or result.get("error")
            ):
                turn.failed, turn.error_code = True, "provider_failed"
            elif any(
                message.get("role") == "tool" and tool_failed(message.get("content"))
                for message in result.get("messages", [])[
                    result.get("history_offset", 0) :
                ]
                if isinstance(message, dict)
            ):
                turn.failed, turn.error_code = True, "tool_failed"
            return result

        async def checked_agent_error(runner, error, *args, **kwargs):
            turn = turn_for()
            turn.failed, turn.error_code = True, queue_module.safe_error_code(error)
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
                not polling_request.get() or self.worker.intake_failed
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
                turn = self._current.get()
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
                    or not turn.open
                    or turn.failed
                ):
                    raise RuntimeError("Telegram egress rejected")
                try:
                    return await post(bot, endpoint, data, *args, **kwargs)
                except asyncio.CancelledError:
                    turn.failed = True
                    raise
                except Exception:  # noqa: BLE001 -- suppress credential-bearing SDK errors
                    # Native formatting/thread/network fallbacks may try again. An
                    # unknown send outcome latches the whole turn against resends.
                    turn.failed = True
                    raise RuntimeError(
                        "Telegram egress outcome is uncertain; no retry"
                    ) from None
            return await post(bot, endpoint, data, *args, **kwargs)

        native_factory = Runner._create_adapter

        def guarded_factory(runner, platform, config):
            adapter = native_factory(runner, platform, config)
            if (
                getattr(platform, "value", platform) == "telegram"
                and adapter is not None
            ):
                cls = type(adapter)
                if not cls.__dict__.get("_emuru_adapter_guarded", False):
                    # The plugin registry loads the same pinned source in its own namespace.
                    if Path(inspect.getfile(cls)).resolve() != (
                        Path(inspect.getfile(Adapter)).resolve()
                    ):
                        raise HealthError("telegram_adapter_contract_changed")
                    verify_native_contract(
                        self.root,
                        Bot,
                        Application,
                        Updater,
                        cls,
                        None,
                        adapter_only=True,
                    )
                    guard_adapter(cls)
            return adapter

        Runner._create_adapter = guarded_factory
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

    def install_launch_guards(self, run, gateway_cli, config):
        """Native discovery is best-effort; the required MCP gate must fail closed."""
        native_start, native_exit = run.start_gateway, run._exit_after_graceful_shutdown
        runner_start = run.GatewayRunner.start
        native_attach = gateway_cli._attach_to_host_gateway_or_guard

        def refuse_host():
            from gateway.host_attach import host_gateway

            if host_gateway() is not None:
                raise HealthError("unguarded_host_gateway_running")

        async def guarded_host(replace, force=False):
            refuse_host()

        def guarded_attach(*args, **kwargs):
            refuse_host()
            return native_attach(*args, **kwargs)

        async def guarded_start(**kwargs):
            # CLI imports may reload dotenv; keep native auth narrowed to the owner.
            os.environ["TELEGRAM_ALLOWED_USERS"] = str(self.queue.owner_id)
            os.environ["TELEGRAM_ALLOW_ALL_USERS"] = "false"
            kwargs["config"] = config
            return await native_start(**kwargs)

        async def ready_start(runner):
            if not vault_tools_ready():
                raise HealthError("vault_mcp_not_ready")
            result = await runner_start(runner)
            if not result:
                raise HealthError("native_start_failed")
            await self.worker.require_consumer()
            print(
                "EMURU Telegram checkpoint: required guards and five MCP tools READY",
                flush=True,
            )
            return result

        def closed_exit(code):
            try:
                self.queue.close()
            finally:
                native_exit(code)

        run._host_attach_or_none = guarded_host
        gateway_cli._attach_to_host_gateway_or_guard = guarded_attach
        run.start_gateway = guarded_start
        run.GatewayRunner.start = ready_start
        run._exit_after_graceful_shutdown = closed_exit
