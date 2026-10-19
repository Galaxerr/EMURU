"""Fake PTB and Hermes boundaries exercise the installed bridge's actual guards."""

import asyncio
import json
import sys
from enum import Enum
from types import SimpleNamespace as NS

import pytest
from telegram_helpers import DeterministicTelegram
from telegram_helpers import update as raw

from emuru.telegram.queue import TelegramQueue
from emuru.telegram.worker import TelegramWorker, TurnResult


@pytest.fixture
def boundary(tmp_path, policy, runtime, monkeypatch):
    effects, network, reports, fatal = [], [], [], []
    delivery_attempts = []
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
            if self.fail_send and endpoint in {"sendMessage", "sendDocument"}:
                raise RuntimeError("SYNTHETIC_TOKEN must not be logged")
            return True

        async def send_message(self, chat_id, text, **kwargs):
            if text.startswith(("I can’t reach", "I couldn’t finish")):
                reports.append(1)
            return await self._post(
                "sendMessage", {"chat_id": chat_id, "text": text, **kwargs}
            )

        async def send_document(self, chat_id, document, **kwargs):
            return await self._post(
                "sendDocument",
                {"chat_id": chat_id, "document": document.getvalue(), **kwargs},
            )

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
            if getattr(self, "reset_error_before", False):
                raise RuntimeError("SYNTHETIC_SECRET")
            self.resets += 1
            self.session_store._entries["owner"] = NS(session_id=f"B{self.resets}")
            self.histories[f"B{self.resets}"] = []
            self.cached_agent = self.cached_history = None
            if getattr(self, "reset_error_after", False):
                raise RuntimeError("SYNTHETIC_SECRET")
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
            try:
                result = await self._run_agent(event)
            except RuntimeError as error:
                return await self._hmwa_agent_error_reply(error)
            return result.get("final_response", "native reply")

        async def _run_agent(self, *args, **kwargs):
            if getattr(self, "agent_error", False):
                raise RuntimeError("SYNTHETIC_SECRET")
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
            self.adapter._updates_dispatched_total += 1
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
            self._updates_dispatched_total = 0
            self._pending_text_batch_tasks = {}
            self.runner = Runner()
            self.gateway_runner = self.runner

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
                    if answer:
                        delivery_attempts.append(answer)
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

    prompt = NS(STEER_CHANNEL_NOTE="[OUT-OF-BAND USER MESSAGE] <their message>")
    runtime.native.restrict_prompt(prompt)
    receipts = NS(
        _load_receipts=lambda adapter, bot: None,
        _record_receipt=lambda adapter, key: adapter._seen_update_ids.update(
            {key: 100000}
        ),
    )
    execution = runtime.native.NativeTelegram(
        queue,
        runtime.ROOT,
        app=app,
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
        bridge=execution.worker,
        execution=execution,
        bot=bot,
        adapter=adapter,
        app=app,
        Update=Update,
        effects=effects,
        network=network,
        delivery_attempts=delivery_attempts,
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


def test_ingress_progress_includes_filtered_status_and_duplicate_updates(boundary):
    b = boundary

    async def check():
        rejected = raw(1, "text accompanying forbidden media")
        rejected["message"]["photo"] = [{"file_id": "synthetic"}]
        b.bot.batch = [
            b.Update(rejected),
            b.Update(raw(2, "hello")),
            b.Update(raw(3, "/status")),
            b.Update(raw(2, "hello")),
        ]
        await b.bot.get_updates()
        for update in b.bot.batch:
            await b.app.process_update(update)
        assert b.adapter._updates_dispatched_total == 4
        assert not b.effects
        assert b.queue.status() == {"queued": 1, "completed": 1}
        assert await b.bridge.once()
        assert b.adapter._updates_dispatched_total == 4
        assert b.effects == ["hello"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "decision",
    [
        "button_accept",
        "text_accept",
        "button_reject",
        "text_reject",
        "command_reject",
        "addressed_command_reject",
        "explicit_command_reject",
        "large_accept",
        "expire",
    ],
)
def test_single_proposal_message_revision_and_decisions(boundary, tmp_path, decision):
    from telegram_helpers import callback_update
    from vault_helpers import create_vault

    from emuru.vault import mutations as m

    b = boundary
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    vault = create_vault(tmp_path)
    b.execution.mutation_target = (home, vault)
    previews = []

    async def model(event):
        # Native streaming and final summaries must not escape before the proposal card.
        draft = await b.bot._post(
            "sendMessage", {"chat_id": 42, "text": "Draft streaming"}
        )
        await b.bot._post(
            "editMessageText",
            {
                "chat_id": 42,
                "message_id": draft["message_id"],
                "text": "Updated streaming draft",
            },
        )
        if "What does this mean?" in event.text:
            return {"final_response": "A draft needs your approval."}
        result = m.propose_note(
            home,
            vault,
            "10_Projects/new.md",
            content="# Revised"
            if previews
            else "# Original"
            + ("\n" + "😀" * 9000 if decision == "large_accept" else ""),
            authenticated_id=m.authenticated_request(home, vault),
        )
        previews.append(result)
        return {"final_response": "Proposal stored; please approve."}

    b.adapter.runner._run_agent = model

    async def request(payload):
        if "callback_query" in payload:
            b.bot.batch = [NS(to_json=lambda: json.dumps(payload))]
            await b.bot.get_updates()
            assert b.network[-1] == (
                "answerCallbackQuery",
                {"callback_query_id": payload["callback_query"]["id"]},
            )
        else:
            b.queue.stage(7, [payload], "emuru")
        assert await b.bridge.once()
        assert (
            b.queue.db.execute(
                "SELECT state FROM updates WHERE update_id=?", (payload["update_id"],)
            ).fetchone()[0]
            == "completed"
        )

    async def check():
        payload = raw(1, "create project at 10_Projects/new.md")
        payload["message"]["link_preview_options"] = {"is_disabled": True}
        await request(payload)
        first = previews[0]
        messages = [
            data
            for endpoint, data in b.network
            if endpoint in {"sendMessage", "sendDocument"}
        ]
        assert len(messages) == 1
        card = messages[0].get("text", messages[0].get("caption"))
        assert (
            "# Original" in card or b"# Original" in messages[0]["document"]
        ) and "Apply these exact changes?" in card
        buttons = messages[0]["reply_markup"]["inline_keyboard"][0]
        assert [button["text"] for button in buttons] == ["Accept", "Reject"]
        assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
        if decision == "expire":
            import time

            with m.journal(home) as db:
                db.execute(
                    "UPDATE proposal_lifecycle SET created_at=?", (time.time() - 86401,)
                )
                db.commit()
            b.bot.batch = ()
            await b.bot.get_updates()
            assert (
                m.owner_preview(home, vault, first["proposal_id"], 42, 7)["outcome"]
                == "expired"
            )
            assert not (vault / first["path"]).exists()
            return
        await request(raw(2, "What does this mean?"))
        assert b.network[-1][1]["text"] == "A draft needs your approval."
        await request(raw(3, "Change the goal"))
        revised = previews[-1]
        assert revised["proposal_id"] == first["proposal_id"]
        assert revised["approval_digest"] != first["approval_digest"]
        assert b.network[-1][1]["text"].count("# Revised") == 1
        await request(
            callback_update(4, first["proposal_id"], first["approval_digest"][:16])
        )
        assert "Proposal changed" in b.network[-1][1]["text"]
        assert not (vault / first["path"]).exists()
        stale = raw(5, "I accept")
        stale["message"]["reply_to_message"] = {
            "message_id": 500,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7, "is_bot": True},
            "text": card,
        }
        await request(stale)
        assert "Proposal changed" in b.network[-1][1]["text"]
        assert not (vault / first["path"]).exists()
        if decision.startswith("button"):
            payload = callback_update(
                6,
                revised["proposal_id"],
                revised["approval_digest"][:16],
                "a" if decision.endswith("accept") else "r",
            )
        else:
            payload = raw(
                6,
                f"/reject {revised['proposal_id']}"
                if decision == "explicit_command_reject"
                else "/reject@emuru"
                if decision == "addressed_command_reject"
                else "/reject"
                if decision == "command_reject"
                else "I accept"
                if decision.endswith("accept")
                else "I reject",
            )
        await request(payload)
        outcome = m.owner_preview(home, vault, revised["proposal_id"], 42, 7)
        assert outcome["outcome"] == (
            "applied" if decision.endswith("accept") else "cancelled"
        )
        if decision.endswith("accept"):
            assert (vault / revised["path"]).read_text() == "# Revised"
        else:
            assert not (vault / revised["path"]).exists()
            assert (
                b.network[-1][1]["text"]
                == f"Proposal {revised['proposal_id']} cancelled."
            )
        assert b.queue.stage(7, [payload], "emuru") == ["completed"]
        assert not await b.bridge.once()

    asyncio.run(check())


@pytest.mark.parametrize("count", [0, 2])
def test_reject_requires_one_pending_proposal(boundary, tmp_path, count):
    from vault_helpers import create_vault

    from emuru.vault import mutations as m

    b = boundary
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    vault = create_vault(tmp_path)
    b.execution.mutation_target = (home, vault)
    for uid in range(count):
        identity, _ = m.begin_request(home, vault, 42, 7, uid, f"project {uid}")
        m.propose_note(
            home,
            vault,
            f"10_Projects/{uid}.md",
            content="# Draft",
            authenticated_id=identity,
        )

    async def check():
        b.queue.stage(7, [raw(10, "/reject")], "emuru")
        assert await b.bridge.once()
        assert b.network[-1][1]["text"] == (
            "No pending proposal."
            if not count
            else "Several proposals are pending. Use /cancel <proposal-id>."
        )
        assert len(m.pending_previews(home, vault, 42, 7)) == count
        assert not b.adapter.runner.model_histories

    asyncio.run(check())


@pytest.mark.parametrize("command", ["/new", "/new@emuru", "/help", "/stop"])
def test_native_commands_ignore_interrupted_proposal_request(
    boundary, tmp_path, command
):
    from vault_helpers import create_vault

    from emuru.vault import mutations as m

    b = boundary
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    vault = create_vault(tmp_path)
    b.execution.mutation_target = (home, vault)
    stale, _ = m.begin_request(home, vault, 42, 7, 100, command)
    runner = b.adapter.runner
    runner.histories["A"] = [{"role": "user", "content": "Preserve history"}]

    async def check():
        for uid in (101, 102):
            b.queue.stage(7, [raw(uid, command)], "emuru")
            assert await b.bridge.once()
            assert b.network[-1][1]["text"].split("\n")[0] == (
                "New session started. Previous conversation preserved."
                if command.startswith("/new")
                else "native command"
            )
            assert b.queue.stage(7, [raw(uid, command)], "emuru") == ["completed"]
            assert not await b.bridge.once()
        assert runner.resets == (2 if command.startswith("/new") else 0)
        assert runner.histories["A"] == [
            {"role": "user", "content": "Preserve history"}
        ]
        assert not runner.model_histories
        with m.journal(home) as db:
            assert (
                db.execute(
                    "SELECT state FROM owner_requests WHERE id=?", (stale,)
                ).fetchone()[0]
                == "started"
            )

    asyncio.run(check())


def test_failed_read_can_be_asked_again_without_replaying_updates(boundary, tmp_path):
    from vault_helpers import create_vault

    from emuru.vault import mutations

    b = boundary
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    vault = create_vault(tmp_path)
    b.execution.mutation_target = (home, vault)
    question = (
        "Identify projects, documented status, and source path. Do not update notes."
    )
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}

    async def check():
        b.adapter.runner.agent_result = {"failed": True}
        b.queue.stage(7, [raw(1, question)])
        assert await b.bridge.once()
        assert await b.bridge.once()  # One failure notice.
        b.adapter.runner.agent_result = {"final_response": "native reply"}
        b.queue.stage(7, [raw(2, question), raw(2, question)])
        assert await b.bridge.once()
        assert not await b.bridge.once()
        assert b.network[-1][1]["text"] == "native reply"
        assert len(b.adapter.runner.model_histories) == 2
        assert b.queue.stage(7, [raw(1, question)]) == ["failed"]
        assert not await b.bridge.once()
        with mutations.journal(home) as db:
            assert db.execute("SELECT count(*) FROM owner_requests").fetchone()[0] == 2
        assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before

    asyncio.run(check())


@pytest.mark.parametrize("rotated", [False, True])
def test_reset_reports_actual_session_transition(boundary, rotated, caplog):
    b = boundary
    runner = b.adapter.runner
    runner.reset_error_before, runner.reset_error_after = not rotated, rotated

    async def check():
        b.queue.stage(7, [raw(1, "/new")], "emuru")
        assert await b.bridge.once()
        if rotated:
            assert (
                b.network[-1][1]["text"]
                == "New session started. Previous conversation preserved."
            )
            assert b.queue.status() == {"completed": 1}
        else:
            assert b.queue.status() == {"failed": 1}
        assert runner.resets == int(rotated)
        assert not runner.model_histories
        assert "SYNTHETIC_SECRET" not in caplog.text
        b.queue.stage(7, [raw(1, "/new")], "emuru")
        await b.bridge.once()  # Only an outstanding failure notice may run.
        assert runner.resets == int(rotated)

    asyncio.run(check())


def test_registry_adapter_starts_consumer_and_drains_ingress(
    boundary, runtime, monkeypatch
):
    b = boundary
    monkeypatch.setattr(runtime.native, "verify_native_contract", lambda *a, **kw: None)

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
        assert b.execution.identity()[0] == b.bot.id == 7
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
        b.bot.id = 8
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

    async def crash():
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


@pytest.mark.parametrize("failure", ["storage", "serialization"])
def test_poll_stages_before_return_and_durability_failure_prevents_ack(
    boundary, monkeypatch, failure
):
    b = boundary

    async def check():
        b.bot.batch = [b.Update(raw(1)), b.Update(raw(1)), b.Update(raw(2))]
        result = await b.bot.get_updates(offset=0)
        assert len(result) == 3 and b.queue.status() == {"queued": 2}
        assert b.effects == []

        def fail(*args):
            raise OSError("disk full")

        if failure == "storage":
            monkeypatch.setattr(b.queue, "stage", fail)
        else:
            monkeypatch.setattr(b.Update, "to_json", fail)
        with pytest.raises(RuntimeError, match="persistence failed"):
            await b.bot.get_updates(offset=3)
        assert b.bridge.intake_failed and len(b.fatal) == 1
        assert b.queue.status() == {"queued": 2}
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
        with b.execution._turn_scope(1):
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
        assert len(b.network) == 1
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once()
        assert b.queue.status() == {"failed": 1}
        assert await b.bridge.once()
        assert b.reports == [1]
        assert b.effects == ["hello"]
        assert "SYNTHETIC_TOKEN" not in caplog.text

    asyncio.run(check())


@pytest.mark.parametrize(
    "result",
    [
        {"failed": True},
        "exception",
        {"error": "SYNTHETIC_SECRET"},
        {"messages": [{"role": "tool", "content": '{"error":"SYNTHETIC_SECRET"}'}]},
    ],
)
def test_returned_provider_or_tool_failure_is_terminal(boundary, result, caplog):
    b = boundary
    b.adapter.runner.agent_error = result == "exception"
    b.adapter.runner.agent_result = result

    async def check():
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once()
        assert b.queue.status() == {"failed": 1}
        assert b.delivery_attempts == []
        assert b.network == []
        assert b.queue.db.execute("SELECT text FROM updates").fetchone()[0] is None
        assert await b.bridge.once()
        b.queue.stage(7, [raw(1)])
        assert not await b.bridge.once()
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
    classes = (
        type(b.bot),
        type(b.app),
        b.Updater,
        type(b.adapter),
        type(b.adapter.runner),
    )
    runtime.native.verify_native_contract(tmp_path, *classes)

    async def changed(bot, unexpected):
        pass

    monkeypatch.setattr(type(b.bot), "get_updates", changed)
    with pytest.raises(RuntimeError, match="seam changed"):
        runtime.native.verify_native_contract(tmp_path, *classes)


def test_unclaimed_synthetic_recovery_and_second_turn_are_blocked(boundary):
    b = boundary

    async def check():
        event = b.adapter._build_message_event(NS(text="hello"), None, 1)
        with pytest.raises(RuntimeError, match="durable claim"):
            await b.adapter.runner._handle_message(event)
        assert [
            e.origin.platform for e in b.adapter.runner._resume_pending_candidates()
        ] == ["discord"]
        with b.execution._turn_scope(1):
            event.internal = True
            with pytest.raises(RuntimeError, match="Unclaimed"):
                await b.adapter.runner._handle_message(event)
            event.internal = False
            await b.adapter.runner._handle_message_with_agent(event)
            with pytest.raises(RuntimeError, match="second agent"):
                await b.adapter.runner._handle_message_with_agent(event)
        assert b.effects == ["hello"]

        async def detached_send():
            await asyncio.sleep(0)
            await b.bot._post("sendMessage", {"chat_id": 42, "text": "late reply"})

        with b.execution._turn_scope(2):
            late = asyncio.create_task(detached_send())
        with pytest.raises(RuntimeError, match="egress"):
            await late
        assert b.network == []

    asyncio.run(check())


def test_native_receipts_and_polling_contract_are_reused(boundary):
    b = boundary

    async def check():
        b.adapter._seen_update_ids["7:1"] = 100000
        b.queue.stage(7, [raw(1)])
        assert await b.bridge.once()
        assert b.effects == [] and b.queue.status() == {"completed": 1}
        options = await b.Updater().start_polling(drop_pending_updates=True, timeout=99)
        assert options["drop_pending_updates"] is False
        assert options["timeout"] == 25 and options["allowed_updates"] == [
            "message",
            "callback_query",
        ]
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
        b.adapter.runner._startup_restore_in_progress = True
        await b.adapter.connect()
        await asyncio.sleep(0)
        assert b.queue.status() == {"queued": 2} and b.effects == []
        b.adapter.runner._startup_restore_in_progress = False
        await asyncio.wait_for(b.started.wait(), 1)
        for _ in range(100):
            if b.queue.status() == {"completed": 2}:
                break
            await asyncio.sleep(0.001)
        assert b.effects == ["hello", "second"]
        assert b.queue.status() == {"completed": 2}
        await b.bridge.stop()

    asyncio.run(check())


@pytest.mark.parametrize(
    "outcome,state,error",
    [
        (TurnResult(True), "completed", None),
        (TurnResult(False, "provider_failed"), "failed", "provider_failed"),
        (TurnResult(False, "tool_failed"), "failed", "tool_failed"),
        (
            TurnResult(False, "native_cancelled", True),
            "interrupted",
            "turn_interrupted",
        ),
        (RuntimeError("SYNTHETIC_SECRET"), "failed", "turn_failed"),
        (asyncio.CancelledError(), "interrupted", "turn_interrupted"),
        (False, "failed", "turn_failed"),
    ],
)
def test_worker_outcomes_through_execution_seam(
    tmp_path, policy, caplog, outcome, state, error
):
    queue = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: 100000)
    calls = []

    async def execute(payload):
        calls.append(payload["update_id"])
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    execution = DeterministicTelegram(execute)
    worker = TelegramWorker(queue, execution)

    async def check():
        worker.stage(7, [raw(1), raw(1)])
        if isinstance(outcome, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await worker.once()
        else:
            assert await worker.once()
        row = queue.db.execute("SELECT * FROM updates").fetchone()
        assert (row["state"], row["error_code"], row["session_key"], row["text"]) == (
            state,
            error,
            "owner",
            None,
        )
        if state == "interrupted":
            with pytest.raises(RuntimeError, match="restart"):
                await worker.once()
            # Restart preserves terminal state and permits only one uncertainty notice.
            worker_after_restart = TelegramWorker(queue, execution)
            assert await worker_after_restart.once()
            assert not await worker_after_restart.once()
        else:
            if state == "failed":
                assert await worker.once()
            assert not await worker.once()
        assert calls == [1]
        assert execution.notices == ([] if state == "completed" else [1])

    try:
        asyncio.run(check())
        assert "SYNTHETIC_SECRET" not in caplog.text
    finally:
        queue.close()


def test_owner_deadline_stops_turn_and_preserves_pending(boundary):
    b = boundary
    b.execution.turn_timeout = 0.01

    async def check():
        b.queue.stage(7, [raw(1, "wait"), raw(2, "second")])
        await b.bridge.once()
        assert b.queue.status() == {"interrupted": 1, "queued": 1}
        assert b.bridge.retired
        b.release.set()
        await asyncio.sleep(0)
        assert not b.network

    asyncio.run(check())


def test_terminal_notices_use_durable_tool_dispatch_facts(boundary):
    b = boundary
    b.queue.stage(7, [raw(1, "hello")])
    b.queue.claim(7)
    assert b.queue.terminal_notice(7, 1) == (
        "I can’t reach a model right now, so I couldn’t carry out this request. Please try again later."
    )
    b.queue.mark_tools_started(7, 1)
    b.queue.finish(1, False)
    assert b.queue.terminal_notice(7, 1) == (
        "I couldn’t finish this request. Some actions may already have completed; please check before trying again."
    )
    assert b.queue.terminal_notice(7, 999).startswith("I couldn’t finish")


def test_tool_guard_blocks_late_calls_and_records_before_effect(boundary):
    b = boundary
    calls = []
    executor = NS(
        _dispatch_authorized_once=lambda agent, state, ref, *, execute: execute({})
    )
    b.execution.install_tool_guard(executor)
    b.queue.stage(7, [raw(1, "hello")])
    b.queue.claim(7)
    agent = NS()
    with b.execution._turn_scope(1) as turn:
        assert b.execution.inference_permitted(agent)
        executor._dispatch_authorized_once(
            agent, None, None, execute=lambda args: calls.append("write")
        )
        assert b.queue.terminal_notice(7, 1).startswith("I couldn’t finish")
        turn.completed = True
    with pytest.raises(RuntimeError, match="tool execution rejected"):
        executor._dispatch_authorized_once(
            agent, None, None, execute=lambda args: calls.append("late write")
        )
    assert calls == ["write"]


@pytest.mark.parametrize("complete", [True, False])
def test_shutdown_closes_intake_drains_one_turn_and_preserves_pending(
    boundary, complete
):
    b = boundary

    async def check():
        b.execution.turn_timeout = 0.1
        b.queue.stage(7, [raw(1, "wait"), raw(2, "pending")])
        await b.adapter.connect()
        await asyncio.wait_for(b.started.wait(), 1)
        stopped_poll = asyncio.Event()

        async def stop_poll():
            assert b.bridge._stopping
            stopped_poll.set()

        b.app.updater = NS(running=True, stop=stop_poll)
        stopping = asyncio.create_task(b.execution.shutdown())
        await asyncio.wait_for(stopped_poll.wait(), 1)
        with pytest.raises(RuntimeError, match="stopping"):
            b.bridge.stage(7, [raw(3, "new")])
        with pytest.raises(RuntimeError, match="stopping"):
            await b.bot.get_updates()
        assert b.queue.status() == {"started": 1, "queued": 1}
        assert not stopping.done()
        if complete:
            b.release.set()
        await asyncio.wait_for(stopping, 1)
        await b.execution.shutdown()
        assert b.queue.status() == {
            "completed" if complete else "interrupted": 1,
            "queued": 1,
        }
        assert b.effects == ["wait"]
        assert len(b.network) == (1 if complete else 0)
        if complete:
            assert b.network[0] == (
                "sendMessage",
                {"chat_id": 42, "text": "native reply"},
            )
        else:
            b.release.set()
            await asyncio.sleep(0.01)
            assert b.network == []
        with pytest.raises(RuntimeError):
            await b.adapter.connect()

    asyncio.run(check())


def test_shutdown_during_tool_preserves_uncertainty_and_blocks_late_effects(
    boundary, monkeypatch
):
    import contextvars
    import threading

    b = boundary
    tool_started, tool_release, tool_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    errors = []
    threads = []
    effects = []
    executor = NS(
        _dispatch_authorized_once=lambda agent, state, ref, *, execute: execute({})
    )
    b.execution.install_tool_guard(executor)

    interrupts = []
    monkeypatch.setitem(
        sys.modules,
        "agent.interrupt_compat",
        NS(request_hard_interrupt=lambda *args: interrupts.append(True)),
    )

    def tool(agent):
        def write(args):
            effects.append("write")
            tool_started.set()
            assert tool_release.wait(2)

        executor._dispatch_authorized_once(agent, None, None, execute=write)
        with pytest.raises(RuntimeError, match="tool execution rejected"):
            executor._dispatch_authorized_once(
                agent, None, None, execute=lambda args: effects.append("late write")
            )

    async def run_agent(*args):
        agent = NS()
        assert b.execution.inference_permitted(agent)

        def invoke():
            try:
                tool(agent)
            except BaseException as error:  # noqa: BLE001 -- report thread failures in the parent test
                errors.append(error)
            finally:
                tool_done.set()

        thread = threading.Thread(
            target=contextvars.copy_context().run, args=(invoke,), daemon=True
        )
        threads.append(thread)
        thread.start()
        while not tool_done.is_set():
            await asyncio.sleep(0.001)
        return {"final_response": "must not escape"}

    b.adapter.runner._run_agent = run_agent

    async def check():
        b.execution.turn_timeout = 0.1
        b.queue.stage(7, [raw(1, "tool"), raw(2, "pending")])
        await b.adapter.connect()
        while not tool_started.is_set():
            await asyncio.sleep(0.001)
        await b.execution.shutdown()
        assert b.queue.status() == {"interrupted": 1, "queued": 1}
        assert "Some actions may already have completed" in b.queue.terminal_notice(
            7, 1
        )
        tool_release.set()
        await asyncio.sleep(0.02)
        assert tool_done.is_set() and errors == []
        assert effects == ["write"] and b.network == []
        assert interrupts == [True]
        b.queue.recover()
        assert b.queue.status() == {"interrupted": 1, "queued": 1}

    try:
        asyncio.run(asyncio.wait_for(check(), 2))
    finally:
        tool_release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.usefixtures("mcp_environment")
@pytest.mark.parametrize("failed_preview", [False, True])
def test_bound_stdio_proposals_preview_apply_and_repetition(
    boundary, tmp_path, monkeypatch, failed_preview
):
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from vault_helpers import create_vault

    from emuru.telegram.worker import TurnResult
    from emuru.vault import mutations

    b = boundary
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    vault = create_vault(tmp_path)
    for name, value in {
        "mutation-policy.json": {
            "enabled": True,
            "roots": ["10_Projects"],
            "vault_path": str(vault),
        },
        "vault-target.json": {"mode": "real", "vault_path": str(vault)},
    }.items():
        record = home / name
        record.write_text(json.dumps(value))
        record.chmod(0o600)
    b.execution.mutation_target = (home, vault)
    model_calls = []
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "emuru.vault.server"],
        env={"EMURU_VAULT_PATH": str(vault), "EMURU_MUTATION_PROFILE_HOME": str(home)},
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    async def check():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "vault_propose" in tools and "vault_apply" not in tools
            denied = await client.call_tool(
                "vault_propose",
                {
                    "path": "10_Projects/new.md",
                    "content": "claimed authority",
                    "request_id": "owner",
                },
            )
            assert denied.is_error

            async def model(payload, session):
                model_calls.append(payload["message"]["text"])
                result = await client.call_tool(
                    "vault_propose",
                    {
                        "path": "10_Projects/new.md",
                        "content": "# Project\n" + "😀" * 5000,
                        "request_id": "model-cannot-set-owner",
                    },
                )
                assert not result.is_error
                return TurnResult(True)

            monkeypatch.setattr(b.execution, "_execute_native", model)
            b.queue.stage(7, [raw(1, "create project")])
            if failed_preview:
                b.bot.fail_send = True
            await b.bridge.once()
            assert not (vault / "10_Projects/new.md").exists()
            with mutations.journal(home) as db:
                proposal = db.execute(
                    "SELECT proposal_id FROM proposal_bindings"
                ).fetchone()[0]
                complete = db.execute(
                    "SELECT complete FROM preview_deliveries"
                ).fetchone()[0]
                assert complete == int(not failed_preview)
            b.bot.fail_send = False
            if failed_preview:
                # Failure notice gets one attempt before the next queued command.
                await b.bridge.once()
                b.queue.stage(7, [raw(2, f"/apply {proposal} writers-paused")])
                await b.bridge.once()
                assert not (vault / "10_Projects/new.md").exists()
            b.queue.stage(7, [raw(3, "create project")])
            await b.bridge.once()
            assert model_calls == ["create project"]
            sent = [data for endpoint, data in b.network if endpoint == "sendDocument"]
            assert sent and all(
                data["caption"].startswith("EMURU approval:") for data in sent
            )
            assert all(
                data["document"].endswith(("😀" * 5000).encode()) for data in sent
            )
            assert all(
                len(data["reply_markup"]["inline_keyboard"][0]) == 2 for data in sent
            )
            b.queue.stage(7, [raw(4, "/apply"), raw(5, f"/apply {proposal}")])
            await b.bridge.once()
            await b.bridge.once()
            assert not (vault / "10_Projects/new.md").exists()
            b.queue.stage(7, [raw(6, f"/apply {proposal} writers-paused")])
            b.bot.fail_send = failed_preview
            await b.bridge.once()
            b.bot.fail_send = False
            if failed_preview:
                await b.bridge.once()
                assert any(
                    "Mutation outcome:" in data.get("text", "")
                    and "applied" in data["text"]
                    for endpoint, data in b.network
                    if endpoint == "sendMessage"
                )
            assert (vault / "10_Projects/new.md").exists()
            modified = (vault / "10_Projects/new.md").stat().st_mtime_ns
            b.queue.stage(7, [raw(7, f"/apply {proposal} writers-paused")])
            b.bot.fail_send = failed_preview
            await b.bridge.once()
            b.bot.fail_send = False
            if failed_preview:
                await b.bridge.once()
                assert "Mutation outcome:" in b.network[-1][1]["text"]
                assert "applied" in b.network[-1][1]["text"]
            assert (vault / "10_Projects/new.md").stat().st_mtime_ns == modified
            b.queue.stage(7, [raw(8, "/new"), raw(9, "create project")])
            # Fake native model isn't needed for the reset in this integration test.
            row = b.queue.claim(7)
            b.queue.finish(row["sequence"], True)
            await b.bridge.once()
            assert model_calls == ["create project"]
            # Request bindings survive Telegram terminal-receipt pruning.
            with b.queue.transaction():
                b.queue._prune(100000 + 8 * 86400)
            b.queue.stage(7, [raw(10, "create project")])
            await b.bridge.once()
            assert model_calls == ["create project"]
            b.queue.stage(
                7,
                [
                    raw(11, "/propose-new create project"),
                    raw(11, "/propose-new create project"),
                ],
            )
            await b.bridge.once()
            assert model_calls == ["create project", "create project"]
            assert not await b.bridge.once()

            async def capture(payload, session):
                model_calls.append("capture")
                result = await client.call_tool(
                    "vault_write",
                    {"path": "00_Inbox/owned-capture.md", "content": "idea"},
                )
                assert not result.is_error
                return TurnResult(True)

            monkeypatch.setattr(b.execution, "_execute_native", capture)
            b.queue.stage(7, [raw(12, "capture idea"), raw(13, "capture idea")])
            await b.bridge.once()
            await b.bridge.once()
            assert model_calls.count("capture") == 1
            assert (vault / "00_Inbox/owned-capture.md").read_text() == "idea\n"

    asyncio.run(asyncio.wait_for(check(), 60))


def test_model_and_previews_share_one_turn_budget(boundary, monkeypatch):
    b = boundary
    b.execution.turn_timeout = 0.1
    phases = []

    async def model_and_previews(payload, session):
        await asyncio.sleep(0.01)
        phases.append("model completed")
        await asyncio.sleep(0.15)
        phases.append("preview completed")
        return TurnResult(True)

    monkeypatch.setattr(b.execution, "_execute", model_and_previews)
    b.queue.stage(7, [raw(1)])
    asyncio.run(b.bridge.once())
    assert phases == ["model completed"]
    assert b.queue.status() == {"interrupted": 1}
    assert b.bridge.retired
