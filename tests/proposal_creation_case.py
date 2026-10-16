"""Exact owner creation prompt against synthetic notes; optionally use the live authenticated gateway."""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from mcp import Client

from emuru.models.gateway import write_private
from emuru.telegram.queue import TelegramQueue, load_telegram_settings
from emuru.vault import mutations
from emuru.vault.indexer import INDEXABLE_ROOTS
from emuru.vault.mcp import create_mcp

PROMPT = 'Create a new project named "Release Validation" with the goal "Verify EMURU v0.5.0 manual acceptance". Its first task should be "Validate project creation". Show me the exact proposed changes before applying anything.'


async def check(chat, root):
    with tempfile.TemporaryDirectory(prefix="emuru-owner-proposal-") as temporary:
        private = Path(temporary)
        home = private / "profiles/emuru"
        home.mkdir(parents=True, mode=0o700)
        vault = private / "vault"
        shutil.copytree(root / "tests/fixtures/hermes-vault", vault)
        (vault / ".git").mkdir(exist_ok=True)
        write_private(
            home / "vault-target.json",
            json.dumps({"mode": "real", "vault_path": str(vault)}),
        )
        write_private(
            home / "mutation-policy.json",
            json.dumps(
                {"enabled": True, "roots": ["10_Projects"], "vault_path": str(vault)}
            ),
        )
        queue = TelegramQueue(
            home / "emuru-telegram/queue.sqlite3",
            load_telegram_settings(root / "infra/hermes/telegram-settings.json"),
            42,
            clock=lambda: 100000,
        )
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 100000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False},
                "text": PROMPT,
            },
        }
        queue.stage(7, [payload])
        assert queue.claim(7)
        identity, repeated = mutations.begin_request(home, vault, 42, 7, 1, PROMPT)
        assert not repeated
        mutations.activate_request(home, identity, queue.path, 7, 1)
        before = {
            p: p.read_bytes()
            for p in vault.rglob("*.md")
            if p.relative_to(vault).parts[0] in INDEXABLE_ROOTS
        }
        trace = []
        try:
            async with Client(create_mcp(vault, proposal_home=home)) as client:
                definitions = (await client.list_tools()).tools
                proposal_tool = next(
                    t for t in definitions if t.name == "vault_propose"
                )
                assert "Approved roots: 10_Projects" in proposal_tool.description
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": "mcp__vault__" + t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        },
                    }
                    for t in definitions
                ]
                messages = [
                    {
                        "role": "system",
                        "content": (root / "agents/emuru/SOUL.md").read_text(),
                    },
                    {"role": "user", "content": PROMPT},
                ]
                for _ in range(12):
                    message = chat(messages, tools)
                    messages.append(message)
                    calls = message.get("tool_calls", [])
                    if not calls:
                        break
                    for call in calls:
                        name = call["function"]["name"].removeprefix("mcp__vault__")
                        assert (
                            name in {t.name for t in definitions}
                            and name != "vault_write"
                        ), "Creation must use a stored proposal, never Inbox capture"
                        args = json.loads(call["function"]["arguments"])
                        trace.append(name)
                        result = await client.call_tool(name, args)
                        assert not result.is_error, result.content
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps(result.structured_content),
                            }
                        )
                else:
                    raise AssertionError("Creation did not finish within twelve turns")
            previews = mutations.request_previews(home, identity)
            assert len(previews) == 1 and "vault_propose" in trace, (
                "Owner creation request returned an unstored draft"
            )
            preview = previews[0]
            assert (
                preview["path"].startswith("10_Projects/")
                and preview["before_revision"] is None
            )
            assert preview["outcome"] == "proposed" and "changes" not in preview
            for phrase in (
                "Release Validation",
                "Verify EMURU v0.5.0 manual acceptance",
                "Validate project creation",
            ):
                assert phrase.lower() in preview["content"].lower()
            assert "vault_search" in trace or "vault_map" in trace
            assert {
                p: p.read_bytes()
                for p in vault.rglob("*.md")
                if p.relative_to(vault).parts[0] in INDEXABLE_ROOTS
            } == before
            assert not (vault / preview["path"]).exists()
            assert not any(
                t.name in {"vault_apply", "vault_cancel"} for t in definitions
            )
            assert (
                mutations.owner_preview(home, vault, preview["proposal_id"], 42, 7)[
                    "proposal_id"
                ]
                == preview["proposal_id"]
            )
            return trace
        finally:
            queue.close()


def gateway_chat(messages, tools):
    body = {
        "model": "emuru",
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "max_tokens": 4096,
    }
    request = Request(
        "http://litellm:4000/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["EMURU_GATEWAY_KEY"],
            "Content-Type": "application/json",
        },
    )
    with build_opener(ProxyHandler({})).open(request, timeout=120) as response:
        return json.load(response)["choices"][0]["message"]


if __name__ == "__main__":
    os.umask(0o077)
    trace = asyncio.run(check(gateway_chat, Path("/opt/emuru")))
    print(
        "Exact owner creation prompt: stored non-mutating, owner-bound proposal PASS; tools="
        + ",".join(trace)
    )
