from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

INDEXABLE_ROOTS = (
    "00_Inbox",
    "10_Projects",
    "20_Areas",
    "30_Resources",
    "40_Journal",
    "90_Archive",
)


OUTPUTS = {
    "graph": "_index/graph.json",
    "sqlite": "_index/notes.sqlite",
    "index": "99_System/INDEX.md",
}


def derived_outputs(vault_root: Path) -> dict[str, Path]:
    return {key: vault_root / name for key, name in OUTPUTS.items()}


def derived_paths(vault_root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    outputs = tuple(derived_outputs(vault_root).values())
    temporary = tuple(Path(str(path) + ".tmp") for path in outputs)
    sqlite = derived_outputs(vault_root)["sqlite"]
    sidecars = tuple(
        Path(str(sqlite) + ".tmp" + suffix) for suffix in ("-journal", "-wal", "-shm")
    )
    return tuple(dict.fromkeys(path.parent for path in outputs)), (
        *outputs,
        *temporary,
        *sidecars,
    )


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass(frozen=True)
class Note:
    path: str
    title: str
    note_type: str
    project: str
    tags: list[str]
    updated: str
    summary: str
    sha256: str
    links: list[str]
    body: str


def _frontmatter_and_body(
    text: str,
) -> tuple[dict[str, Any], str]:

    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        return {}, text

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            data = yaml.safe_load(raw) or {}

            if not isinstance(data, dict):
                raise ValueError("frontmatter must be a YAML mapping")

            body = "\n".join(lines[i + 1 :])
            return data, body

    raise ValueError("opening frontmatter delimiter has no closing delimiter")


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    value = str(value).strip()

    return [value] if value else []


def extract_wikilinks(body: str) -> list[str]:
    result: set[str] = set()

    for raw in WIKILINK_RE.findall(body):
        target = raw.split("|", 1)[0]
        target = target.split("#", 1)[0].strip()

        target = target.removesuffix(".md")

        if target:
            result.add(target.replace("\\", "/"))

    return sorted(result)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_note(
    path: Path,
    vault_root: Path,
) -> Note:

    text = path.read_text(encoding="utf-8")

    metadata, body = _frontmatter_and_body(text)

    relative = path.relative_to(vault_root).as_posix()

    return Note(
        path=relative,
        title=str(metadata.get("title") or path.stem),
        note_type=str(metadata.get("type") or "note"),
        project=str(metadata.get("project") or ""),
        tags=_normalize_tags(metadata.get("tags")),
        updated=str(metadata.get("updated") or ""),
        summary=str(metadata.get("summary") or ""),
        sha256=_sha256(text),
        links=extract_wikilinks(body),
        body=body,
    )


def iter_note_paths(
    vault_root: Path,
):
    resolved_root = vault_root.resolve()

    for root_name in INDEXABLE_ROOTS:
        base = vault_root / root_name

        if base.is_symlink() or not base.is_dir():
            continue

        for dirpath, dirnames, filenames in os.walk(
            base,
            followlinks=False,
        ):
            dirnames[:] = [
                directory
                for directory in dirnames
                if not (Path(dirpath) / directory).is_symlink()
            ]

            for filename in filenames:
                if not filename.endswith(".md"):
                    continue

                path = Path(dirpath) / filename

                if path.is_symlink():
                    continue

                resolved = path.resolve()

                if not resolved.is_relative_to(resolved_root):
                    continue

                yield path


def collect_notes(
    vault_root: Path,
) -> list[Note]:

    notes = (parse_note(path, vault_root) for path in iter_note_paths(vault_root))

    return sorted(
        notes,
        key=lambda note: note.path.casefold(),
    )


def _link_aliases(
    note: Note,
) -> set[str]:

    relative = note.path.removesuffix(".md")

    return {
        relative.casefold(),
        Path(relative).name.casefold(),
        note.title.casefold(),
    }


def build_graph(
    notes: list[Note],
) -> dict[str, Any]:

    aliases: dict[
        str,
        list[str],
    ] = {}

    for note in notes:
        for alias in _link_aliases(note):
            aliases.setdefault(
                alias,
                [],
            ).append(note.path)

    edges: list[dict[str, Any]] = []

    for note in notes:
        for raw_target in note.links:
            matches = aliases.get(
                raw_target.casefold(),
                [],
            )

            if len(matches) == 1:
                status = "resolved"
                target = matches[0]

            elif len(matches) > 1:
                status = "ambiguous"
                target = None

            else:
                status = "unresolved"
                target = None

            edges.append(
                {
                    "source": note.path,
                    "raw_target": raw_target,
                    "target": target,
                    "status": status,
                }
            )

    nodes = [
        {key: value for key, value in asdict(note).items() if key != "body"}
        for note in notes
    ]

    return {
        "generated_at": (datetime.now(UTC).isoformat()),
        "nodes": nodes,
        "edges": edges,
    }


def write_graph(
    vault_root: Path,
    graph: dict[str, Any],
) -> Path:

    final_path = derived_outputs(vault_root)["graph"]
    final_path.parent.mkdir(exist_ok=True)

    temp_path = Path(str(final_path) + ".tmp")

    temp_path.write_text(
        json.dumps(
            graph,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    os.replace(
        temp_path,
        final_path,
    )

    return final_path


def write_sqlite(
    vault_root: Path,
    notes: list[Note],
) -> Path:

    final_path = derived_outputs(vault_root)["sqlite"]
    final_path.parent.mkdir(exist_ok=True)

    temp_path = Path(str(final_path) + ".tmp")

    temp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(temp_path)

    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE note_fts
            USING fts5(
                path UNINDEXED,
                title,
                summary,
                body,
                tags,
                tokenize='unicode61'
            );
            """
        )

        for note in notes:
            conn.execute(
                """
                INSERT INTO note_fts (
                    path,
                    title,
                    summary,
                    body,
                    tags
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    note.path,
                    note.title,
                    note.summary,
                    note.body,
                    " ".join(note.tags),
                ),
            )

        conn.commit()

    finally:
        conn.close()

    os.replace(
        temp_path,
        final_path,
    )

    return final_path


def write_human_index(
    vault_root: Path,
    notes: list[Note],
) -> Path:

    output = derived_outputs(vault_root)["index"]

    output.parent.mkdir(exist_ok=True)

    grouped = {root: [] for root in INDEXABLE_ROOTS}

    for note in notes:
        root = note.path.split("/", 1)[0]

        grouped[root].append(note)

    lines = [
        "---",
        "title: EMURU Vault Index",
        "type: generated",
        "tags: [emuru, index]",
        "---",
        "",
        "# EMURU Vault Index",
        "",
        ("> Generated by EMURU. Manual changes will be overwritten."),
        "",
    ]

    for root in INDEXABLE_ROOTS:
        items = grouped[root]

        if not items:
            continue

        lines.append(f"## {root}")
        lines.append("")

        for note in items:
            link_path = note.path[:-3]

            suffix = f" — {note.summary}" if note.summary else ""

            lines.append(f"- [[{link_path}|{note.title}]]{suffix}")

        lines.append("")

    output.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )

    return output


def index_vault(
    vault_root: Path,
) -> dict[str, Any]:

    vault_root = vault_root.expanduser().resolve()

    if not vault_root.is_dir():
        raise ValueError(f"vault does not exist or is not a directory: {vault_root}")

    notes = collect_notes(vault_root)

    graph = build_graph(notes)

    directories, files = derived_paths(vault_root)
    for directory in directories:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("Vault derived directory is invalid.")
        directory.mkdir(exist_ok=True)
    for path in files:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Vault derived file is invalid.")

    outputs = derived_outputs(vault_root)
    # Build everything before publication. Restore previous outputs on interruption.
    with tempfile.TemporaryDirectory(
        prefix="refresh-", dir=directories[0]
    ) as temporary:
        staging = Path(temporary)
        write_graph(staging, graph)
        write_sqlite(staging, notes)
        write_human_index(staging, notes)
        staged = derived_outputs(staging)
        backups = {}
        for key, path in outputs.items():
            backup = staging / (key + ".backup")
            if path.exists():
                shutil.copy2(path, backup)
                backups[key] = backup
        published = []
        try:
            for key, path in outputs.items():
                os.replace(staged[key], path)
                published.append(key)
        except BaseException:
            for key in reversed(published):
                if key in backups:
                    os.replace(backups[key], outputs[key])
                else:
                    outputs[key].unlink(missing_ok=True)
            raise

    return {
        "notes": len(notes),
        "edges": len(graph["edges"]),
        **{key: str(path) for key, path in outputs.items()},
    }


def _normalize_relative_path(path: str) -> Path:
    cleaned = path.strip().replace("\\", "/")

    if not cleaned:
        raise ValueError("Path cannot be empty.")

    relative = Path(cleaned)

    if relative.is_absolute():
        raise ValueError("Absolute paths are not allowed. Use a vault-relative path.")

    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError("Path traversal is not allowed.")

    return relative


def _assert_no_symlink_components(
    vault_root: Path,
    relative: Path,
) -> None:
    current = vault_root

    for part in relative.parts:
        current = current / part

        if current.is_symlink():
            raise ValueError("Symlink paths are not allowed.")


def _resolve_read_path(
    vault_root: Path,
    path: str,
) -> Path:
    relative = _normalize_relative_path(path)

    if not relative.parts:
        raise ValueError("Invalid path.")

    root_name = relative.parts[0]

    if root_name not in INDEXABLE_ROOTS:
        raise ValueError(f"Access denied to vault root: {root_name}")

    _assert_no_symlink_components(
        vault_root,
        relative,
    )

    target = (vault_root / relative).resolve()

    try:
        target.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError("Path escapes the vault.") from exc

    if not target.is_file():
        raise ValueError(f"Note does not exist: {relative.as_posix()}")

    if target.suffix.lower() != ".md":
        raise ValueError("Only Markdown notes may be opened.")

    return target


def _resolve_write_path(
    vault_root: Path,
    path: str,
) -> Path:
    relative = _normalize_relative_path(path)

    if not relative.parts:
        raise ValueError("Invalid path.")

    if relative.parts[0] != "00_Inbox":
        raise ValueError("Agent writes are restricted to 00_Inbox/.")

    if relative.suffix.lower() != ".md":
        raise ValueError("Agent-created notes must be Markdown files.")

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
        raise ValueError("Write path escapes the vault.") from exc

    if target.exists():
        raise ValueError("Refusing to overwrite an existing note.")

    return target


def _load_graph(
    vault_root: Path,
) -> dict[str, Any]:
    graph_path = derived_outputs(vault_root)["graph"]

    if not graph_path.is_file():
        raise ValueError("Vault index is missing.")

    return json.loads(graph_path.read_text(encoding="utf-8"))


def _safe_fts_query(query: str) -> str:
    words = re.findall(
        r"\w+",
        query,
        flags=re.UNICODE,
    )

    if not words:
        raise ValueError("Search query must contain at least one word.")

    return " AND ".join(f'"{word}"' for word in words)


def vault_map(vault_root: Path, limit: int) -> dict[str, Any]:
    graph = _load_graph(vault_root)

    grouped: dict[
        str,
        list[dict[str, Any]],
    ] = {root: [] for root in INDEXABLE_ROOTS}

    total = 0
    truncated = False

    for node in graph["nodes"]:
        if total >= limit:
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


def vault_search(
    vault_root: Path,
    query: str,
    limit: int,
) -> dict[str, Any]:
    fts_query = _safe_fts_query(query)

    database = derived_outputs(vault_root)["sqlite"]

    if not database.is_file():
        raise ValueError("Vault search index is missing.")

    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)

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


def vault_open(
    vault_root: Path,
    path: str,
    max_chars: int,
) -> dict[str, Any]:
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


def vault_neighbors(
    vault_root: Path,
    path: str,
) -> dict[str, Any]:
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
        raise ValueError("Note is not present in the current index.")

    outgoing = [edge for edge in graph["edges"] if edge["source"] == relative]

    incoming = [edge for edge in graph["edges"] if edge["target"] == relative]

    return {
        "path": relative,
        "title": node["title"],
        "outgoing": outgoing,
        "incoming": incoming,
    }


def vault_write(
    vault_root: Path,
    path: str,
    content: str,
) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("Cannot create an empty note.")

    target = _resolve_write_path(
        vault_root,
        path,
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Exclusive creation also refuses dangling symlinks and concurrent overwrites.
    created = False
    try:
        with target.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(content.rstrip() + "\n")
        index_vault(vault_root)
    except BaseException:
        if created:
            try:
                target.unlink()
            except OSError as rollback_error:
                relative = target.relative_to(vault_root).as_posix()
                raise ValueError(
                    f"Note committed at {relative}; index refresh failed. "
                    "Do not repeat the write; repair the index."
                ) from rollback_error
        raise

    relative = target.relative_to(vault_root).as_posix()

    return {
        "created": True,
        "path": relative,
    }
