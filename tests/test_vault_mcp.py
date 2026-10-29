"""Vault tools through the MCP client, including read and create-only restrictions."""

import pytest
from mcp import Client
from vault_helpers import create_vault, write_note

from emuru.vault import indexer
from emuru.vault.mcp import create_mcp

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("mcp_environment")]


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


@pytest.mark.parametrize("malformed_at_start", [False, True])
@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("---\ntitle: [PRIVATE_CANARY\n---\n", id="yaml"),
        pytest.param(
            "---\ntitle: " + "[" * 600 + "bad" + "]" * 599 + "\n---\n",
            id="nested-yaml",
        ),
        pytest.param('---\ntitle: "\\uD800"\n---\n', id="surrogate"),
    ],
)
async def test_malformed_note_preserves_healthy_mcp_retrieval(
    tmp_path, monkeypatch, malformed_at_start, malformed
):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/healthy.md", "healthyword")
    failed_path = "30_Resources/broken.md"
    if malformed_at_start:
        write_note(vault, failed_path, malformed)
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        if not malformed_at_start:
            before = await client.call_tool("vault_search", {"query": "healthyword"})
            assert not before.is_error
            write_note(vault, failed_path, malformed)
        found = await client.call_tool("vault_search", {"query": "healthyword"})
        assert not found.is_error
        assert found.structured_content["count"] == 1

        def forbidden(*args):
            pytest.fail("unchanged partial generation rebuilt")

        with monkeypatch.context() as patch:
            patch.setattr(indexer, "_index_vault", forbidden)
            for tool, arguments in [
                ("vault_search", {"query": "healthyword"}),
                ("vault_search", {"query": "absentword"}),
                ("vault_map", {}),
                ("vault_neighbors", {"path": "10_Projects/healthy.md"}),
            ]:
                result = await client.call_tool(tool, arguments)
                assert not result.is_error
                assert result.structured_content["incomplete"] is True
                assert result.structured_content["failed_paths"] == [failed_path]
                assert "PRIVATE_CANARY" not in result.model_dump_json()
                assert str(vault) not in result.model_dump_json()
        write_note(vault, failed_path, "repairedword")
        repaired = await client.call_tool("vault_search", {"query": "repairedword"})
        assert not repaired.is_error
        assert repaired.structured_content["count"] == 1
        assert repaired.structured_content["incomplete"] is False
        assert repaired.structured_content["failed_paths"] == []


@pytest.mark.parametrize(
    "raw",
    [
        b"---\ntitle: [PRIVATE_CANARY\n---\n",
        b"---\ntitle: PRIVATE_CANARY\n",
        b"---\n[PRIVATE_CANARY]\n---\n",
        b"---\n[]\n---\n",
        b"---\nfalse\n---\n",
        b"---\n0\n---\n",
        b"---\nupdated: 2026-99-99\n---\nPRIVATE_CANARY",
        b"\xffPRIVATE_CANARY",
        pytest.param(
            b"---\ntitle: " + b"[" * 600 + b"bad" + b"]" * 599 + b"\n---\n",
            id="deep-invalid-yaml",
        ),
        pytest.param(b'---\ntitle: "\\uD800"\n---\n', id="escaped-surrogate"),
    ],
)
async def test_all_malformed_notes_report_incomplete_empty_results(tmp_path, raw):
    vault = create_vault(tmp_path)
    failed_paths = ["00_Inbox/a.md", "30_Resources/z.md"]
    for path in reversed(failed_paths):
        (vault / path).write_bytes(raw)
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        for tool, arguments, count in [
            ("vault_search", {"query": "absentword"}, "count"),
            ("vault_map", {}, "total_notes"),
        ]:
            result = await client.call_tool(tool, arguments)
            assert not result.is_error
            assert result.structured_content[count] == 0
            assert result.structured_content["incomplete"] is True
            assert result.structured_content["failed_paths"] == failed_paths
            assert "PRIVATE_CANARY" not in result.model_dump_json()


@pytest.mark.parametrize("frontmatter", ["", "null", "{}"])
async def test_empty_mapping_frontmatter_stays_complete(tmp_path, frontmatter):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/note.md", f"---\n{frontmatter}\n---\nhealthyword")
    async with Client(create_mcp(vault), raise_exceptions=True) as client:
        result = await client.call_tool("vault_search", {"query": "healthyword"})
        assert not result.is_error
        assert result.structured_content["count"] == 1
        assert result.structured_content["incomplete"] is False
        assert result.structured_content["failed_paths"] == []


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


async def test_failed_refresh_returns_saved_note_as_index_pending(
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
    assert result.is_error is False
    assert result.structured_content == {
        "created": True,
        "path": "00_Inbox/interrupted.md",
        "outcome": "index_pending",
        "message": "saved; indexing pending",
    }
    assert (vault / "00_Inbox/interrupted.md").read_text(
        encoding="utf-8"
    ) == "New note\n"


async def test_refresh_failure_does_not_unlink_published_note(tmp_path, monkeypatch):
    import emuru.vault.indexer as adapter

    vault = create_vault(tmp_path)
    server = create_mcp(vault)
    target = vault / "00_Inbox/committed.md"

    def interrupted(*args):
        raise OSError("refresh interrupted")

    unlinked: list[str] = []
    unlink = adapter.os.unlink

    def track_unlink(path, *args, **kwargs):
        if path == target.name:
            unlinked.append(path)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(adapter, "index_vault", interrupted)
    monkeypatch.setattr(adapter.os, "unlink", track_unlink)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "vault_write",
            {"path": "00_Inbox/committed.md", "content": "Committed note"},
        )
    assert result.is_error is False and target.is_file()
    assert result.structured_content == {
        "created": True,
        "path": "00_Inbox/committed.md",
        "outcome": "index_pending",
        "message": "saved; indexing pending",
    }
    assert unlinked == []


async def test_refresh_failure_returns_unavailable_then_repairs(tmp_path, monkeypatch):
    from emuru.vault import indexer

    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/note.md", "oldtoken")
    server = create_mcp(vault)
    note.write_text("newtoken")
    original = indexer._index_vault
    calls = []

    def fail(*args):
        calls.append(1)
        raise OSError("refresh failed")

    monkeypatch.setattr(indexer, "_index_vault", fail)
    async with Client(server, raise_exceptions=True) as client:
        for name, arguments in [
            ("vault_search", {"query": "oldtoken"}),
            ("vault_map", {}),
            ("vault_neighbors", {"path": "10_Projects/note.md"}),
        ]:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert "retrieval unavailable" in result.content[0].text
        assert len(calls) == 6
        monkeypatch.setattr(indexer, "_index_vault", original)
        result = await client.call_tool("vault_search", {"query": "newtoken"})
        assert not result.is_error and result.structured_content["count"] == 1
