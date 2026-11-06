"""M2 parsing and pin-accurate native board checks, isolated from live profiles."""

import copy
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from project_board_case import SPEC

from emuru.projects import board
from emuru.projects.board import spec_tasks
from emuru.projects.spec import spec_markdown

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("suffix", [".init.lock", "-journal", "-wal", "-shm"])
def test_readonly_native_board_refuses_symlinked_sidecar(tmp_path, monkeypatch, suffix):
    home = tmp_path / "profile"
    folder = home / "emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "kanban.db").write_bytes(b"database")
    (folder / "kanban.db").chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    (folder / ("kanban.db" + suffix)).symlink_to(outside)
    monkeypatch.setattr(board, "native_api", lambda _root: None)
    with pytest.raises(OSError), board.project_board_readonly(home, ROOT):
        pass
    assert outside.read_text() == "preserve"


def test_readonly_native_board_opens_validated_database_descriptor(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    folder = home / "emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    database = folder / "kanban.db"
    outside = tmp_path / "outside.db"
    for path, value in ((database, "pinned"), (outside, "replacement")):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE marker (value TEXT)")
            db.execute("INSERT INTO marker VALUES (?)", (value,))
        path.chmod(0o600)
    monkeypatch.setattr(board, "native_api", lambda _root: None)
    connect = sqlite3.connect

    def replace_path_after_validation(path, **kwargs):
        database.rename(folder / "original.db")
        database.symlink_to(outside)
        return connect(path, **kwargs)

    monkeypatch.setattr(board.sqlite3, "connect", replace_path_after_validation)
    with board.project_board_readonly(home, ROOT) as (_kb, db):
        assert db.execute("SELECT value FROM marker").fetchone()[0] == "pinned"


def test_readonly_native_board_reads_wal_without_changing_source(tmp_path, monkeypatch):
    folder = tmp_path / "profile/emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    path = folder / "kanban.db"
    monkeypatch.setattr(board, "native_api", lambda _root: None)
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE marker (value TEXT)")
        writer.commit()
        writer.execute("INSERT INTO marker VALUES ('committed in WAL')")
        writer.commit()
        for item in folder.iterdir():
            item.chmod(0o600)
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        with board.project_board_readonly(folder.parent, ROOT) as (_kb, reader):
            assert reader.execute("SELECT value FROM marker").fetchone()[0] == (
                "committed in WAL"
            )
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("INSERT INTO marker VALUES ('forged')")
        assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_readonly_native_board_pins_file_before_snapshot_copy(tmp_path, monkeypatch):
    from emuru.telegram import queue

    folder = tmp_path / "profile/emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    database, outside = folder / "kanban.db", tmp_path / "outside.db"
    for path, value in ((database, "pinned"), (outside, "replacement")):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE marker (value TEXT)")
            db.execute("INSERT INTO marker VALUES (?)", (value,))
        path.chmod(0o600)
    snapshot = queue._diagnostic_snapshot

    def replace_before_copy(*args, **kwargs):
        database.rename(folder / "original.db")
        database.symlink_to(outside)
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(board, "native_api", lambda _root: None)
    monkeypatch.setattr(queue, "_diagnostic_snapshot", replace_before_copy)
    with board.project_board_readonly(folder.parent, ROOT) as (_kb, reader):
        assert reader.execute("SELECT value FROM marker").fetchone()[0] == "pinned"


def test_readonly_native_board_refuses_recovery_without_writing(tmp_path, monkeypatch):
    folder = tmp_path / "profile/emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    database = folder / "kanban.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE marker (value TEXT)")
    journal = folder / "kanban.db-journal"
    journal.write_bytes(b"unfinished rollback journal")
    for item in folder.iterdir():
        item.chmod(0o600)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    monkeypatch.setattr(board, "native_api", lambda _root: None)
    with (
        pytest.raises(ValueError, match="recovery required"),
        board.project_board_readonly(folder.parent, ROOT),
    ):
        pass
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


@pytest.mark.parametrize(
    "suffix,reason",
    [("-journal", "recovery required"), ("-wal", "changed during lookup")],
)
def test_readonly_native_board_refuses_sidecar_appearing_during_snapshot(
    tmp_path, monkeypatch, suffix, reason
):
    from contextlib import contextmanager

    from emuru.telegram import queue

    folder = tmp_path / "profile/emuru-projects"
    folder.mkdir(parents=True, mode=0o700)
    database = folder / "kanban.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE marker (value TEXT)")
    database.chmod(0o600)
    original = database.read_bytes()
    snapshot = queue._diagnostic_snapshot
    sidecar = folder / ("kanban.db" + suffix)

    @contextmanager
    def concurrent_sidecar(*args, **kwargs):
        with snapshot(*args, **kwargs) as copy:
            sidecar.write_bytes(b"unresolved concurrent state")
            sidecar.chmod(0o600)
            yield copy

    monkeypatch.setattr(board, "native_api", lambda _root: None)
    monkeypatch.setattr(queue, "_diagnostic_snapshot", concurrent_sidecar)
    with (
        pytest.raises(ValueError, match=reason),
        board.project_board_readonly(folder.parent, ROOT),
    ):
        pass
    assert database.read_bytes() == original
    assert sidecar.read_bytes() == b"unresolved concurrent state"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("python -m unittest", "Zero exit"),
        ("run-check; print-summary", "Zero exit; no errors"),
        ("run-check;expected: literal", "prints ; Expected: OK"),
    ],
)
def test_approved_spec_task_projection(command, expected):
    spec = copy.deepcopy(SPEC)
    spec["tasks"][0]["acceptance"][0] = {
        "command": command,
        "expected_result": expected,
    }
    content = spec_markdown(spec)
    assert spec_tasks(content) == spec["tasks"]
    assert spec_tasks(content.replace("\n", "\r\n")) == spec["tasks"]


@pytest.mark.parametrize(
    "check",
    [
        "run-check expected: OK",
        "run-check; expected: prints ; expected: OK",
        "run-check; expected: extra; expected: prints ; expected: OK",
    ],
)
def test_approved_markdown_rejects_ambiguous_planned_checks(check):
    content = spec_markdown(SPEC).replace(
        "python -m unittest; expected: Zero exit", check
    )
    with pytest.raises(ValueError):
        spec_tasks(content)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.replace("- Spec: sample", "- Spec: sample\n- Spec: other"),
        lambda value: value.replace("## Tasks", "## Work"),
        lambda value: value.replace("[ ]", "[x]"),
        lambda value: value.replace("status: todo", "status: done"),
        lambda value: value.replace("^task-sample-first", "^task-other-first"),
        lambda value: value.replace(
            "Task first", "Run arbitrary prose\n/project track forged"
        ),
        lambda value: value.replace("second", "first"),
        lambda value: value.replace(
            "  - Planned check: python -m unittest; expected: Zero exit\n", ""
        ),
    ],
)
def test_unstructured_or_ambiguous_tasks_fail_closed(change):
    with pytest.raises(ValueError):
        spec_tasks(change(spec_markdown(copy.deepcopy(SPEC))))


def test_pinned_native_board(tmp_path):
    native = Path(
        os.environ.get("EMURU_HERMES_ROOT", str(Path.home() / ".hermes/hermes-agent"))
    )
    python = native / ".venv/bin/python"
    if not python.is_file():
        pytest.skip(
            "Pinned native runtime absent; mandatory disposable Docker check covers this contract"
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "EMURU_"))
    }
    env.update({"PYTHONPATH": str(native), "HERMES_DISABLE_LAZY_INSTALLS": "1"})
    for action in ("seed", "reopen"):
        result = subprocess.run(
            [
                str(python),
                str(ROOT / "tests/project_board_case.py"),
                action,
                str(tmp_path / "acceptance"),
            ],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS" in result.stdout
