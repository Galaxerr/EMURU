"""Existing-project revision contract over real MCP; no model acceptance claimed."""

import hashlib
import json

import pytest
from mcp import Client
from project_update_case import update_case
from vault_helpers import create_vault

from emuru.vault import mutations
from emuru.vault.mcp import create_mcp

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("mcp_environment")]


def setup(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    vault = create_vault(tmp_path)
    path = "10_Projects/CSV-tool.md"
    raw = (
        b"# CSV tool\r\n\r\nManual paragraph: keep exactly.\r\n"
        b"## Tasks\r\n- [ ] Read CSV files ^task-read\r\n"
        b"\r\n## Decisions\r\n- Standard library only ^decision-library\r\n"
    )
    (vault / path).write_bytes(raw)
    return home, vault, path, raw


async def test_registered_schema_explains_revision_copy_and_bounded_recovery(tmp_path):
    home, vault, _, _ = setup(tmp_path)
    async with Client(create_mcp(vault, proposal_home=home)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "vault_map",
            "vault_search",
            "vault_open",
            "vault_neighbors",
            "vault_write",
            "vault_propose",
        }
        schema = tools["vault_propose"].input_schema
        assert set(schema["properties"]) == {
            "path",
            "content",
            "expected_revision",
            "anchor",
            "replacement",
            "request_id",
        }
        assert "revision" in tools["vault_open"].description
        assert (
            "exact string" in schema["properties"]["expected_revision"]["description"]
        )
        assert (
            "Do not repeat an identical failed call"
            in tools["vault_propose"].description
        )
        assert "reopen the same path at most once" in tools["vault_propose"].description


@pytest.mark.parametrize(
    "bad_revision", ["missing", "content", "invented", "partial", "external"]
)
async def test_bad_revision_then_one_fresh_open_preserves_note(tmp_path, bad_revision):
    home, vault, path, raw = setup(tmp_path)
    target = vault / path
    async with Client(create_mcp(vault, proposal_home=home)) as client:
        response = await client.call_tool("vault_open", {"path": path})
        opened = response.structured_content
        # Models also receive the text representation, not only structured_content.
        assert json.loads(response.content[0].text)["revision"] == opened["revision"]
        assert opened["revision"] == hashlib.sha256(raw).hexdigest()
        anchor = "- [ ] Read CSV files ^task-read\r\n"
        replacement = (
            anchor + "- [ ] Validate CSV rows before processing ^task-validate\r\n"
        )
        args = {"path": path, "anchor": anchor, "replacement": replacement}
        if bad_revision == "content":
            args["expected_revision"] = opened["content"]
        elif bad_revision == "invented":
            args["expected_revision"] = "0" * 64
        elif bad_revision == "partial":
            fragment = (
                await client.call_tool("vault_open", {"path": path, "max_chars": 7})
            ).structured_content
            assert fragment["truncated"] and fragment["revision"] == opened["revision"]
            args["expected_revision"] = hashlib.sha256(
                fragment["content"].encode()
            ).hexdigest()
        elif bad_revision == "external":
            args["expected_revision"] = opened["revision"]
            raw += b"\r\nManual addition made after open.\r\n"
            target.write_bytes(raw)

        failed = await client.call_tool("vault_propose", args)
        assert failed.is_error
        error = "\n".join(
            block.text for block in failed.content if hasattr(block, "text")
        )
        assert "Stale or missing full-file revision." in error
        assert "Do not repeat" in error
        assert target.read_bytes() == raw
        with mutations.journal(home) as db:
            assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0

        # Simulated compliant client: one fresh open, then a changed proposal.
        fresh = (
            await client.call_tool("vault_open", {"path": path})
        ).structured_content
        corrected = {**args, "expected_revision": fresh["revision"]}
        assert corrected != args  # Never resend the identical failed arguments.
        assert fresh["content"].count(anchor) == 1
        result = await client.call_tool("vault_propose", corrected)
        assert not result.is_error
        preview = result.structured_content
        expected = raw.replace(anchor.encode(), replacement.encode(), 1)
        assert preview["content"].encode() == expected
        assert preview["outcome"] == "proposed"
        assert target.read_bytes() == raw  # A proposal never applies itself.
        assert list((vault / "10_Projects").glob("*.md")) == [target]
        assert not list((vault / "00_Inbox").iterdir())
        assert b"^task-read" in expected and b"^decision-library" in expected
        # Only a separate explicit synthetic operator approval publishes the edit.
        mutations.apply_proposal(
            home,
            vault,
            preview["proposal_id"],
            preview["approval_digest"],
            writers_paused=True,
        )
        readback = (
            await client.call_tool("vault_open", {"path": path})
        ).structured_content
        assert readback["content"].encode() == expected == target.read_bytes()


class ScriptedRevisionRecovery:
    """Exercises the acceptance checker; does not demonstrate model adherence."""

    def __init__(self, path, mode):
        self.path, self.mode, self.step = path, mode, -1

    def request(self, url, body, timeout):
        self.step += 1
        results = [
            json.loads(m["content"])
            for m in body["messages"]
            if m.get("role") == "tool"
        ]
        opened = next((r for r in reversed(results) if "revision" in r), None)
        anchor = "- [ ] Read CSV files ^task-read\r\n"
        args = {
            "path": self.path,
            "anchor": anchor,
            "replacement": anchor + "- [ ] Validate CSV rows ^task-validate\r\n",
            "expected_revision": None if self.step <= 2 else opened["revision"],
        }
        if (
            self.step == 0
            or (self.step == 2 and self.mode != "repeat")
            or (self.step == 3 and self.mode == "reopen")
        ):
            name, args = "vault_open", {"path": self.path}
        elif self.step < 4:
            name = "vault_propose"
        else:
            draft = {
                **args,
                "references": [],
                "applied": False,
                "proposal_id": results[-1]["proposal_id"],
            }
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(draft) + "\nNo update was applied.",
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": str(self.step),
                                "type": "function",
                                "function": {
                                    "name": "mcp__vault__" + name,
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    }
                }
            ]
        }


@pytest.mark.parametrize("mode", ["recover", "repeat", "reopen"])
async def test_conversation_checker_bounds_stale_recovery(tmp_path, mode):
    home, vault, path, _ = setup(tmp_path)
    request = update_case(
        ScriptedRevisionRecovery(path, mode),
        "synthetic",
        vault,
        path,
        f"Add CSV validation to {path}, preserving manual text and task IDs.",
        home=home,
    )
    if mode == "recover":
        _, trace = await request
        assert trace == ["vault_open", "vault_propose", "vault_open", "vault_propose"]
    else:
        message = (
            "identical failed proposal"
            if mode == "repeat"
            else "reopened more than once"
        )
        with pytest.RaisesGroup(
            pytest.RaisesGroup(pytest.RaisesExc(AssertionError, match=message))
        ):
            await request
