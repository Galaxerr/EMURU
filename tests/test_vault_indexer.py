import json
import sqlite3
from pathlib import Path

import pytest

from emuru.vault_indexer import (
    collect_notes,
    extract_wikilinks,
    index_vault,
)

INDEXABLE_DIRS = (
    "00_Inbox",
    "10_Projects",
    "20_Areas",
    "30_Resources",
    "40_Journal",
    "90_Archive",
)


def create_vault(tmp_path: Path) -> Path:
    """Create a minimal fake EMURU vault."""
    vault = tmp_path / "EMURU-vault"

    for directory in (
        *INDEXABLE_DIRS,
        "99_Private",
        "99_System",
        "_index",
    ):
        (vault / directory).mkdir(
            parents=True,
            exist_ok=True,
        )

    return vault


def write_note(
    vault: Path,
    relative_path: str,
    content: str,
) -> Path:
    """Write a Markdown note into the fake vault."""
    path = vault / relative_path

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        content.strip() + "\n",
        encoding="utf-8",
    )

    return path


def load_graph(vault: Path) -> dict:
    return json.loads((vault / "_index" / "graph.json").read_text(encoding="utf-8"))


def open_index_db(
    vault: Path,
) -> sqlite3.Connection:
    return sqlite3.connect(vault / "_index" / "notes.sqlite")


def test_extract_wikilinks():
    body = """
    See [[AI Security]]
    and [[Cloud/GCP|Google Cloud]]
    and [[Project#Architecture]]
    and [[notes/example.md]]
    and [[AI Security]] again.
    """

    assert extract_wikilinks(body) == [
        "AI Security",
        "Cloud/GCP",
        "Project",
        "notes/example",
    ]


def test_frontmatter_is_parsed(
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

    assert note.path == ("10_Projects/emuru.md")
    assert note.title == "EMURU"
    assert note.note_type == "project"
    assert note.project == "emuru"
    assert note.tags == [
        "ai",
        "agents",
    ]
    assert note.updated == "2026-10-04"
    assert note.summary == ("Personal AI agent platform.")
    assert "Project content." in note.body


def test_note_without_frontmatter_uses_defaults(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "30_Resources/plain-note.md",
        """
        # Plain Note

        This note has no frontmatter.
        """,
    )

    notes = collect_notes(vault)

    assert len(notes) == 1

    note = notes[0]

    assert note.title == "plain-note"
    assert note.note_type == "note"
    assert note.project == ""
    assert note.tags == []
    assert note.summary == ""


def test_only_allowlisted_roots_are_indexed(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "10_Projects/public.md",
        """
        # Public

        This should be indexed.
        """,
    )

    write_note(
        vault,
        "99_Private/private.md",
        """
        EMURU_PRIVATE_CANARY
        """,
    )

    write_note(
        vault,
        "UnexpectedFolder/unknown.md",
        """
        EMURU_UNKNOWN_FOLDER_CANARY
        """,
    )

    notes = collect_notes(vault)

    paths = {note.path for note in notes}

    assert "10_Projects/public.md" in paths

    assert "99_Private/private.md" not in paths

    assert "UnexpectedFolder/unknown.md" not in paths


def test_private_content_never_enters_outputs(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "10_Projects/public.md",
        """
        ---
        title: Public Project
        type: project
        ---

        Safe public knowledge.
        """,
    )

    write_note(
        vault,
        "99_Private/private.md",
        """
        EMURU_PRIVATE_CANARY_934782
        """,
    )

    index_vault(vault)

    graph_text = (vault / "_index" / "graph.json").read_text(encoding="utf-8")

    human_index = (vault / "99_System" / "INDEX.md").read_text(encoding="utf-8")

    assert "EMURU_PRIVATE_CANARY_934782" not in graph_text

    assert "EMURU_PRIVATE_CANARY_934782" not in human_index

    db = open_index_db(vault)

    try:
        private_metadata = db.execute(
            """
            SELECT COUNT(*)
            FROM note_meta
            WHERE path LIKE '99_Private/%'
            """
        ).fetchone()[0]

        private_search = db.execute(
            """
            SELECT COUNT(*)
            FROM note_fts
            WHERE note_fts MATCH ?
            """,
            ("EMURU_PRIVATE_CANARY_934782",),
        ).fetchone()[0]

        assert private_metadata == 0
        assert private_search == 0

    finally:
        db.close()


def test_wikilink_resolves_to_graph_edge(
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
        ---

        EMURU uses [[AI Security]].
        """,
    )

    write_note(
        vault,
        "30_Resources/ai-security.md",
        """
        ---
        title: AI Security
        type: resource
        ---

        Security research notes.
        """,
    )

    index_vault(vault)

    graph = load_graph(vault)

    edge = next(
        edge for edge in graph["edges"] if (edge["source"] == "10_Projects/emuru.md")
    )

    assert edge["raw_target"] == ("AI Security")

    assert edge["target"] == ("30_Resources/ai-security.md")

    assert edge["status"] == "resolved"


def test_unresolved_wikilink_is_recorded(
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
        ---

        See [[Something That Does Not Exist]].
        """,
    )

    index_vault(vault)

    graph = load_graph(vault)

    assert len(graph["edges"]) == 1

    edge = graph["edges"][0]

    assert edge["raw_target"] == ("Something That Does Not Exist")

    assert edge["target"] is None
    assert edge["status"] == "unresolved"


def test_ambiguous_wikilink_is_recorded(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "10_Projects/source.md",
        """
        ---
        title: Source
        ---

        See [[Architecture]].
        """,
    )

    write_note(
        vault,
        "20_Areas/architecture.md",
        """
        ---
        title: Architecture
        ---

        Area architecture note.
        """,
    )

    write_note(
        vault,
        "30_Resources/architecture.md",
        """
        ---
        title: Architecture
        ---

        Resource architecture note.
        """,
    )

    index_vault(vault)

    graph = load_graph(vault)

    edge = next(
        edge for edge in graph["edges"] if (edge["source"] == "10_Projects/source.md")
    )

    assert edge["raw_target"] == ("Architecture")
    assert edge["target"] is None
    assert edge["status"] == "ambiguous"


def test_fts_search_returns_matching_note(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    write_note(
        vault,
        "30_Resources/retrieval.md",
        """
        ---
        title: Retrieval Systems
        type: resource
        tags:
          - search
          - knowledge
        summary: Notes about information retrieval.
        ---

        Semantic retrieval and full-text
        search are useful for EMURU.
        """,
    )

    write_note(
        vault,
        "30_Resources/networking.md",
        """
        ---
        title: Networking
        type: resource
        ---

        TCP and UDP networking notes.
        """,
    )

    index_vault(vault)

    db = open_index_db(vault)

    try:
        rows = db.execute(
            """
            SELECT path, title
            FROM note_fts
            WHERE note_fts MATCH ?
            """,
            ("retrieval",),
        ).fetchall()

    finally:
        db.close()

    assert rows == [
        (
            "30_Resources/retrieval.md",
            "Retrieval Systems",
        )
    ]


def test_generated_human_index_contains_notes(
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
        summary: Personal AI agent platform.
        ---

        # EMURU
        """,
    )

    index_vault(vault)

    index_text = (vault / "99_System" / "INDEX.md").read_text(encoding="utf-8")

    assert "# EMURU Vault Index" in index_text

    assert "## 10_Projects" in index_text

    assert "[[10_Projects/emuru|EMURU]]" in index_text

    assert "Personal AI agent platform." in index_text


def test_file_symlink_is_not_indexed(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    private_note = write_note(
        vault,
        "99_Private/secret.md",
        """
        EMURU_SYMLINK_SECRET
        """,
    )

    link = vault / "30_Resources" / "fake-public-note.md"

    link.symlink_to(private_note)

    notes = collect_notes(vault)

    assert all(note.path != "30_Resources/fake-public-note.md" for note in notes)


def test_directory_symlink_is_not_followed(
    tmp_path: Path,
):
    vault = create_vault(tmp_path)

    private_dir = vault / "99_Private" / "secret-directory"

    private_dir.mkdir()

    (private_dir / "secret.md").write_text(
        "EMURU_DIRECTORY_SYMLINK_SECRET",
        encoding="utf-8",
    )

    link = vault / "30_Resources" / "linked-private-directory"

    link.symlink_to(
        private_dir,
        target_is_directory=True,
    )

    notes = collect_notes(vault)

    assert all("linked-private-directory" not in note.path for note in notes)


def test_missing_vault_raises_error(
    tmp_path: Path,
):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(
        ValueError,
        match="vault does not exist",
    ):
        index_vault(missing)
