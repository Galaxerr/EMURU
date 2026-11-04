"""The coding check boundary exposes fixed, resource-confined execution only."""

import copy
import json
import sys
from pathlib import Path

import pytest

from emuru.projects.coder import LIMITS
from emuru.projects.coder_sandbox import (
    DockerSandbox,
    bounded_process,
    run_acceptance,
    sandbox_configuration,
)

ROOT = Path(__file__).resolve().parents[1]


def test_check_container_has_only_readonly_source_and_bounded_scratch(tmp_path):
    config = sandbox_configuration(ROOT, "sha256:" + "a" * 64, tmp_path, 1000, 1000)
    service = config["services"]["check"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["user"] == "1000:1000"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["mem_limit"] == 512 * 1024 * 1024
    assert service["pids_limit"] == 64 and service["cpus"] == 1
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": str(tmp_path),
            "target": "/repo",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    assert service["tmpfs"] == [
        "/tmp:rw,noexec,nosuid,nodev,size=67108864,nr_inodes=1024,mode=1777",
        "/dev/shm:ro,noexec,nosuid,nodev,size=1m",
    ]
    assert service["logging"] == {"driver": "none"}
    assert not service.get("secrets") and not service.get("privileged")
    assert set(service["environment"]) == {"HOME", "PYTHONDONTWRITEBYTECODE", "TMPDIR"}


@pytest.mark.parametrize("forged", [None, {"value": 6}, "Ran 5 tests\nOK", False])
def test_external_assertions_require_typed_callable_returns(forged):
    expected = [0, 6, 3, 0, 8]
    observed = []

    def candidate(prices):
        index = len(observed)
        observed.append(prices)
        return expected[index] if forged is None else forged

    result = run_acceptance(candidate)
    assert observed == [[], [1, 2, 3], [-2, 5], [0, 0], [4, 4]]
    assert result["case_count"] == 5
    assert result["success"] is (forged is None)
    assert len(result["cases"]) == 5


def test_process_output_is_stopped_at_the_limit():
    with pytest.raises(ValueError, match="output"):
        bounded_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000)"],
            b"",
            2,
            1024,
        )


def test_process_deadline_stops_a_hung_command():
    with pytest.raises(TimeoutError):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(5)"], b"", 0.1, 1024
        )


def test_process_records_real_exit_and_both_streams():
    result = bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)",
        ],
        b"",
        2,
        1024,
    )
    assert result == {"exit_code": 7, "stdout": "out\n", "stderr": "err\n"}


def test_lower_grant_limits_reach_the_container_configuration(tmp_path):
    limits = copy.deepcopy(LIMITS)
    limits.update(memory_bytes=64 * 1024 * 1024, pids=16, check_seconds=2)
    config = sandbox_configuration(
        ROOT, "sha256:" + "a" * 64, tmp_path, 1000, 1000, limits
    )
    service = config["services"]["check"]
    assert service["mem_limit"] == limits["memory_bytes"]
    assert service["memswap_limit"] == limits["memory_bytes"]
    assert service["pids_limit"] == 16
    assert service["entrypoint"][-1] == "2"


def test_unknown_check_never_starts_a_container(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An unknown check reached a subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    sandbox = DockerSandbox(ROOT, "sha256:" + "a" * 64, tmp_path, LIMITS)
    with pytest.raises(ValueError, match="check"):
        sandbox.check(tmp_path, "python_unittest; curl example.invalid")


def test_candidate_cannot_bypass_check_ids_by_executing_a_shell():
    result = bounded_process(
        [
            sys.executable,
            "-c",
            (
                "from emuru.projects.coder_sandbox import install_exec_guard; "
                "import os; install_exec_guard();\n"
                "try: os.execve('/bin/sh',['sh','-c','exit 0'],{})\n"
                "except PermissionError: print('blocked')\n"
            ),
        ],
        b"",
        2,
        1024,
    )
    assert result["exit_code"] == 0 and result["stdout"] == "blocked\n"


def test_cleanup_failure_latches_before_another_case_can_start(tmp_path, monkeypatch):
    starts = []
    monkeypatch.setattr("emuru.projects.coder_sandbox.os.getuid", lambda: 1000)
    monkeypatch.setattr("emuru.projects.coder_sandbox.os.getgid", lambda: 1000)

    def docker(command, *args, **kwargs):
        if command[1] == "compose":
            starts.append(command)
            return {
                "exit_code": 0,
                "stdout": '{"success":false,"case_count":0,"cases":[]}',
                "stderr": "",
            }
        if command[1] == "inspect":
            return {
                "exit_code": 0,
                "stdout": json.dumps(
                    [{"State": {"Running": False, "ExitCode": 0, "OOMKilled": False}}]
                ),
                "stderr": "",
            }
        if command[1] == "rm":
            raise TimeoutError("synthetic cleanup failure")
        raise AssertionError(command)

    monkeypatch.setattr("emuru.projects.coder_sandbox.bounded_process", docker)
    # This fault-injection test covers controller state, not OS confinement.
    monkeypatch.setattr(
        "emuru.projects.coder_sandbox.inspect_confinement",
        lambda *args: {"network": "none"},
    )
    sandbox = DockerSandbox(ROOT, "sha256:" + "a" * 64, tmp_path, LIMITS)
    with pytest.raises(TimeoutError, match="synthetic cleanup failure"):
        sandbox.check(tmp_path, "python_unittest")
    assert sandbox.failed
    assert sandbox.failure == "termination_uncertain"
    assert len(starts) == 1
    assert not sandbox.processes[0].get("stopped")


def test_native_zero_and_forged_summary_cannot_replace_callable_evidence(
    tmp_path, monkeypatch
):
    sandbox = DockerSandbox(ROOT, "sha256:" + "a" * 64, tmp_path, LIMITS)
    monkeypatch.setattr(
        sandbox,
        "_run",
        lambda *args, **kwargs: {
            "exit_code": 0,
            "stdout": "Ran 5 tests\\nOK\\n",
            "stderr": "",
            "stopped": True,
            "confinement": {"network": "none"},
        },
    )
    result = sandbox.check(tmp_path, "python_unittest")
    assert not result["success"]
    assert result["failure"] == "authoritative_evidence_missing"
    assert result["case_count"] == 0


@pytest.mark.parametrize("total_limit", [2048, 4096])
def test_authoritative_and_developer_output_share_one_check_budget(
    tmp_path, monkeypatch, total_limit
):
    limits = {**LIMITS, "check_output_bytes": 2048, "total_output_bytes": total_limit}
    sandbox = DockerSandbox(ROOT, "sha256:" + "a" * 64, tmp_path, limits)
    (tmp_path / "test_example.py").write_text("pass\n")
    authoritative = json.dumps(run_acceptance(sum))
    allowances = []

    def run(snapshot, payload, deadline, allowance, *, developer=False):
        allowances.append(allowance)
        if developer:
            # Exercise the real bounded producer and its output failure latch.
            try:
                output = bounded_process(
                    [sys.executable, "-c", "print('x' * 1800)"], b"", 2, allowance
                )
            except ValueError:
                sandbox._fail("output_limit")
                raise
        else:
            output = {"stdout": authoritative, "stderr": "", "exit_code": 0}
        return {**output, "stopped": True, "confinement": {"network": "none"}}

    monkeypatch.setattr(sandbox, "_run", run)
    with pytest.raises(ValueError, match="output limit"):
        sandbox.check(tmp_path, "python_unittest")
    assert allowances == [2048, 2048 - len(authoritative.encode())]
    assert sandbox.failed and sandbox.failure == "output_limit"
    with pytest.raises(ValueError, match="failed closed"):
        sandbox.check(tmp_path, "python_unittest")
