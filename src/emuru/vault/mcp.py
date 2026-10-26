from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

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

        Returns content, truncated, and revision. For an update, copy the exact
        string in this response's revision field into vault_propose.expected_revision
        for this same path. Never hash or copy content as the revision. Even when
        content is truncated, revision identifies the full raw file. If the intended
        anchor is missing, reopen with max_chars up to 50000; stop if still missing.
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

        @mcp.tool(
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False)
        )
        def vault_proposal_status(
            proposal_id: str | None = None, latest: bool = False
        ) -> dict[str, Any]:
            """Read authoritative current proposal lifecycle for the authenticated owner.

            Call freshly before reporting current approval/application/cancellation
            status; historical vault_propose results cannot override this lookup.
            Supply proposal_id for one proposal, latest=true for the last created
            proposal, or neither to list only currently pending approvals.
            Returns proposals with proposal_id, path, state, outcome and
            receipt_confirmed. State pending means proposed without a receipt;
            applied/cancelled/expired are receipt-confirmed. prepared is unresolved,
            index_pending means saved; indexing pending, uncertain means quarantined,
            and not_applied means recovery confirmed no application.
            No creation, approval, application, cancellation, expiry, indexing or
            recovery occurs. Missing authentication or unavailable state is an error,
            never evidence of pending status. Owner/bot/vault come from trusted
            transport, never arguments. No draft content or approval token is returned.
            """
            return _call(
                mutations.proposal_status,
                proposal_home,
                vault_root,
                proposal_id,
                latest=latest,
            )

        @mcp.tool(
            description=(
                "Prepare and store a non-mutating preview for an explicit owner creation or edit request. "
                "Approved roots: "
                + ", ".join(mutations.load_policy(proposal_home)["roots"])
                + ". "
                "Use this for new projects/resources outside Inbox; do not substitute an unstored draft when this tool is available. "
                "Creation requires content and an absent target; omit update fields or use JSON null. "
                "For updates, first vault_open this same path. Copy its latest revision field exactly into expected_revision; never use content, an invented hash, or a previous response's revision. "
                "Copy one unique exact anchor from that response's content and supply replacement preserving unrelated text and existing block IDs; omit content or use JSON null. "
                "Do not repeat an identical failed call. On a stale revision, reopen the same path at most once, reassess the anchor and intended edit, and use the fresh revision only in a corrected proposal. If recovery fails, stop and report the path and error. "
                "Redundant update content is accepted only if it exactly matches the anchored result. "
                "Search for duplicates first. A direct owner creation request needs no existing Inbox source. "
                "Never apply or approve changes. Owner approval is handled by the trusted Telegram transport."
            )
        )
        def vault_propose(
            path: str,
            content: Annotated[
                str | None,
                Field(
                    description="Full note text for creation; omit or use JSON null for updates. Redundant update content must exactly match the anchored result."
                ),
            ] = None,
            expected_revision: Annotated[
                str | None,
                Field(
                    description="For updates, copy the exact string from the latest vault_open response's revision field for this same path. Not content, a computed hash, before_revision, or after_revision. Omit or use JSON null only for creation."
                ),
            ] = None,
            anchor: Annotated[
                str | None,
                Field(
                    description="Copy unique exact text from the latest vault_open content, preserving whitespace and existing block IDs. Must occur exactly once. Omit or use JSON null for creation."
                ),
            ] = None,
            replacement: Annotated[
                str | None,
                Field(
                    description="Replacement text for updates; omit or use JSON null for creation."
                ),
            ] = None,
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
