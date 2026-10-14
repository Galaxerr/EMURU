"""Real synthetic stdio preview, explicit operator apply, and exact readback."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from vault_helpers import create_vault

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/hermes/vault.py"


def fixture(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    return home, create_vault(tmp_path)


def command(home, vault, mode, *arguments):
    return [
        sys.executable,
        str(SCRIPT),
        mode,
        "--vault",
        str(vault),
        "--profile-home",
        str(home),
        *arguments,
    ]


def cli(home, vault, mode, *arguments):
    result = subprocess.run(
        command(home, vault, mode, *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
async def test_real_stdio_propose_apply_readback(tmp_path):
    home, vault = fixture(tmp_path)
    target = vault / "10_Projects/human.md"
    original = b"---\r\ntitle: Human\r\n---\r\n- [ ] task ^stable\r\nHuman content [[Other]].\r\n"
    target.write_bytes(original)
    startup = command(home, vault, "mutation-serve")
    params = StdioServerParameters(command=startup[0], args=startup[1:], cwd=str(ROOT))
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        async with asyncio.timeout(30):
            await client.initialize()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "vault_map",
                "vault_search",
                "vault_open",
                "vault_neighbors",
                "vault_write",
                "vault_propose",
            }
            assert not any("apply" in name for name in names)
            opened = await client.call_tool(
                "vault_open", {"path": "10_Projects/human.md", "max_chars": 10}
            )
            assert not opened.is_error
            proposal = await client.call_tool(
                "vault_propose",
                {
                    "path": "10_Projects/human.md",
                    "expected_revision": opened.structured_content["revision"],
                    "anchor": "- [ ] task ^stable",
                    "replacement": "- [x] task ^stable",
                },
            )
            assert not proposal.is_error
            preview = proposal.structured_content
            assert target.read_bytes() == original
            assert preview["content"].encode() == original.replace(b"- [ ]", b"- [x]")
            receipt = cli(
                home,
                vault,
                "mutation-apply",
                "--proposal-id",
                preview["proposal_id"],
                "--approval-digest",
                preview["approval_digest"],
                "--writers-paused",
            )
            assert receipt["outcome"] == "applied"
            assert (
                cli(
                    home,
                    vault,
                    "mutation-apply",
                    "--proposal-id",
                    preview["proposal_id"],
                    "--approval-digest",
                    preview["approval_digest"],
                    "--writers-paused",
                )
                == receipt
            )
            after = await client.call_tool(
                "vault_open", {"path": "10_Projects/human.md"}
            )
            assert after.structured_content["content"].encode() == original.replace(
                b"- [ ]", b"- [x]"
            )
            forbidden = await client.call_tool(
                "vault_propose", {"path": "99_Private/no.md", "content": "do not write"}
            )
            assert forbidden.is_error
            unknown = await client.call_tool(
                "vault_apply", {"proposal_id": preview["proposal_id"]}
            )
            assert unknown.is_error
            assert not (vault / "99_Private/no.md").exists()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_operator_cli_create_cancel_and_recover(tmp_path):
    home, vault = fixture(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"path": "10_Projects/new.md", "content": "# New\r\n"})
    )
    preview = cli(home, vault, "mutation-propose", "--request", str(request))
    assert not (vault / "10_Projects/new.md").exists()
    cancelled = cli(
        home, vault, "mutation-cancel", "--proposal-id", preview["proposal_id"]
    )
    assert cancelled["outcome"] == "cancelled"
    assert cli(home, vault, "mutation-recover") == []
    assert not (vault / "10_Projects/new.md").exists()


def test_operator_cli_requires_explicit_paths_and_pause(tmp_path):
    home, vault = fixture(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "mutation-recover"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "require --vault" in result.stderr
    request = tmp_path / "request.json"
    request.write_text('{"path":"10_Projects/new.md","content":"new"}')
    proposal = cli(home, vault, "mutation-propose", "--request", str(request))
    result = subprocess.run(
        command(
            home,
            vault,
            "mutation-apply",
            "--proposal-id",
            proposal["proposal_id"],
            "--approval-digest",
            proposal["approval_digest"],
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "paused" in result.stderr
    assert not (vault / "10_Projects/new.md").exists()
