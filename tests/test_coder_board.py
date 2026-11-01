import hashlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from emuru.projects import coder_board
from emuru.projects.coder_runtime import qualification_fixture
from emuru.projects.scout import digest


def test_authorize_reopens_approval_task_and_native_binding(tmp_path, monkeypatch):
    _, grant = qualification_fixture(tmp_path / "fixture")
    task = {
        "id": "first",
        "description": "Task first",
        "acceptance": [
            {"command": "python -m unittest", "expected_result": "Zero exit"}
        ],
    }
    grant.update(
        task="t_123abc",
        task_digest=digest(task),
        owner=42,
        bot=7,
        proposal="c" * 32,
        approval="d" * 64,
        revision="e" * 64,
        vault=digest(os.path.abspath(tmp_path / "vault")),
    )
    preview = {
        "current": True,
        "path": "Projects/sample.md",
        "after_revision": grant["revision"],
        "approval_digest": grant["approval"],
        "content": "approved",
    }
    project = coder_board.board._digest(
        ["42", "7", os.path.abspath(tmp_path / "vault"), preview["path"]]
    )
    grant["project"] = project
    card = SimpleNamespace(
        body=json.dumps(
            {
                "schema": 1,
                "revision": grant["revision"],
                "proposal": grant["proposal"],
                "approval": grant["approval"],
                "task": task,
            }
        ),
        tenant=project,
        created_by="emuru-owner",
        assignee=None,
        status="blocked",
    )

    class Board:
        @staticmethod
        def get_task(_db, _task_id):
            return card

    @contextmanager
    def project_board(*_args):
        yield Board(), object()

    monkeypatch.setattr(coder_board.mutations, "approved_project", lambda *_: preview)
    monkeypatch.setattr(coder_board.board, "spec_tasks", lambda _content: [task])
    monkeypatch.setattr(coder_board.board, "project_board", project_board)
    monkeypatch.setattr(coder_board, "catalogue_digest", lambda: grant["catalogue"])
    monkeypatch.setattr(
        coder_board, "git", lambda *_: (grant["baseline"] + "\n").encode()
    )

    assert (
        coder_board.authorize(
            tmp_path / "home", tmp_path / "vault", tmp_path, "t_123abc", grant
        )
        == grant
    )

    preview["current"] = False
    with pytest.raises(ValueError, match="stale or mismatched"):
        coder_board.authorize(
            tmp_path / "home", tmp_path / "vault", tmp_path, "t_123abc", grant
        )


def test_authorize_rejects_task_id_mismatch_before_receipt_lookup(
    tmp_path, monkeypatch
):
    _, grant = qualification_fixture(tmp_path / "fixture")
    monkeypatch.setattr(
        coder_board.mutations,
        "approved_project",
        lambda *_: pytest.fail("approval lookup must not follow task mismatch"),
    )
    with pytest.raises(ValueError, match="does not match"):
        coder_board.authorize(
            tmp_path / "home", tmp_path / "vault", tmp_path, "t_999", grant
        )


def test_process_ledger_requires_one_start_and_verified_stop(tmp_path):
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    name = "emuru-coder-check-12345678"
    coder_board.process_event(folder, name, "check", "starting")
    coder_board.process_event(folder, name, "check", "stopped")
    assert [event["phase"] for event in coder_board._processes(folder)] == [
        "starting",
        "stopped",
    ]
    coder_board.process_event(folder, name, "check", "starting")
    with pytest.raises(ValueError, match="ledger is invalid"):
        coder_board._processes(folder)


def test_attachment_verification_reopens_exact_retained_bytes(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    attachments = []
    expected = {}
    for name, content in (("evidence.json", b"{}"), ("diff.patch", b"diff\n")):
        path = folder / name
        path.write_bytes(content)
        path.chmod(0o600)
        expected[name] = hashlib.sha256(content).hexdigest()
        attachments.append(
            SimpleNamespace(
                filename=name,
                stored_path=str(path),
                size=len(content),
                uploaded_by="emuru-coder-runner",
            )
        )

    class Board:
        @staticmethod
        def list_attachments(_db, _task_id):
            return attachments

    verified = coder_board._verify_artifact_rows(
        Board(), object(), "t_123abc", grant, folder, expected
    )
    assert set(verified) == {"evidence.json", "diff.patch"}
    (folder / "diff.patch").write_bytes(b"evil\n")
    with pytest.raises(ValueError, match="content digest"):
        coder_board._verify_artifact_rows(
            Board(), object(), "t_123abc", grant, folder, expected
        )
