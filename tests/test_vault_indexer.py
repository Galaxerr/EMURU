import json
import sqlite3

from emuru.vault_indexer import (
    extract_wikilinks,
    index_vault,
)


def create_vault(tmp_path):
    vault = tmp_path / "EMURU-vault"

    folders = [
        "00_Inbox",
        "10_Projects",
        "20_Areas",
        "30_Resources",
        "40_Journal",
        "90_Archive",
        "99_Private",
        "99_System",
        "_index",
    ]

    for folder in folders:
        (vault / folder).mkdir(
            parents=True,
            exist_ok=True,
        )

    return vault


def test_extracts_wikilinks():
    body = """
    See [[AI Security]]
    and [[Cloud/GCP|Google Cloud]]
    and [[Project#Architecture]].
    """

    assert extract_wikilinks(body) == [
        "AI Security",
        "Cloud/GCP",
        "Project",
    ]


def test_private_notes_are_not_indexed(
    tmp_path,
):
    vault = create_vault(tmp_path)

    (vault / "10_Projects" / "public.md").write_text(
        "# Public\nsafe content",
        encoding="utf-8",
    )

    (vault / "99_Private" / "secret.md").write_text(
        "EMURU_PRIVATE_CANARY",
        encoding="utf-8",
    )

    index_vault(vault)

    graph = (vault / "_index" / "graph.json").read_text(encoding="utf-8")

    assert "public.md" in graph
    assert "secret.md" not in graph
    assert "EMURU_PRIVATE_CANARY" not in graph

    generated_index = (vault / "99_System" / "INDEX.md").read_text(encoding="utf-8")

    assert "EMURU_PRIVATE_CANARY" not in generated_index

    db = sqlite3.connect(vault / "_index" / "notes.sqlite")

    try:
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM note_meta
            WHERE path LIKE '99_Private/%'
            """
        ).fetchone()[0]

        assert count == 0

    finally:
        db.close()


def test_wikilink_is_resolved(
    tmp_path,
):
    vault = create_vault(tmp_path)

    (vault / "10_Projects" / "emuru.md").write_text(
        """
---
title: EMURU
type: project
tags: [ai]
---

Uses [[AI Security]].
""".strip(),
        encoding="utf-8",
    )

    (vault / "30_Resources" / "ai-security.md").write_text(
        """
---
title: AI Security
type: resource
---

Security research.
""".strip(),
        encoding="utf-8",
    )

    index_vault(vault)

    graph = json.loads((vault / "_index" / "graph.json").read_text(encoding="utf-8"))

    edge = graph["edges"][0]

    assert edge["source"] == ("10_Projects/emuru.md")

    assert edge["target"] == ("30_Resources/ai-security.md")

    assert edge["status"] == "resolved"


def test_fts_search(
    tmp_path,
):
    vault = create_vault(tmp_path)

    (vault / "30_Resources" / "retrieval.md").write_text(
        """
---
title: Retrieval
type: resource
---

Semantic retrieval foundations.
""".strip(),
        encoding="utf-8",
    )

    index_vault(vault)

    db = sqlite3.connect(vault / "_index" / "notes.sqlite")

    try:
        rows = db.execute(
            """
            SELECT path
            FROM note_fts
            WHERE note_fts MATCH ?
            """,
            ("retrieval",),
        ).fetchall()

        assert rows == [("30_Resources/retrieval.md",)]

    finally:
        db.close()
