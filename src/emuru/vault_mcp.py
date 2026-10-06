from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from emuru.vault_indexer import (
    INDEXABLE_ROOTS,
    index_vault,
)

MAX_OPEN_CHARS = 50_000
DEFAULT_OPEN_CHARS = 12_000

MAX_SEARCH_RESULTS = 20
DEFAULT_SEARCH_RESULTS = 8

MAX_MAP_ENTRIES = 200


def _validate_vault_root(vault_root: Path) -> Path:
    root = vault_root.expanduser().resolve()

    if not root.is_dir():
        raise ValueError(f"Vault does not exist or is not a directory: {root}")

    return root


def _normalize_relative_path(path: str) -> Path:
    cleaned = path.strip().replace("\\", "/")

    if not cleaned:
        raise ToolError("Path cannot be empty.")

    relative = Path(cleaned)

    if relative.is_absolute():
        raise ToolError("Absolute paths are not allowed. Use a vault-relative path.")

    if any(part in {".", ".."} for part in relative.parts):
        raise ToolError("Path traversal is not allowed.")

    return relative


def _assert_no_symlink_components(
    vault_root: Path,
    relative: Path,
) -> None:
    current = vault_root

    for part in relative.parts:
        current = current / part

        if current.exists() and current.is_symlink():
            raise ToolError("Symlink paths are not allowed.")


def _resolve_read_path(
    vault_root: Path,
    path: str,
) -> Path:
    relative = _normalize_relative_path(path)

    if not relative.parts:
        raise ToolError("Invalid path.")

    root_name = relative.parts[0]

    if root_name not in INDEXABLE_ROOTS:
        raise ToolError(f"Access denied to vault root: {root_name}")

    _assert_no_symlink_components(
        vault_root,
        relative,
    )

    target = (vault_root / relative).resolve()

    try:
        target.relative_to(vault_root)
    except ValueError as exc:
        raise ToolError("Path escapes the vault.") from exc

    if not target.is_file():
        raise ToolError(f"Note does not exist: {relative.as_posix()}")

    if target.suffix.lower() != ".md":
        raise ToolError("Only Markdown notes may be opened.")

    return target


def _resolve_write_path(
    vault_root: Path,
    path: str,
) -> Path:
    relative = _normalize_relative_path(path)

    if not relative.parts:
        raise ToolError("Invalid path.")

    if relative.parts[0] != "00_Inbox":
        raise ToolError("Agent writes are restricted to 00_Inbox/.")

    if relative.suffix.lower() != ".md":
        raise ToolError("Agent-created notes must be Markdown files.")

    _assert_no_symlink_components(
        vault_root,
        relative.parent,
    )

    target = vault_root / relative

    parent = target.parent

    resolved_parent = parent.resolve()

    try:
        resolved_parent.relative_to(vault_root)
    except ValueError as exc:
        raise ToolError("Write path escapes the vault.") from exc

    if target.exists():
        raise ToolError("Refusing to overwrite an existing note.")

    return target


def _load_graph(
    vault_root: Path,
) -> dict[str, Any]:
    graph_path = vault_root / "_index" / "graph.json"

    if not graph_path.is_file():
        raise ToolError("Vault index is missing.")

    return json.loads(graph_path.read_text(encoding="utf-8"))


def _safe_fts_query(query: str) -> str:
    words = re.findall(
        r"\w+",
        query,
        flags=re.UNICODE,
    )

    if not words:
        raise ToolError("Search query must contain at least one word.")

    return " AND ".join(f'"{word}"' for word in words)


def create_mcp(
    vault_root: Path,
) -> MCPServer:
    vault_root = _validate_vault_root(vault_root)

    # Build a fresh index when the MCP server starts.
    index_vault(vault_root)

    mcp = MCPServer("EMURU Vault")

    @mcp.tool()
    def vault_map() -> dict[str, Any]:
        """
        Return a high-level map of the EMURU vault.

        Use this before opening notes when you need to understand
        the available projects, areas, resources, journal entries,
        inbox items, or archived knowledge.
        """
        graph = _load_graph(vault_root)

        grouped: dict[
            str,
            list[dict[str, Any]],
        ] = {root: [] for root in INDEXABLE_ROOTS}

        total = 0
        truncated = False

        for node in graph["nodes"]:
            if total >= MAX_MAP_ENTRIES:
                truncated = True
                break

            root = node["path"].split(
                "/",
                1,
            )[0]

            if root not in grouped:
                continue

            grouped[root].append(
                {
                    "path": node["path"],
                    "title": node["title"],
                    "type": node["note_type"],
                    "project": node["project"],
                    "tags": node["tags"],
                    "summary": node["summary"],
                }
            )

            total += 1

        return {
            "roots": grouped,
            "returned_notes": total,
            "total_notes": len(graph["nodes"]),
            "truncated": truncated,
        }

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

        limit = min(
            limit,
            MAX_SEARCH_RESULTS,
        )

        fts_query = _safe_fts_query(query)

        database = vault_root / "_index" / "notes.sqlite"

        if not database.is_file():
            raise ToolError("Vault search index is missing.")

        connection = sqlite3.connect(database)

        try:
            rows = connection.execute(
                """
                SELECT
                    path,
                    title,
                    summary,
                    snippet(
                        note_fts,
                        3,
                        '',
                        '',
                        ' … ',
                        20
                    ) AS excerpt,
                    bm25(note_fts) AS rank
                FROM note_fts
                WHERE note_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (
                    fts_query,
                    limit,
                ),
            ).fetchall()

        finally:
            connection.close()

        results = [
            {
                "path": row[0],
                "title": row[1],
                "summary": row[2],
                "excerpt": row[3],
            }
            for row in rows
        ]

        return {
            "query": query,
            "results": results,
            "count": len(results),
        }

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

        max_chars = min(
            max_chars,
            MAX_OPEN_CHARS,
        )

        target = _resolve_read_path(
            vault_root,
            path,
        )

        text = target.read_text(encoding="utf-8")

        truncated = len(text) > max_chars

        content = text[:max_chars]

        relative = target.relative_to(vault_root).as_posix()

        return {
            "path": relative,
            "content": content,
            "characters": len(content),
            "total_characters": len(text),
            "truncated": truncated,
        }

    @mcp.tool()
    def vault_neighbors(
        path: str,
    ) -> dict[str, Any]:
        """
        Return incoming and outgoing wikilink relationships
        for one indexed note.
        """
        target = _resolve_read_path(
            vault_root,
            path,
        )

        relative = target.relative_to(vault_root).as_posix()

        graph = _load_graph(vault_root)

        node = next(
            (item for item in graph["nodes"] if item["path"] == relative),
            None,
        )

        if node is None:
            raise ToolError("Note is not present in the current index.")

        outgoing = [edge for edge in graph["edges"] if edge["source"] == relative]

        incoming = [edge for edge in graph["edges"] if edge["target"] == relative]

        return {
            "path": relative,
            "title": node["title"],
            "outgoing": outgoing,
            "incoming": incoming,
        }

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
        if not content.strip():
            raise ToolError("Cannot create an empty note.")

        target = _resolve_write_path(
            vault_root,
            path,
        )

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        target.write_text(
            content.rstrip() + "\n",
            encoding="utf-8",
        )

        # Keep search and graph state immediately consistent
        # after an agent-created note.
        index_vault(vault_root)

        relative = target.relative_to(vault_root).as_posix()

        return {
            "created": True,
            "path": relative,
        }

    return mcp
