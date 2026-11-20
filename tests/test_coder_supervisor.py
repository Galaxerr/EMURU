"""Coder supervision at the existing admission/runtime boundaries."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import pytest
from test_project_controls import controls  # noqa: F401
from test_supervisor import ROOT, durable, enrollment  # noqa: F401

from emuru.projects import coder_board, supervisor
from emuru.projects.scout import digest


def test_coder_reserves_full_stage_once_before_claim(enrollment):
    from emuru.projects import supervisor_coder

    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    stage = supervisor_coder.reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    assert stage["deadline_ns"] == 601000000000
    detail = supervisor.status(home, ROOT, grant["project"], card)
    assert detail["reserved"] == {
        "coder_runs": 1,
        "reviewer_runs": 0,
        "scout_runs": 0,
        "active_seconds": 600,
        "coder_iterations": 12,
        "reviewer_iterations": 0,
        "checks": 4,
        "output_bytes": 262144,
        "artifact_bytes": 8388608,
    }
    assert detail["adapter"] == "coder"
    before = durable(home)
    with pytest.raises(ValueError, match="uncertain|attempted"):
        supervisor_coder.reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    assert durable(home) == before
    assert supervisor_coder.load(home, grant)["admission"] == digest(stage)


def test_blocked_check_wait_observes_cancel_within_one_second():
    import sys
    import time

    from emuru.projects.coder_sandbox import bounded_process

    started = time.monotonic()
    with pytest.raises(InterruptedError, match="stop requested"):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            b"",
            20,
            1024,
            stopped=lambda: time.monotonic() - started >= 0.1,
        )
    assert time.monotonic() - started < 1


def test_native_model_call_requires_correlated_host_admission():
    import io
    import json

    from emuru.projects.coder_native import CoderProtocol

    output = io.StringIO()
    protocol = CoderProtocol(
        io.StringIO('{"id":1,"result":{"admitted":true}}\n'), output
    )
    protocol.model()
    assert json.loads(output.getvalue()) == {"type": "model", "id": 1}
    assert protocol.tool_calls == 0
    with pytest.raises(ValueError):
        CoderProtocol(
            io.StringIO('{"id":2,"result":{"admitted":true}}\n'), io.StringIO()
        ).model()


@pytest.fixture
def admitted(controls):
    from emuru.projects.supervisor_coder import CoderStage, reserve

    home, grant, card, _queue, deliver = controls
    reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    return CoderStage(home, home, ROOT, grant), card, deliver


def test_hold_before_coder_admission_prevents_charge(controls):
    from emuru.projects.supervisor_coder import reserve

    home, grant, card, _queue, deliver = controls
    detail = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", detail["token"], 1)
    before = durable(home)
    with pytest.raises(ValueError, match="unrestricted"):
        reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    assert durable(home) == before


def test_hold_drains_admitted_coder_but_cancel_closes_it(admitted):
    stage, card, deliver = admitted
    detail = supervisor.status(stage.home, ROOT, stage.grant["project"], card)
    deliver("hold", detail["token"], 1)
    stage.check()
    held = supervisor.status(stage.home, ROOT, stage.grant["project"], card)
    assert held["reserved"]["coder_runs"] == 1
    assert "stop_requested" in deliver("cancel", held["token"], 2)[0]
    with pytest.raises(InterruptedError, match="stop requested"):
        stage.check()
    stopped = supervisor.status(stage.home, ROOT, stage.grant["project"], card)
    assert stopped["stop_requested"] is True
    assert stopped["execution"] == "uncertain"
    assert stopped["stop_proof"] == "unavailable"


def test_restarted_coder_keeps_original_deadline(admitted, monkeypatch):
    from emuru.projects.supervisor_coder import CoderStage

    stage, _card, _deliver = admitted
    monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: 601000000000)
    reopened = CoderStage(stage.home, stage.vault, ROOT, stage.grant)
    assert reopened.deadline == 601.0
    before = durable(stage.home)
    with pytest.raises(TimeoutError, match="original"):
        reopened.check()
    assert durable(stage.home) == before


def test_coder_cleanup_refuses_live_launcher_before_docker(admitted, monkeypatch):
    stage, _card, _deliver = admitted
    monkeypatch.setattr(coder_board, "launcher_stopped", lambda _: False)
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **kw: pytest.fail("live cleanup launched Docker")
    )
    before = durable(stage.home)
    with pytest.raises(ValueError, match="launcher may still execute"):
        coder_board.cleanup(stage.home, ROOT, stage.grant["task"], stage.grant)
    assert durable(stage.home) == before


@pytest.mark.parametrize("kind,limit", [("model", 12), ("check", 4)])
def test_coder_mandatory_counter_exhaustion_survives_reopen(admitted, kind, limit):
    from emuru.projects.supervisor_coder import CoderStage

    stage, _card, _deliver = admitted
    stage.event("claim-started")
    stage.event("claimed", native_task=stage.grant["task"], run=1)
    for _ in range(limit):
        stage.admit(kind)
    reopened = CoderStage(stage.home, stage.vault, ROOT, stage.grant)
    before = durable(stage.home)
    with pytest.raises(ValueError, match="counter invalid or exhausted"):
        reopened.admit(kind)
    assert durable(stage.home) == before


def test_cancel_during_prelaunch_wait_creates_no_unbound_child(admitted):
    import sys

    stage, card, deliver = admitted
    detail = supervisor.status(stage.home, ROOT, stage.grant["project"], card)
    deliver("cancel", detail["token"], 1)
    with pytest.raises(InterruptedError):
        stage.command([sys.executable, "-c", "raise AssertionError('effect')"])
    assert not any(r.get("phase") == "client-planned" for r in stage.records())


@pytest.mark.parametrize("remaining", [-1, 0.15])
def test_container_absolute_deadline_cannot_restart_after_delayed_start(
    tmp_path, remaining
):
    import subprocess
    import sys
    import time

    from emuru.projects.supervisor_coder import DEADLINE_EXEC

    marker = tmp_path / "unauthorized-effect"
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            DEADLINE_EXEC,
            str(time.monotonic_ns() + int(remaining * 10**9)),
            sys.executable,
            "-c",
            f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()",
        ],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode != 0 and not marker.exists()
    assert time.monotonic() - started < 1


def test_narrower_grant_reserves_only_its_existing_allowance(enrollment):
    import json

    from emuru.projects.supervisor_coder import reserve

    home, policy, grant, card = enrollment
    grant["limits"].update(
        attempt_seconds=15,
        model_iterations=3,
        check_executions=2,
        total_output_bytes=4096,
        artifact_bytes=1048576,
    )
    policy.write_text(json.dumps(grant))
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    detail = supervisor.status(home, ROOT, grant["project"], card)
    assert detail["reserved"]["active_seconds"] == 15
    assert detail["reserved"]["coder_iterations"] == 3
    assert detail["reserved"]["checks"] == 2
    assert detail["reserved"]["output_bytes"] == 4096
    assert detail["reserved"]["artifact_bytes"] == 1048576


def test_cleanup_never_infers_exact_container_death_from_missing_name(
    admitted, monkeypatch
):
    stage, _card, _deliver = admitted
    name, identity = "emuru-coder-check-" + "a" * 32, "b" * 64
    stage.event("planned", name=name, service="check", image=stage.stage["image"])
    stage.event("created", name=name, container_id=identity)
    queries = []

    def inspect(target, **_kwargs):
        queries.append(target)
        if target == name:
            return None
        return {
            "Id": identity,
            "Name": "/renamed-active",
            "Image": stage.stage["image"],
            "Config": {"Labels": {"io.emuru.coder.stage": stage.stage["admission"]}},
            "State": {"Running": True},
        }

    monkeypatch.setattr(stage, "inspect", inspect)
    with pytest.raises(ValueError, match="identity changed"):
        stage.remove(name)
    assert identity in queries
    assert not any(r.get("phase") == "container-stopped" for r in stage.records())


def test_cleanup_reuses_immutable_stop_proof_without_spending_more_slots(
    admitted, monkeypatch
):
    stage, _card, _deliver = admitted
    names = []
    for count in range(10):
        name = "emuru-coder-check-" + f"{count:032x}"
        identity = f"{count + 1:064x}"
        stage.event("planned", name=name, service="check", image=stage.stage["image"])
        stage.event("created", name=name, container_id=identity)
        stage.event("container-stopped", name=name, container_id=identity)
        names.append(name)
    monkeypatch.setattr(
        stage,
        "inspect",
        lambda *a, **kw: pytest.fail("Removed immutable ID was re-inspected"),
    )
    before = durable(stage.home)
    for _ in range(2):
        for name in names:
            stage.remove(name)
    assert durable(stage.home) == before
