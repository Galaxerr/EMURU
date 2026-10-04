from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
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

        if not base.is_dir():
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

    out_dir = vault_root / "_index"
    out_dir.mkdir(exist_ok=True)

    final_path = out_dir / "graph.json"

    temp_path = out_dir / "graph.json.tmp"

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

    out_dir = vault_root / "_index"
    out_dir.mkdir(exist_ok=True)

    final_path = out_dir / "notes.sqlite"

    temp_path = out_dir / "notes.sqlite.tmp"

    temp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(temp_path)

    try:
        conn.executescript(
            """
            CREATE TABLE note_meta (
                path TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                note_type TEXT NOT NULL,
                project TEXT NOT NULL,
                updated TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                links_json TEXT NOT NULL
            );

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
                INSERT INTO note_meta (
                    path,
                    title,
                    note_type,
                    project,
                    updated,
                    sha256,
                    links_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.path,
                    note.title,
                    note.note_type,
                    note.project,
                    note.updated,
                    note.sha256,
                    json.dumps(
                        note.links,
                        ensure_ascii=False,
                    ),
                ),
            )

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

    output = vault_root / "99_System" / "INDEX.md"

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

    graph_path = write_graph(
        vault_root,
        graph,
    )

    sqlite_path = write_sqlite(
        vault_root,
        notes,
    )

    index_path = write_human_index(
        vault_root,
        notes,
    )

    return {
        "notes": len(notes),
        "edges": len(graph["edges"]),
        "graph": str(graph_path),
        "sqlite": str(sqlite_path),
        "index": str(index_path),
    }
