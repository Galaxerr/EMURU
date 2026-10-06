from pathlib import Path

import pytest
from mcp import Client
from vault_helpers import create_vault, write_note

from emuru.vault_mcp import create_mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_lists_expected_tools(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.list_tools()

    names = {tool.name for tool in result.tools}

    assert names == {
        "vault_map",
        "vault_search",
        "vault_open",
        "vault_neighbors",
        "vault_write",
    }


@pytest.mark.anyio
async def test_vault_map(
    tmp_path: Path,
):
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

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_map",
            {},
        )

    assert result.is_error is False

    data = result.structured_content

    assert data is not None

    assert data["total_notes"] == 1

    project_notes = data["roots"]["10_Projects"]

    assert project_notes[0]["title"] == "EMURU"


@pytest.mark.anyio
async def test_vault_search(
    tmp_path: Path,
):
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

        Prompt injection and model
        security are important.
        """,
    )

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_search",
            {"query": "prompt injection"},
        )

    assert result.is_error is False

    data = result.structured_content

    assert data is not None
    assert data["count"] == 1

    assert data["results"][0]["path"] == "30_Resources/security.md"


@pytest.mark.anyio
async def test_vault_open(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "10_Projects/emuru.md",
        """
        ---
        title: EMURU
        ---

        This is the EMURU project.
        """,
    )

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_open",
            {"path": "10_Projects/emuru.md"},
        )

    assert result.is_error is False

    data = result.structured_content

    assert data is not None

    assert "This is the EMURU project." in data["content"]


@pytest.mark.anyio
async def test_private_note_cannot_be_opened(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "99_Private/secret.md",
        """
        EMURU_SUPER_PRIVATE_CANARY
        """,
    )

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_open",
            {"path": "99_Private/secret.md"},
        )

    assert result.is_error is True

    assert "Access denied" in result.content[0].text


@pytest.mark.anyio
async def test_path_traversal_is_rejected(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_open",
            {"path": "10_Projects/../../secret.md"},
        )

    assert result.is_error is True

    assert "Path traversal" in result.content[0].text


@pytest.mark.anyio
async def test_write_creates_inbox_note(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_write",
            {
                "path": "00_Inbox/new-idea.md",
                "content": "# New Idea\n\nBuild something.",
            },
        )

    assert result.is_error is False

    created = vault / "00_Inbox" / "new-idea.md"

    assert created.is_file()

    assert "Build something." in created.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_write_outside_inbox_is_denied(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_write",
            {
                "path": "10_Projects/hacked.md",
                "content": "Should not exist.",
            },
        )

    assert result.is_error is True

    assert not (vault / "10_Projects" / "hacked.md").exists()


@pytest.mark.anyio
async def test_existing_note_cannot_be_overwritten(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "00_Inbox/existing.md",
        "Original content.",
    )

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_write",
            {
                "path": "00_Inbox/existing.md",
                "content": "Malicious replacement.",
            },
        )

    assert result.is_error is True

    content = (vault / "00_Inbox" / "existing.md").read_text(encoding="utf-8")

    assert "Original content." in content

    assert "Malicious replacement." not in content


@pytest.mark.anyio
async def test_write_is_immediately_searchable(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        write_result = await client.call_tool(
            "vault_write",
            {
                "path": "00_Inbox/new-concept.md",
                "content": ("# New Concept\n\nQuasarOrchid is a unique test phrase."),
            },
        )

        assert write_result.is_error is False

        search_result = await client.call_tool(
            "vault_search",
            {"query": "QuasarOrchid"},
        )

    data = search_result.structured_content

    assert data is not None
    assert data["count"] == 1

    assert data["results"][0]["path"] == "00_Inbox/new-concept.md"


@pytest.mark.anyio
async def test_vault_neighbors(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "10_Projects/emuru.md",
        """
        ---
        title: EMURU
        ---

        Uses [[AI Security]].
        """,
    )

    write_note(
        vault,
        "30_Resources/security.md",
        """
        ---
        title: AI Security
        ---

        Security notes.
        """,
    )

    server = create_mcp(vault)

    async with Client(
        server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "vault_neighbors",
            {"path": "30_Resources/security.md"},
        )

    assert result.is_error is False

    data = result.structured_content

    assert data is not None

    assert len(data["incoming"]) == 1

    assert data["incoming"][0]["source"] == "10_Projects/emuru.md"
