"""One explicitly admitted Scout; durable ownership, no replay or budget refund."""

import json
import os
import re
from pathlib import Path

from emuru.projects import supervisor
from emuru.projects.scout import MAX_REPORT_BYTES, digest, encoded
from emuru.projects.supervisor_controls import control_state
from emuru.vault import mutations

MAX_EVENTS = 32
METADATA_RESERVATION = MAX_EVENTS * supervisor.RECEIPT_BYTES
PROJECT_LIMITS = {
    "coder_runs": 2,
    "reviewer_runs": 2,
    "scout_runs": 1,
    "active_seconds": 1920,
    "coder_iterations": 24,
    "reviewer_iterations": 8,
    "checks": 8,
    "output_bytes": 640 * 1024,
    "artifact_bytes": 17 * 1024 * 1024 + MAX_REPORT_BYTES,
}


def launcher_identity():
    pid = os.getpid()
    return {
        "pid": pid,
        "start_time": Path(f"/proc/{pid}/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
        "boot_id": supervisor.boot_id(),
    }


def _valid_process(value):
    return (
        isinstance(value, dict)
        and set(value) == {"pid", "start_time", "boot_id"}
        and type(value["pid"]) is int
        and value["pid"] > 0
        and isinstance(value["start_time"], str)
        and value["start_time"].isdigit()
        and isinstance(value["boot_id"], str)
        and bool(value["boot_id"])
    )


def metadata_pending(records):
    total = 0
    for stage in (
        r
        for r in records
        if r["kind"] in {"stage", "coder-stage", "reviewer-stage"}
        and r["phase"] == "admitted"
    ):
        if stage["kind"] != "stage" and any(
            r.get("admission") == stage["generation"]
            and r.get("phase") in {"verified", "stopped", "awaiting-review"}
            for r in records
        ):
            continue
        used = sum(
            len(encoded(r))
            for r in records
            if r is stage or r.get("admission") == stage["generation"]
        )
        capacity = METADATA_RESERVATION
        if stage["kind"] in {"coder-stage", "reviewer-stage"}:
            from emuru.projects.supervisor_coder import METADATA_RESERVATION as capacity
        if used > capacity:
            raise ValueError("Scout metadata reservation exceeded.")
        total += capacity - used
    return total


def reserved(records, project):
    total = dict.fromkeys(supervisor.COUNTERS, 0)
    for record in records:
        if (
            record["kind"] in {"stage", "coder-stage", "reviewer-stage"}
            and record["phase"] == "admitted"
            and record["project"] == project
        ):
            for key in total:
                total[key] += record["reserved"][key]
    if any(total[k] > PROJECT_LIMITS[k] for k in total):
        raise ValueError("Project reservation exceeds approved ceiling.")
    return total


def unresolved(records):
    completed = {
        r.get("admission")
        for r in records
        if r.get("phase") == "verified"
        or r.get("phase") == "stopped"
        and r.get("native_task") is not None
    }
    reviewed = {
        r["grant_sha256"]
        for r in records
        if r["kind"] == "reviewer-stage"
        and r["phase"] == "admitted"
        and r["generation"] in completed
    }
    return any(
        stage["generation"] not in completed
        and not (stage["kind"] == "coder-stage" and stage["grant_sha256"] in reviewed)
        for stage in records
        if stage["kind"] in {"stage", "coder-stage", "reviewer-stage"}
        and stage["phase"] == "admitted"
    )


def validate_record(value, records):
    common = {
        "schema",
        "generation",
        "previous",
        "kind",
        "project",
        "task",
        "enrollment",
        "phase",
    }
    phase = value.get("phase")
    if phase == "admitted":
        expected_reservation = dict.fromkeys(supervisor.COUNTERS, 0)
        expected_reservation.update(
            scout_runs=1, active_seconds=120, artifact_bytes=MAX_REPORT_BYTES
        )
        fields = common | {
            "worker_grant_sha256",
            "report_sha256",
            "reserved",
            "started_ns",
            "deadline_ns",
            "launcher",
            "image",
        }
        if (
            set(value) != fields
            or not isinstance(value["reserved"], dict)
            or value["reserved"] != expected_reservation
            or set(value["reserved"]) != set(supervisor.COUNTERS)
            or any(
                type(value["reserved"][k]) is not int
                or not 0 <= value["reserved"][k] <= PROJECT_LIMITS[k]
                for k in supervisor.COUNTERS
            )
            or value["reserved"]["scout_runs"] != 1
            or value["reserved"]["active_seconds"] != 120
            or value["reserved"]["artifact_bytes"] != MAX_REPORT_BYTES
            or any(type(value[k]) is not int for k in ("started_ns", "deadline_ns"))
            or not _valid_process(value["launcher"])
            or not isinstance(value["image"], str)
            or not re.fullmatch("sha256:[0-9a-f]{64}", value["image"])
            or not 0 < value["deadline_ns"] - value["started_ns"] <= 120 * 10**9
            or any(
                not isinstance(value[k], str)
                or not re.fullmatch("[0-9a-f]{64}", value[k])
                for k in ("worker_grant_sha256", "report_sha256")
            )
        ):
            raise ValueError("Invalid Scout reservation or deadline.")
    else:
        fields = common | {"admission"}
        extensions = {
            "claim-started": set(),
            "claimed": {"native_task", "run"},
            "planned": {"name", "operation", "service"},
            "created": {"name", "container_id"},
            "container-stopped": {"name", "container_id"},
            "launcher-stopped": {"observer"},
            "verified": {"native_task", "run", "report_sha256"},
            "stopped": {"native_task", "run"},
            "client-planned": {"name"},
            "client-bound": {"name", "process"},
            "client-stopped": {"name", "process"},
            "stop-observed": {"name", "observed_ns"},
        }
        if phase not in extensions or set(value) != fields | extensions[phase]:
            raise ValueError("Invalid Scout lifecycle record.")
        original = next(
            (
                r
                for r in records
                if r["kind"] == "stage"
                and r["phase"] == "admitted"
                and r["generation"] == value["admission"]
            ),
            None,
        )
        if original is None or any(
            original[k] != value[k] for k in ("project", "task", "enrollment")
        ):
            raise ValueError("Scout lifecycle admission binding changed.")
        if (
            sum(
                r.get("admission") == value["admission"] or r is original
                for r in records
            )
            >= MAX_EVENTS
        ):
            raise ValueError("Scout lifecycle metadata exhausted.")
        if phase in {"created", "container-stopped"} and (
            not re.fullmatch("[0-9a-f]{64}", value["container_id"])
            or not re.fullmatch("emuru-scout-[0-9a-f]{32}", value["name"])
        ):
            raise ValueError("Invalid bound Scout container identity.")
        prior = [r for r in records if r.get("admission") == value["admission"]]
        if any(r["phase"] in {"verified", "stopped"} for r in prior):
            raise ValueError("Terminal Scout lifecycle cannot be reopened.")
        if "name" in value and (
            not isinstance(value["name"], str)
            or not re.fullmatch("emuru-scout-[0-9a-f]{32}", value["name"])
        ):
            raise ValueError("Invalid Scout operation identity.")
        if phase == "planned":
            if (
                value["service"] not in {"scout", "ledger"}
                or value["operation"]
                not in {"inspect", "claim", "finish", "inspect-stage", "stop"}
                or len(prior) > MAX_EVENTS - 8
            ):
                raise ValueError("Scout operation or remaining stop capacity invalid.")
            if any(r.get("name") == value["name"] for r in prior):
                raise ValueError("Scout operation already attempted.")
        if phase in {"created", "client-planned"} and not any(
            r.get("name") == value["name"]
            and r["phase"] == ("planned" if phase == "created" else "created")
            for r in prior
        ):
            raise ValueError("Scout creation lacks durable intent or identity.")
        if phase == "container-stopped" and not any(
            r["phase"] == "created"
            and (r["name"], r["container_id"]) == (value["name"], value["container_id"])
            for r in prior
        ):
            raise ValueError("Scout stop proof does not match recorded container.")
        if phase in {"client-bound", "client-stopped"}:
            if not _valid_process(value["process"]):
                raise ValueError("Scout client process identity invalid.")
            if not any(
                r["phase"]
                == ("client-planned" if phase == "client-bound" else "client-bound")
                and r["name"] == value["name"]
                and (phase == "client-bound" or r["process"] == value["process"])
                for r in prior
            ):
                raise ValueError("Scout client lacks matching process intent.")
        if phase == "launcher-stopped" and (
            not _valid_process(value["observer"])
            or value["observer"] == original["launcher"]
        ):
            raise ValueError("Independent original-launcher stop observation required.")
        if phase == "stop-observed" and (
            type(value["observed_ns"]) is not int
            or value["observed_ns"] < 0
            or not any(
                r["phase"] == "client-planned" and r["name"] == value["name"]
                for r in prior
            )
        ):
            raise ValueError("Scout stop observation lacks a bounded process wait.")
        if phase in {"claimed", "verified", "stopped"} and (
            not isinstance(value["native_task"], str)
            or not re.fullmatch("t_[0-9a-f]+", value["native_task"])
            or type(value["run"]) is not int
            or value["run"] <= 0
        ):
            raise ValueError("Scout native task/run identity invalid.")
        if phase == "claimed" and any(r["phase"] == "claimed" for r in prior):
            raise ValueError("Scout claim receipt already committed.")
        if phase == "claimed" and not any(r["phase"] == "claim-started" for r in prior):
            raise ValueError("Scout claim lacks its one-way native admission intent.")
        if phase == "claim-started" and any(
            r["phase"] == "claim-started" for r in prior
        ):
            raise ValueError("Scout native admission already attempted.")
        if phase in {"verified", "stopped"} and not any(
            r["phase"] == "claimed"
            and (r["native_task"], r["run"]) == (value["native_task"], value["run"])
            for r in prior
        ):
            raise ValueError(
                "Scout terminal receipt lacks exact original run association."
            )
        if phase == "verified" and (
            value["report_sha256"] != original["report_sha256"]
            or not all_stopped(records, original)
        ):
            raise ValueError(
                "Verified Scout requires exact report and complete process stop proof."
            )
        if phase == "stopped" and (
            not all_stopped(records, original)
            or not any(r["phase"] == "launcher-stopped" for r in prior)
        ):
            raise ValueError(
                "Scout stop requires independent launcher/client/container proof."
            )
    if not any(
        r["kind"] == "enrollment"
        and r["stage"] == "scout"
        and r["generation"] == value.get("enrollment")
        and all(r[k] == value[k] for k in ("project", "task"))
        for r in records
    ):
        raise ValueError("Scout stage has no matching enrollment.")


def _stage(records, admission):
    return next(
        (
            r
            for r in records
            if r["kind"] == "stage"
            and r["phase"] == "admitted"
            and digest(r) == admission
        ),
        None,
    )


def _compatible(records, stage, root):
    enrollment = next(r for r in records if r["generation"] == stage["enrollment"])
    if stage["image"] != enrollment["identity"]["image"] or enrollment[
        "identity"
    ] != supervisor.identity(root, stage["image"]):
        raise ValueError("Scout source/image/pin/schema incompatible.")
    return enrollment


def _event(home, records, size, stage, phase, **fields):
    value = {
        "kind": "stage",
        "phase": phase,
        "project": stage["project"],
        "task": stage["task"],
        "enrollment": stage["enrollment"],
        "admission": stage["generation"],
        **fields,
    }
    validate_record(
        {
            "schema": 1,
            "generation": len(records) + 1,
            "previous": digest(records[-1]),
            **value,
        },
        records,
    )
    return supervisor._append(
        Path(home) / "emuru-projects/supervisor", records, size, value
    )[0]


def _eligible(records, home, vault, root, policy, task, image):
    from emuru.projects.supervisor_lifecycle import require_admission

    require_admission(records)
    enrollment = next(
        (
            r
            for r in reversed(records)
            if r["kind"] == "enrollment" and r["task"] == task
        ),
        None,
    )
    if (
        enrollment is None
        or enrollment["stage"] != "scout"
        or enrollment["grant_sha256"] != digest(policy)
    ):
        raise ValueError("Current exact Scout enrollment required.")
    if (
        enrollment["identity"] != supervisor.identity(root, image)
        or enrollment["boot_id"] != supervisor.boot_id()
        or supervisor.time.monotonic_ns() >= enrollment["deadline_ns"]
    ):
        raise ValueError("Scout enrollment expired or incompatible.")
    if control_state(records, enrollment) != "unrestricted":
        raise ValueError("Scout enrollment held or revoked; no claim admitted.")
    authority = enrollment.get("authority")
    if authority is None or authority["vault"] != os.path.abspath(vault):
        raise ValueError("Supervised Scout needs its exact bound vault.")
    preview = mutations.approved_project(
        home,
        vault,
        enrollment["proposal"],
        authority["owner"],
        authority["bot"],
        readonly=True,
    )
    if (
        not preview["current"]
        or preview["after_revision"] != authority["revision"]
        or preview["approval_digest"] != authority["approval"]
    ):
        raise ValueError("Scout approved specification changed.")
    if unresolved(records):
        raise ValueError("Prior stage ownership uncertain; no new admission.")
    used = reserved(records, enrollment["project"])
    if used["scout_runs"]:
        raise ValueError(
            "Project Scout budget exhausted: one run already reserved; no refund."
        )
    return enrollment


def preflight(home, vault, root, policy, task, image):
    """Read-only first: attempted/restricted work cannot even replay source preparation."""
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        _eligible(records, home, vault, root, policy, task, image)


def reserve(home, vault, root, policy, task, image, worker_grant):
    """Caller holds the lifetime lock; only local facts/writes under the short lock."""
    launcher = launcher_identity()
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        enrollment = _eligible(records, home, vault, root, policy, task, image)
        started = supervisor.time.monotonic_ns()
        allowance = dict.fromkeys(supervisor.COUNTERS, 0)
        allowance.update(
            scout_runs=1, active_seconds=120, artifact_bytes=MAX_REPORT_BYTES
        )
        return supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "stage",
                "phase": "admitted",
                "project": enrollment["project"],
                "task": task,
                "enrollment": enrollment["generation"],
                "worker_grant_sha256": digest(worker_grant),
                "report_sha256": worker_grant["report_sha256"],
                "reserved": allowance,
                "started_ns": started,
                "deadline_ns": started + policy["timeout_seconds"] * 10**9,
                "launcher": launcher,
                "image": image,
            },
        )[0]


def stop_requested(home, admission):
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stage = _stage(records, admission)
        if stage is None:
            raise ValueError("Scout admission missing.")
        enrollment = next(r for r in records if r["generation"] == stage["enrollment"])
        return control_state(records, enrollment) == "revoked"


def event(home, admission, phase, **fields):
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        stage = _stage(records, admission)
        if stage is None:
            raise ValueError("Scout admission missing.")
        existing = next(
            (
                r
                for r in records
                if r.get("admission") == stage["generation"]
                and r.get("phase") == phase
                and ("name" not in fields or r.get("name") == fields["name"])
            ),
            None,
        )
        if existing is not None:
            if any(existing.get(key) != value for key, value in fields.items()):
                raise ValueError("Scout lifecycle receipt cannot change.")
            return existing
        return _event(home, records, size, stage, phase, **fields)


def claim(home, root, grant, admission):
    from emuru.projects import scout_board
    from emuru.projects.coder_board import ensure_idle

    ensure_idle(home, root, None)
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        stage = _stage(records, admission)
        if stage is None or stage["worker_grant_sha256"] != digest(grant):
            raise ValueError("Scout native claim requires exact admitted grant.")
        enrollment = _compatible(records, stage, root)
        if (
            control_state(records, enrollment) == "revoked"
            or stage["launcher"]["boot_id"] != supervisor.boot_id()
            or supervisor.time.monotonic_ns() >= stage["deadline_ns"]
        ):
            raise ValueError("Scout stopped or deadline exhausted before claim.")
        if any(
            r.get("admission") == stage["generation"]
            and r.get("phase") in {"claim-started", "claimed"}
            for r in records
        ):
            raise ValueError("Scout claim already attempted; never replay.")
        intent = _event(home, records, size, stage, "claim-started")
        size += len(encoded(intent))
        result = scout_board._claim_native(home, root, grant)
        if result["status"] != "claimed":
            raise ValueError("Scout native claim outcome uncertain; no replay.")
        _event(
            home,
            records,
            size,
            stage,
            "claimed",
            native_task=result["task"],
            run=result["run"],
        )
        return result


def worker_stopped(records, stage):
    plans = [
        r
        for r in records
        if r.get("admission") == stage["generation"]
        and r.get("phase") == "planned"
        and r["service"] == "scout"
    ]
    return bool(plans) and all(
        any(
            r.get("admission") == stage["generation"]
            and r.get("phase") == "container-stopped"
            and r["name"] == p["name"]
            for r in records
        )
        for p in plans
    )


def all_stopped(records, stage, *, exclude=None):
    events = [
        r
        for r in records
        if r.get("admission") == stage["generation"] and r.get("name") != exclude
    ]
    plans = [r for r in events if r["phase"] == "planned"]
    clients = [r for r in events if r["phase"] == "client-planned"]
    return all(
        any(
            r["phase"] == "container-stopped" and r["name"] == plan["name"]
            for r in events
        )
        for plan in plans
    ) and all(
        any(
            r["phase"] == "client-stopped" and r["name"] == plan["name"] for r in events
        )
        for plan in clients
    )


def finish(home, root, request):
    from emuru.projects import scout_board

    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stage = _stage(records, request.get("admission"))
        if stage is None or not worker_stopped(records, stage):
            raise ValueError("Independent Scout worker stop proof required.")
        enrollment = _compatible(records, stage, root)
        if control_state(records, enrollment) == "revoked":
            raise ValueError(
                "Stop requested; retain native claim for explicit reconciliation."
            )
        claim = next(
            (
                r
                for r in records
                if r.get("admission") == stage["generation"]
                and r.get("phase") == "claimed"
            ),
            None,
        )
        if (
            claim is None
            or (request["task"], request["run"]) != (claim["native_task"], claim["run"])
            or digest(request.get("report")) != stage["report_sha256"]
        ):
            raise ValueError("Scout report or native run binding changed.")
        return scout_board._finish(home, root, request)


def load(home, policy, task):
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stages = [
            r
            for r in records
            if r["kind"] == "stage" and r["phase"] == "admitted" and r["task"] == task
        ]
        stage = stages[-1] if stages else None
        enrollment = next(
            (
                r
                for r in records
                if stage is not None and r["generation"] == stage["enrollment"]
            ),
            None,
        )
        if stage is None or enrollment["grant_sha256"] != digest(policy):
            raise ValueError("Exact Scout policy/admission required for inspection.")
        return stage, records


def save_report(home, admission, report):
    from emuru.models.gateway import write_private
    from emuru.projects.coder_runtime import _read_private

    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stage = _stage(records, admission)
        data = encoded(report)
        if (
            stage is None
            or digest(report) != stage["report_sha256"]
            or len(data) > MAX_REPORT_BYTES
        ):
            raise ValueError("Scout durable report bound or digest invalid.")
    folder = Path(home) / "emuru-projects/scout-artifacts"
    with mutations.directory(folder.parent) as parent:
        mutations.private_directory(parent)
        try:
            os.mkdir(folder.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    target = folder / (stage["report_sha256"] + ".json")
    try:
        if _read_private(target, MAX_REPORT_BYTES) != data:
            raise ValueError("Existing immutable Scout report changed.")
    except FileNotFoundError:
        write_private(target, data.decode())
        with mutations.directory(folder) as parent:
            os.fsync(parent)


def inspect_native(home, root, request):
    """Explicit metadata inspection/stop; never claim or re-execute work."""
    from emuru.projects import board, scout_board

    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        stage = _stage(records, request["admission"])
        if stage is None:
            raise ValueError("Scout admission missing.")
        _compatible(records, stage, root)
        with board.project_board_readonly(home, root) as (kb, db):
            children = []
            for card in kb.list_tasks(
                db, tenant=stage["project"], include_archived=True
            ):
                binding = json.loads(card.body)
                if not isinstance(binding, dict) or binding.get("schema") != 1:
                    raise ValueError("Native Scout project binding invalid.")
                if (
                    isinstance(binding.get("scout"), dict)
                    and digest(binding["scout"]) == stage["worker_grant_sha256"]
                ):
                    _native_card(stage, card)
                    children.append(card)
            if len(children) != 1:
                return {"status": "uncertain", "task": None, "run": None}
            card = children[0]
            claimed = next(
                (
                    r
                    for r in records
                    if r.get("admission") == stage["generation"]
                    and r.get("phase") == "claimed"
                ),
                None,
            )
            run = (
                card.current_run_id
                or json.loads(card.result or "{}").get("run")
                or (claimed["run"] if claimed else None)
            )
            native = kb.get_run(db, run) if run else None
            latest = kb.latest_run(db, card.id)
            if (
                native is None
                or native.task_id != card.id
                or native.profile != "emuru-scout"
                or latest is None
                or latest.id != run
            ):
                raise ValueError("Scout native run association uncertain.")
            if card.status == "done":
                result = scout_board.status(kb, db, card, home)
                if (
                    result["evidence"]["run"] != run
                    or result["evidence"]["sha256"] != stage["report_sha256"]
                    or native.ended_at is None
                    or native.outcome != "completed"
                ):
                    raise ValueError("Scout completion evidence changed.")
                return {**result, "run": run}
            if request["action"] == "inspect-stage":
                return {"status": "uncertain", "task": card.id, "run": run}
            if not any(
                r.get("admission") == stage["generation"]
                and r.get("phase") == "launcher-stopped"
                for r in records
            ) or not all_stopped(records, stage, exclude=request.get("controller")):
                raise ValueError(
                    "Independent full stop proof required before native block."
                )
            if (
                card.current_run_id is None
                and native.ended_at is not None
                and native.outcome == "blocked"
            ):
                return {
                    "status": "uncertain",
                    "task": card.id,
                    "run": run,
                    "stop": "proven",
                }
        with board.project_board(home, root) as (kb, db):
            card = kb.get_task(db, card.id)
            if (
                card is None
                or card.status != "running"
                or card.claim_lock != "emuru-scout"
                or card.current_run_id != run
            ):
                raise ValueError("Scout native ownership changed; no transition.")
            if not kb.block_task(
                db,
                card.id,
                reason="Operator-confirmed process stop; effects uncertain. No automatic retry.",
                expected_run_id=run,
            ):
                raise ValueError("Scout native run-fenced block refused.")
        return {"status": "uncertain", "task": card.id, "run": run, "stop": "proven"}


def projection(home, root, records, enrollment):
    stages = [
        r
        for r in records
        if r["kind"] == "stage"
        and r["phase"] == "admitted"
        and r["enrollment"] == enrollment["generation"]
    ]
    if not stages:
        return {}
    stage = stages[-1]
    events = [r for r in records if r.get("admission") == stage["generation"]]
    final = next(
        (r for r in reversed(events) if r["phase"] in {"verified", "stopped"}), None
    )
    result = {
        "execution": "finished"
        if final and final["phase"] == "verified"
        else "uncertain",
        "stop_requested": control_state(records, enrollment) == "revoked",
        "stage_deadline_ns": stage["deadline_ns"],
        "adapter": "scout",
        "stop_proof": "recorded worker/container stop"
        if worker_stopped(records, stage)
        else "unavailable",
    }
    claimed = next((r for r in events if r["phase"] == "claimed"), None)
    if claimed:
        from emuru.projects import board, scout_board

        with board.project_board_readonly(home, root) as (kb, db):
            child = kb.get_task(db, claimed["native_task"])
            if child is None:
                raise ValueError("Supervised Scout native child missing.")
            _native_card(stage, child)
            evidence = scout_board.status(kb, db, child, home)
            actual_run = kb.get_run(db, claimed["run"])
            latest = kb.latest_run(db, child.id)
            if (
                actual_run is None
                or actual_run.task_id != child.id
                or actual_run.profile != "emuru-scout"
                or latest is None
                or latest.id != claimed["run"]
            ):
                raise ValueError("Supervised Scout native run changed.")
            result.update(native_task=child.id, stage_native=child.status)
            if (
                evidence.get("evidence", {}).get("sha256") == stage["report_sha256"]
                and evidence["evidence"]["run"] == claimed["run"]
                and actual_run.ended_at is not None
                and actual_run.outcome == "completed"
            ):
                result["verified_outcome"] = "verified read-only scout report"
            elif final is not None and final["phase"] == "verified":
                raise ValueError(
                    "Recorded Scout verification no longer matches native run/evidence."
                )
    return result


def _native_card(stage, card):
    binding = json.loads(card.body)
    grant = binding.get("scout") if isinstance(binding, dict) else None
    if (
        card.created_by != "emuru-scout-owner"
        or card.assignee != "emuru-scout"
        or card.tenant != stage["project"]
        or not isinstance(grant, dict)
        or grant.get("source_task") != stage["task"]
        or digest(grant) != stage["worker_grant_sha256"]
        or grant.get("report_sha256") != stage["report_sha256"]
    ):
        raise ValueError("Native Scout actor/project/task/grant binding changed.")
