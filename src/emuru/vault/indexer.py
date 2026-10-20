from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from emuru.vault.mutations import (
    directory as open_directory,
)
from emuru.vault.mutations import (
    mutation_parent,
    publish_note,
    regular,
    writer_lock,
)

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


COMPLETION = "_index/completion.json"


def derived_outputs(vault_root: Path) -> dict[str, Path]:
    return {key: vault_root / name for key, name in OUTPUTS.items()}


def derived_paths(vault_root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    outputs = (*derived_outputs(vault_root).values(), vault_root / COMPLETION)
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
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError as error:
                raise ValueError("Invalid YAML frontmatter.") from error

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

        if target.lower().endswith(".md"):
            target = target[:-3]

        if target:
            result.add(target.replace("\\", "/"))

    return sorted(result)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_note(
    path: Path,
    vault_root: Path,
    raw: bytes | None = None,
) -> Note:

    if raw is None:
        raw = _read_file(vault_root, path.relative_to(vault_root))
    text = raw.decode("utf-8")

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
        sha256=hashlib.sha256(raw).hexdigest(),
        links=extract_wikilinks(body),
        body=body,
    )


class AccessViolation(ValueError):
    """Unsafe filesystem objects must fail without retry or content reads."""


def _read_file(vault_root: Path, relative: Path) -> bytes:
    with open_directory(vault_root / relative.parent) as parent:
        fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as stream:
            try:
                regular(stream.fileno())
            except ValueError as error:
                raise AccessViolation(
                    "Vault file must be regular with one link."
                ) from error
            return stream.read()


def _snapshot(vault_root: Path) -> dict[str, bytes]:
    snapshot = {}

    def visit(parent, name, relative):
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            return
        if stat.S_ISDIR(metadata.st_mode):
            fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                for child in sorted(os.listdir(fd)):
                    visit(fd, child, relative / child)
            finally:
                os.close(fd)
        elif relative.suffix.lower() == ".md":
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise AccessViolation("Unsafe Markdown file type or hard link.")
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as stream:
                try:
                    regular(stream.fileno())
                except ValueError as error:
                    raise AccessViolation("Unsafe Markdown file.") from error
                snapshot[relative.as_posix()] = stream.read()

    with open_directory(vault_root) as root:
        for name in INDEXABLE_ROOTS:
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            visit(root, name, Path(name))
    return snapshot


def _fingerprint(snapshot: dict[str, bytes]) -> str:
    entries = [
        (path, hashlib.sha256(raw).hexdigest())
        for path, raw in sorted(snapshot.items())
    ]
    return _sha256(json.dumps(entries, ensure_ascii=False))


def iter_note_paths(vault_root: Path):
    for path in _snapshot(vault_root):
        yield vault_root / path


def collect_notes(vault_root: Path) -> list[Note]:
    return _parse_snapshot(vault_root, _snapshot(vault_root))


def _parse_snapshot(vault_root, snapshot):
    return sorted(
        (
            parse_note(vault_root / path, vault_root, raw)
            for path, raw in snapshot.items()
        ),
        key=lambda note: note.path.casefold(),
    )


def _link_aliases(
    note: Note,
) -> set[str]:

    relative = note.path[:-3]

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


def index_vault(vault_root: Path) -> dict[str, Any]:
    vault_root = vault_root.expanduser().absolute()
    if not vault_root.is_dir():
        raise ValueError(f"vault does not exist or is not a directory: {vault_root}")
    with writer_lock(vault_root):
        for attempt in range(2):
            try:
                return _index_vault(vault_root)
            except (AccessViolation, PermissionError):
                raise
            except (OSError, ValueError, sqlite3.Error):
                if attempt:
                    raise


def _index_vault(
    vault_root: Path,
    snapshot: dict[str, bytes] | None = None,
) -> dict[str, Any]:

    vault_root = vault_root.expanduser().absolute()

    if not vault_root.is_dir():
        raise ValueError(f"vault does not exist or is not a directory: {vault_root}")

    if snapshot is None:
        snapshot = _snapshot(vault_root)
    source = _fingerprint(snapshot)
    notes = _parse_snapshot(vault_root, snapshot)

    graph = build_graph(notes)

    directories, files = derived_paths(vault_root)
    for directory in directories:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise AccessViolation("Vault derived directory is invalid.")
        directory.mkdir(exist_ok=True)
    for path in files:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise AccessViolation("Vault derived file is invalid.")

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
        if _fingerprint(_snapshot(vault_root)) != source:
            raise ValueError("Vault changed during index refresh.")
        completion = {
            "source": source,
            "outputs": {
                key: hashlib.sha256(path.read_bytes()).hexdigest()
                for key, path in staged.items()
            },
        }
        for path in staged.values():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        backups = {}
        for key, path in outputs.items():
            backup = staging / (key + ".backup")
            if path.exists():
                shutil.copy2(path, backup)
                backups[key] = backup
        marker = vault_root / COMPLETION
        marker.unlink(missing_ok=True)
        with open_directory(marker.parent) as parent:
            os.fsync(parent)
        published = []
        try:
            for key, path in outputs.items():
                os.replace(staged[key], path)
                published.append(key)
            for folder in directories:
                with open_directory(folder) as parent:
                    os.fsync(parent)
            temporary_record = Path(str(marker) + ".tmp")
            temporary_record.unlink(missing_ok=True)
            fd = os.open(
                temporary_record,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            with os.fdopen(fd, "w") as stream:
                regular(stream.fileno())
                json.dump(completion, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_record, marker)
            with open_directory(marker.parent) as parent:
                os.fsync(parent)
        except BaseException:
            marker.unlink(missing_ok=True)
            with open_directory(marker.parent) as parent:
                os.fsync(parent)
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

    target = vault_root / relative

    try:
        target.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError("Path escapes the vault.") from exc

    if not target.is_file():
        raise ValueError(f"Note does not exist: {relative.as_posix()}")

    if target.suffix.lower() != ".md":
        raise ValueError("Only Markdown notes may be opened.")

    return target


def _load_graph(
    vault_root: Path,
) -> dict[str, Any]:
    graph_path = derived_outputs(vault_root)["graph"]

    if not graph_path.is_file():
        raise ValueError("Vault index is missing.")

    return json.loads(_read_file(vault_root, Path(OUTPUTS["graph"])))


def _safe_fts_query(query: str) -> str:
    words = re.findall(
        r"\w+",
        query,
        flags=re.UNICODE,
    )

    if not words:
        raise ValueError("Search query must contain at least one word.")

    return " AND ".join(f'"{word}"' for word in words)


def _completed(vault_root, source):
    try:
        record = json.loads(_read_file(vault_root, Path(COMPLETION)))
        return record == {
            "source": source,
            "outputs": {
                key: hashlib.sha256(_read_file(vault_root, Path(name))).hexdigest()
                for key, name in OUTPUTS.items()
            },
        }
    except (AccessViolation, PermissionError):
        raise
    except (OSError, ValueError):
        return False


def _validate_results(vault_root, result, snapshot):
    paths = set()
    if "roots" in result:
        paths.update(
            node["path"] for nodes in result["roots"].values() for node in nodes
        )
    if "results" in result:
        paths.update(node["path"] for node in result["results"])
    if "outgoing" in result:
        paths.add(result["path"])
        for edge in result["outgoing"] + result["incoming"]:
            paths.add(edge["source"])
            if edge["target"] is not None:
                paths.add(edge["target"])
    for path in paths:
        target = _resolve_read_path(vault_root, path)
        if _read_file(vault_root, target.relative_to(vault_root)) != snapshot.get(path):
            raise ValueError("Vault result changed during retrieval.")


def _fresh_retrieval(vault_root, function, *args):
    vault_root = vault_root.expanduser().absolute()
    with writer_lock(vault_root):
        for attempt in range(2):
            try:
                snapshot = _snapshot(vault_root)
                if not _completed(vault_root, _fingerprint(snapshot)):
                    _index_vault(vault_root, snapshot)
                result = function(vault_root, *args)
                _validate_results(vault_root, result, snapshot)
                return result
            except (AccessViolation, PermissionError):
                raise
            except (OSError, ValueError, sqlite3.Error) as error:
                if attempt:
                    raise ValueError(
                        "Vault retrieval unavailable after two attempts."
                    ) from error


def vault_map(vault_root: Path, limit: int) -> dict[str, Any]:
    return _fresh_retrieval(vault_root, _vault_map, limit)


def vault_search(vault_root: Path, query: str, limit: int) -> dict[str, Any]:
    _safe_fts_query(query)
    return _fresh_retrieval(vault_root, _vault_search, query, limit)


def vault_neighbors(vault_root: Path, path: str) -> dict[str, Any]:
    _resolve_read_path(vault_root, path)
    return _fresh_retrieval(vault_root, _vault_neighbors, path)


def _vault_map(vault_root: Path, limit: int) -> dict[str, Any]:
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


def _vault_search(
    vault_root: Path,
    query: str,
    limit: int,
) -> dict[str, Any]:
    fts_query = _safe_fts_query(query)

    database = derived_outputs(vault_root)["sqlite"]

    if not database.is_file():
        raise ValueError("Vault search index is missing.")

    with open_directory(database.parent) as parent:
        fd = os.open(
            database.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            regular(fd)
            connection = sqlite3.connect(
                f"file:/proc/self/fd/{fd}?mode=ro&immutable=1", uri=True
            )
        finally:
            os.close(fd)

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

    raw = _read_file(vault_root, target.relative_to(vault_root))
    text = raw.decode("utf-8")

    truncated = len(text) > max_chars

    content = text[:max_chars]

    relative = target.relative_to(vault_root).as_posix()

    return {
        "path": relative,
        "content": content,
        "characters": len(content),
        "total_characters": len(text),
        "truncated": truncated,
        "revision": hashlib.sha256(raw).hexdigest(),
    }


def _vault_neighbors(
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

    with (
        writer_lock(vault_root),
        mutation_parent(vault_root, path, capture=True) as (parent, name),
    ):
        normalized = (content.rstrip() + "\n").encode("utf-8")
        publish_note(parent, name, normalized, before=None)
        try:
            index_vault(vault_root)
        except (OSError, ValueError, sqlite3.Error):
            return {
                "created": True,
                "path": path,
                "outcome": "index_pending",
                "message": "saved; indexing pending",
            }

    return {"created": True, "path": path}
