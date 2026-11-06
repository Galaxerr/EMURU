"""One owner-authorized second attempt. Parent records remain immutable."""

import re

from emuru.projects.scout import digest, encoded

STATE_SCHEMA = 1
CEILINGS = {
    "coder_runs": 2,
    "reviewer_runs": 2,
    "active_seconds": 1800,
    "coder_iterations": 24,
    "reviewer_iterations": 8,
    "checks": 8,
    "output_bytes": 640 * 1024,
}
ARTIFACT_BYTES = 17 * 1024 * 1024


def validate_authority(grant):
    value = grant["rework"]
    hashes = {
        "grant_sha256",
        "evidence_sha256",
        "receipt_sha256",
        "findings_sha256",
        "final_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != hashes | {"schema", "loop", "coder_run", "reviewer_run"}
        or type(value["schema"]) is not int
        or value["schema"] != STATE_SCHEMA
        or not isinstance(value["loop"], str)
        or not re.fullmatch("[0-9a-f]{32}", value["loop"])
        or value["loop"] == grant["grant_id"]
        or any(
            not isinstance(value[k], str) or not re.fullmatch("[0-9a-f]{64}", value[k])
            for k in hashes
        )
        or any(
            type(value[k]) is not int or value[k] <= 0
            for k in ("coder_run", "reviewer_run")
        )
        or value["coder_run"] == value["reviewer_run"]
    ):
        raise ValueError("Invalid rework authority.")


def parent(folder, grant):
    from emuru.projects.coder import load_grant

    validate_authority(grant)
    previous = folder.parent / grant["rework"]["loop"]
    original = load_grant(previous / "grant.json")
    if (
        original["schema"] != 2
        or any(
            grant[k] != original[k]
            for k in original
            if k not in {"schema", "grant_id", "limits"}
        )
        or any(grant["limits"][k] > original["limits"][k] for k in original["limits"])
    ):
        raise ValueError("Rework scope, owner or limits changed.")
    if digest(original) != grant["rework"]["grant_sha256"]:
        raise ValueError("Rework parent grant changed.")
    return previous, original


def lineage(folder, grant):
    """Reopen both snapshots/receipts; never treat parent bytes as committed bytes."""
    from emuru.projects.coder_board import _read_json
    from emuru.projects.coder_runtime import verify_evidence
    from emuru.projects.reviewer import OUTPUT_BYTES, _read

    previous, original = parent(folder, grant)
    binding = grant["rework"]
    run = _read_json(previous / "run.json")
    report = verify_evidence(previous, binding["evidence_sha256"], task_run=run)
    receipt = _read(previous / "review/receipt.json", OUTPUT_BYTES)
    if (
        not report["success"]
        or receipt["status"] != "changes_requested"
        or receipt["verdict"]["verdict"] != "changes_requested"
        or receipt["task"] != grant["task"]
        or run["run"] != binding["coder_run"]
        or receipt["coder_run"] != binding["coder_run"]
        or receipt["run"] != binding["reviewer_run"]
        or receipt["grant_sha256"] != digest(original)
        or receipt["evidence_sha256"] != binding["evidence_sha256"]
        or digest(receipt) != binding["receipt_sha256"]
        or digest(receipt["verdict"]["blockers"]) != binding["findings_sha256"]
        or digest(report["final_manifest"]) != binding["final_sha256"]
    ):
        raise ValueError("Rework parent lineage changed.")
    return previous, original, report, receipt


def reservation(grant):
    from emuru.projects.reviewer import ITERATIONS, OUTPUT_BYTES, SECONDS

    return {
        "coder_runs": 1,
        "reviewer_runs": 1,
        "active_seconds": grant["limits"]["attempt_seconds"] + SECONDS,
        "coder_iterations": grant["limits"]["model_iterations"],
        "reviewer_iterations": ITERATIONS,
        "checks": grant["limits"]["check_executions"],
        "output_bytes": grant["limits"]["total_output_bytes"] + OUTPUT_BYTES,
    }


def bounded(total):
    if set(total) != set(CEILINGS) or any(
        type(total[k]) is not int or not 0 < total[k] <= CEILINGS[k] for k in CEILINGS
    ):
        raise ValueError("Rework aggregate reservation exhausted or unknown.")
    return total


def check_artifacts(folder, grant, additional=0):
    from emuru.projects.coder_runtime import _artifact_size

    total = _artifact_size(folder) + additional
    if grant["schema"] == 3:
        previous, _ = parent(folder, grant)
        total += _artifact_size(previous)
    if total > ARTIFACT_BYTES:
        raise ValueError("Rework loop artifact budget exceeded.")


def authorize(home, vault, root, grant, *, starting=False):
    """Validate fresh owner authority against stopped, native-authenticated history."""
    from emuru.projects import board, coder_board, reviewer

    folder = coder_board.attempt_path(home, grant)
    previous, original, report, receipt = lineage(folder, grant)
    if starting and not coder_board._launcher_stopped(previous):
        raise ValueError("Parent launcher may still execute.")
    with board.project_board_readonly(home, root) as (kb, db):
        actual = reviewer.verify(
            home,
            vault,
            root,
            grant["task"],
            original,
            previous,
            kb,
            db,
            historical=True,
        )
        old_run = kb.get_run(db, receipt["run"])
        coder = kb.get_run(db, receipt["coder_run"])
        if (
            actual != receipt
            or old_run.outcome != "changes_requested"
            or old_run.ended_at is None
            or coder.ended_at is None
        ):
            raise ValueError("Parent native rejection is unverified.")
        if starting:
            card = kb.get_task(db, grant["task"])
            if (
                card.status != "ready"
                or card.assignee != coder_board.CODER
                or card.current_run_id is not None
                or kb.latest_run(db, card.id).id != receipt["run"]
            ):
                raise ValueError("Rework native ready/run binding changed.")
    return report, receipt


def link_value(grant):
    return {"schema": 1, "grant_id": grant["grant_id"], "grant_sha256": digest(grant)}


def preflight(folder, grant):
    from emuru.projects.coder_runtime import _artifact_size

    previous, original = parent(folder, grant)
    if (
        _artifact_size(previous) + len(encoded(link_value(grant)))
        > original["limits"]["artifact_bytes"]
    ):
        raise ValueError("Rework parent artifact budget exceeded.")


def link(folder, grant):
    from emuru.projects.reviewer import _save

    previous, _ = parent(folder, grant)
    _save(
        previous,
        "rework-intent.json",
        link_value(grant),
    )


def selected(matches):
    """Select by exact immutable lineage, never by timestamp."""
    from emuru.projects.coder import load_grant
    from emuru.projects.reviewer import _read

    if len(matches) != 2:
        raise ValueError("Multiple coder intents lack one-cycle lineage.")
    grants = [(p, load_grant(p / "grant.json")) for p in matches]
    children = [(p, g) for p, g in grants if g["schema"] == 3]
    if len(children) != 1:
        raise ValueError("Multiple coder intents lack one-cycle lineage.")
    folder, grant = children[0]
    previous, _ = parent(folder, grant)
    if previous not in matches or _read(previous / "rework-intent.json") != {
        "schema": 1,
        "grant_id": grant["grant_id"],
        "grant_sha256": digest(grant),
    }:
        raise ValueError("Rework intent lineage incomplete or changed.")
    return folder
