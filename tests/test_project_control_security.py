"""Control restrictions and receipt history at the admitted project boundary."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

from test_project_controls import (  # noqa: F401 -- shared public-boundary fixtures
    controls,
    enrollment,
)
from test_supervisor import ROOT, durable

from emuru.projects import supervisor
from emuru.projects.supervisor_controls import outcome


def test_new_request_with_old_token_cannot_repeat_a_transition(controls):
    home, grant, card, _queue, deliver = controls
    original = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", original["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("cancel", original["token"], 2)
    assert "Stale control token" in response
    assert supervisor.status(home, ROOT, grant["project"], card) == held


def test_current_token_same_state_hold_preserves_generation_and_budget(controls):
    home, grant, card, _queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", initial["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("hold", held["token"], 2)
    assert "held" in response
    assert supervisor.status(home, ROOT, grant["project"], card) == held


def test_continue_cannot_reopen_cancelled_enrollment(controls):
    home, grant, card, _queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    deliver("cancel", initial["token"], 1)
    revoked = supervisor.status(home, ROOT, grant["project"], card)
    assert revoked["control"] == "revoked"
    response, _, _ = deliver("continue", revoked["token"], 2)
    assert "Continue requires" in response
    assert supervisor.status(home, ROOT, grant["project"], card) == revoked


def test_continue_cannot_renew_expired_held_authority(controls, monkeypatch):
    home, grant, card, _queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", initial["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: held["deadline_ns"])
    expired = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("continue", expired["token"], 2)
    assert "Continue requires" in response
    assert supervisor.status(home, ROOT, grant["project"], card) == expired


def test_original_outcome_survives_later_generation_without_reapplying(controls):
    home, grant, card, _queue, deliver = controls
    initial = supervisor.status(home, ROOT, grant["project"], card)
    response, _, _ = deliver("hold", initial["token"], 1)
    held = supervisor.status(home, ROOT, grant["project"], card)
    deliver("continue", held["token"], 2)
    before = durable(home)
    assert outcome(home, home, grant["owner"], grant["bot"], 1) == response
    assert durable(home) == before
    assert (
        supervisor.status(home, ROOT, grant["project"], card)["control"]
        == "unrestricted"
    )
