"""Authenticated controls at the existing queue/project boundary; no worker replay."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import pytest
from test_supervisor import (  # noqa: F401 -- shared public enrollment fixture
    ROOT,
    durable,
    enrollment,
)

from emuru.projects import supervisor, supervisor_lifecycle
from emuru.projects.supervisor_controls import outcome, owner_control
from emuru.telegram.queue import TelegramQueue, load_telegram_settings
from emuru.vault import mutations


@pytest.fixture
def controls(enrollment, monkeypatch):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    monkeypatch.setattr(
        mutations,
        "approved_project",
        lambda *a, **kw: {
            "current": True,
            "after_revision": grant["revision"],
            "proposal_id": grant["proposal"],
        },
    )
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        grant["owner"],
        clock=lambda: 100000,
    )

    def deliver(action, token, uid):
        text = f"/project {action} {grant['proposal']} {card.id} {token}"
        queue.stage(
            grant["bot"],
            [
                {
                    "update_id": uid,
                    "message": {
                        "message_id": uid,
                        "date": 100000,
                        "chat": {"id": grant["owner"], "type": "private"},
                        "from": {"id": grant["owner"], "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        row = queue.claim(grant["bot"], "emuru")
        assert row["update_id"] == uid
        request, _ = mutations.begin_request(
            home, home, grant["owner"], grant["bot"], uid, text, fresh=True
        )
        result = owner_control(
            home, home, ROOT, text, grant["owner"], grant["bot"], request, uid
        )
        queue.finish(row["sequence"], True)
        return result, request, text

    yield home, grant, card, queue, deliver
    queue.close()


def test_hold_then_continue_only_changes_control_and_token(controls):
    home, grant, card, _queue, deliver = controls
    first = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("hold", first["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    assert "held" in response and held["control"] == "held"
    assert held["token"] != first["token"]
    response, _, _ = deliver("continue", held["token"], 2)
    ready = supervisor.status(home, ROOT, grant["project"], card)
    assert "awaiting operator launch" in response
    assert ready["control"] == "unrestricted" and ready["execution"] == "unstarted"
    assert ready["reserved"] == first["reserved"]


def test_continue_preserves_hold_while_sleep_fenced(controls):
    home, grant, card, _queue, deliver = controls
    first = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", first["token"], 1)
    supervisor_lifecycle.fence(home)
    held = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("continue", held["token"], 2)
    after = supervisor.status(home, ROOT, grant["project"], card)
    assert "sleep fence" in response
    assert after["control"] == "held" and after["sleep_fence"]
    assert after["reserved"] == held["reserved"]
    assert after["deadline_ns"] == held["deadline_ns"]
    assert after["execution"] == "unstarted"
    assert deliver("continue", held["token"], 3)[0] == response


def test_exact_binding_can_be_restricted_after_expiry_and_note_edit_without_renewal(
    controls, monkeypatch
):
    home, grant, card, _queue, deliver = controls
    before = supervisor.status(home, ROOT, grant["project"], card)
    monkeypatch.setattr(
        supervisor.time, "monotonic_ns", lambda: before["deadline_ns"] + 1
    )
    monkeypatch.setattr(
        mutations,
        "approved_project",
        lambda *a, **kw: {
            "current": False,
            "after_revision": grant["revision"],
            "proposal_id": grant["proposal"],
        },
    )
    assert "held" in deliver("hold", before["token"], 1)[0]
    held = supervisor.status(home, ROOT, grant["project"], card)
    assert (
        held["authority"] == "expired" and held["deadline_ns"] == before["deadline_ns"]
    )
    assert "revoked" in deliver("cancel", held["token"], 2)[0]
    after = supervisor.status(home, ROOT, grant["project"], card)
    assert after["authority"] == "expired" and after["reserved"] == before["reserved"]


def test_cancelled_unstarted_registration_cannot_clear_restriction(controls):
    home, grant, card, _queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    assert "revoked" in deliver("cancel", initial["token"], 1)[0]
    before = durable(home)
    with pytest.raises(ValueError, match="Restricted enrollment"):
        supervisor.enroll(
            home,
            home,
            ROOT,
            "coder",
            home.parent / "grant.json",
            card.id,
            "sha256:" + "a" * 64,
        )
    assert durable(home) == before


def test_noop_and_refusal_outcomes_survive_transport_pruning_without_replay(
    controls, monkeypatch
):
    home, grant, card, queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", initial["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    noop = deliver("hold", held["token"], 2)[0]
    assert (
        supervisor.status(home, ROOT, grant["project"], card)["token"] == held["token"]
    )
    deliver("continue", held["token"], 3)
    refused = deliver("hold", initial["token"], 4)[0]
    assert "Stale" in refused
    now = 100000 + 8 * 86400
    queue.clock = lambda: now
    queue.stage(
        7,
        [
            {
                "update_id": 5,
                "message": {
                    "message_id": 5,
                    "date": now,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                    "text": "/status",
                },
            }
        ],
        "emuru",
    )
    assert not (queue.path.parent / "project-control-receipts/7-2.json").exists()
    before = durable(home)
    monkeypatch.setattr(
        supervisor,
        "token_binding",
        lambda *_: pytest.fail("Reopening outcome rechecked token or replayed control"),
    )
    assert outcome(home, home, 42, 7, 2) == noop
    assert outcome(home, home, 42, 7, 4) == refused
    assert durable(home) == before
