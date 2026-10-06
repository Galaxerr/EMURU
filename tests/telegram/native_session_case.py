"""Installed-native session regression, called by the offline runtime seam check."""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch


async def check(run, adapter, bridge, directory):
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
    finally:
        db.close()
