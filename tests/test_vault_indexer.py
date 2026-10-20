"""Vault parsing, index outputs, link resolution, and private-content isolation."""

import json
import multiprocessing
import os
import signal
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from vault_helpers import create_vault, write_note

from emuru.vault import indexer, mutations
from emuru.vault.indexer import (
    collect_notes,
    extract_wikilinks,
    index_vault,
    vault_search,
)


def test_extract_wikilinks():
    body = """See [[AI Security]] and [[Cloud/GCP|Google Cloud]],
    [[Project#Architecture]], [[notes/example.md]], and [[AI Security]] again."""
    assert extract_wikilinks(body) == [
        "AI Security",
        "Cloud/GCP",
        "Project",
        "notes/example",
    ]


def test_frontmatter_is_parsed(tmp_path):
    vault = create_vault(tmp_path)
    write_note(
        vault,
        "10_Projects/emuru.md",
        """
---
title: EMURU
type: project
project: emuru
tags:
  - ai
  - agents
updated: 2026-10-04
summary: Personal AI agent platform.
---
# EMURU
Project content.
""",
    )
    notes = collect_notes(vault)
    assert len(notes) == 1
    note = notes[0]
    assert note.path == "10_Projects/emuru.md"
    assert note.title == "EMURU"
    assert note.note_type == "project"
    assert note.project == "emuru"
    assert note.tags == ["ai", "agents"]
    assert note.updated == "2026-10-04"
    assert note.summary == "Personal AI agent platform."
    assert "Project content." in note.body


def test_note_without_frontmatter_uses_defaults(tmp_path):
    vault = create_vault(tmp_path)
    write_note(vault, "30_Resources/plain-note.md", "# Plain Note\nNo frontmatter.")
    notes = collect_notes(vault)
    assert len(notes) == 1
    note = notes[0]
    assert note.title == "plain-note"
    assert note.note_type == "note"
    assert note.project == note.summary == ""
    assert note.tags == []


def test_only_allowlisted_roots_are_indexed(tmp_path):
    vault = create_vault(tmp_path)
    for path, content in (
        ("10_Projects/public.md", "# Public\nThis should be indexed."),
        ("99_Private/private.md", "EMURU_PRIVATE_CANARY"),
        ("UnexpectedFolder/unknown.md", "EMURU_UNKNOWN_FOLDER_CANARY"),
    ):
        write_note(vault, path, content)
    assert {note.path for note in collect_notes(vault)} == {"10_Projects/public.md"}


def test_private_content_never_enters_outputs(tmp_path):
    vault = create_vault(tmp_path)
    canary = "EMURU_PRIVATE_CANARY_934782"
    write_note(
        vault, "10_Projects/public.md", "# Public Project\nSafe public knowledge."
    )
    write_note(vault, "99_Private/private.md", canary)
    index_vault(vault)
    graph_text = (vault / "_index/graph.json").read_text(encoding="utf-8")
    assert canary not in graph_text
    assert not any(
        node["path"].startswith("99_Private/")
        for node in json.loads(graph_text)["nodes"]
    )
    assert canary not in (vault / "99_System/INDEX.md").read_text(encoding="utf-8")
    with closing(sqlite3.connect(vault / "_index/notes.sqlite")) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM note_fts WHERE path LIKE '99_Private/%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM note_fts WHERE note_fts MATCH ?", (canary,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "target,paths,status",
    [
        ("AI Security", ["30_Resources/ai-security.md"], "resolved"),
        ("Something That Does Not Exist", [], "unresolved"),
        (
            "Architecture",
            ["20_Areas/architecture.md", "30_Resources/architecture.md"],
            "ambiguous",
        ),
    ],
)
def test_wikilink_resolution(tmp_path, target, paths, status):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/source.md", f"See [[{target}]].")
    for path in paths:
        write_note(vault, path, f"---\ntitle: {target}\n---\nReference note.")
    index_vault(vault)
    edges = json.loads((vault / "_index/graph.json").read_text())["edges"]
    assert len(edges) == 1
    assert edges[0] == {
        "source": "10_Projects/source.md",
        "raw_target": target,
        "target": paths[0] if status == "resolved" else None,
        "status": status,
    }


def test_fts_search_returns_matching_note(tmp_path):
    vault = create_vault(tmp_path)
    write_note(
        vault,
        "30_Resources/retrieval.md",
        """
---
title: Retrieval Systems
type: resource
tags: [search, knowledge]
summary: Notes about information retrieval.
---
Semantic retrieval and full-text search are useful for EMURU.
""",
    )
    write_note(vault, "30_Resources/networking.md", "TCP and UDP networking notes.")
    index_vault(vault)
    results = vault_search(vault, "retrieval", 8)
    assert results["count"] == 1
    assert results["results"][0]["path"] == "30_Resources/retrieval.md"
    assert results["results"][0]["title"] == "Retrieval Systems"


def test_generated_human_index_contains_notes(tmp_path):
    vault = create_vault(tmp_path)
    write_note(
        vault,
        "10_Projects/emuru.md",
        """
---
title: EMURU
type: project
summary: Personal AI agent platform.
---
# EMURU
""",
    )
    index_vault(vault)
    text = (vault / "99_System/INDEX.md").read_text(encoding="utf-8")
    for expected in (
        "# EMURU Vault Index",
        "## 10_Projects",
        "[[10_Projects/emuru|EMURU]]",
        "Personal AI agent platform.",
    ):
        assert expected in text


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_symlinks_are_not_indexed(tmp_path, directory):
    vault = create_vault(tmp_path)
    note = write_note(
        vault, "99_Private/secret-directory/secret.md", "EMURU_SYMLINK_SECRET"
    )
    link = (
        vault
        / "30_Resources"
        / ("linked-private" if directory else "linked-private.md")
    )
    link.symlink_to(note.parent if directory else note, target_is_directory=directory)
    assert all("linked-private" not in note.path for note in collect_notes(vault))


def test_missing_vault_raises_error(tmp_path):
    with pytest.raises(ValueError, match="vault does not exist"):
        index_vault(tmp_path / "does-not-exist")


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_interrupted_write_preserves_previous_index(tmp_path, monkeypatch, error):
    import emuru.vault.indexer as index

    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/original.md", "Original")
    index_vault(vault)
    paths = [
        vault / name
        for name in ("_index/graph.json", "_index/notes.sqlite", "99_System/INDEX.md")
    ]
    before = [path.read_bytes() for path in paths]

    def interrupt(root):
        raise error("publication interrupted")

    monkeypatch.setattr(index, "index_vault", interrupt)
    if error is KeyboardInterrupt:
        with pytest.raises(error, match="publication interrupted"):
            index.vault_write(vault, "00_Inbox/new.md", "New")
    else:
        assert index.vault_write(vault, "00_Inbox/new.md", "New")["outcome"] == (
            "index_pending"
        )
    assert (vault / "00_Inbox/new.md").read_text(encoding="utf-8") == "New\n"
    assert [path.read_bytes() for path in paths] == before


def test_allowlisted_root_symlink_does_not_expose_private_notes(tmp_path):
    vault = create_vault(tmp_path)
    write_note(vault, "99_Private/secret.md", "PRIVATE_ROOT_SYMLINK_CANARY")
    root = vault / "30_Resources"
    root.rmdir()
    root.symlink_to(vault / "99_Private", target_is_directory=True)
    index_vault(vault)
    assert vault_search(vault, "PRIVATE_ROOT_SYMLINK_CANARY", 8)["count"] == 0


@pytest.mark.parametrize("retrieval", ["search", "map", "neighbors"])
def test_external_changes_refresh_all_retrievals(tmp_path, retrieval):
    vault = create_vault(tmp_path)
    source = write_note(vault, "10_Projects/source.md", "[[Target]] oldtoken")
    index_vault(vault)

    def read():
        if retrieval == "search":
            return indexer.vault_search(vault, "newtoken", 20)
        if retrieval == "map":
            return indexer.vault_map(vault, 200)
        return indexer.vault_neighbors(vault, "10_Projects/source.md")

    target = write_note(
        vault,
        "30_Resources/target.md",
        "---\ntitle: Target\nsummary: New summary\n---\nnewtoken [[source]]",
    )
    result = json.dumps(read())
    assert "30_Resources/target.md" in result
    source.write_text("[[Target]] newtoken")
    read()
    assert vault_search(vault, "oldtoken", 20)["count"] == 0
    target.rename(vault / "30_Resources/renamed.md")
    result = json.dumps(read())
    assert "30_Resources/target.md" not in result
    assert "30_Resources/renamed.md" in result
    (vault / "30_Resources/renamed.md").unlink()
    result = json.dumps(read())
    assert "30_Resources/renamed.md" not in result
    source.unlink()
    assert indexer.vault_map(vault, 200)["total_notes"] == 0
    assert vault_search(vault, "newtoken", 20)["count"] == 0


def test_content_hash_ignores_timestamps_and_size(tmp_path):
    vault = create_vault(tmp_path)
    note = write_note(vault, "40_Journal/note.md", "oldtoken")
    index_vault(vault)
    metadata = note.stat()
    note.write_bytes(b"newtoken\n")
    os.utime(note, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert note.stat().st_size == metadata.st_size
    assert vault_search(vault, "oldtoken", 8)["count"] == 0
    assert vault_search(vault, "newtoken", 8)["count"] == 1
    write_note(vault, "90_Archive/old.md", "newtoken")
    assert vault_search(vault, "newtoken", 8)["count"] == 2


def test_metadata_and_link_edits_refresh_both_directions(tmp_path):
    vault = create_vault(tmp_path)
    source = write_note(vault, "10_Projects/source.md", "[[Old]]")
    target = write_note(vault, "30_Resources/target.md", "---\ntitle: Old\n---\nbody")
    index_vault(vault)
    target.write_text("---\ntitle: New\nsummary: newsum\n---\n[[source]]")
    assert (
        indexer.vault_neighbors(vault, "10_Projects/source.md")["outgoing"][0]["status"]
        == "unresolved"
    )
    source.write_text("[[New]]")
    neighbors = indexer.vault_neighbors(vault, "30_Resources/target.md")
    assert neighbors["title"] == "New"
    assert neighbors["incoming"][0]["source"] == "10_Projects/source.md"
    assert neighbors["outgoing"][0]["target"] == "10_Projects/source.md"
    assert vault_search(vault, "newsum", 8)["results"][0]["summary"] == "newsum"


def test_unchanged_content_and_open_skip_rebuild(tmp_path, monkeypatch):
    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/source.md", "body")
    index_vault(vault)

    def forbidden(*args):
        pytest.fail("unchanged content rebuilt")

    monkeypatch.setattr(indexer, "_index_vault", forbidden)
    indexer.vault_map(vault, 200)
    indexer.vault_search(vault, "body", 8)
    indexer.vault_neighbors(vault, "10_Projects/source.md")
    note.write_text("changed")
    assert (
        indexer.vault_open(vault, "10_Projects/source.md", 100)["content"] == "changed"
    )


@pytest.mark.parametrize("kind", ["file", "directory", "root"])
def test_symlinks_introduced_after_indexing(tmp_path, kind):
    vault = create_vault(tmp_path)
    public = write_note(vault, "30_Resources/sub/public.md", "publictoken")
    secret = write_note(vault, "99_Private/secret.md", "privatecanary")
    index_vault(vault)
    if kind == "file":
        public.unlink()
        public.symlink_to(secret)
    elif kind == "directory":
        public.parent.rename(vault / "99_Private/moved")
        (vault / "30_Resources/sub").symlink_to(secret.parent, target_is_directory=True)
    else:
        (vault / "30_Resources").rename(vault / "99_Private/moved")
        (vault / "30_Resources").symlink_to(secret.parent, target_is_directory=True)
    assert vault_search(vault, "privatecanary", 8)["count"] == 0
    assert vault_search(vault, "publictoken", 8)["count"] == 0
    assert indexer.vault_map(vault, 200)["total_notes"] == 0


@pytest.mark.parametrize("kind", ["fifo", "socket", "hardlink"])
def test_unsafe_markdown_rejected_before_read(tmp_path, kind):
    import socket

    vault = create_vault(tmp_path)
    secret = write_note(vault, "99_Private/secret.md", "privatecanary")
    index_vault(vault)
    unsafe = vault / "30_Resources/unsafe.md"
    sock = socket.socket(socket.AF_UNIX)
    try:
        if kind == "fifo":
            os.mkfifo(unsafe)
        elif kind == "socket":
            sock.bind(str(unsafe))
        else:
            os.link(secret, unsafe)
        with pytest.raises(indexer.AccessViolation):
            vault_search(vault, "privatecanary", 8)
    finally:
        sock.close()


@pytest.mark.parametrize("failures", [1, 2])
def test_unstable_refresh_bounded_retry(tmp_path, monkeypatch, failures):
    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/source.md", "oldtoken")
    index_vault(vault)
    note.write_text("newtoken")
    original = indexer.write_human_index
    attempts = []

    def unstable(root, notes):
        result = original(root, notes)
        attempts.append(1)
        if len(attempts) <= failures:
            note.write_text("newtoken " + str(len(attempts)))
        return result

    monkeypatch.setattr(indexer, "write_human_index", unstable)
    if failures == 1:
        assert vault_search(vault, "newtoken", 8)["count"] == 1
    else:
        with pytest.raises(ValueError, match="retrieval unavailable"):
            vault_search(vault, "oldtoken", 8)
    assert len(attempts) == 2


@pytest.mark.parametrize("name", [*indexer.OUTPUTS.values(), indexer.COMPLETION])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_completion_and_outputs_repaired(tmp_path, name, damage):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/source.md", "body")
    index_vault(vault)
    path = vault / name
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupt")
    assert vault_search(vault, "body", 8)["count"] == 1
    assert indexer._completed(vault, indexer._fingerprint(indexer._snapshot(vault)))


def test_returned_path_change_retries(tmp_path, monkeypatch):
    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/source.md", "body")
    index_vault(vault)
    original = indexer._vault_map
    calls = []

    def change(root, limit):
        result = original(root, limit)
        if not calls:
            note.unlink()
        calls.append(1)
        return result

    monkeypatch.setattr(indexer, "_vault_map", change)
    assert indexer.vault_map(vault, 200)["total_notes"] == 0
    assert len(calls) == 2


def _kill_refresh(vault, point):
    original = indexer.os.replace

    def replace(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if Path(target) == vault / point:
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    from pathlib import Path

    indexer.os.replace = replace
    index_vault(vault)


@pytest.mark.parametrize("point", ["_index/graph.json", "99_System/INDEX.md"])
def test_sigkill_publication_surviving_reader_repairs(tmp_path, point):
    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/source.md", "oldtoken")
    index_vault(vault)
    assert vault_search(vault, "oldtoken", 8)["count"] == 1
    note.write_text("newtoken")
    child = multiprocessing.get_context("spawn").Process(
        target=_kill_refresh, args=(vault, point)
    )
    child.start()
    child.join(15)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("refresh child hung")
    assert child.exitcode == -signal.SIGKILL
    assert not (vault / indexer.COMPLETION).exists()
    assert note.read_text() == "newtoken"
    assert vault_search(vault, "oldtoken", 8)["count"] == 0
    assert vault_search(vault, "newtoken", 8)["count"] == 1


def test_retrieval_capture_apply_share_reentrant_lock(tmp_path):
    vault = create_vault(tmp_path)
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text(json.dumps({"enabled": False, "roots": ["10_Projects"]}))
    policy.chmod(0o600)
    proposal = mutations.propose_note(
        home, vault, "10_Projects/project.md", content="project"
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(indexer.vault_map, vault, 200),
            pool.submit(indexer.vault_write, vault, "00_Inbox/capture.md", "capture"),
            pool.submit(
                mutations.apply_proposal,
                home,
                vault,
                proposal["proposal_id"],
                proposal["approval_digest"],
                writers_paused=True,
            ),
        ]
        for future in futures:
            future.result(timeout=15)
    assert indexer.vault_map(vault, 200)["total_notes"] == 2


@pytest.mark.parametrize("failures", [1, 2])
def test_scan_failures_retry_once(tmp_path, monkeypatch, failures):
    vault = create_vault(tmp_path)
    write_note(vault, "10_Projects/note.md", "body")
    index_vault(vault)
    scan = indexer._snapshot
    calls = []

    def fail(root):
        calls.append(1)
        if len(calls) <= failures:
            raise OSError("scan interrupted")
        return scan(root)

    monkeypatch.setattr(indexer, "_snapshot", fail)
    if failures == 1:
        assert vault_search(vault, "body", 8)["count"] == 1
    else:
        with pytest.raises(ValueError, match="retrieval unavailable"):
            vault_search(vault, "body", 8)
    assert len(calls) == 2


def test_publication_exception_rolls_back_but_invalidates_completion(
    tmp_path, monkeypatch
):
    vault = create_vault(tmp_path)
    note = write_note(vault, "10_Projects/note.md", "oldtoken")
    index_vault(vault)
    before = {name: (vault / name).read_bytes() for name in indexer.OUTPUTS.values()}
    note.write_text("newtoken")
    replace = indexer.os.replace

    def fail(source, target, *args, **kwargs):
        if target == vault / "_index/notes.sqlite" and str(source).endswith(
            "notes.sqlite"
        ):
            raise OSError("publication failed")
        return replace(source, target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(indexer.os, "replace", fail)
        with pytest.raises(ValueError, match="retrieval unavailable"):
            vault_search(vault, "oldtoken", 8)
    assert not (vault / indexer.COMPLETION).exists()
    assert all((vault / name).read_bytes() == raw for name, raw in before.items())
    assert note.read_text() == "newtoken"
    assert vault_search(vault, "newtoken", 8)["count"] == 1


def test_open_rejects_parent_swapped_after_path_check(tmp_path, monkeypatch):
    vault = create_vault(tmp_path)
    note = write_note(vault, "30_Resources/sub/note.md", "public")
    write_note(vault, "99_Private/note.md", "privatecanary")
    check = indexer._assert_no_symlink_components

    def swap(root, relative):
        check(root, relative)
        note.parent.rename(vault / "30_Resources/old")
        (vault / "30_Resources/sub").symlink_to(
            vault / "99_Private", target_is_directory=True
        )

    monkeypatch.setattr(indexer, "_assert_no_symlink_components", swap)
    with pytest.raises(OSError):
        indexer.vault_open(vault, "30_Resources/sub/note.md", 100)


def test_permission_denied_fails_without_retry(tmp_path, monkeypatch):
    vault = create_vault(tmp_path)
    calls = []

    def denied(root):
        calls.append(1)
        raise PermissionError("access denied")

    monkeypatch.setattr(indexer, "_snapshot", denied)
    with pytest.raises(PermissionError, match="access denied"):
        vault_search(vault, "body", 8)
    assert len(calls) == 1


def test_index_pending_capture_repaired_without_replaying_note(tmp_path, monkeypatch):
    vault = create_vault(tmp_path)
    index_vault(vault)

    def fail(root):
        raise OSError("refresh failed")

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "index_vault", fail)
        result = indexer.vault_write(vault, "00_Inbox/saved.md", "savedtoken")
    assert result["outcome"] == "index_pending"
    note = vault / "00_Inbox/saved.md"
    before = note.stat()
    assert vault_search(vault, "savedtoken", 8)["count"] == 1
    assert note.stat().st_ino == before.st_ino
    assert note.stat().st_mtime_ns == before.st_mtime_ns
