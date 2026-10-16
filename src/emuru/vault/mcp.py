from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from emuru.vault import indexer as index
from emuru.vault import mutations

MAX_OPEN_CHARS = 50_000
DEFAULT_OPEN_CHARS = 12_000
MAX_SEARCH_RESULTS = 20
DEFAULT_SEARCH_RESULTS = 8
MAX_MAP_ENTRIES = 200


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error


def create_mcp(vault_root: Path, *, proposal_home: Path | None = None) -> MCPServer:
    vault_root = vault_root.expanduser().absolute()
    if proposal_home is not None:
        mutations.require_authority(proposal_home, vault_root)
        mutations.recover_proposals(proposal_home, vault_root)
    index.index_vault(vault_root)
    mcp = MCPServer("EMURU Vault")

    @mcp.tool()
    def vault_map() -> dict[str, Any]:
        """
        Return a high-level map of the EMURU vault.

        Use this before opening notes when you need to understand
        the available projects, areas, resources, journal entries,
        inbox items, or archived knowledge.
        """
        return _call(index.vault_map, vault_root, MAX_MAP_ENTRIES)

    @mcp.tool()
    def vault_search(
        query: str,
        limit: int = DEFAULT_SEARCH_RESULTS,
    ) -> dict[str, Any]:
        """
        Search indexed EMURU vault notes.

        Use a short lexical query: all words must match. Words become
        quoted terms joined with AND; OR is a literal word and security*
        searches for security, not a wildcard prefix. Raw FTS syntax is
        not supported.

        Returns matching note paths, titles, summaries and excerpts.
        Use the returned path with vault_open when more detail is
        required.
        """
        if limit < 1:
            raise ToolError("Search limit must be at least 1.")
        return _call(
            index.vault_search, vault_root, query, min(limit, MAX_SEARCH_RESULTS)
        )

    @mcp.tool()
    def vault_open(
        path: str,
        max_chars: int = DEFAULT_OPEN_CHARS,
    ) -> dict[str, Any]:
        """
        Read one Markdown note from an allowed vault directory.

        Paths must be vault-relative, for example:
        10_Projects/EMURU.md
        """
        if max_chars < 1:
            raise ToolError("max_chars must be positive.")
        return _call(index.vault_open, vault_root, path, min(max_chars, MAX_OPEN_CHARS))

    @mcp.tool()
    def vault_neighbors(
        path: str,
    ) -> dict[str, Any]:
        """
        Return incoming and outgoing wikilink relationships
        for one indexed note.
        """
        return _call(index.vault_neighbors, vault_root, path)

    @mcp.tool()
    def vault_write(
        path: str,
        content: str,
    ) -> dict[str, Any]:
        """
        Create a new Markdown note under 00_Inbox only.

        The path must start with 00_Inbox/ and the tool refuses
        to overwrite existing notes.
        """
        if (
            proposal_home is not None
            and mutations.load_policy(proposal_home)["enabled"]
        ):
            return _call(
                mutations.capture_note, proposal_home, vault_root, path, content
            )
        return _call(index.vault_write, vault_root, path, content)

    if proposal_home is not None:

        @mcp.tool()
        def vault_propose(
            path: str,
            content: str | None = None,
            expected_revision: str | None = None,
            anchor: str | None = None,
            replacement: str | None = None,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            """Preview one approved-root creation or exact anchored update.

            Creation requires content and an absent target. Updates require the
            full revision from vault_open, one exact anchor, and replacement.
            Reuse an owner-supplied request_id for the same exact request.
            Existing paths and requests return their prior note/proposal/outcome.
            This stores a private proposal. It cannot apply or approve it.
            """
            return _call(
                mutations.propose_note,
                proposal_home,
                vault_root,
                path,
                content=content,
                expected_revision=expected_revision,
                anchor=anchor,
                replacement=replacement,
                request_id=request_id,
                authenticated_id=(
                    mutations.authenticated_request(proposal_home, vault_root)
                    if mutations.load_policy(proposal_home)["enabled"]
                    else None
                ),
            )

    return mcp
