"""Installed-native session regression, called by the offline runtime seam check."""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch


async def check(run, adapter, bridge, directory):
    from hermes_constants import get_hermes_home

    assert get_hermes_home().name.startswith("emuru-telegram-native-home-")
    from agent import system_prompt
    from gateway.config import GatewayConfig
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    root = Path(directory) / "native-session"
    db = SessionDB(root / "state.db")

    class IsolatedStore(SessionStore):
        def _open_session_db_for_active_scope(self, db_path=None):
            return db

    config = GatewayConfig(sessions_dir=root / "sessions", multiplex_profiles=False)
    store = IsolatedStore(config.sessions_dir, config)
    store._db = db  # Pin all native transcript/routing operations to this temporary DB.
    runner = adapter.gateway_runner
    runner.config, runner.session_store = config, store
    runner._init_lifecycle_state()
    runner._init_runtime_caches()
    runner._session_db = db
    captured, agents, transitions = [], [], []
    marker = "UNSAVED_SYNTHETIC_MARKER_984"
    question = "What synthetic marker did I give you?"

    async def no_external_hooks(*args, **kwargs):
        pass

    # Keep native reset, cache eviction, conversation clearing and history replay;
    # only resource cleanup, optional lifecycle plugins and the provider are fakes.
    runner._cleanup_old_agent_for_reset = no_external_hooks
    runner._fire_session_reset_hooks = no_external_hooks
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._spawn_release_thread = lambda *args, **kwargs: None

    async def executor(event):
        key = runner._session_key_for_source(event.source)
        if event.text == "/new":
            before = store._entries[key].session_id
            handled, reply = await runner._hm_cmd_new(event, event.source, key)
            assert handled and key not in runner._agent_cache
            transitions.append((before, store._entries[key].session_id))
            return reply
        entry = store.get_or_create_session(event.source)
        history = store.load_transcript(entry.session_id)
        notes = []
        await runner._hmwa_first_contact_notes(event.source, history, notes)
        assert not notes
        cached = runner._agent_cache.get(key)
        agent = (
            cached[0]
            if cached
            else NS(
                session_id=entry.session_id,
                _session_messages=[],
                valid_tool_names={"mcp__vault__vault_search"},
                model="synthetic",
                _tool_use_enforcement=False,
                _execution_guidance=False,
            )
        )
        agents.append(agent)
        context = NS(
            history=history,
            session_id=entry.session_id,
            session_key=key,
            channel_prompt=None,
            user_config={},
        )
        native_turn = TurnRunner(runner, context)
        provider_history, _, _ = native_turn._load_turn_history(agent, bool(cached))
        system = "\n".join(filter(None, system_prompt._guidance_parts(agent)))
        messages = [
            {"role": "system", "content": system},
            *provider_history,
            {"role": "user", "content": event.text},
        ]
        captured.append((entry.session_id, messages))
        assert "OUT-OF-BAND USER MESSAGE" not in json.dumps(messages)
        assert "<their message>" not in json.dumps(messages)
        user = {"role": "user", "content": event.text}
        reply = {"role": "assistant", "content": "Synthetic provider reply"}
        store.append_to_transcript(entry.session_id, user)
        store.append_to_transcript(entry.session_id, reply)
        agent._session_messages = [*provider_history, user, reply]
        runner._agent_cache[key] = (
            agent,
            "synthetic",
            len(agent._session_messages),
            entry.session_id,
        )
        return reply["content"]

    adapter._message_handler = executor
    bot = adapter._app.bot
    now = int(bridge.queue.now())
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
        for uid, text in ((31, marker), (32, "/new"), (33, question))
    ]
    try:
        bridge.queue.stage(bot.id, updates, bot.username)
        with patch("hermes_cli.lifecycle.invoke_hook"):
            while await bridge.once():
                pass
        assert len(captured) == 2 and len(transitions) == 1
        old, new = transitions[0]
        assert old != new and captured[0][0] == old and captured[1][0] == new
        assert agents[0] is not agents[1] and agents[1].session_id == new
        assert marker not in json.dumps(captured[1][1])
        assert captured[1][1][-1] == {"role": "user", "content": question}
        assert marker in json.dumps(store.load_transcript(old))
        bridge.queue.stage(bot.id, [updates[1]], bot.username)
        assert not await bridge.once() and len(transitions) == 1
        histories = {sid: store.load_transcript(sid) for sid in (old, new)}
        db.close()
        db = SessionDB(root / "state.db")
        recreated = IsolatedStore(config.sessions_dir, config)
        recreated._db = db
        assert {sid: recreated.load_transcript(sid) for sid in (old, new)} == histories

        if adapter._update_receipt_flush is not None:
            await adapter._update_receipt_flush
        receipts = bridge.execution._receipts
        replacement = NS(
            _update_receipt_dir=adapter._update_receipt_dir,
            _update_receipts_loaded=set(),
            _seen_update_ids={},
        )
        receipts._load_receipts(replacement, bot.id)
        assert {f"{bot.id}:{uid}" for uid in (1, 2, 31, 32, 33)}.issubset(
            replacement._seen_update_ids
        )
        receipt_path = receipts._receipt_path(adapter, bot.id)
        before = receipt_path.read_bytes(), receipt_path.stat().st_mtime_ns
        receipts._load_receipts(replacement, bot.id)
        assert (receipt_path.read_bytes(), receipt_path.stat().st_mtime_ns) == before
    finally:
        db.close()


def check_tools(execution, directory):
    """Actual pinned dispatch callback; synthetic effect survives failure without replay."""
    from agent import tool_executor

    queue = execution.queue
    bot, username = execution.identity()
    now = int(queue.now())
    payload = {
        "update_id": 90,
        "message": {
            "message_id": 90,
            "date": now,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "synthetic effect",
        },
    }
    queue.stage(bot, [payload], username)
    row = queue.claim(bot, username)
    assert row["update_id"] == 90
    execution.install_tool_guard(tool_executor)
    agent = NS(_tool_guardrails=NS(before_call=lambda *args: NS(allows_execution=True)))
    state = NS(args={})
    ref = NS(name="mcp__vault__vault_write", args={})
    inbox = Path(directory) / "synthetic-vault" / "00_Inbox"
    inbox.mkdir(parents=True)
    note = inbox / "one.md"

    def write(args):
        with note.open("x") as file:
            file.write("# Synthetic committed effect\n")
        return "created"

    with (
        patch.object(tool_executor, "_pre_tool_block", return_value=(None, {})),
        patch.object(tool_executor, "_begin_tool_execution"),
        patch.object(
            tool_executor,
            "_run_with_activity_heartbeat",
            side_effect=lambda agent, name, callback: callback(),
        ),
        patch("agent.terminal_approval_batch.prepare_current_terminal"),
    ):
        with execution._turn_scope(90) as turn:
            assert execution.inference_permitted(agent)
            result = tool_executor._dispatch_authorized_once(
                agent,
                state,
                ref,
                execute=write,
                scope_block=None,
                display_index=None,
                begin_execution=None,
                authorization_gate=None,
            )
            assert result == "created"
            turn.completed = True
        try:
            tool_executor._dispatch_authorized_once(
                agent,
                state,
                ref,
                execute=write,
                scope_block=None,
                display_index=None,
                begin_execution=None,
                authorization_gate=None,
            )
        except RuntimeError as error:
            assert str(error) == "Owner turn ended; tool execution rejected"
        else:
            raise AssertionError("Late tool dispatch was allowed")
    queue.finish(row["sequence"], False, "provider_failed")
    assert note.exists() and len(list(inbox.glob("*.md"))) == 1
    assert queue.terminal_notice(bot, 90).startswith("I couldn’t finish")
    assert queue.stage(bot, [payload], username) == ["failed"]
    assert queue.claim(bot, username) is None


async def check_mutations(adapter, execution, directory):
    """Pinned PTB command admission and final preview/application; no provider or network."""
    import shutil

    from emuru.vault import mutations

    home = Path(directory) / "mutation-profile"
    home.mkdir(mode=0o700)
    vault = Path(directory) / "mutation-vault"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "fixtures/hermes-vault", vault
    )
    for name, data in {
        "mutation-policy.json": {
            "enabled": True,
            "roots": ["10_Projects"],
            "vault_path": str(vault),
        },
        "vault-target.json": {"mode": "real", "vault_path": str(vault)},
    }.items():
        record = home / name
        record.write_text(json.dumps(data))
        record.chmod(0o600)
    execution.mutation_target = (home, vault)
    proposals, model_calls = [], []

    async def model(event):
        model_calls.append(event.text)
        identity = mutations.authenticated_request(home, vault)
        number = len(proposals) + 1
        revising = event.text.startswith("The owner requests a replacement proposal.")
        path = (
            proposals[-1]["path"]
            if revising
            else f"10_Projects/native-owner-{number}.md"
        )
        content = (
            "# Revised native project\n- [ ] Revised task ^task-native\n"
            if revising
            else f"# Native owner project {number}\n- [ ] Task ^task-native-{number}\n"
        )
        result = mutations.propose_note(
            home,
            vault,
            path,
            content=content,
            authenticated_id=identity,
        )
        proposals.append(result)
        return "Stored proposal; no mutation applied."

    adapter._message_handler = model
    queue, bot = execution.queue, adapter._app.bot

    async def request(uid, text, reply=None):
        payload = {
            "update_id": uid,
            "message": {
                "message_id": uid,
                "date": int(queue.now()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False},
                "text": text,
            },
        }
        if reply is not None:
            payload["message"]["reply_to_message"] = {
                "message_id": 500,
                "date": int(queue.now()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": bot.id, "is_bot": True, "first_name": "EMURU"},
                "text": "EMURU approval: " + reply,
            }
        queue.stage(bot.id, [payload], bot.username)
        while await execution.worker.once():
            pass
        assert (
            queue.db.execute(
                "SELECT state FROM updates WHERE update_id=?", (uid,)
            ).fetchone()[0]
            == "completed"
        )

    await request(100, "create native project")
    proposal = proposals[0]
    identity = proposal["proposal_id"]
    assert not (vault / proposal["path"]).exists()
    await request(101, "create native project")
    assert model_calls == ["create native project"]
    await request(102, "/apply")
    await request(103, f"/apply {identity}")
    assert not (vault / proposal["path"]).exists()
    await request(104, f"/apply {identity} writers-paused")
    assert (vault / proposal["path"]).read_text() == proposal["content"]
    modified = (vault / proposal["path"]).stat().st_mtime_ns
    await request(105, f"/apply {identity} writers-paused")
    assert (vault / proposal["path"]).stat().st_mtime_ns == modified
    await request(106, "/propose-new create native project")
    assert model_calls == ["create native project", "create native project"]
    cancelled = proposals[1]
    assert cancelled["proposal_id"] != identity
    assert not (vault / cancelled["path"]).exists()
    owner, bot_id = execution.queue.owner_id, execution.identity()[0]
    assert (
        mutations.owner_preview(home, vault, cancelled["proposal_id"], owner, bot_id)[
            "proposal_id"
        ]
        == cancelled["proposal_id"]
    )
    try:
        mutations.owner_preview(
            home, vault, cancelled["proposal_id"], owner + 1, bot_id
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Proposal preview was not bound to its owner")
    await request(108, f"/cancel {cancelled['proposal_id']}")
    await request(109, f"/apply {cancelled['proposal_id']} writers-paused")
    assert not (vault / cancelled["path"]).exists()
    await request(107, f"/cancel {identity}")
    assert (vault / proposal["path"]).stat().st_mtime_ns == modified
    # Exercise real PTB admission, durable normalization and native operator commands.
    await request(110, "/propose-new create conversational project")
    pending = proposals[-1]
    assert not (vault / pending["path"]).exists()
    await request(111, "Change the first task", reply=pending["proposal_id"])
    revised = proposals[-1]
    assert (
        revised["proposal_id"] != pending["proposal_id"]
        and revised["path"] == pending["path"]
    )
    assert (
        mutations.owner_preview(home, vault, pending["proposal_id"], owner, bot_id)[
            "outcome"
        ]
        == "cancelled"
    )
    assert not (vault / revised["path"]).exists()
    await request(112, "yes", reply=revised["proposal_id"])
    assert not (vault / revised["path"]).exists()
    await request(113, "approve, writers paused", reply=revised["proposal_id"])
    assert (vault / revised["path"]).read_text() == revised["content"]
    stamp = (vault / revised["path"]).stat().st_mtime_ns
    await request(114, "approve, writers paused", reply=revised["proposal_id"])
    assert (vault / revised["path"]).stat().st_mtime_ns == stamp
    await request(115, "/propose-new create rejected project")
    rejected = proposals[-1]
    await request(116, "reject", reply=rejected["proposal_id"])
    await request(117, "approve, writers paused", reply=rejected["proposal_id"])
    assert not (vault / rejected["path"]).exists()
    assert (
        mutations.owner_preview(home, vault, rejected["proposal_id"], owner, bot_id)[
            "outcome"
        ]
        == "cancelled"
    )
    execution.mutation_target = None
