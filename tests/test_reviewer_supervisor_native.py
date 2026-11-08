"""Actual native/Docker review and rework boundaries, with synthetic SDK replies."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import json
import multiprocessing
import os
import runpy
import time

import pytest
from test_coder_supervisor_native import (
    IMAGE,
    ROOT,
    facts,
    files,
    launch_child,
    native_fixture,  # noqa: F401
    retain,
)

from emuru.projects import coder_board, supervisor

pytestmark = pytest.mark.skipif(
    "EMURU_CODER_TEST_IMAGE" not in os.environ,
    reason="Matching-image review matrix supplies Docker/native qualification",
)


def invoke(fixture, window="review", *, command=None):
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(
        target=launch_child, args=(fixture, window, child)
    )
    process.start()
    return process, parent, child


def join(fixture, process):
    process.join(90)
    assert not process.is_alive()
    assert process.exitcode == 0, (fixture["root"] / "child-result.json").read_text()
    return json.loads((fixture["root"] / "child-result.json").read_text())


def control(fixture, action):
    detail, _, _ = facts(fixture)
    return fixture["project_command"](
        f"/project {action} {fixture['proposal']} {fixture['task_id']} {detail['token']}"
    )


def stop(process, parent, child):
    if process.is_alive():
        process.kill()
        process.join(10)
    parent.close()
    child.close()


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
def test_uninterrupted_review_and_restart_keep_exact_pair(native_fixture):
    fixture = native_fixture
    before_repo, before_vault = files(fixture["repo"]), files(fixture["vault"])
    process, parent, child = invoke(fixture)
    try:
        result = join(fixture, process)
        assert result["status"] == "verified"
        detail, card, count = facts(fixture)
        assert count == 2 and card.status == "done"
        assert detail["reserved"]["active_seconds"] == 900
        assert detail["reserved"]["reviewer_runs"] == 1
        launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
        before = files(fixture["home"])
        assert (
            launch(fixture["home"], fixture["vault"], fixture["policy"], card.id, ROOT)[
                "status"
            ]
            == "verified"
        )
        assert files(fixture["home"]) == before
        assert (
            files(fixture["repo"]) == before_repo
            and files(fixture["vault"]) == before_vault
        )
        retain(fixture, "review-uninterrupted", receipt=result)
    finally:
        stop(process, parent, child)


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
def test_held_first_review_requires_intent_continue_and_explicit_operator(
    native_fixture,
):
    fixture = native_fixture
    process, parent, child = invoke(fixture, "handoff")
    try:
        assert parent.poll(90) and parent.recv() == "handoff"
        assert "held" in control(fixture, "hold")
        parent.send("proceed")
        assert join(fixture, process)["status"] == "awaiting_review"
        assert facts(fixture)[2] == 1
        folder = coder_board.attempt_path(fixture["home"], fixture["grant"])
        intent = folder / "held-before-review.json"
        assert intent.exists() and not (folder / "review").exists()
        assert "ready" in control(fixture, "continue")
        assert facts(fixture)[2] == 1
        raw = intent.read_bytes()
        intent.unlink()
        launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
        before = files(fixture["home"])
        with pytest.raises(FileNotFoundError):
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                review=True,
            )
        assert files(fixture["home"]) == before
        intent.write_bytes(raw)
        intent.chmod(0o600)
        fixture["first_review"] = True
        next_process, next_parent, next_child = invoke(fixture)
        try:
            assert join(fixture, next_process)["status"] == "verified"
            assert facts(fixture)[2] == 2
        finally:
            stop(next_process, next_parent, next_child)
        with pytest.raises(ValueError, match="attempted"):
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                review=True,
            )
        retain(fixture, "review-held-first", held_intent=json.loads(raw))
    finally:
        stop(process, parent, child)


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
@pytest.mark.parametrize(
    "window",
    [
        "review-request",
        "review-claim",
        "verdict",
        "review-ack",
        "kill-review",
        "cancel-review",
    ],
)
def test_review_crash_cancel_never_replays_and_recovers_only_complete_proof(
    native_fixture, window
):
    fixture = native_fixture
    process, parent, child = invoke(fixture, window)
    launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
    try:
        requested = None
        if window in {"kill-review", "cancel-review"}:
            limit = time.monotonic() + 90
            while time.monotonic() < limit:
                assert process.is_alive()
                with supervisor._locked(fixture["home"]) as (directory, size, marker):
                    records, _ = supervisor._records(directory, size, marker)
                if any(
                    r["kind"] == "reviewer-stage" and r.get("phase") == "model-admitted"
                    for r in records
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Reviewer model wait not reached")
            if window == "cancel-review":
                detail, _, _ = facts(fixture)
                assert detail["adapter"] == "reviewer"
                assert detail["actual"]["model_calls"] == 1
                original_deadline = detail["stage_deadline_ns"]
                assert "held" in control(fixture, "hold")
                assert "ready" in control(fixture, "continue")
                assert facts(fixture)[0]["stage_deadline_ns"] == original_deadline
                requested = time.monotonic_ns()
                assert "stop_requested" in control(fixture, "cancel")
                process.join(10)
                assert not process.is_alive()
        else:
            assert parent.poll(90) and parent.recv() == window
        if process.is_alive():
            process.kill()
        process.join(10)
        before = files(fixture["home"])
        prior = launch(
            fixture["home"],
            fixture["vault"],
            fixture["policy"],
            fixture["task_id"],
            ROOT,
        )
        assert prior["status"] == (
            "verified" if window == "review-ack" else "uncertain"
        )
        assert files(fixture["home"]) == before
        if window == "review-request":
            with pytest.raises(ValueError, match="claim outcome unproven"):
                launch(
                    fixture["home"],
                    fixture["vault"],
                    fixture["policy"],
                    fixture["task_id"],
                    ROOT,
                    reconcile=True,
                )
            assert facts(fixture)[2] == 1
        else:
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                cleanup=True,
            )
            result = launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                reconcile=True,
            )
            assert result["status"] == (
                "verified" if window in {"verdict", "review-ack"} else "uncertain"
            )
            assert facts(fixture)[2] == 2
        if requested:
            with supervisor._locked(fixture["home"]) as (directory, size, marker):
                records, _ = supervisor._records(directory, size, marker)
            observed = next(
                r["observed_ns"]
                for r in records
                if r["kind"] == "reviewer-stage" and r.get("phase") == "stop-observed"
            )
            assert observed - requested < 10**9
        retain(fixture, "review-" + window, restart=prior)
    finally:
        stop(process, parent, child)


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
@pytest.mark.parametrize(
    "ending",
    [
        "approve",
        "changes_requested",
        "blocked",
        "claim-kill",
        "hold",
        "cancel",
        "expiry",
    ],
)
def test_fresh_rework_keeps_one_loop_and_second_review(
    native_fixture, monkeypatch, ending
):
    fixture = native_fixture
    fixture["coder_source"] = (
        "def sum_prices(prices):\n    if len(prices)>3:\n        return 0\n    return sum(prices)\n"
    )
    fixture["review_verdict"] = {
        "verdict": "changes_requested",
        "summary": "Four-input defect.",
        "blockers": [
            {
                "path": "sum_prices.py",
                "line": 2,
                "reason": "Drops larger inputs.",
                "requested_change": "Sum every input.",
            }
        ],
        "suggestions": [],
    }
    process, parent, child = invoke(fixture)
    try:
        assert join(fixture, process)["status"] == "changes_requested"
    finally:
        stop(process, parent, child)
    original = fixture["grant"]
    second = runpy.run_path(str(ROOT / "scripts/hermes/rework-check.py"))["new_grant"](
        fixture
    )
    fixture["grant"] = second
    fixture["policy"].write_text(json.dumps(second))
    fixture.pop("coder_source")
    first_verdict = fixture.pop("review_verdict")
    if ending in {"changes_requested", "blocked"}:
        fixture["review_verdict"] = {**first_verdict, "verdict": ending}
    supervisor.enroll(
        fixture["home"],
        fixture["vault"],
        ROOT,
        "coder",
        fixture["policy"],
        fixture["task_id"],
        IMAGE,
    )
    launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
    if ending in {"hold", "cancel", "expiry"}:
        if ending != "expiry":
            response = control(fixture, ending)
            assert ("held" if ending == "hold" else "revoked / unstarted") in response
        before = files(fixture["home"])
        with monkeypatch.context() as clock:
            if ending == "expiry":
                with supervisor._locked(fixture["home"]) as (directory, size, marker):
                    records, _ = supervisor._records(directory, size, marker)
                enrollment = next(
                    r for r in reversed(records) if r["kind"] == "enrollment"
                )
                clock.setattr(
                    supervisor.time, "monotonic_ns", lambda: enrollment["deadline_ns"]
                )
            with pytest.raises(ValueError, match="unrestricted"):
                launch(
                    fixture["home"],
                    fixture["vault"],
                    fixture["policy"],
                    fixture["task_id"],
                    ROOT,
                )
        assert files(fixture["home"]) == before and facts(fixture)[2] == 2
        if ending != "hold":
            retain(fixture, "review-rework-" + ending, original_grant=original)
            return
        assert "ready" in control(fixture, "continue")
        assert facts(fixture)[2] == 2
    process, parent, child = invoke(
        fixture, "rework-claim" if ending == "claim-kill" else "review"
    )
    try:
        if ending == "claim-kill":
            assert parent.poll(90) and parent.recv() == "rework-claim"
            process.kill()
            process.join(10)
            before = files(fixture["home"])
            assert (
                launch(
                    fixture["home"],
                    fixture["vault"],
                    fixture["policy"],
                    fixture["task_id"],
                    ROOT,
                )["status"]
                == "uncertain"
            )
            assert files(fixture["home"]) == before
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                cleanup=True,
            )
            assert (
                launch(
                    fixture["home"],
                    fixture["vault"],
                    fixture["policy"],
                    fixture["task_id"],
                    ROOT,
                    reconcile=True,
                )["status"]
                == "uncertain"
            )
            assert facts(fixture)[2] == 3
            retain(fixture, "review-rework-claim-kill", original_grant=original)
            return
        result = join(fixture, process)
        expected = {"changes_requested": "review_rejected", "blocked": "blocked"}.get(
            ending, "verified"
        )
        assert result["status"] == expected
        detail, card, count = facts(fixture)
        assert (
            card.status == ("done" if expected == "verified" else "blocked")
            and count == 4
        )
        assert detail["reserved"]["active_seconds"] == 1800
        assert (
            detail["reserved"]["coder_runs"] == detail["reserved"]["reviewer_runs"] == 2
        )
        assert detail["reserved"]["checks"] == 8
        assert detail["reserved"]["output_bytes"] == 640 * 1024
        retain(
            fixture, "review-rework-" + ending, original_grant=original, receipt=result
        )
    finally:
        stop(process, parent, child)


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
def test_hard_security_block_never_authorizes_rework(native_fixture):
    fixture = native_fixture
    fixture["review_verdict"] = {
        "verdict": "blocked",
        "summary": "Synthetic hard security refusal.",
        "blockers": [
            {
                "path": "sum_prices.py",
                "line": 1,
                "reason": "Security boundary refused.",
                "requested_change": "Require owner inspection.",
            }
        ],
        "suggestions": [],
    }
    process, parent, child = invoke(fixture)
    try:
        assert join(fixture, process)["status"] == "blocked"
        second = runpy.run_path(str(ROOT / "scripts/hermes/rework-check.py"))[
            "new_grant"
        ](fixture)
        fixture["policy"].write_text(json.dumps(second))
        before = files(fixture["home"])
        with pytest.raises(ValueError, match="lineage"):
            supervisor.enroll(
                fixture["home"],
                fixture["vault"],
                ROOT,
                "coder",
                fixture["policy"],
                fixture["task_id"],
                IMAGE,
            )
        assert files(fixture["home"]) == before and facts(fixture)[2] == 2
        retain(fixture, "review-hard-security-block")
    finally:
        stop(process, parent, child)
