"""Fake PTB and Hermes boundaries exercise the installed bridge's actual guards."""

import asyncio
import json
import sys
from enum import Enum
from types import SimpleNamespace as NS

import pytest
from telegram_helpers import update as raw

from emuru.telegram_queue import TelegramQueue


@pytest.fixture
def boundary(tmp_path, policy, runtime, monkeypatch):
    effects, network, reports, fatal = [], [], [], []
    release, started = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(runtime.os, "kill", lambda *args: fatal.append(args))
    queue = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: 100000)

    class Kind(Enum):
        TEXT = "text"
        COMMAND = "command"

    class Update:
        def __init__(self, payload):
            self.payload = payload
            self.update_id = payload["update_id"]
            self.message = NS(text=payload["message"]["text"])

        def to_json(self):
            return json.dumps(self.payload)

        @classmethod
        def de_json(cls, payload, bot):
            return cls(payload)

    class Bot:
        id = 7
        username = "emuru"
        batch = ()
        fail_send = False

        async def get_updates(self, *args, **kwargs):
            return self.batch

        async def _post(self, endpoint, data=None, **kwargs):
            network.append((endpoint, data))
            if self.fail_send and endpoint == "sendMessage":
                raise RuntimeError("SYNTHETIC_TOKEN must not be logged")
            return True

        async def send_message(self, chat_id, text):
            return await self._post("sendMessage", {"chat_id": chat_id, "text": text})

    class Updater:
        async def start_polling(self, **kwargs):
            return kwargs

    class Runner:
        def __init__(self):
            self.session_store = NS(_entries={"owner": NS(session_id="A")})
            self.histories = {"A": []}
            self.cached_agent = object()
            self.cached_history = []
            self.model_histories = []
            self.resets = 0

        def _session_key_for_source(self, source):
            return "owner"

        async def _handle_reset_command(self, event):
            self.resets += 1
            self.session_store._entries["owner"] = NS(session_id=f"B{self.resets}")
            self.histories[f"B{self.resets}"] = []
            self.cached_agent = self.cached_history = None
            return "native reset notice"

        async def _hm_cmd_new(self, event, source, _quick_key):
            raise AssertionError("Native callback confirmation must be bypassed")

        async def _hmwa_first_contact_notes(self, source, history, turn_sidecar_notes):
            if not history:
                network.append(("onboarding", {"text": "Set a home channel"}))
                turn_sidecar_notes.append("Profile onboarding")

        def _create_adapter(self, platform, config):
            return self.adapter_factory()

        async def _handle_message(self, event):
            if event.text.split(maxsplit=1)[0].lower() in {"/new", "/new@emuru"}:
                handled, result = await self._hm_cmd_new(event, event.source, "owner")
                assert handled
                return result
            if event.text.startswith("/"):
                effects.append(event.text)
                return "native command"
            return await self._handle_message_with_agent(event)

        async def _handle_message_with_agent(self, event):
            effects.append(event.text)
            started.set()
            if event.text == "wait":
                await release.wait()
            result = await self._run_agent(event)
            return result.get("final_response", "native reply")

        async def _run_agent(self, *args, **kwargs):
            sid = self.session_store._entries["owner"].session_id
            notes = []
            await self._hmwa_first_contact_notes(
                args[0].source, self.histories[sid], notes
            )
            self.model_histories.append((args[0].text, sid, list(self.histories[sid])))
            return getattr(self, "agent_result", {"final_response": "native reply"})

        async def _hmwa_agent_error_reply(self, error, *args, **kwargs):
            return "native error"

        def _resume_pending_candidates(self, *args, **kwargs):
            return [
                NS(origin=NS(platform="telegram")),
                NS(origin=NS(platform="discord")),
            ]

    class Application:
        async def process_update(self, update):
            # Model the native admission beneath the stricter gate. The installed
            # runtime check separately exercises real PTB claims and handler groups.
            key = f"{self.bot.id}:{update.update_id}"
            if key in self.adapter._seen_update_ids:
                return
            event = self.adapter._build_message_event(
                update.message, Kind.TEXT, update.update_id
            )
            await self.adapter.handle_message(event)
            self.adapter._seen_update_ids[key] = 100000

    class Adapter:
        def __init__(self):
            self.config = NS(
                extra={"drop_pending_on_cold_boot": False, "max_concurrent_updates": 1}
            )
            self._active_sessions, self._session_tasks, self._pending_messages = (
                {},
                {},
                {},
            )
            self._seen_update_ids = {}
            self._pending_text_batch_tasks = {}
            self.runner = Runner()

        async def connect(self):
            return True

        async def disconnect(self):
            pass

        def _event_session_key(self, event):
            return "owner"

        def _build_message_event(self, message, kind, update_id):
            return NS(
                text=message.text,
                platform_update_id=update_id,
                internal=False,
                source=NS(
                    chat_id="42", user_id="42", chat_type="dm", platform="telegram"
                ),
            )

        async def _run_processing_hook(self, name, *args, **kwargs):
            pass

        async def handle_message(self, event):
            async def background():
                try:
                    answer = await self.runner._handle_message(event)
                    await bot._post("sendMessage", {"chat_id": 42, "text": answer})
                    await self._run_processing_hook(
                        "on_processing_complete", event, "success"
                    )
                except Exception:  # noqa: BLE001 -- mirrors Hermes's contained turn errors
                    await self._run_processing_hook(
                        "on_processing_complete", event, "failure"
                    )
                finally:
                    self._active_sessions.clear()
                    self._session_tasks.clear()

            self._active_sessions["owner"] = True
            self._session_tasks["owner"] = asyncio.create_task(background())

    bot, adapter = Bot(), Adapter()
    # Native directory plugins import the same source under a second namespace.
    plugin_cls = type(
        "TelegramAdapter",
        (),
        {
            k: v
            for k, v in Adapter.__dict__.items()
            if k not in {"__dict__", "__weakref__"}
        },
    )
    adapter.runner.adapter_factory = plugin_cls
    app = Application()
    app.bot, app.adapter = bot, adapter
    adapter._app = app

    async def report(app, uid):
        reports.append(uid)

    bridge = runtime.Bridge(queue, None, report)
    prompt = NS(STEER_CHANNEL_NOTE="[OUT-OF-BAND USER MESSAGE] <their message>")
    runtime.restrict_prompt(prompt)
    receipts = NS(
        _load_receipts=lambda adapter, bot: None,
        _record_receipt=lambda adapter, key: adapter._seen_update_ids.update(
            {key: 100000}
        ),
    )
    runtime.install_guards(
        bridge,
        Bot=Bot,
        Application=Application,
        Updater=Updater,
        Adapter=Adapter,
        Runner=Runner,
        Update=Update,
        MessageType=Kind,
        receipts=receipts,
    )
    result = NS(
        queue=queue,
        bridge=bridge,
        bot=bot,
        adapter=adapter,
        app=app,
        Update=Update,
        effects=effects,
        network=network,
        reports=reports,
        fatal=fatal,
        started=started,
        release=release,
        Updater=Updater,
        plugin_cls=plugin_cls,
        prompt=prompt,
    )
    yield result
    queue.close()


def test_registry_adapter_starts_consumer_and_drains_ingress(
    boundary, runtime, monkeypatch
):
    b = boundary
    monkeypatch.setattr(runtime, "verify_native_contract", lambda *a, **kw: None)

    async def check():
        # Factory returns a second import of the adapter, as the native registry does.
        adapter = b.adapter.runner._create_adapter("telegram", b.adapter.config)
        assert type(adapter) is b.plugin_cls
        b.app.adapter = adapter
        adapter._app = b.app
        b.bot.batch = [b.Update(raw(20, "factory request"))]
        await b.bot.get_updates()
        assert b.queue.status() == {"queued": 1}
        await adapter.connect()
        await b.bridge.require_consumer()
        await asyncio.wait_for(b.started.wait(), 1)
        for _ in range(100):
            if b.queue.status() == {"completed": 1}:
                break
            await asyncio.sleep(0.001)
        assert b.effects == ["factory request"]
        assert b.queue.status() == {"completed": 1}
        assert b.bridge.bot_id == b.bot.id == 7
        assert b.network == [("sendMessage", {"chat_id": 42, "text": "native reply"})]
        await adapter.disconnect()

    asyncio.run(check())


def test_readiness_requires_real_consumer_and_bot_scope(boundary):
    b = boundary

    async def check():
        with pytest.raises(RuntimeError, match="consumer_not_running"):
            await b.bridge.require_consumer()
        await b.adapter.connect()
        await b.bridge.require_consumer()
        assert b.bridge.queue is b.queue
        b.bridge.bot_id = 8
        with pytest.raises(RuntimeError, match="consumer_not_running"):
            await b.bridge.require_consumer()
        await b.bridge.stop()

    asyncio.run(check())


def test_local_status_bypasses_active_turn_and_keeps_duplicate_receipt(boundary):
    b = boundary

    async def check():
        b.queue.stage(7, [raw(1, "wait")])
        await b.adapter.connect()
        await asyncio.wait_for(b.started.wait(), 1)
        b.bot.batch = [b.Update(raw(2, "/status@emuru"))]
        await b.bot.get_updates()
        await b.app.process_update(b.bot.batch[0])
        assert b.queue.status() == {"started": 1, "completed": 1}
        assert b.effects == ["wait"]
        assert "consumer_running" in b.network[0][1]["text"]
        await b.bot.get_updates()
        await b.app.process_update(b.bot.batch[0])
        assert len(b.network) == 1
        unauthorized = raw(3, "/status")
        unauthorized["message"]["from"]["id"] = 43
        b.bot.batch = [b.Update(unauthorized)]
        await b.bot.get_updates()
        await b.app.process_update(b.bot.batch[0])
        assert len(b.network) == 1
        b.release.set()
        await b.bridge.stop()

    asyncio.run(check())


def test_new_is_fifo_control_without_model_and_preserves_old_transcript(boundary):
    b = boundary
    runner = b.adapter.runner
    marker = "UNSAVED_SYNTHETIC_MARKER"
    runner.histories["A"] = [{"role": "user", "content": marker}]
    runner.cached_history = list(runner.histories["A"])

    async def check():
        b.bot.batch = [
            b.Update(raw(1, "wait")),
            b.Update(raw(2, "before boundary")),
            b.Update(raw(3, "/new@emuru")),
            b.Update(raw(4, "after boundary")),
        ]
        await b.bot.get_updates()
        await b.adapter.connect()
        await asyncio.wait_for(b.started.wait(), 1)
        # PTB's foreground dispatch cannot execute /new ahead of the active claim.
        await b.app.process_update(b.bot.batch[2])
        assert runner.resets == 0
        assert runner.session_store._entries["owner"].session_id == "A"
        b.release.set()
        for _ in range(100):
            if b.queue.status() == {"completed": 4}:
                break
            await asyncio.sleep(0.001)
        assert b.queue.status() == {"completed": 4}
        assert runner.resets == 1
        assert runner.session_store._entries["owner"].session_id == "B1"
        assert runner.cached_agent is runner.cached_history is None
        assert runner.histories["A"] == [{"role": "user", "content": marker}]
        assert [(text, sid) for text, sid, _ in runner.model_histories] == [
            ("wait", "A"),
            ("before boundary", "A"),
            ("after boundary", "B1"),
        ]
        assert runner.model_histories[-1][2] == []
        assert b.network[2] == (
            "sendMessage",
            {
                "chat_id": 42,
                "text": "New session started. Previous conversation preserved.",
            },
        )
        assert (
            b.queue.db.execute("SELECT DISTINCT session_key FROM updates").fetchall()[
                0
            ][0]
            == "owner"
        )
        b.bot.batch = [b.Update(raw(3, "/new@emuru"))]
        await b.bot.get_updates()
        await b.app.process_update(b.bot.batch[0])
        await asyncio.sleep(0)
        assert runner.resets == 1 and len(b.network) == 4
        await b.bridge.stop()

    asyncio.run(check())


def test_worker_exit_reports_safe_health_and_preserves_pending(
    boundary, monkeypatch, caplog
):
    b = boundary

    async def crash(app):
        raise RuntimeError("SYNTHETIC_SECRET")

    monkeypatch.setattr(b.bridge, "once", crash)

    async def check():
        b.queue.stage(7, [raw(1)])
        await b.adapter.connect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert b.bridge.health_error == "consumer_failed" and b.bridge.intake_failed
        assert b.fatal and b.queue.status() == {"queued": 1}
        with pytest.raises(RuntimeError, match="consumer_failed"):
            await b.bridge.require_consumer()
        assert json.loads((b.queue.path.parent / "health.json").read_text()) == {
            "health": ["consumer_failed"]
        }
        await b.bridge.stop()

    asyncio.run(check())
    assert "SYNTHETIC_SECRET" not in caplog.text


def test_poll_stages_before_return_and_durability_failure_prevents_ack(
    boundary, monkeypatch
):
    b = boundary

    async def check():
        b.bot.batch = [b.Update(raw(1)), b.Update(raw(1)), b.Update(raw(2))]
        result = await b.bot.get_updates(offset=0)
        assert len(result) == 3 and b.queue.status() == {"queued": 2}
        assert b.effects == []

        def fail(*args):
            raise OSError("disk full")

        monkeypatch.setattr(b.queue, "stage", fail)
        with pytest.raises(RuntimeError, match="persistence failed"):
            await b.bot.get_updates(offset=3)
        assert b.bridge.intake_failed and len(b.fatal) == 1
        with pytest.raises(RuntimeError, match="intake stopped"):
            await b.bot.get_updates(offset=999)
        assert b.effects == []

    asyncio.run(check())


def test_actual_background_turns_are_serialized_and_never_merged(boundary):
    b = boundary

    async def check():
        b.bot.batch = [b.Update(raw(1, "wait")), b.Update(raw(2, "second"))]
        await b.bot.get_updates()
        await b.adapter.connect()
        await asyncio.wait_for(b.started.wait(), 1)
        await b.app.process_update(b.Update(raw(1, "wait")))
        await asyncio.sleep(0)
        assert b.effects == ["wait"]
        assert b.queue.status() == {"started": 1, "queued": 1}
        b.release.set()
        for _ in range(100):
            if b.queue.status() == {"completed": 2}:
                break
            await asyncio.sleep(0.001)
        assert b.effects == ["wait", "second"]
        assert b.queue.status() == {"completed": 2}
        await b.bridge.stop()
        assert not b.fatal

    asyncio.run(check())


def test_egress_owner_text_only_and_no_send_retry_after_uncertain_failure(
    boundary, caplog
):
    b = boundary

    async def check():
        with pytest.raises(RuntimeError, match="durable staging"):
            await b.bot._post("getUpdates", {"offset": 999})
        for endpoint, data in (
            ("sendMessage", {"chat_id": 42, "text": "outside turn"}),
            ("sendPhoto", {"chat_id": 42}),
            ("getFile", {"file_id": "private"}),
        ):
            with pytest.raises(RuntimeError, match="egress"):
                await b.bot._post(endpoint, data)
        token = b.bridge.current.set({"open": True, "failed": False})
        try:
            with pytest.raises(RuntimeError, match="egress"):
                await b.bot._post(
                    "editMessageText", {"chat_id": 43, "text": "wrong destination"}
                )
            b.bot.fail_send = True
            with pytest.raises(RuntimeError):
                await b.bot._post("sendMessage", {"chat_id": 42, "text": "original"})
            with pytest.raises(RuntimeError, match="egress"):
                await b.bot._post(
                    "sendMessage", {"chat_id": 42, "text": "native formatting fallback"}
                )
        finally:
            b.bridge.current.reset(token)
        assert len(b.network) == 1
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once(b.app)
        assert b.queue.status() == {"failed": 1}
        assert await b.bridge.once(b.app)
        assert b.reports == [1]
        assert b.effects == ["hello"]
        assert "SYNTHETIC_TOKEN" not in caplog.text

    asyncio.run(check())


@pytest.mark.parametrize(
    "result",
    [
        {"failed": True},
        {"error": "SYNTHETIC_SECRET"},
        {"messages": [{"role": "tool", "content": '{"error":"SYNTHETIC_SECRET"}'}]},
    ],
)
def test_returned_provider_or_tool_failure_is_terminal(boundary, result, caplog):
    b = boundary
    b.adapter.runner.agent_result = result

    async def check():
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once(b.app)
        assert b.queue.status() == {"failed": 1}
        assert b.queue.db.execute("SELECT text FROM updates").fetchone()[0] is None
        assert await b.bridge.once(b.app)
        b.queue.stage(7, [raw(1)])
        assert not await b.bridge.once(b.app)
        assert b.effects == ["hello"] and b.reports == [1]

    asyncio.run(check())
    assert "SYNTHETIC_SECRET" not in caplog.text


def test_changed_native_signature_fails_closed(
    boundary, runtime, tmp_path, monkeypatch
):
    b = boundary
    monkeypatch.setitem(sys.modules, "telegram", NS(__version__="synthetic"))
    (tmp_path / "infra/hermes").mkdir(parents=True)
    (tmp_path / "infra/hermes/telegram-native-contract.json").write_text(
        json.dumps(
            {
                "telegram_version": "synthetic",
                "methods": {"Bot.get_updates": [True, "(bot, *args, **kwargs)"]},
            }
        )
    )
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    classes = (
        type(b.bot),
        type(b.app),
        b.Updater,
        type(b.adapter),
        type(b.adapter.runner),
    )
    runtime.verify_native_contract(*classes)

    async def changed(bot, unexpected):
        pass

    monkeypatch.setattr(type(b.bot), "get_updates", changed)
    with pytest.raises(RuntimeError, match="seam changed"):
        runtime.verify_native_contract(*classes)


def test_unclaimed_synthetic_recovery_and_second_turn_are_blocked(boundary):
    b = boundary

    async def check():
        event = b.adapter._build_message_event(NS(text="hello"), None, 1)
        with pytest.raises(RuntimeError, match="durable claim"):
            await b.adapter.runner._handle_message(event)
        assert [
            e.origin.platform for e in b.adapter.runner._resume_pending_candidates()
        ] == ["discord"]
        token = b.bridge.current.set(
            {"uid": 1, "open": True, "notice": False, "agent_entered": False}
        )
        try:
            event.internal = True
            with pytest.raises(RuntimeError, match="Unclaimed"):
                await b.adapter.runner._handle_message(event)
            event.internal = False
            await b.adapter.runner._handle_message_with_agent(event)
            with pytest.raises(RuntimeError, match="second agent"):
                await b.adapter.runner._handle_message_with_agent(event)
        finally:
            b.bridge.current.reset(token)
        assert b.effects == ["hello"]

    asyncio.run(check())


def test_native_receipts_and_polling_contract_are_reused(boundary):
    b = boundary

    async def check():
        b.adapter._seen_update_ids["7:1"] = 100000
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once(b.app)
        assert b.effects == [] and b.queue.status() == {"completed": 1}
        options = await b.Updater().start_polling(drop_pending_updates=True, timeout=99)
        assert options["drop_pending_updates"] is False
        assert options["timeout"] == 25 and options["allowed_updates"] == ["message"]
        b.adapter.config.extra["max_concurrent_updates"] = 2
        with pytest.raises(RuntimeError, match="settings changed"):
            await b.adapter.connect()
        with pytest.raises(RuntimeError, match="Dropping pending"):
            await b.bot._post("deleteWebhook", {"drop_pending_updates": True})

    asyncio.run(check())


def test_cancelled_native_turn_is_ambiguous_and_cannot_restart_in_process(boundary):
    b = boundary

    async def check():
        b.queue.stage(7, [raw(1, "wait"), raw(2, "second")])
        await b.adapter.connect()
        await asyncio.wait_for(b.started.wait(), 1)
        await b.bridge.stop()
        assert b.queue.status() == {"interrupted": 1, "queued": 1}
        assert b.effects == ["wait"] and b.bridge.retired
        with pytest.raises(RuntimeError, match="restart"):
            await b.adapter.connect()

    asyncio.run(check())


def test_native_startup_gate_cannot_requeue_or_merge_durable_pending_input(boundary):
    b = boundary

    async def check():
        b.queue.stage(7, [raw(1), raw(2, "second")])
        b.bridge.native_runner = NS(_startup_restore_in_progress=True)
        await b.adapter.connect()
        await asyncio.sleep(0)
        assert b.queue.status() == {"queued": 2} and b.effects == []
        b.bridge.native_runner._startup_restore_in_progress = False
        await asyncio.wait_for(b.started.wait(), 1)
        for _ in range(100):
            if b.queue.status() == {"completed": 2}:
                break
            await asyncio.sleep(0.001)
        assert b.effects == ["hello", "second"]
        assert b.queue.status() == {"completed": 2}
        await b.bridge.stop()

    asyncio.run(check())
