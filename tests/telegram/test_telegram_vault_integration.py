"""Durable replay with a deterministic fake agent and the real synthetic stdio MCP."""

import asyncio
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from emuru.telegram_queue import TelegramQueue


def test_replay_read_write_reindex_and_ambiguous_write_are_not_retried(
    tmp_path, policy, runtime
):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[2]
    vault = tmp_path / "vault"
    shutil.copytree(root / "tests/fixtures/hermes-vault", vault)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env["EMURU_VAULT_PATH"] = str(vault)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "emuru.mcp_server"], env=env, cwd=str(root)
    )
    queue_path = tmp_path / "queue/queue.sqlite3"
    protected = {p: p.read_bytes() for p in vault.rglob("*.md")}

    def raw(uid, text):
        return {
            "update_id": uid,
            "message": {
                "message_id": uid,
                "date": 100000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False},
                "text": text,
            },
        }

    async def check():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "vault_map",
                "vault_search",
                "vault_open",
                "vault_neighbors",
                "vault_write",
            }
            calls, notices = [], []

            async def call(name, arguments):
                calls.append(name)
                result = await session.call_tool(name, arguments)
                assert not result.is_error
                return result.model_dump_json()

            async def fake_agent(app, payload):
                if payload["message"]["text"] == "read":
                    found = await call("vault_search", {"query": "EMURU"})
                    assert "10_Projects/EMURU.md" in found
                    opened = await call("vault_open", {"path": "10_Projects/EMURU.md"})
                    assert "Synthetic source marker" in opened
                    return True
                if payload["message"]["text"] == "save":
                    await call(
                        "vault_write",
                        {
                            "path": "00_Inbox/telegram-memory.md",
                            "content": "# Memory\n\nTELEGRAM_MEMORY_742",
                        },
                    )
                    indexed = await call(
                        "vault_search", {"query": "TELEGRAM_MEMORY_742"}
                    )
                    assert "telegram-memory.md" in indexed
                    # Simulate process death after the committed write, before
                    # completion/egress. A later worker must never call it again.
                    raise asyncio.CancelledError
                raise AssertionError("Unexpected deterministic prompt")

            async def report(app, uid):
                notices.append(uid)

            app = NS(bot=NS(id=7, username="emuru"))
            q = TelegramQueue(queue_path, policy, 42, clock=lambda: 100000)
            q.stage(7, [raw(1, "read"), raw(1, "read"), raw(2, "save")])
            bridge = runtime.Bridge(q, fake_agent, report)
            assert await bridge.once(app)
            try:
                await bridge.once(app)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("The crash simulation did not interrupt the turn")
            q.close()
            q = TelegramQueue(queue_path, policy, 42, clock=lambda: 100000)
            q.recover()
            bridge = runtime.Bridge(q, fake_agent, report)
            while await bridge.once(app):
                pass
            q.stage(7, [raw(2, "save"), raw(1, "read")])
            assert not await bridge.once(app)
            assert notices == [2] and calls.count("vault_write") == 1
            assert q.status() == {"completed": 1, "interrupted": 1}
            q.close()
            assert (vault / "00_Inbox/telegram-memory.md").read_text().count(
                "TELEGRAM_MEMORY_742"
            ) == 1
            assert all(p.read_bytes() == data for p, data in protected.items())

    asyncio.run(asyncio.wait_for(check(), timeout=30))
