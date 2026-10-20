"""Revision hashes returned by vault_open."""

import hashlib

from vault_helpers import create_vault

from emuru.vault.indexer import vault_open


def test_truncated_open_returns_hash_of_full_raw_file(tmp_path):
    vault = create_vault(tmp_path)
    raw = b"# Title\r\nFull note body\r\n"
    path = vault / "10_Projects/note.md"
    path.write_bytes(raw)

    result = vault_open(vault, "10_Projects/note.md", 7)

    assert result["content"] == "# Title"
    assert result["truncated"] is True
    assert result["revision"] == hashlib.sha256(raw).hexdigest()


def test_open_preserves_crlf(tmp_path):
    vault = create_vault(tmp_path)
    raw = b"line one\r\nline two\r\n"
    (vault / "10_Projects/note.md").write_bytes(raw)

    result = vault_open(vault, "10_Projects/note.md", 100)

    assert result["content"] == "line one\r\nline two\r\n"
    assert result["total_characters"] == len(raw.decode("utf-8"))


def test_same_length_byte_edit_changes_revision(tmp_path):
    vault = create_vault(tmp_path)
    path = vault / "10_Projects/note.md"
    path.write_bytes(b"abc\n")
    before = vault_open(vault, "10_Projects/note.md", 100)["revision"]

    path.write_bytes(b"abd\n")
    after = vault_open(vault, "10_Projects/note.md", 100)["revision"]

    assert len(b"abc\n") == len(b"abd\n")
    assert before != after
