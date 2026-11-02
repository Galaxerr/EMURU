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


def test_launcher_binding_distinguishes_live_and_dead_process(tmp_path):
    import subprocess
    import sys

    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    coder_board.record_launcher(folder)
    assert not coder_board._launcher_stopped(folder)
    (folder / "launcher.json").unlink()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import time; from pathlib import Path; "
                "from emuru.projects.coder_board import record_launcher; "
                "record_launcher(Path(__import__('sys').argv[1])); time.sleep(30)"
            ),
            str(folder),
        ]
    )
    try:
        import time

        deadline = time.monotonic() + 5
        while not (folder / "launcher.json").exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        assert not coder_board._launcher_stopped(folder)
        child.kill()
        child.wait(timeout=5)
        assert coder_board._launcher_stopped(folder)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_reconciliation_refuses_live_launcher_before_native_mutation(
    tmp_path, monkeypatch
):
    _, grant = qualification_fixture(tmp_path / "fixture")
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    coder_board._write_new(folder, "grant.json", json.dumps(grant).encode())
    coder_board.record_launcher(folder)
    monkeypatch.setattr(coder_board, "_find_attempt", lambda *_: folder)
    monkeypatch.setattr(
        coder_board, "authorize", lambda *_a, **_k: pytest.fail("mutation")
    )
    with pytest.raises(ValueError, match="launcher may still execute"):
        coder_board.reconcile(tmp_path, tmp_path, tmp_path, grant["task"], grant)


def test_uncertain_process_can_record_verified_stop(tmp_path):
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    name = "emuru-coder-check-12345678"
    for phase in ("starting", "uncertain", "stopped"):
        coder_board.process_event(folder, name, "check", phase)
    assert coder_board._processes(folder)[-1]["phase"] == "stopped"


@pytest.mark.parametrize(
    "observation",
    [
        {"exit_code": 0, "stdout": "live-container\n"},
        {"exit_code": 1, "stdout": ""},
    ],
)
def test_reconciliation_refuses_surviving_or_unprovable_container(
    tmp_path, monkeypatch, observation
):
    _, grant = qualification_fixture(tmp_path / "fixture")
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    coder_board._write_new(folder, "grant.json", json.dumps(grant).encode())
    coder_board._write_new(
        folder, "run.json", json.dumps({"task": grant["task"], "run": 1}).encode()
    )
    coder_board.process_event(folder, "emuru-coder-check-12345678", "check", "starting")
    monkeypatch.setattr(coder_board, "_find_attempt", lambda *_: folder)
    monkeypatch.setattr(coder_board, "_launcher_stopped", lambda *_: True)
    monkeypatch.setattr(coder_board, "authorize", lambda *_a, **_k: None)
    monkeypatch.setattr(coder_board, "status", lambda *_: {"status": "uncertain"})
    monkeypatch.setattr(coder_board, "bounded_process", lambda *_: observation)
    monkeypatch.setattr(
        coder_board, "finish", lambda *_: pytest.fail("must preserve claim")
    )

    @contextmanager
    def readonly(*_args):
        yield SimpleNamespace(get_task=lambda *_: object()), object()

    monkeypatch.setattr(coder_board.board, "project_board_readonly", readonly)
    with pytest.raises(ValueError, match="termination is not verified"):
        coder_board.reconcile(tmp_path, tmp_path, tmp_path, grant["task"], grant)
    assert coder_board._processes(folder)[-1]["phase"] == "starting"


def test_cleanup_removes_only_recorded_resources_and_preserves_evidence(
    tmp_path, monkeypatch
):
    _, grant = qualification_fixture(tmp_path / "fixture")
    folder = tmp_path / "attempt"
    folder.mkdir(mode=0o700)
    coder_board._write_new(folder, "grant.json", json.dumps(grant).encode())
    coder_board._write_new(
        folder, "intent.json", json.dumps({"grant_sha256": digest(grant)}).encode()
    )
    coder_board._write_new(folder, "evidence.json", b"retained")
    gateway = tmp_path / ".runtime/emuru-coder-gateway-example/gateway"
    gateway.mkdir(parents=True, mode=0o700)
    gateway.parent.chmod(0o700)
    for name in ("provider.key", "gateway.key"):
        (gateway / name).write_bytes(b"private secret")
        (gateway / name).chmod(0o600)
    coder_board._write_new(
        folder, "gateway.json", json.dumps({"folder": str(gateway)}).encode()
    )
    name = "emuru-coder-gateway-0123456789abcdef"
    coder_board.process_event(folder, name, "gateway", "starting")
    monkeypatch.setattr(coder_board, "_find_attempt", lambda *_: folder)
    monkeypatch.setattr(coder_board, "_launcher_stopped", lambda *_: True)
    resources = {
        name,
        "emuru-coder-model-0123456789abcdef",
        "emuru-coder-egress-0123456789abcdef",
    }
    commands = []

    def observed(command, *_):
        commands.append(command)
        if "--filter" in command:
            identity = command[-1].split("=^", 1)[1].strip("/$")
            return {"exit_code": 0, "stdout": identity if identity in resources else ""}
        resources.remove(command[-1])
        return {"exit_code": 0, "stdout": ""}

    monkeypatch.setattr(coder_board, "bounded_process", observed)
    assert (
        coder_board.cleanup(tmp_path, tmp_path, grant["task"], grant)["status"]
        == "uncertain"
    )
    assert resources == set()
    assert list(gateway.iterdir()) == []
    assert (folder / "evidence.json").read_bytes() == b"retained"
    assert coder_board._processes(folder)[-1]["phase"] == "stopped"
    assert not any("run" in command or "start" in command for command in commands)


def test_gateway_secret_cleanup_refuses_symlink_and_outside_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    secret = outside / "provider.key"
    secret.write_bytes(b"retain")
    secret.chmod(0o600)
    parent = tmp_path / ".runtime/emuru-coder-gateway-test"
    parent.mkdir(parents=True, mode=0o700)
    (parent / "gateway").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        coder_board._cleanup_gateway_secrets(
            tmp_path, {"folder": str(parent / "gateway")}
        )
    with pytest.raises(ValueError, match="path is invalid"):
        coder_board._cleanup_gateway_secrets(tmp_path, {"folder": str(outside)})
    assert secret.read_bytes() == b"retain"
