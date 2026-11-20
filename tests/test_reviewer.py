"""Review authority and strict model verdicts, without native/provider effects."""

import json

import pytest

from emuru.projects import reviewer
from emuru.projects.coder import validate_grant
from emuru.projects.coder_native import validate_request
from emuru.projects.coder_runtime import qualification_fixture


def verdict(**updates):
    return {
        "verdict": "approve",
        "summary": "Matches the approved task.",
        "blockers": [],
        "suggestions": [],
        **updates,
    }


def test_review_requires_new_explicit_grant_and_preserves_legacy(tmp_path):
    _, grant = qualification_fixture(tmp_path / "fixture")
    assert validate_grant(grant)["schema"] == 1
    approved = {**grant, "schema": 2, "review": reviewer.authority()}
    assert validate_grant(approved) == approved
    for change in (
        {"enabled": False},
        {"enabled": 1},
        {"schema": 1.0},
        {"schema": 2},
        {"checklist": "0" * 64},
    ):
        with pytest.raises(ValueError):
            validate_grant({**approved, "review": {**approved["review"], **change}})


def test_verdict_is_strict_bounded_and_cannot_supply_identity():
    assert reviewer.parse_verdict(json.dumps(verdict()), ["sum_prices.py"]) == verdict()
    finding = {
        "path": "sum_prices.py",
        "line": 2,
        "reason": "Wrong sum",
        "requested_change": "Sum every input.",
    }
    requested = verdict(verdict="changes_requested", blockers=[finding])
    assert reviewer.parse_verdict(json.dumps(requested), ["sum_prices.py"]) == requested
    hard_refusal = verdict(verdict="blocked", blockers=[finding])
    assert (
        reviewer.parse_verdict(json.dumps(hard_refusal), ["sum_prices.py"])
        == hard_refusal
    )
    for value in (
        verdict(reviewer="emuru-reviewer"),
        verdict(verdict="changes_requested"),
        verdict(blockers=[finding]),
        verdict(verdict="verified"),
        verdict(summary="x" * 4097),
        verdict(suggestions=[{**finding, "path": "../secret"}]),
        verdict(suggestions=[{**finding, "line": True}]),
        verdict(suggestions=[finding] * 17),
    ):
        with pytest.raises((ValueError, TypeError)):
            reviewer.parse_verdict(json.dumps(value), ["sum_prices.py"])
    for raw in (
        '{"verdict":"approve","verdict":"blocked"}',
        json.dumps(verdict()) + " trailing",
        "\ud800",
        " " * 32769,
        "```json\n" + json.dumps(verdict()) + "\n```",
    ):
        with pytest.raises((ValueError, UnicodeError)):
            reviewer.parse_verdict(raw, ["sum_prices.py"])


def test_reviewer_request_has_its_own_complete_input_and_stage_caps():
    request = {
        "role": "reviewer",
        "prompt": "x" * reviewer.INPUT_BYTES,
        "max_iterations": 4,
        "deadline_seconds": 300,
    }
    validate_request(request)
    for update in (
        {"prompt": request["prompt"] + "x"},
        {"max_iterations": 5},
        {"deadline_seconds": 301},
        {"role": "other"},
    ):
        with pytest.raises(ValueError):
            validate_request({**request, **update})


def test_review_loop_reserves_once_and_old_rollback_is_refused(tmp_path):
    from emuru.deployment import check_review_state
    from emuru.projects.coder_board import create_attempt

    _, original = qualification_fixture(tmp_path / "fixture")
    grant = {**original, "schema": 2, "review": reviewer.authority()}
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    (home / "emuru-projects").mkdir(mode=0o700)
    folder = create_attempt(home, grant)
    reservation = json.loads((folder / "loop.json").read_text())
    assert reservation["active_seconds"] == 900
    assert reservation["output_bytes"] == 327680
    with pytest.raises(FileExistsError):
        create_attempt(home, grant)
    before = (folder / "loop.json").read_bytes()
    with pytest.raises(ValueError, match="incompatible"):
        check_review_state(home, tmp_path / "old-source")
    assert (folder / "loop.json").read_bytes() == before
    from pathlib import Path

    check_review_state(home, Path(__file__).resolve().parents[1])
    (folder / "loop.json").write_text('{"schema":999}')
    with pytest.raises(ValueError, match="incompatible"):
        check_review_state(home, Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("field,ceiling", [("summary", 4096)])
def test_verdict_accepts_exact_text_bound_and_refuses_one_over(field, ceiling):
    assert (
        reviewer.parse_verdict(json.dumps(verdict(**{field: "a" * ceiling})), [])[field]
        == "a" * ceiling
    )
    with pytest.raises(ValueError):
        reviewer.parse_verdict(json.dumps(verdict(**{field: "a" * (ceiling + 1)})), [])


@pytest.mark.parametrize("field", ["reason", "requested_change"])
def test_finding_text_bounds(field):
    finding = {"path": None, "line": None, "reason": "a", "requested_change": "a"}
    reviewer.parse_verdict(
        json.dumps(verdict(suggestions=[{**finding, field: "a" * 2048}])), []
    )
    with pytest.raises(ValueError):
        reviewer.parse_verdict(
            json.dumps(verdict(suggestions=[{**finding, field: "a" * 2049}])), []
        )


def test_review_state_cannot_reset_a_deadline_or_change_schema(tmp_path):
    import time
    from pathlib import Path

    from emuru.projects.scout import encoded

    stage = {
        "schema": 1,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "started_ns": time.monotonic_ns(),
        "deadline_ns": time.monotonic_ns(),
    }
    stage["deadline_ns"] = stage["started_ns"] + 300 * 10**9
    path = tmp_path / "stage.json"
    path.write_bytes(encoded(stage))
    path.chmod(0o600)
    assert reviewer._stage(tmp_path, launching=True) == stage
    for update in (
        {"schema": 1.0},
        {"deadline_ns": stage["deadline_ns"] + 1},
        {"boot_id": "old-boot"},
    ):
        path.write_bytes(encoded({**stage, **update}))
        with pytest.raises(ValueError):
            reviewer._stage(tmp_path, launching=True)


def test_legacy_attempt_does_not_require_repo_for_review_rollback_check(tmp_path):
    from pathlib import Path

    from emuru.deployment import check_review_state
    from emuru.projects.coder_board import create_attempt

    _, grant = qualification_fixture(tmp_path / "fixture")
    home = tmp_path / "home"
    (home / "emuru-projects").mkdir(parents=True, mode=0o700)
    create_attempt(home, grant)
    Path(grant["path"]).rename(tmp_path / "archived-repository")
    check_review_state(home, tmp_path / "legacy-source")


@pytest.mark.parametrize("operation", ["cleanup", "reconcile"])
def test_unknown_loop_schema_refuses_pre_review_native_mutation(
    tmp_path, monkeypatch, operation
):
    from emuru.projects import board, coder_board
    from emuru.projects.scout import encoded

    _, original = qualification_fixture(tmp_path / "fixture")
    grant = {**original, "schema": 2, "review": reviewer.authority()}
    home = tmp_path / "home"
    (home / "emuru-projects").mkdir(parents=True, mode=0o700)
    folder = coder_board.create_attempt(home, grant)
    (folder / "loop.json").write_bytes(encoded({"schema": 999}))
    monkeypatch.setattr(coder_board, "_launcher_stopped", lambda *_: True)

    def prohibited(*_args, **_kwargs):
        pytest.fail("Unknown state reached native initialization")

    monkeypatch.setattr(board, "project_board", prohibited)
    monkeypatch.setattr(board, "project_board_readonly", prohibited)
    with pytest.raises(ValueError, match="incompatible"):
        if operation == "cleanup":
            coder_board.cleanup(home, tmp_path, grant["task"], grant)
        else:
            coder_board.reconcile(home, tmp_path, tmp_path, grant["task"], grant)


def test_complete_verdict_accepts_32_kib_and_refuses_one_byte_over():
    expected = verdict()
    raw = json.dumps(expected)
    raw += " " * (32768 - len(raw.encode()))
    assert reviewer.parse_verdict(raw, ["sum_prices.py"]) == expected
    with pytest.raises(ValueError, match="verdict exceeds its bound"):
        reviewer.parse_verdict(raw + " ", ["sum_prices.py"])
