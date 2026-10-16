"""Bounded synthetic model acceptance; no production route or application."""

import json
import re
import runpy
from pathlib import Path

from mcp import Client

from emuru.vault.mcp import create_mcp

ROOT = Path(__file__).resolve().parents[1]
CHAT = runpy.run_path(str(ROOT / "scripts/hermes/qualify.py"))["chat"]
CONVERSATION = json.loads(
    (ROOT / "tests/fixtures/project-update-conversation.json").read_text()
)
FORMAT = """
For this synthetic acceptance case, include the final draft in a JSON code block with exactly:
path, expected_revision, anchor, replacement, references, applied, proposal_id.
Encode Markdown inside the replacement JSON string; use only JSON code blocks.
references is a list of {path, block_id, text} for existing affected blocks.
Use null block_id for an existing line without a block ID; never invent IDs.
For separate drafts, return separate JSON code blocks. For creation, anchor and
expected_revision are null and replacement contains the complete Markdown draft.
Use null proposal_id when no proposal was stored.
This output format grants no tool authority.
"""


async def update_case(inference, model, vault, path, prompt, *, home=None, soul=None):
    """Allow real retrieval/proposals; reject the observed Inbox substitute before effects."""
    server = create_mcp(vault, proposal_home=home)
    creating = not (vault / path).exists()
    before = {p: p.read_bytes() for p in vault.rglob("*.md")}
    guidance = soul or (ROOT / "agents/emuru/SOUL.md").read_text()
    messages = [
        {
            "role": "system",
            "content": guidance,
        },
        {"role": "user", "content": prompt + "\n" + FORMAT},
    ]
    opened, previews, trace = {}, [], []
    async with Client(server) as client:
        definitions = (await client.list_tools()).tools
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
        assert not any("apply" in t.name for t in definitions)
        for _ in range(8):
            message, _, _ = CHAT(
                inference,
                model,
                messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=2048,
            )
            messages.append(message)
            calls = message.get("tool_calls", [])
            if not calls:
                content = message["content"]
                encoded = re.findall(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if not encoded:
                    encoded = [content]
                drafts = []
                for item in encoded:
                    assert "{" in item, content
                    draft, _ = json.JSONDecoder().raw_decode(item[item.index("{") :])
                    drafts.append(draft)
                break
            for call in calls:
                registered_name = call["function"]["name"]
                assert registered_name in {t["function"]["name"] for t in tools}, (
                    registered_name
                )
                name = registered_name.removeprefix("mcp__vault__")
                args = json.loads(call["function"]["arguments"])
                trace.append(name)
                assert name != "vault_write", (
                    "project update substituted an Inbox capture"
                )
                assert name in {t.name for t in definitions}, (
                    "unregistered tool requested"
                )
                if name == "vault_propose":
                    assert args["path"] == path
                    if creating:
                        assert opened and args.get("content")
                    else:
                        assert path in opened, (
                            "proposal without opening resolved project"
                        )
                        assert args.get("content") is None
                        assert args["expected_revision"] == opened[path]["revision"]
                        assert opened[path]["content"].count(args["anchor"]) == 1
                result = await client.call_tool(name, args)
                assert not result.is_error, result.content
                data = result.structured_content
                if name == "vault_open":
                    assert not data["truncated"]
                    opened[args["path"]] = data
                if name == "vault_propose":
                    previews.append(data)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(data),
                    }
                )
        else:
            raise AssertionError("project update did not finish within eight turns")
    assert "no update was applied" in content.lower()
    assert "vault_search" in trace or "vault_map" in trace or path in prompt
    assert opened and (creating or path in opened), "draft without opening its source"
    source = "" if creating else opened[path]["content"]
    for draft in drafts:
        assert set(draft) == {
            "path",
            "expected_revision",
            "anchor",
            "replacement",
            "references",
            "applied",
            "proposal_id",
        }
        assert draft["path"] == path and draft["applied"] is False
        if creating:
            assert draft["expected_revision"] is None and draft["anchor"] is None
            replacement = draft["replacement"]
            assert replacement.startswith("---\n")
            for field in ("title", "type", "project", "tags", "summary"):
                assert re.search(rf"^{field}:", replacement, re.MULTILINE)
            for section in ("Goals", "Tasks", "Decisions", "Results", "Next actions"):
                assert f"## {section}" in replacement
            assert any(
                f"[[{p.removesuffix('.md')}]]" in replacement
                or f"[[{p}]]" in replacement
                for p in opened
            )
            if draft["proposal_id"] is not None:
                preview = next(
                    p for p in previews if p["proposal_id"] == draft["proposal_id"]
                )
                assert (
                    preview["outcome"] == "proposed"
                    and preview["content"] == replacement
                )
            continue
        assert draft["expected_revision"] == opened[path]["revision"]
        anchor, replacement = draft["anchor"], draft["replacement"]
        assert anchor and source.count(anchor) == 1 and anchor != source
        assert replacement != anchor
        affected = set(re.findall(r"\^([\w-]+)", anchor))
        assert affected <= set(re.findall(r"\^([\w-]+)", replacement))
        for ref in draft["references"]:
            assert ref["path"] == path
            assert ref["text"] in source.splitlines()
            if ref["block_id"] is None:
                assert not re.search(r"\^[\w-]+$", ref["text"])
            else:
                assert ref["text"].endswith("^" + ref["block_id"].removeprefix("^"))
        if draft["proposal_id"] is not None:
            preview = next(
                p for p in previews if p["proposal_id"] == draft["proposal_id"]
            )
            assert preview["outcome"] == "proposed"
            assert preview["content"] == source.replace(anchor, replacement, 1)
    replacements = "\n".join(d["replacement"] for d in drafts)
    if "sqlite" in prompt.lower():
        assert "sqlite" in replacements.lower()
    if "flask" in prompt.lower():
        assert "flask" in replacements.lower()
    if "task complete" in prompt:
        assert "[x]" in replacements
    assert {d["proposal_id"] for d in drafts if d["proposal_id"]} == {
        p["proposal_id"] for p in previews
    }
    assert {p: p.read_bytes() for p in vault.rglob("*.md")} == before
    return drafts, trace
