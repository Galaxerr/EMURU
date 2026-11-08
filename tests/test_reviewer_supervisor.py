"""Review admission preserves the existing native/reviewer trust boundaries."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import json

import pytest
from test_supervisor import ROOT, durable, enrollment  # noqa: F401

from emuru.projects import coder_board, supervisor, supervisor_coder
from emuru.projects.coder_native import validate_request
from emuru.projects.scout import digest


def test_supervised_reviewer_uses_host_admission_and_original_deadline():
    validate_request(
        {
            "role": "reviewer",
            "prompt": "Review checked source.",
            "max_iterations": 4,
            "deadline_seconds": 300,
            "supervised": True,
            "deadline_ns": 123000000000,
        }
    )


def test_reviewer_reservation_is_distinct_and_never_refunded(enrollment):
    from emuru.projects import supervisor_reviewer

    home, policy, grant, card = enrollment
    grant.update(
        schema=2,
        review=__import__(
            "emuru.projects.reviewer", fromlist=["authority"]
        ).authority(),
    )
    policy.write_text(json.dumps(grant))
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    coder = supervisor_coder.reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="handoff"):
        supervisor_reviewer.reserve(
            home, home, ROOT, grant, coder, "sha256:" + "a" * 64
        )
    assert supervisor_reviewer.allowance() == {
        "coder_runs": 0,
        "reviewer_runs": 1,
        "scout_runs": 0,
        "active_seconds": 300,
        "coder_iterations": 0,
        "reviewer_iterations": 4,
        "checks": 0,
        "output_bytes": 65536,
        "artifact_bytes": 0,
    }
    before = durable(home)
    with pytest.raises(ValueError, match="handoff"):
        supervisor_reviewer.reserve(
            home, home, ROOT, grant, coder, "sha256:" + "a" * 64
        )
    assert durable(home) == before


@pytest.fixture
def checked(enrollment):
    from emuru.projects import reviewer

    home, policy, grant, card = enrollment
    grant.update(schema=2, review=reviewer.authority())
    policy.write_text(json.dumps(grant))
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    supervisor_coder.reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    stage = supervisor_coder.CoderStage(home, home, ROOT, grant)
    stage.event("claim-started")
    stage.event("claimed", native_task=card.id, run=1)
    stage.event(
        "completion-started", native_task=card.id, run=1, evidence_sha256="b" * 64
    )
    stage.event("awaiting-review", native_task=card.id, run=1, evidence_sha256="b" * 64)
    return stage, card


def test_review_charges_once_and_counts_models_separately(checked, monkeypatch):
    from emuru.projects import reviewer, supervisor_reviewer
    from emuru.projects.supervisor_scout import reserved, unresolved

    coder, card = checked
    admission = supervisor_reviewer.reserve(
        coder.home, coder.vault, ROOT, coder.grant, coder.stage, "sha256:" + "a" * 64
    )
    review = supervisor_reviewer.ReviewStage(coder.home, coder.vault, ROOT, coder.grant)
    assert review.deadline == 301
    assert admission["started_ns"] == coder.stage["started_ns"]
    assert reserved(review.records(), coder.grant["project"])["active_seconds"] == 900
    assert unresolved(review.records())
    review.event("claim-started")
    review.event("claimed", native_task=card.id, run=2)
    monkeypatch.setattr(reviewer, "_prepared", lambda *a, **kw: None)
    for _ in range(4):
        review.admit("model")
    with pytest.raises(ValueError, match="counter invalid or exhausted"):
        review.admit("model")
    for kind in ("tool", "check"):
        with pytest.raises(ValueError, match="forbidden"):
            review.admit(kind)
    before = durable(coder.home)
    with pytest.raises(ValueError, match="already attempted"):
        supervisor_reviewer.reserve(
            coder.home,
            coder.vault,
            ROOT,
            coder.grant,
            coder.stage,
            "sha256:" + "a" * 64,
        )
    assert durable(coder.home) == before
    review.event(
        "completion-started",
        native_task=card.id,
        run=2,
        evidence_sha256="b" * 64,
        receipt_sha256="c" * 64,
    )
    review.event("verified", native_task=card.id, run=2, evidence_sha256="b" * 64)
    assert not unresolved(review.records())
    assert reserved(review.records(), coder.grant["project"])["reviewer_runs"] == 1


@pytest.mark.parametrize("restriction", ["hold", "cancel", "expiry", "boot"])
def test_restriction_committed_before_review_reservation_blocks_charge(
    checked, monkeypatch, restriction
):
    from emuru.projects import supervisor_reviewer

    coder, card = checked
    if restriction in {"hold", "cancel"}:
        with supervisor._locked(coder.home, write=True) as (parent, size, marker):
            records, size = supervisor._records(parent, size, marker)
            supervisor._append(
                coder.home / "emuru-projects/supervisor",
                records,
                size,
                {
                    "kind": "control",
                    "project": coder.grant["project"],
                    "task": card.id,
                    "enrollment": coder.stage["enrollment"],
                    "action": restriction,
                    "request": "a" * 32,
                    "text_hash": "c" * 64,
                    "response": restriction,
                    "token": "d" * 64,
                    "grant_sha256": digest(coder.grant),
                },
                emergency=True,
            )
    elif restriction == "expiry":
        monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: 86401000000000)
    else:
        monkeypatch.setattr(supervisor, "boot_id", lambda: "rebooted")
    before = durable(coder.home)
    with pytest.raises(ValueError, match="unrestricted"):
        supervisor_reviewer.reserve(
            coder.home,
            coder.vault,
            ROOT,
            coder.grant,
            coder.stage,
            "sha256:" + "a" * 64,
        )
    assert durable(coder.home) == before


def test_review_admitted_before_hold_drains_but_cancel_stops(checked):
    from emuru.projects import supervisor_reviewer

    coder, card = checked
    supervisor_reviewer.reserve(
        coder.home, coder.vault, ROOT, coder.grant, coder.stage, "sha256:" + "a" * 64
    )
    review = supervisor_reviewer.ReviewStage(coder.home, coder.vault, ROOT, coder.grant)
    original_deadline = review.deadline
    for index, action in enumerate(("hold", "cancel")):
        with supervisor._locked(coder.home, write=True) as (parent, size, marker):
            records, size = supervisor._records(parent, size, marker)
            supervisor._append(
                coder.home / "emuru-projects/supervisor",
                records,
                size,
                {
                    "kind": "control",
                    "project": coder.grant["project"],
                    "task": card.id,
                    "enrollment": coder.stage["enrollment"],
                    "action": action,
                    "request": str(index) * 32,
                    "text_hash": "c" * 64,
                    "response": action,
                    "token": "d" * 64,
                    "grant_sha256": digest(coder.grant),
                },
                emergency=True,
            )
        if action == "hold":
            review.check()
            assert review.deadline == original_deadline
        else:
            with pytest.raises(InterruptedError, match="stop requested"):
                review.check()
            assert any(r.get("phase") == "stop-observed" for r in review.records())


def test_held_intent_binds_complete_checked_packet_and_cannot_reset_deadline(
    checked, monkeypatch
):
    from emuru.projects import reviewer, supervisor_reviewer

    coder, card = checked
    folder = coder.home / "attempt"
    folder.mkdir(mode=0o700)
    monkeypatch.setattr(
        reviewer, "packet", lambda *a: ({"checked": True}, {}, "b" * 64)
    )
    monkeypatch.setattr(reviewer, "_coder_usage", lambda *a: None)
    monkeypatch.setattr(coder_board, "launcher_stopped", lambda *_: True)
    monkeypatch.setattr(
        reviewer,
        "_read",
        lambda *_: {
            "schema": 1,
            "coder_admission": coder.stage["admission"],
            "coder_run": 1,
            "grant_sha256": digest(coder.grant),
            "evidence_sha256": "b" * 64,
            "packet_sha256": "0" * 64,
        },
    )
    before = durable(coder.home)
    with pytest.raises(ValueError, match="intent changed"):
        supervisor_reviewer.handoff(
            coder, folder, {"task": card.id, "run": 1}, first_review=True
        )
    assert durable(coder.home) == before


def test_changed_review_packet_never_reports_safe_running(checked, monkeypatch):
    from emuru.projects import coder_sandbox, reviewer, supervisor_reviewer

    coder, card = checked
    supervisor_reviewer.reserve(
        coder.home, coder.vault, ROOT, coder.grant, coder.stage, "sha256:" + "a" * 64
    )
    review = supervisor_reviewer.ReviewStage(coder.home, coder.vault, ROOT, coder.grant)
    review.event("claim-started")
    review.event("claimed", native_task=card.id, run=2)
    name = "emuru-coder-review-abcdefgh"
    review.event("planned", name=name, service="inference", image="sha256:" + "a" * 64)
    review.event("created", name=name, container_id="f" * 64)
    card.status, card.current_run_id, card.claim_lock = "running", 2, reviewer.REVIEWER
    monkeypatch.setattr(reviewer, "status", lambda *a: {"status": "uncertain"})
    monkeypatch.setattr(coder_board, "launcher_stopped", lambda *_: False)
    monkeypatch.setattr(
        coder_sandbox,
        "bounded_process",
        lambda *a, **kw: pytest.fail("Unverified packet reached process inspection"),
    )
    result = supervisor_reviewer.native_status(review, coder.home, card, None, None)
    assert result["status"] == "uncertain"
