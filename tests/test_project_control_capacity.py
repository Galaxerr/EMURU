"""Full-size metadata admission through enrollment and authenticated controls."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_project_controls import controls, enrollment  # noqa: F401 -- public fixtures
from test_supervisor import ROOT

from emuru.projects import board, supervisor
from emuru.projects.supervisor_controls import owner_control
from emuru.vault import mutations


def test_full_one_mib_metadata_keeps_real_hold_cancel_capacity_for_two_bindings(
    controls, monkeypatch
):
    home, grant, first, queue, deliver = controls
    second = SimpleNamespace(**vars(first))
    second.id = "t_cabba"
    second_grant = {**grant, "task": second.id, "grant_id": "e" * 32}
    policy = home.parent / "second-grant.json"
    policy.write_text(json.dumps(second_grant))
    policy.chmod(0o600)

    class Native:
        @staticmethod
        def get_task(_db, task):
            return {first.id: first, second.id: second}.get(task)

    @contextmanager
    def readonly(*_):
        yield Native(), None

    monkeypatch.setattr(board, "project_board_readonly", readonly)
    verified_identity = supervisor.identity(ROOT, "sha256:" + "a" * 64)
    monkeypatch.setattr(supervisor, "identity", lambda root, image: verified_identity)
    supervisor.enroll(
        home, home, ROOT, "coder", policy, second.id, "sha256:" + "a" * 64
    )
    initial_zero = (home / "emuru-projects/supervisor/00000001.json").read_bytes()
    registrations = 0
    for _ in range(1500):
        try:
            supervisor.enroll(
                home, home, ROOT, "coder", policy, second.id, "sha256:" + "a" * 64
            )
        except ValueError as error:
            assert "metadata admission exhausted" in str(error)
            break
        registrations += 1
    else:
        raise AssertionError("1 MiB metadata cap did not halt ordinary enrollment")
    assert registrations > 100
    folder = home / "emuru-projects/supervisor"
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    accounted = 4096 + sum(map(len, before.values()))
    assert accounted <= 1024 * 1024 - 4096
    initial = supervisor.status(home, ROOT, grant["project"], first)
    assert "held" in deliver("hold", initial["token"], 1)[0]

    def second_control(action, uid):
        current = supervisor.status(home, ROOT, grant["project"], second)
        text = f"/project {action} {grant['proposal']} {second.id} {current['token']}"
        queue.stage(
            7,
            [
                {
                    "update_id": uid,
                    "message": {
                        "message_id": uid,
                        "date": 100000,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        row = queue.claim(7, "emuru")
        request, _ = mutations.begin_request(home, home, 42, 7, uid, text, fresh=True)
        response = owner_control(home, home, ROOT, text, 42, 7, request, uid)
        queue.finish(row["sequence"], True)
        return response

    assert "held" in second_control("hold", 2)
    held = supervisor.status(home, ROOT, grant["project"], first)
    generation = held["generation"]
    with pytest.raises(OSError, match="durably retained"):
        deliver("continue", held["token"], 3)
    failed = queue.db.execute(
        "SELECT sequence FROM updates WHERE bot_id=7 AND update_id=3"
    ).fetchone()
    queue.finish(failed[0], False, "queue_database_error")
    assert (
        supervisor.status(home, ROOT, grant["project"], first)["generation"]
        == generation
    )
    assert "revoked" in deliver("cancel", held["token"], 4)[0]
    assert "revoked" in second_control("cancel", 5)
    after = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert all(after[name] == data for name, data in before.items())
    actual_emergency_bytes = sum(
        len(data) for name, data in after.items() if name not in before
    )
    assert actual_emergency_bytes + 512 <= 4096
    assert 4096 + sum(map(len, after.values())) <= 1024 * 1024
    assert (folder / "00000001.json").read_bytes() == initial_zero
    assert (
        supervisor.status(home, ROOT, grant["project"], first)["reserved"]
        == initial["reserved"]
    )
