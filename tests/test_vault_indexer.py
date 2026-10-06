"""Vault parsing, index outputs, link resolution, and private-content isolation."""

import json
import sqlite3
from contextlib import closing

import pytest
from vault_helpers import create_vault, write_note

from emuru.vault_indexer import collect_notes, extract_wikilinks, index_vault


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
    with closing(sqlite3.connect(vault / "_index/notes.sqlite")) as db:
        rows = db.execute(
            "SELECT path,title FROM note_fts WHERE note_fts MATCH ?", ("retrieval",)
        ).fetchall()
    assert rows == [("30_Resources/retrieval.md", "Retrieval Systems")]


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
