"""Shared synthetic vault setup with explicit allowed-root expectations."""

from pathlib import Path

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
        (vault / directory).mkdir(parents=True, exist_ok=True)

    return vault


def write_note(vault: Path, relative_path: str, content: str) -> Path:
    """Write a Markdown note into the fake vault."""
    path = vault / relative_path

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(content.strip() + "\n", encoding="utf-8")

    return path
