"""Qualification authority belongs to observed checks and bytes, never prose."""

import hashlib
import json
import os

import pytest

from emuru.projects.coder_runtime import (
    Qualification,
    _accepted,
    qualification_fixture,
    verify_evidence,
)
from emuru.projects.coder_sandbox import CASES


def test_native_summary_is_never_test_completion_evidence():
    summary_only = {
        "success": True,
        "case_count": 5,
        "cases": [],
        "developer_tests": {
            "exit_code": 0,
            "stdout": "Ran 5 tests\\nOK\\n",
            "completion_verified": False,
        },
    }
    assert not _accepted(summary_only)


@pytest.mark.parametrize("supplemental", [False, True])
def test_failed_check_revokes_all_tools_and_cannot_be_recovered(tmp_path, supplemental):
    _, grant = qualification_fixture(tmp_path / "fixture")
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)

    class FailedChecks:
        failed = False
        processes = ()
        calls = 0

        def check(self, snapshot, check_id, **kwargs):
            self.calls += 1
            if supplemental:
                return {
                    "success": True,
                    "case_count": 5,
                    "check_id": check_id,
                    "host_observation": {
                        "stopped": True,
                        "confinement": {"network": "none"},
                    },
                    "cases": [
                        {
                            "input": prices,
                            "expected": expected,
                            "host_call_returned": True,
                            "returned_type": "int",
                            "value": expected,
                            "accepted": True,
                        }
                        for prices, expected in CASES
                    ],
                    "developer_tests": {"exit_code": 1, "completion_verified": False},
                }
            return {"success": False, "case_count": 5, "check_id": check_id}

    backend = FailedChecks()

    def native(dispatch):
        assert (
            dispatch("coder_check", {"check_id": "python_unittest"})["success"] is False
        )
        for name, args in (
            ("coder_read", {"path": "sum_prices.py"}),
            (
                "coder_write",
                {
                    "path": "sum_prices.py",
                    "content": "return 1",
                    "expected_sha256": "a" * 64,
                },
            ),
            ("coder_check", {"check_id": "python_unittest"}),
        ):
            with pytest.raises(ValueError, match="closed"):
                dispatch(name, args)
        return {"success": True}

    report = attempt.run(native, backend)
    assert report["success"] is False
    assert report["failure"] == "tests_failed"
    assert backend.calls == 1
    assert len(report["checks"]) == 1
    assert verify_evidence(attempt.folder, attempt.evidence_sha256) == report
    with pytest.raises(ValueError, match="does not match"):
        verify_evidence(
            attempt.folder,
            attempt.evidence_sha256,
            task_run={"task": grant["task"], "run": 1},
        )


def test_execution_error_cannot_be_erased_by_native_success(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)

    def native(dispatch):
        with pytest.raises(ValueError):
            dispatch("coder_check", {"check_id": "python_unittest; id"})
        with pytest.raises(ValueError, match="closed"):
            dispatch("coder_read", {"path": "sum_prices.py"})
        return {"success": True}

    report = attempt.run(native, object())
    assert not report["success"] and report["failure"] == "execution_failed"
    assert not report["checks"]


def test_artifact_budget_rejects_initial_source_before_materialization(tmp_path):
    from emuru.projects.scout import digest, encoded

    _, grant = qualification_fixture(tmp_path / "fixture")
    grant["limits"]["artifact_bytes"] = (
        len(encoded(grant))
        + len(encoded({"grant": digest(grant), "qualification_only": True}))
        + 1
    )
    with pytest.raises(ValueError, match="artifact budget"):
        Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)
    assert not (tmp_path / "attempt/source").exists()


def test_artifact_budget_rejects_atomic_edit_before_changing_bytes(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    grant["limits"]["artifact_bytes"] = 4096
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)
    before = attempt.workspace.read("sum_prices.py")
    with pytest.raises(ValueError, match="artifact budget"):
        attempt.workspace.write("sum_prices.py", "#" * 4096, before["sha256"])
    assert attempt.workspace.read("sum_prices.py") == before


def test_artifact_budget_rejects_snapshot_before_creating_it(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    grant["limits"]["artifact_bytes"] = 4096
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)
    before = attempt.workspace.read("sum_prices.py")
    attempt.workspace.write("sum_prices.py", "#" * 1500, before["sha256"])
    snapshot = attempt.folder / "check-source"
    with pytest.raises(ValueError, match="artifact budget"):
        attempt.workspace.snapshot(snapshot)
    assert not snapshot.exists()


def test_model_success_without_an_edit_is_not_qualified(tmp_path):
    repo, grant = qualification_fixture(tmp_path / "fixture")
    original = (repo / "sum_prices.py").read_bytes()
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)

    class PassingBackend:
        failed = False
        executions = 0
        processes = ()

        def check(self, snapshot, check_id, **kwargs):
            return {
                "success": True,
                "case_count": 5,
                "check_id": check_id,
                "host_observation": {
                    "stopped": True,
                    "confinement": {"network": "none"},
                },
                "cases": [
                    {
                        "input": prices,
                        "expected": expected,
                        "host_call_returned": True,
                        "returned_type": "int",
                        "value": expected,
                        "accepted": True,
                    }
                    for prices, expected in CASES
                ],
            }

    report = attempt.run(lambda dispatch: {"success": True}, PassingBackend())
    assert report["success"] is False
    assert report["changed_files"] == []
    assert (repo / "sum_prices.py").read_bytes() == original


def test_an_interrupted_attempt_is_never_started_twice(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="existing|incomplete"):
        Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "tamper", ["post_check", "changed_files", "diff", "missing", "hardlink"]
)
def test_scoped_edit_and_final_checks_bind_to_reopened_bytes(tmp_path, tamper):
    repo, grant = qualification_fixture(tmp_path / "fixture")
    attempt = Qualification(tmp_path / "attempt", grant, "sha256:" + "a" * 64)

    def native(dispatch):
        original = dispatch("coder_read", {"path": "sum_prices.py"})
        dispatch(
            "coder_write",
            {
                "path": "sum_prices.py",
                "content": "def sum_prices(prices):\n    return sum(prices)\n",
                "expected_sha256": original["sha256"],
            },
        )
        return {"success": True}

    class ObservedChecks:
        failed = False
        processes = ()

        def check(self, snapshot, check_id, **kwargs):
            return {
                "success": True,
                "case_count": 5,
                "check_id": check_id,
                "host_observation": {
                    "stopped": True,
                    "confinement": {"network": "none"},
                },
                "cases": [
                    {
                        "input": prices,
                        "expected": expected,
                        "host_call_returned": True,
                        "returned_type": "int",
                        "value": expected,
                        "accepted": True,
                    }
                    for prices, expected in CASES
                ],
            }

    report = attempt.run(native, ObservedChecks())
    assert report["success"] and report["changed_files"] == ["sum_prices.py"]
    assert (repo / "sum_prices.py").read_text().endswith("return 0\n")
    assert verify_evidence(attempt.folder, attempt.evidence_sha256) == report
    expected_digest = attempt.evidence_sha256
    if tamper == "post_check":
        (attempt.folder / "source/sum_prices.py").write_text("print('forged')\n")
    elif tamper in {"changed_files", "diff"}:
        if tamper == "changed_files":
            report["changed_files"] = ["ungranted.py"]
        else:
            patch = b"fabricated diff\n"
            (attempt.folder / "diff.patch").write_bytes(patch)
            report["diff_sha256"] = hashlib.sha256(patch).hexdigest()
        data = json.dumps(report, sort_keys=True).encode()
        (attempt.folder / "evidence.json").write_bytes(data)
        expected_digest = hashlib.sha256(data).hexdigest()
    elif tamper == "missing":
        (attempt.folder / "final-source").chmod(0o700)
        (attempt.folder / "final-source/sum_prices.py").unlink()
    else:
        os.link(attempt.folder / "diff.patch", tmp_path / "linked-patch")
    with pytest.raises(ValueError):
        verify_evidence(attempt.folder, expected_digest)
