"""Native lifecycle evidence does not authenticate candidate Python completion."""

import copy
import json
import sys

import pytest

from emuru.projects import coder_inference

IMAGE = "sha256:" + "a" * 64
NAME = "emuru-coder-inference-fixture"


def facts():
    return {
        "Id": "b" * 64,
        "Name": "/" + NAME,
        "Config": {"Image": IMAGE},
        "State": {
            "Status": "exited",
            "Running": False,
            "Dead": False,
            "OOMKilled": False,
            "Error": "",
            "ExitCode": 0,
        },
    }


def test_native_completion_binds_final_frame_to_observed_process():
    final = {"type": "final", "success": True, "tool_calls": 4}
    evidence = coder_inference.verify_native_completion(
        IMAGE, NAME, final, 4, 0, facts()
    )
    assert evidence["container_id"] == "b" * 64
    assert evidence["image"] == IMAGE
    assert evidence["tool_calls"] == 4
    assert evidence["exit_code"] == 0
    assert len(evidence["final_sha256"]) == 64


@pytest.mark.parametrize(
    "attack",
    [
        "no_frame",
        "prose",
        "extra_claim",
        "wrong_count",
        "boolean_count",
        "late_failure",
        "running",
        "crashed",
        "oom",
        "daemon_error",
        "wrong_container",
        "wrong_image",
    ],
)
def test_native_completion_rejects_unproved_or_conflicting_signals(attack):
    final = {"type": "final", "success": True, "tool_calls": 4}
    actual = copy.deepcopy(facts())
    code = 0
    if attack == "no_frame":
        final = None
    elif attack == "prose":
        final = "completed successfully"
    elif attack == "extra_claim":
        final["normal_completion"] = True
    elif attack == "wrong_count":
        final["tool_calls"] = 3
    elif attack == "boolean_count":
        final["tool_calls"] = True
    elif attack == "late_failure":
        code = actual["State"]["ExitCode"] = 7
    elif attack == "running":
        actual["State"].update(Running=True, Status="running")
    elif attack == "crashed":
        actual["State"]["Dead"] = True
    elif attack == "oom":
        actual["State"]["OOMKilled"] = True
    elif attack == "daemon_error":
        actual["State"]["Error"] = "synthetic failure"
    elif attack == "wrong_container":
        actual["Name"] = "/other"
    else:
        actual["Config"]["Image"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError):
        coder_inference.verify_native_completion(IMAGE, NAME, final, 4, code, actual)


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "zero_without_frame",
        "forged_claim",
        "late_failure",
        "cancelled",
        "cleanup_failure",
        "timeout",
    ],
)
def test_native_transport_requires_protocol_daemon_and_cleanup_evidence(
    monkeypatch, mode
):
    original_popen = coder_inference.subprocess.Popen
    launched = []

    def launch(command, **kwargs):
        launched.append(command)
        final = {"type": "final", "success": mode != "cancelled", "tool_calls": 0}
        if mode == "cancelled":
            final["error"] = "coder_cancelled"
        if mode == "forged_claim":
            final["normal_completion"] = True
        program = "import sys,time; sys.stdin.readline(); "
        if mode == "timeout":
            program += "time.sleep(5)"
        elif mode == "zero_without_frame":
            program += "sys.exit(0)"
        else:
            program += f"print({json.dumps(final)!r},flush=True); sys.exit({1 if mode in {'cancelled', 'late_failure'} else 0})"
        return original_popen([sys.executable, "-c", program], **kwargs)

    def docker(command, *args, **kwargs):
        if command[1] == "inspect":
            actual = facts()
            actual["Name"] = "/" + command[2]
            actual["State"]["ExitCode"] = (
                1 if mode in {"cancelled", "late_failure"} else 0
            )
            return {"exit_code": 0, "stdout": json.dumps([actual]), "stderr": ""}
        if command[1] == "rm":
            return {
                "exit_code": int(mode == "cleanup_failure"),
                "stdout": "",
                "stderr": "",
            }
        assert command[1] == "ps"
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(coder_inference.subprocess, "Popen", launch)
    monkeypatch.setattr(coder_inference, "bounded_process", docker)
    request = {"prompt": "fixture", "deadline_seconds": 1}
    if mode in {"normal", "cancelled"}:
        result = coder_inference.run_inference(
            IMAGE, "fixture-network", "synthetic-key", request, lambda *_: {}
        )
        assert result["success"] is (mode == "normal")
        assert result["native_lifecycle"]["removed"] is True
    else:
        with pytest.raises((ValueError, RuntimeError, TimeoutError)):
            coder_inference.run_inference(
                IMAGE, "fixture-network", "synthetic-key", request, lambda *_: {}
            )
    assert len(launched) == 1
