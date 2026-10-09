"""Vault tools through the MCP client, including read and create-only restrictions."""

import pytest
from mcp import Client
from vault_helpers import create_vault, write_note

from emuru.vault.mcp import create_mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_lists_expected_tools(tmp_path):
    vault = create_vault(tmp_path)
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.list_tools()
    assert {tool.name for tool in result.tools} == {
        "vault_map",
        "vault_search",
        "vault_open",
        "vault_neighbors",
        "vault_write",
    }


async def test_vault_map(tmp_path):
    vault = create_vault(tmp_path)
    write_note(
        vault,
        "10_Projects/emuru.md",
        """
---
title: EMURU
type: project
project: emuru
tags: [ai]
summary: Personal AI agent.
---
# EMURU
""",
    )
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool("vault_map", {})
    assert result.is_error is False
    data = result.structured_content
    assert data is not None and data["total_notes"] == 1
    assert data["roots"]["10_Projects"][0]["title"] == "EMURU"


async def test_vault_search(tmp_path):
    vault = create_vault(tmp_path)
    write_note(
        vault,
        "30_Resources/security.md",
        """
---
title: AI Security
type: resource
summary: AI security notes.
---
Prompt injection and model security are important.
""",
    )
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool("vault_search", {"query": "prompt injection"})
    assert result.is_error is False
    data = result.structured_content
    assert data is not None and data["count"] == 1
    assert data["results"][0]["path"] == "30_Resources/security.md"


async def test_vault_open(tmp_path):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/emuru.md", "# EMURU\nThis is the EMURU project.")
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool("vault_open", {"path": "10_Projects/emuru.md"})
    assert result.is_error is False
    assert result.structured_content is not None
    assert "This is the EMURU project." in result.structured_content["content"]


@pytest.mark.parametrize(
    "path,message",
    [
        ("99_Private/secret.md", "Access denied"),
        ("10_Projects/../../secret.md", "Path traversal"),
    ],
)
async def test_forbidden_reads(tmp_path, path, message):
    vault = create_vault(tmp_path)
    write_note(vault, "99_Private/secret.md", "EMURU_SUPER_PRIVATE_CANARY")
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool("vault_open", {"path": path})
    assert result.is_error is True
    assert message in result.content[0].text


@pytest.mark.parametrize("extension", ["md", "MD", "mD"])
async def test_write_creates_inbox_note_and_is_immediately_searchable(
    tmp_path, extension
):
    vault = create_vault(tmp_path)
    path = f"00_Inbox/new-concept.{extension}"
    write_note(
        vault,
        "10_Projects/reference.MD",
        f"Links to [[00_Inbox/new-concept.{extension}]].",
    )
    content = "# New Concept\nBuild something. QuasarOrchid is a unique test phrase."
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_write", {"path": path, "content": content}
        )
        assert result.is_error is False
        assert (vault / path).is_file()
        assert "Build something." in (vault / path).read_text(encoding="utf-8")
        search = await client.call_tool("vault_search", {"query": "QuasarOrchid"})
        opened = await client.call_tool("vault_open", {"path": path})
        neighbors = await client.call_tool("vault_neighbors", {"path": path})
    assert search.is_error is False
    data = search.structured_content
    assert data is not None and data["count"] == 1
    assert data["results"][0]["path"] == path
    assert (
        not opened.is_error and "QuasarOrchid" in opened.structured_content["content"]
    )
    assert not neighbors.is_error
    assert (
        neighbors.structured_content["incoming"][0]["source"]
        == "10_Projects/reference.MD"
    )


@pytest.mark.parametrize("path", ["10_Projects/hacked.md", "00_Inbox/existing.md"])
async def test_forbidden_writes_preserve_existing_notes(tmp_path, path):
    vault = create_vault(tmp_path)
    existing = write_note(vault, "00_Inbox/existing.md", "Original content.")
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_write",
            {
                "path": path,
                "content": "Malicious replacement.",
            },
        )
    assert result.is_error is True
    assert existing.read_text(encoding="utf-8") == "Original content.\n"
    if path != "00_Inbox/existing.md":
        assert not (vault / path).exists()


async def test_vault_neighbors(tmp_path):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/emuru.md", "Uses [[AI Security]].")
    write_note(
        vault,
        "30_Resources/security.md",
        "---\ntitle: AI Security\n---\nSecurity notes.",
    )
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_neighbors", {"path": "30_Resources/security.md"}
        )
    assert result.is_error is False
    data = result.structured_content
    assert data is not None and len(data["incoming"]) == 1
    assert data["incoming"][0]["source"] == "10_Projects/emuru.md"


async def test_failed_refresh_reports_error_without_committing_note(
    tmp_path, monkeypatch
):
    import emuru.vault.indexer as adapter

    vault = create_vault(tmp_path)
    server = create_mcp(vault)

    def interrupted(*args):
        raise OSError("refresh interrupted")

    monkeypatch.setattr(adapter, "index_vault", interrupted)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_write", {"path": "00_Inbox/interrupted.md", "content": "New note"}
        )
    assert result.is_error
    assert not (vault / "00_Inbox/interrupted.md").exists()


async def test_refresh_failure_reports_note_if_rollback_is_blocked(
    tmp_path, monkeypatch
):
    from pathlib import Path

    import emuru.vault.indexer as adapter

    vault = create_vault(tmp_path)
    server = create_mcp(vault)
    target = vault / "00_Inbox/committed.md"

    def interrupted(*args):
        raise OSError("refresh interrupted")

    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == target:
            raise PermissionError("rollback blocked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(adapter, "index_vault", interrupted)
    monkeypatch.setattr(Path, "unlink", blocked)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_write",
            {"path": "00_Inbox/committed.md", "content": "Committed note"},
        )
    assert result.is_error and target.is_file()
    assert (
        "Note committed at 00_Inbox/committed.md; index refresh failed"
        in result.content[0].text
    )
