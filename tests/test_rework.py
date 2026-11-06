"""One fresh owner grant, exact parent lineage, no automatic replay."""

import pytest

from emuru.projects.coder import validate_grant
from emuru.projects.coder_runtime import qualification_fixture


def test_rework_grant_requires_complete_explicit_parent_authority(tmp_path):
    from emuru.projects import reviewer

    _, original = qualification_fixture(tmp_path / "fixture")
    grant = {
        **original,
        "schema": 3,
        "review": reviewer.authority(),
        "rework": {
            "schema": 1,
            "loop": "a" * 32,
            "grant_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "receipt_sha256": "d" * 64,
            "findings_sha256": "e" * 64,
            "final_sha256": "f" * 64,
            "coder_run": 1,
            "reviewer_run": 2,
        },
    }
    assert validate_grant(grant) == grant
    for update in (
        {"loop": grant["grant_id"]},
        {"coder_run": True},
        {"reviewer_run": 1},
        {"schema": 999},
        {"final_sha256": "../escape"},
    ):
        with pytest.raises(ValueError):
            validate_grant({**grant, "rework": {**grant["rework"], **update}})
    with pytest.raises(ValueError):
        validate_grant({k: v for k, v in grant.items() if k != "rework"})


@pytest.fixture
def parent_attempt(tmp_path):
    from emuru.projects import coder_board, reviewer
    from emuru.projects.coder_runtime import Qualification
    from emuru.projects.coder_sandbox import CASES
    from emuru.projects.scout import digest, encoded

    _, grant = qualification_fixture(tmp_path / "fixture")
    grant = {**grant, "schema": 2, "review": reviewer.authority()}
    home = tmp_path / "home"
    (home / "emuru-projects").mkdir(parents=True, mode=0o700)
    folder = coder_board.create_attempt(home, grant)
    run = {"task": grant["task"], "run": 1}
    coder_board._write_new(folder, "run.json", encoded(run))
    attempt = Qualification(folder, grant, "sha256:" + "a" * 64, task_run=run)

    class Checks:
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

    def native(dispatch):
        before = dispatch("coder_read", {"path": "sum_prices.py"})
        dispatch(
            "coder_write",
            {
                "path": "sum_prices.py",
                "content": "def sum_prices(prices):\n    return sum(prices) if len(prices) < 4 else 0\n",
                "expected_sha256": before["sha256"],
            },
        )
        return {"success": True}

    report = attempt.run(native, Checks())
    finding = {
        "path": "sum_prices.py",
        "line": 2,
        "reason": "Four inputs return zero.",
        "requested_change": "Sum all inputs.",
    }
    receipt = {
        "schema": 1,
        "status": "changes_requested",
        "task": grant["task"],
        "coder_run": 1,
        "run": 2,
        "grant_sha256": digest(grant),
        "evidence_sha256": attempt.evidence_sha256,
        "verdict": {"verdict": "changes_requested", "blockers": [finding]},
    }
    (folder / "review").mkdir(mode=0o700)
    coder_board._write_new(folder / "review", "receipt.json", encoded(receipt))
    child = {
        **grant,
        "schema": 3,
        "grant_id": "b" * 32,
        "rework": {
            "schema": 1,
            "loop": grant["grant_id"],
            "grant_sha256": digest(grant),
            "evidence_sha256": attempt.evidence_sha256,
            "receipt_sha256": digest(receipt),
            "findings_sha256": digest([finding]),
            "final_sha256": digest(report["final_manifest"]),
            "coder_run": 1,
            "reviewer_run": 2,
        },
    }
    return home, folder, child, Checks


def test_rework_starts_from_parent_bytes_preserves_original_diff_and_rejection(
    parent_attempt,
):
    from emuru.projects import coder_board
    from emuru.projects.coder_runtime import Qualification, verify_evidence
    from emuru.projects.scout import encoded

    home, previous, grant, Checks = parent_attempt
    old = (previous / "review/receipt.json").read_bytes()
    folder = coder_board.create_attempt(home, grant)
    run = {"task": grant["task"], "run": 3}
    coder_board._write_new(folder, "run.json", encoded(run))
    attempt = Qualification(folder, grant, "sha256:" + "a" * 64, task_run=run)
    assert "else 0" in attempt.workspace.read("sum_prices.py")["content"]

    def native(dispatch):
        before = dispatch("coder_read", {"path": "sum_prices.py"})
        dispatch(
            "coder_write",
            {
                "path": "sum_prices.py",
                "content": "def sum_prices(prices):\n    return sum(prices)\n",
                "expected_sha256": before["sha256"],
            },
        )
        return {"success": True}

    report = attempt.run(native, Checks())
    assert report["success"]
    assert report["parent"] == grant["rework"]
    assert "else 0" not in (folder / "diff.patch").read_text()
    assert "-    return 0" in (folder / "diff.patch").read_text()
    assert (previous / "review/receipt.json").read_bytes() == old
    assert coder_board._find_attempt(home, grant["task"]) == folder
    assert verify_evidence(folder, attempt.evidence_sha256, task_run=run) == report


@pytest.mark.parametrize(
    "field", ["owner", "baseline", "write_files", "check_ids", "read_files"]
)
def test_changed_parent_scope_is_refused_before_workspace_creation(
    parent_attempt, field
):
    from emuru.projects.coder import Workspace

    _home, previous, grant, _ = parent_attempt
    grant = {
        **grant,
        field: 99 if field == "owner" else "0" * 40 if field == "baseline" else [],
    }
    folder = previous.parent / grant["grant_id"]
    folder.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        Workspace(folder / "source", grant)
    assert not (folder / "source").exists()


@pytest.mark.parametrize(
    "field",
    [
        "evidence_sha256",
        "grant_sha256",
        "receipt_sha256",
        "findings_sha256",
        "final_sha256",
        "coder_run",
        "reviewer_run",
    ],
)
def test_wrong_parent_binding_refuses_source_materialization(parent_attempt, field):
    from emuru.projects.coder import Workspace

    _, previous, grant, _ = parent_attempt
    grant["rework"][field] = 99 if field.endswith("_run") else "0" * 64
    folder = previous.parent / grant["grant_id"]
    folder.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        Workspace(folder / "source", grant)
    assert not (folder / "source").exists()


@pytest.mark.parametrize(
    "quantity,ceiling",
    [
        ("coder_runs", 2),
        ("reviewer_runs", 2),
        ("active_seconds", 1800),
        ("coder_iterations", 24),
        ("reviewer_iterations", 8),
        ("checks", 8),
        ("output_bytes", 655360),
    ],
)
def test_aggregate_ceiling_accepts_limit_refuses_one_over(quantity, ceiling):
    from emuru.projects.rework import bounded

    total = {
        "coder_runs": 2,
        "reviewer_runs": 2,
        "active_seconds": 1800,
        "coder_iterations": 24,
        "reviewer_iterations": 8,
        "checks": 8,
        "output_bytes": 655360,
    }
    assert bounded(total)[quantity] == ceiling
    with pytest.raises(ValueError):
        bounded({**total, quantity: ceiling + 1})
    with pytest.raises(ValueError):
        bounded({**total, quantity: None})


def test_second_reservation_cannot_reset_or_create_third_attempt(parent_attempt):
    import json

    from emuru.projects import coder_board, reviewer
    from emuru.projects.rework import parent

    home, previous, grant, _ = parent_attempt
    old = (previous / "loop.json").read_bytes()
    folder = coder_board.create_attempt(home, grant)
    reservation = reviewer.check_loop(folder, grant)
    assert reservation["coder_runs"] == reservation["reviewer_runs"] == 2
    assert reservation["active_seconds"] == 1800
    assert reservation["output_bytes"] == 655360
    assert (previous / "loop.json").read_bytes() == old
    with pytest.raises(FileExistsError):
        coder_board.create_attempt(home, grant)
    with pytest.raises(ValueError):
        parent(
            folder.parent / ("c" * 32),
            {
                **grant,
                "grant_id": "c" * 32,
                "rework": {**grant["rework"], "loop": grant["grant_id"]},
            },
        )
    value = json.loads((folder / "loop.json").read_text())
    value["active_seconds"] = 900
    (folder / "loop.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        reviewer.check_loop(folder, grant)


def test_rework_without_new_bytes_cannot_qualify_rejected_digest(parent_attempt):
    from emuru.projects import coder_board
    from emuru.projects.coder_runtime import Qualification
    from emuru.projects.scout import encoded

    home, _, grant, Checks = parent_attempt
    folder = coder_board.create_attempt(home, grant)
    run = {"task": grant["task"], "run": 3}
    coder_board._write_new(folder, "run.json", encoded(run))
    attempt = Qualification(folder, grant, "sha256:" + "a" * 64, task_run=run)
    report = attempt.run(lambda _: {"success": True}, Checks())
    assert not report["success"]


@pytest.mark.parametrize("which", ["parent", "child"])
def test_unknown_intent_schema_refuses_two_attempt_selection(parent_attempt, which):
    import json

    from emuru.projects import coder_board

    home, previous, grant, _ = parent_attempt
    folder = coder_board.create_attempt(home, grant)
    path = (previous if which == "parent" else folder) / "intent.json"
    value = json.loads(path.read_bytes())
    path.write_text(json.dumps({**value, "schema": 999}))
    with pytest.raises(ValueError, match="intent"):
        coder_board._find_attempt(home, grant["task"])


def test_rework_link_respects_parent_artifact_cap_before_creating_child(parent_attempt):
    from emuru.projects import coder_board
    from emuru.projects.coder_runtime import _artifact_size

    home, previous, grant, _ = parent_attempt
    cap = grant["limits"]["artifact_bytes"]
    with (previous / "padding").open("wb") as stream:
        stream.truncate(cap - _artifact_size(previous))
    (previous / "padding").chmod(0o600)
    assert _artifact_size(previous) == cap
    with pytest.raises(ValueError, match="artifact budget"):
        coder_board.create_attempt(home, grant)
    assert not (previous.parent / grant["grant_id"]).exists()
    assert _artifact_size(previous) == cap


def test_partial_lineage_status_is_readonly_uncertain(parent_attempt):
    from types import SimpleNamespace

    from emuru.projects import coder_board

    home, previous, grant, _ = parent_attempt
    coder_board.create_attempt(home, grant)
    (previous / "rework-intent.json").unlink()
    card = SimpleNamespace(id=grant["task"])
    result = coder_board.status(None, None, card, home, home)
    assert result["status"] == "uncertain"
    assert "lineage" in result["detail"]


@pytest.mark.parametrize("raw", ["[]", "null", "42", '{"schema":1,"schema":999}'])
def test_malformed_intent_has_readonly_uncertain_diagnostics(parent_attempt, raw):
    from types import SimpleNamespace

    from emuru.projects import coder_board

    home, previous, grant, _ = parent_attempt
    (previous / "intent.json").write_text(raw)
    result = coder_board.status(
        None, None, SimpleNamespace(id=grant["task"]), home, home
    )
    assert result["status"] == "uncertain"
    assert (previous / "intent.json").read_text() == raw


@pytest.mark.parametrize(
    "name",
    [
        "attempt_seconds",
        "model_iterations",
        "check_executions",
        "total_output_bytes",
        "artifact_bytes",
        "check_seconds",
        "check_output_bytes",
        "file_bytes",
        "total_bytes",
        "files",
    ],
)
def test_rework_preserves_each_per_attempt_ceiling(parent_attempt, name):
    from emuru.projects.coder import LIMITS, validate_grant

    _, _, grant, _ = parent_attempt
    assert validate_grant(grant)["limits"][name] == LIMITS[name]
    with pytest.raises(ValueError):
        validate_grant({**grant, "limits": {**grant["limits"], name: LIMITS[name] + 1}})


def test_loop_artifacts_accept_exact_ceiling_refuse_one_over(parent_attempt):
    from emuru.projects import coder_board
    from emuru.projects.coder_runtime import _artifact_size
    from emuru.projects.rework import ARTIFACT_BYTES, check_artifacts

    home, previous, grant, _ = parent_attempt
    folder = coder_board.create_attempt(home, grant)
    remaining = ARTIFACT_BYTES - _artifact_size(previous) - _artifact_size(folder)
    check_artifacts(folder, grant, remaining)
    with pytest.raises(ValueError, match="artifact budget"):
        check_artifacts(folder, grant, remaining + 1)


def test_fabricated_original_snapshot_cannot_become_parent_evidence(parent_attempt):
    import hashlib
    import json

    from emuru.projects.coder_runtime import verify_evidence
    from emuru.projects.scout import encoded

    _, previous, grant, _ = parent_attempt
    report = json.loads((previous / "evidence.json").read_bytes())
    path = previous / "initial-source/sum_prices.py"
    path.chmod(0o600)
    path.write_text("def sum_prices(prices):\n    return 999\n")
    path.chmod(0o444)
    report["initial_manifest"]["sum_prices.py"].update(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size
    )
    raw = encoded(report)
    (previous / "evidence.json").write_bytes(raw)
    with pytest.raises(ValueError, match="committed baseline"):
        verify_evidence(
            previous,
            hashlib.sha256(raw).hexdigest(),
            task_run={"task": grant["task"], "run": 1},
        )
