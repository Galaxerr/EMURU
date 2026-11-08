"""Same-card independent review and the single fresh-grant supervised rework."""

from pathlib import Path

from emuru.projects import board, coder_board, reviewer, supervisor, supervisor_coder
from emuru.projects.scout import digest
from emuru.projects.supervisor_controls import control_state
from emuru.projects.supervisor_scout import launcher_identity, reserved, unresolved


def allowance():
    # Review artifacts consume the existing coder/M5 artifact reservation once.
    return {
        **dict.fromkeys(supervisor.COUNTERS, 0),
        "reviewer_runs": 1,
        "active_seconds": reviewer.SECONDS,
        "reviewer_iterations": reviewer.ITERATIONS,
        "output_bytes": reviewer.OUTPUT_BYTES,
    }


def reserve(home, vault, root, grant, coder, image, *, held_intent=None):
    """Revalidate next-stage authority and serialize admission with owner controls."""
    identity = supervisor.identity(root, image)
    with board.project_board_readonly(home, root) as (kb, db):
        if coder_board._active_work(kb, db, grant["task"]):
            raise ValueError("Another native stage is active or unresolved.")
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        if not any(
            r.get("admission") == coder["generation"]
            and r.get("phase") == "awaiting-review"
            for r in records
        ):
            raise ValueError("Checked coder handoff required before review admission.")
        enrollment = next(r for r in records if r["generation"] == coder["enrollment"])
        if (
            enrollment["grant_sha256"] != digest(grant)
            or enrollment["identity"] != identity
            or enrollment["boot_id"] != supervisor.boot_id()
            or supervisor.time.monotonic_ns() >= enrollment["deadline_ns"]
            or control_state(records, enrollment) == "revoked"
            or enrollment["authority"]["vault"] != str(Path(vault).absolute())
        ):
            raise ValueError(
                "Current unrestricted, unchanged reviewer enrollment required."
            )
        if any(
            r["kind"] == "reviewer-stage"
            and r["phase"] == "admitted"
            and r["grant_sha256"] == digest(grant)
            for r in records
        ):
            raise ValueError(
                "Reviewer already attempted; no replacement or deadline reset."
            )
        if control_state(records, enrollment) == "held":
            if held_intent is None:
                raise ValueError("Current unrestricted reviewer enrollment required.")
            target = coder_board.attempt_path(home, grant) / "held-before-review.json"
            if target.exists():
                if reviewer._read(target) != held_intent:
                    raise ValueError("Held-before-review intent changed.")
            else:
                reviewer._save(target.parent, target.name, held_intent)
            return None
        # Only this completely checked, stopped coder may retain handoff ownership.
        others = [
            r
            for r in records
            if r is not coder
            and not (
                r["kind"] == "coder-stage" and r["generation"] == coder["generation"]
            )
        ]
        if unresolved(others):
            raise ValueError("Prior stage ownership uncertain; no reviewer admission.")
        charge = allowance()
        used = reserved(records, grant["project"])
        from emuru.projects.supervisor_scout import PROJECT_LIMITS

        if any(used[k] + charge[k] > PROJECT_LIMITS[k] for k in charge):
            raise ValueError("Project review budget exhausted; no refund.")
        started = supervisor.time.monotonic_ns()
        return supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "reviewer-stage",
                "phase": "admitted",
                "project": grant["project"],
                "task": grant["task"],
                "enrollment": enrollment["generation"],
                "grant_sha256": digest(grant),
                "reserved": charge,
                "image": image,
                "started_ns": started,
                "deadline_ns": started + reviewer.SECONDS * 10**9,
                "launcher": launcher_identity(),
            },
        )[0]


class ReviewStage(supervisor_coder.CoderStage):
    """Reuse the exact-process transport; reviewer capability admits models only."""

    def __init__(self, home, vault, root, grant):
        self.home, self.vault, self.root, self.grant = home, vault, root, grant
        with supervisor._locked(home) as (parent, size, marker):
            records, _ = supervisor._records(parent, size, marker)
        stage = next(
            (
                r
                for r in records
                if r["kind"] == "reviewer-stage"
                and r["phase"] == "admitted"
                and r["grant_sha256"] == digest(grant)
            ),
            None,
        )
        if stage is None or stage["reserved"] != allowance():
            raise ValueError("Exact reviewer admission/grant/reservation required.")
        self.stage = {**stage, "admission": digest(stage)}
        self.deadline = stage["deadline_ns"] / 10**9

    def admit(self, kind):
        if kind != "model":
            raise ValueError("Reviewer tools/checks forbidden.")
        with board.project_board_readonly(self.home, self.root) as (kb, db):
            reviewer._prepared(
                self.home,
                self.vault,
                self.root,
                self.grant["task"],
                self.grant,
                coder_board.attempt_path(self.home, self.grant),
                kb,
                db,
            )
        self.check()
        records = self.records()
        count = sum(
            r.get("admission") == self.stage["generation"]
            and r.get("phase") == "model-admitted"
            for r in records
        )
        self.event("model-admitted", count=count + 1)

    def claim(self, callback):
        if self.stage["launcher"] != launcher_identity():
            raise ValueError("Original reviewer launcher required; no replay.")
        with supervisor._locked(self.home, write=True) as (parent, size, marker):
            records, size = supervisor._records(parent, size, marker)
            enrollment = next(
                r for r in records if r["generation"] == self.stage["enrollment"]
            )
            if (
                control_state(records, enrollment) == "revoked"
                or supervisor.time.monotonic_ns() >= self.stage["deadline_ns"]
            ):
                raise ValueError(
                    "Reviewer stopped or original deadline exhausted before claim."
                )
            if any(
                r.get("admission") == self.stage["generation"]
                and r.get("phase") == "claim-started"
                for r in records
            ):
                raise ValueError("Reviewer claim already attempted; no replay.")
            value = {
                "kind": "reviewer-stage",
                "project": self.stage["project"],
                "task": self.stage["task"],
                "enrollment": self.stage["enrollment"],
                "admission": self.stage["generation"],
            }
            _, size = supervisor._append(
                Path(self.home) / "emuru-projects/supervisor",
                records,
                size,
                {**value, "phase": "claim-started"},
            )
            result = callback()
            supervisor._append(
                Path(self.home) / "emuru-projects/supervisor",
                records,
                size,
                {
                    **value,
                    "phase": "claimed",
                    "native_task": result["task"],
                    "run": result["run"],
                },
            )
            return result


def current_stage(home, vault, root, grant):
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    if any(
        r["kind"] == "reviewer-stage"
        and r["phase"] == "admitted"
        and r["grant_sha256"] == digest(grant)
        for r in records
    ):
        return ReviewStage(home, vault, root, grant)
    return supervisor_coder.CoderStage(home, vault, root, grant)


def handoff(coder, folder, run, *, first_review=False):
    """Only uninterrupted handoff or explicit never-claimed held intent can launch."""
    value, report, evidence = reviewer.packet(
        coder.home,
        coder.vault,
        coder.root,
        coder.grant["task"],
        coder.grant,
        folder,
        run,
    )
    reviewer._coder_usage(folder, coder.grant, report)
    intent = {
        "schema": 1,
        "coder_admission": coder.stage["admission"],
        "coder_run": run["run"],
        "grant_sha256": digest(coder.grant),
        "evidence_sha256": evidence,
        "packet_sha256": digest(value),
    }
    events = [
        r for r in coder.records() if r.get("admission") == coder.stage["generation"]
    ]
    if not supervisor_coder.resources_stopped(events) or not any(
        r["phase"] == "awaiting-review"
        and r["evidence_sha256"] == evidence
        and r["run"] == run["run"]
        for r in events
    ):
        raise ValueError("Complete checked coder handoff required.")
    if first_review:
        if reviewer._read(folder / "held-before-review.json") != intent:
            raise ValueError("Held-before-review intent changed.")
        if not coder_board.launcher_stopped(coder.stage["launcher"]):
            raise ValueError("Original coder launcher may still execute.")
    elif coder.stage["launcher"] != launcher_identity():
        raise ValueError("Uninterrupted original coder invocation required.")
    admission = reserve(
        coder.home,
        coder.vault,
        coder.root,
        coder.grant,
        coder.stage,
        report["image"],
        held_intent=intent,
    )
    if admission is None:
        return {
            "status": "awaiting_review",
            "detail": "Held before first review; continue then explicit --review required.",
        }
    stage = ReviewStage(coder.home, coder.vault, coder.root, coder.grant)
    reviewer.begin(
        coder.home,
        coder.vault,
        coder.root,
        coder.grant["task"],
        coder.grant,
        folder,
        run,
        supervision=stage,
    )
    return reviewer.execute(
        coder.home,
        coder.vault,
        coder.root,
        coder.grant["task"],
        coder.grant,
        folder,
        supervision=stage,
    )


def finalized(stage, folder, receipt):
    events = [
        r for r in stage.records() if r.get("admission") == stage.stage["generation"]
    ]
    if not supervisor_coder.resources_stopped(events):
        raise ValueError("Reviewer exact-resource stop proof required.")
    if receipt["model_calls"] != sum(r["phase"] == "model-admitted" for r in events):
        raise ValueError("Reviewer mandatory model accounting changed.")
    if reviewer._stage(folder / "review") != {
        "schema": 1,
        "boot_id": stage.stage["launcher"]["boot_id"],
        "started_ns": stage.stage["started_ns"],
        "deadline_ns": stage.stage["deadline_ns"],
    }:
        raise ValueError("Reviewer M5 stage deadline differs from supervisor.")
    if not any(r["phase"] == "verified" for r in events):
        stage.event(
            "verified",
            native_task=stage.grant["task"],
            run=receipt["run"],
            evidence_sha256=receipt["evidence_sha256"],
        )
    return receipt


def native_status(stage, folder, card, kb, db):
    records = stage.records()
    enrollment = next(
        r for r in records if r["generation"] == stage.stage["enrollment"]
    )
    if enrollment["identity"] != supervisor.identity(stage.root, stage.stage["image"]):
        raise ValueError("Reviewer source/image/pin/schema incompatible.")
    events = [r for r in records if r.get("admission") == stage.stage["generation"]]
    result = reviewer.status(kb, db, card, stage.home, stage.root, stage.grant, folder)
    if result and result["status"] in {
        "verified",
        "changes_requested",
        "review_rejected",
        "blocked",
    }:
        receipt = result["evidence"]
        if (
            not supervisor_coder.resources_stopped(events)
            or receipt["model_calls"]
            != sum(r["phase"] == "model-admitted" for r in events)
            or reviewer._stage(folder / "review")["deadline_ns"]
            != stage.stage["deadline_ns"]
        ):
            raise ValueError("Supervised reviewer completion proof incomplete.")
        return result
    claim = next((r for r in events if r["phase"] == "claimed"), None)
    if (
        claim
        and result
        and result["status"] == "awaiting_review"
        and card.status == "running"
        and card.current_run_id == claim["run"]
        and card.claim_lock == reviewer.REVIEWER
        and not coder_board.launcher_stopped(stage.stage["launcher"])
    ):
        import json

        from emuru.projects.coder_sandbox import bounded_process

        for created in (r for r in events if r["phase"] == "created"):
            if any(
                r["phase"] == "container-stopped" and r["name"] == created["name"]
                for r in events
            ):
                continue
            plan = next(
                r
                for r in events
                if r["phase"] == "planned" and r["name"] == created["name"]
            )
            if plan["service"] != "inference":
                continue
            observed = bounded_process(
                ["docker", "inspect", created["container_id"]], b"", 1, 65536
            )
            rows = json.loads(observed["stdout"]) if observed["exit_code"] == 0 else []
            if (
                len(rows) == 1
                and rows[0].get("Id") == created["container_id"]
                and rows[0].get("Image") == stage.stage["image"]
                and rows[0]
                .get("Config", {})
                .get("Labels", {})
                .get("io.emuru.coder.stage")
                == stage.stage["admission"]
                and rows[0].get("State", {}).get("Running") is True
            ):
                return {"status": "running", "task": card.id, "run": claim["run"]}
    return {
        "status": "uncertain",
        "detail": "Review attempted; inspect stop/evidence; no replacement or replay.",
    }


def reconcile(stage, folder):
    """After exact stop, accept complete evidence or block; never claim or infer."""
    events = [
        r for r in stage.records() if r.get("admission") == stage.stage["generation"]
    ]
    if not supervisor_coder.resources_stopped(events):
        raise ValueError("Reviewer stop unproven.")
    with board.project_board_readonly(stage.home, stage.root) as (kb, db):
        card = kb.get_task(db, stage.grant["task"])
        latest = kb.latest_run(db, card.id)
        runs = kb.list_runs(db, card.id)
        original = coder_board._read_json(folder / "run.json")
        claimed = next((r for r in events if r["phase"] == "claimed"), None)
        if (
            latest is None
            or latest.profile != reviewer.REVIEWER
            or latest.id <= original["run"]
            or len(runs) != (4 if stage.grant["schema"] == 3 else 2)
            or claimed
            and claimed["run"] != latest.id
        ):
            raise ValueError(
                "Reviewer native claim outcome unproven; preserve ownership."
            )
        run = latest.id
    if claimed is None:
        stage.event("claimed", native_task=card.id, run=run)
        if not (folder / "review/run.json").exists():
            reviewer._save(
                folder / "review",
                "run.json",
                {
                    "schema": 1,
                    "task": card.id,
                    "run": run,
                    "profile": reviewer.REVIEWER,
                },
            )
    with board.project_board_readonly(stage.home, stage.root) as (kb, db):
        reviewer._prepared(
            stage.home, stage.vault, stage.root, card.id, stage.grant, folder, kb, db
        )
    try:
        with board.project_board_readonly(stage.home, stage.root) as (kb, db):
            reviewer.verify(
                stage.home,
                stage.vault,
                stage.root,
                card.id,
                stage.grant,
                folder,
                kb,
                db,
            )
    except (OSError, ValueError, KeyError, TypeError):
        with board.project_board(stage.home, stage.root) as (kb, db):
            card = kb.get_task(db, card.id)
            if card.status == "running":
                if card.current_run_id != run or not kb.block_task(
                    db,
                    card.id,
                    reason="Reviewer stopped; incomplete evidence; no replay.",
                    expected_run_id=run,
                ):
                    raise ValueError("Reviewer stop transition refused.")
            elif card.status != "blocked" or latest.outcome != "blocked":
                raise ValueError("Reviewer native terminal mismatch.")
        stage.event(
            "stopped", native_task=card.id, run=run, observer=launcher_identity()
        )
        return {
            "status": "uncertain",
            "stop": "proven",
            "detail": "Review stopped; effects uncertain; no replay.",
        }
    with board.project_board_readonly(stage.home, stage.root) as (kb, db):
        receipt = reviewer.verify(
            stage.home, stage.vault, stage.root, card.id, stage.grant, folder, kb, db
        )
    return complete(stage, folder, receipt, reconciling=True)


def rework_predecessor(records, grant):
    """Fresh M5 lineage must also consume exactly one completed supervised pair."""
    binding = grant["rework"]
    stages = [
        r
        for r in records
        if r["kind"] in {"coder-stage", "reviewer-stage"}
        and r["phase"] == "admitted"
        and r["project"] == grant["project"]
    ]
    if (
        len(stages) != 2
        or {s["kind"] for s in stages} != {"coder-stage", "reviewer-stage"}
        or any(s["grant_sha256"] != binding["grant_sha256"] for s in stages)
        or any(
            not any(
                r.get("admission") == s["generation"]
                and r.get("phase")
                == ("verified" if s["kind"] == "reviewer-stage" else "awaiting-review")
                and r["run"]
                == binding[
                    "reviewer_run" if s["kind"] == "reviewer-stage" else "coder_run"
                ]
                for r in records
            )
            for s in stages
        )
    ):
        raise ValueError(
            "Exactly one completed supervised predecessor pair required for rework."
        )


def complete(stage, folder, receipt, *, reconciling=False):
    """Persist acceptance intent before the exact native verdict transition."""
    events = [
        r for r in stage.records() if r.get("admission") == stage.stage["generation"]
    ]
    if (
        not supervisor_coder.resources_stopped(events)
        or receipt["model_calls"] != sum(r["phase"] == "model-admitted" for r in events)
        or reviewer._stage(folder / "review")
        != {
            "schema": 1,
            "boot_id": stage.stage["launcher"]["boot_id"],
            "started_ns": stage.stage["started_ns"],
            "deadline_ns": stage.stage["deadline_ns"],
        }
    ):
        raise ValueError("Reviewer accounting/deadline/stop proof incomplete.")
    prior = next((r for r in events if r["phase"] == "completion-started"), None)
    if prior is not None and (
        prior["run"] != receipt["run"]
        or prior["evidence_sha256"] != receipt["evidence_sha256"]
        or prior["receipt_sha256"] != digest(receipt)
    ):
        raise ValueError("Reviewer completion intent changed.")
    if not reconciling:
        stage.check()
    with supervisor._locked(stage.home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        enrollment = next(
            r for r in records if r["generation"] == stage.stage["enrollment"]
        )
        if control_state(records, enrollment) == "revoked" and not (
            reconciling and prior
        ):
            raise InterruptedError("Reviewer stop requested before verdict acceptance.")
        if prior is None:
            supervisor._append(
                Path(stage.home) / "emuru-projects/supervisor",
                records,
                size,
                {
                    "kind": "reviewer-stage",
                    "phase": "completion-started",
                    "project": stage.stage["project"],
                    "task": stage.grant["task"],
                    "enrollment": stage.stage["enrollment"],
                    "admission": stage.stage["generation"],
                    "native_task": stage.grant["task"],
                    "run": receipt["run"],
                    "evidence_sha256": receipt["evidence_sha256"],
                    "receipt_sha256": digest(receipt),
                },
            )
        with board.project_board(stage.home, stage.root) as (kb, db):
            result = reviewer._accept(kb, db, stage.grant["task"], folder, receipt)

    return finalized(stage, folder, result.get("evidence", result))
