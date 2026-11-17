"""Finite operator allowances on the existing supervisor ledger; no dispatch.

Lock order: dispatcher, native board, supervisor. Controls take only supervisor.
Immutable manifests and all orphan bytes count toward existing metadata capacity.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from emuru.projects import board, continuation, supervisor
from emuru.projects.scout import digest, encoded
from emuru.vault import mutations

KINDS = {"batch-allowance", "batch-task", "batch-control"}
SCOPE = (
    "repository",
    "baseline",
    "read_files",
    "write_files",
    "create_files",
    "check_ids",
    "catalogue",
    "limits",
)


def additional(tasks, scout=0):
    """Section 5 maxima, respecting smaller coder grants; artifacts are per loop."""
    from emuru.projects import supervisor_reviewer

    total = dict.fromkeys(supervisor.COUNTERS, 0)
    for task in tasks:
        limits, attempts = task["limits"], task["attempts"]
        coder = {
            **dict.fromkeys(supervisor.COUNTERS, 0),
            "coder_runs": 1,
            "active_seconds": limits["attempt_seconds"],
            "coder_iterations": limits["model_iterations"],
            "checks": limits["check_executions"],
            "output_bytes": limits["total_output_bytes"],
            "artifact_bytes": limits["artifact_bytes"],
        }
        # Keep stage formulas in agreement with actual admission; review is fixed.
        review = supervisor_reviewer.allowance()
        for k in total:
            total[k] += attempts * (coder[k] + review[k])
        total["artifact_bytes"] += 17 * 1024**2 - attempts * limits["artifact_bytes"]
    total["scout_runs"] += scout
    total["active_seconds"] += 120 * scout
    total["artifact_bytes"] += 65536 * scout
    return total


def latest(records, project):
    return next(
        (
            r
            for r in reversed(records)
            if r["kind"] == "batch-allowance" and r["project"] == project
        ),
        None,
    )


def state(records, allowance):
    result = "unrestricted"
    for r in records:
        if r["kind"] == "batch-control" and r["allowance"] == allowance["generation"]:
            if result == "revoked":
                raise ValueError("Revoked batch history cannot change.")
            result = {"hold": "held", "continue": "unrestricted", "cancel": "revoked"}[
                r["action"]
            ]
    return result


def memberships(records, task):
    return [r for r in records if r["kind"] == "batch-task" and r["task"] == task]


def control_state(records, task):
    values = [
        state(records, records[r["allowance"] - 1]) for r in memberships(records, task)
    ]
    return (
        "revoked"
        if "revoked" in values
        else "held"
        if "held" in values
        else "unrestricted"
    )


def project_limits(records, project):
    from emuru.projects.supervisor_scout import PROJECT_LIMITS

    current = latest(records, project)
    return current["ceilings"] if current else PROJECT_LIMITS


def validate_record(value, records):
    common = {"schema", "kind", "generation", "previous", "project"}
    fields = {
        "batch-allowance": {
            "manifest",
            "binding_digest",
            "preview",
            "previous_allowance",
            "ceilings",
            "approval",
            "authority",
            "identity",
            "boot_id",
            "started_ns",
            "deadline_ns",
            "task_count",
        },
        "batch-task": {
            "allowance",
            "task",
            "logical_key",
            "contract_digest",
            "scope",
            "attempts",
        },
        "batch-control": {
            "allowance",
            "action",
            "request",
            "text_hash",
            "token",
            "response",
        },
    }
    kind = value["kind"]
    if value.get("schema") != 2 or set(value) != common | fields[kind]:
        raise ValueError("Invalid batch record; preserve newest state.")
    if kind == "batch-allowance":
        previous = latest(records, value["project"])
        if value["previous_allowance"] != (previous["manifest"] if previous else None):
            raise ValueError("Batch previous allowance changed.")
        if (
            not isinstance(value["ceilings"], dict)
            or set(value["ceilings"]) != set(supervisor.COUNTERS)
            or any(type(v) is not int or v < 0 for v in value["ceilings"].values())
            or type(value["task_count"]) is not int
            or not 1 <= value["task_count"] <= 20
            or any(type(value[k]) is not int for k in ("started_ns", "deadline_ns"))
            or not 0
            <= value["started_ns"]
            < value["deadline_ns"]
            <= value["started_ns"] + 86400 * 10**9
            or not isinstance(value["boot_id"], str)
            or not value["boot_id"]
            or not isinstance(value["authority"], dict)
            or set(value["authority"])
            != {"owner", "bot", "vault", "proposal", "revision", "approval"}
            or any(
                type(value["authority"][k]) is not int or value["authority"][k] <= 0
                for k in ("owner", "bot")
            )
            or not isinstance(value["authority"]["vault"], str)
            or not Path(value["authority"]["vault"]).is_absolute()
            or not isinstance(value["identity"], dict)
        ):
            raise ValueError("Invalid batch allowance authority or ceilings.")
        identity, authority = value["identity"], value["authority"]
        if (
            set(identity) != {"source", "image", "pin", "native", "schema"}
            or type(identity.get("schema")) is not int
            or identity["schema"] != 1
            or any(
                not isinstance(identity[k], str) or not re.fullmatch(p, identity[k])
                for k, p in (
                    ("source", "[0-9a-f]{64}"),
                    ("image", "sha256:[0-9a-f]{64}"),
                    ("pin", "[0-9a-f]{40}"),
                    ("native", "[0-9a-f]{64}"),
                )
            )
            or any(
                not isinstance(authority[k], str) or not re.fullmatch(p, authority[k])
                for k, p in (
                    ("proposal", "[0-9a-f]{32}"),
                    ("revision", "[0-9a-f]{64}"),
                    ("approval", "[0-9a-f]{64}"),
                )
            )
            or authority["vault"] != os.path.abspath(authority["vault"])
        ):
            raise ValueError("Invalid batch execution identity or owner authority.")
        for k in ("manifest", "binding_digest", "preview", "approval"):
            if not isinstance(value[k], str) or not re.fullmatch(
                "[0-9a-f]{64}", value[k]
            ):
                raise ValueError("Invalid batch digest.")
        if any(
            r["kind"] == kind and r["manifest"] == value["manifest"] for r in records
        ):
            raise ValueError("Duplicate batch allowance.")
    else:
        if type(value["allowance"]) is not int or not 1 <= value["allowance"] <= len(
            records
        ):
            raise ValueError("Missing batch allowance.")
        allowance = records[value["allowance"] - 1]
        if (
            allowance["kind"] != "batch-allowance"
            or allowance["project"] != value["project"]
        ):
            raise ValueError("Cross-project batch record.")
        if kind == "batch-task":
            if (
                type(value["attempts"]) is not int
                or value["attempts"] not in (1, 2)
                or not isinstance(value["logical_key"], str)
                or not re.fullmatch("[A-Za-z0-9-]{1,200}", value["logical_key"])
                or any(
                    not isinstance(value[k], str)
                    or not re.fullmatch("[0-9a-f]{64}", value[k])
                    for k in ("contract_digest", "scope")
                )
                or any(
                    r["kind"] == kind
                    and r["allowance"] == value["allowance"]
                    and r["task"] == value["task"]
                    for r in records
                )
            ):
                raise ValueError("Invalid or duplicate batch task.")
        elif (
            value["action"] not in {"hold", "continue", "cancel"}
            or any(
                not isinstance(value[k], str) or not re.fullmatch(p, value[k])
                for k, p in (
                    ("request", "[0-9a-f]{32}"),
                    ("text_hash", "[0-9a-f]{64}"),
                    ("token", "[0-9a-f]{64}"),
                )
            )
            or not isinstance(value["response"], str)
            or len(value["response"]) > 3500
            or state(records, allowance) == "revoked"
        ):
            raise ValueError("Invalid batch control.")


def _payload(home, allowance):
    report = continuation._manifest(home, identity=allowance["preview"])
    if (
        digest(report["manifest"]) != allowance["manifest"]
        or report["digest"] != allowance["approval"]
        or digest({k: v for k, v in report.items() if k != "digest"})
        != report["digest"]
    ):
        raise ValueError("Immutable batch manifest/preview identity changed.")
    return {"manifest": report["manifest"], "preview": report}


def load_manifest(path):
    from emuru.projects.coder_board import _read_json

    return _read_json(path)


def _shape(manifest, policy):
    from emuru.projects.coder import validate_grant

    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema",
            "project",
            "proposal",
            "binding_digest",
            "tasks",
            "scout",
            "previous_allowance",
            "ceilings",
        }
        or type(manifest["schema"]) is not int
        or manifest["schema"] != 1
        or type(manifest["scout"]) is not int
        or manifest["scout"] not in (0, 1)
        or not isinstance(manifest["tasks"], list)
        or not 1 <= len(manifest["tasks"]) <= 20
        or len(encoded(manifest)) > 32768
    ):
        raise ValueError("Invalid bounded batch manifest.")
    for key, pattern in (
        ("project", "[0-9a-f]{64}"),
        ("proposal", "[0-9a-f]{32}"),
        ("binding_digest", "[0-9a-f]{64}"),
    ):
        if not isinstance(manifest[key], str) or not re.fullmatch(
            pattern, manifest[key]
        ):
            raise ValueError("Invalid batch binding.")
    if manifest["previous_allowance"] is not None and (
        not isinstance(manifest["previous_allowance"], str)
        or not re.fullmatch("[0-9a-f]{64}", manifest["previous_allowance"])
    ):
        raise ValueError("Invalid previous allowance.")
    if (
        not isinstance(manifest["ceilings"], dict)
        or set(manifest["ceilings"]) != set(supervisor.COUNTERS)
        or any(type(v) is not int or v < 0 for v in manifest["ceilings"].values())
    ):
        raise ValueError("Invalid batch cumulative ceilings.")
    seen = set()
    for entry in manifest["tasks"]:
        if (
            not isinstance(entry, dict)
            or set(entry)
            != set(SCOPE)
            | {"logical_key", "contract_digest", "native_task", "attempts"}
            or type(entry["attempts"]) is not int
            or entry["attempts"] not in (1, 2)
            or not isinstance(entry["logical_key"], str)
            or not re.fullmatch("[A-Za-z0-9-]{1,200}", entry["logical_key"])
            or not isinstance(entry["contract_digest"], str)
            or not re.fullmatch("[0-9a-f]{64}", entry["contract_digest"])
            or not isinstance(entry["native_task"], str)
            or not re.fullmatch("t_[0-9a-f]+", entry["native_task"])
            or entry["native_task"] in seen
            or entry["repository"] != policy["repository"]
            or entry["baseline"] != policy["baseline"]
        ):
            raise ValueError("Invalid, duplicate or unapproved batch task scope.")
        seen.add(entry["native_task"])
        candidate = {
            **policy,
            **{k: entry[k] for k in SCOPE},
            "task": entry["native_task"],
        }
        validate_grant(candidate)
        # The supplied private policy is the operator allowlist, not an expansion token.
        if (
            any(
                not set(entry[k]) <= set(policy[k])
                for k in ("read_files", "write_files", "create_files", "check_ids")
            )
            or any(entry["limits"][k] > policy["limits"][k] for k in policy["limits"])
            or entry["catalogue"] != policy["catalogue"]
        ):
            raise ValueError("Batch scope exceeds operator policy.")


def _consumed(records, project, task=None):
    total = dict.fromkeys(supervisor.COUNTERS, 0)
    for r in records:
        if (
            r["kind"] in {"stage", "coder-stage", "reviewer-stage"}
            and r["phase"] == "admitted"
            and r["project"] == project
            and (task is None or r["task"] == task and r["kind"] != "stage")
        ):
            for k in total:
                total[k] += r["reserved"][k]
    return total


def _account(home, records, manifest, policy, completed):
    """Existing consumed C plus still authorized U plus genuinely new D, once."""
    project = manifest["project"]
    consumed = _consumed(records, project)
    active = {}
    for r in records:
        if r["kind"] == "batch-task" and r["project"] == project:
            active[r["task"]] = r
    unspent, new = (dict.fromkeys(supervisor.COUNTERS, 0) for _ in range(2))
    # M6 enrollments still authorize their exact remaining stage, even outside this selection.
    legacy = {}
    from emuru.projects import coder_board, supervisor_coder, supervisor_reviewer
    from emuru.projects.coder import load_grant
    from emuru.projects.supervisor_controls import control_state as task_control

    scout_allocated = any(
        r["kind"] == "batch-allowance"
        and r["project"] == project
        and _payload(home, r)["manifest"]["scout"]
        for r in records
    )
    enrollments = {
        (r["task"], r["stage"]): r
        for r in records
        if r["kind"] == "enrollment" and r["project"] == project
    }
    for (task, stage), enrollment in enrollments.items():
        if (
            stage == "coder"
            and task in active
            or stage == "scout"
            and scout_allocated
            or task in completed
            or task_control(records, enrollment) == "revoked"
        ):
            continue
        if stage == "scout":
            capacity = {
                **dict.fromkeys(supervisor.COUNTERS, 0),
                "scout_runs": 1,
                "active_seconds": 120,
                "artifact_bytes": 65536,
            }
        else:
            folder = coder_board._find_attempt(home, task)
            grant = (
                policy
                if enrollment["grant_sha256"] == digest(policy)
                else load_grant(folder / "grant.json")
                if folder
                else None
            )
            if grant is None or digest(grant) != enrollment["grant_sha256"]:
                raise ValueError(
                    "Prior M6 unspent grant scope unavailable; preserve authority."
                )
            capacity = supervisor_coder.allowance(grant)
            if grant["schema"] in {2, 3}:
                review = supervisor_reviewer.allowance()
                capacity = {k: capacity[k] + review[k] for k in capacity}
        spent = {k: 0 for k in supervisor.COUNTERS}
        for r in records:
            if (
                r.get("enrollment") == enrollment["generation"]
                and r.get("phase") == "admitted"
            ):
                for k in spent:
                    spent[k] += r["reserved"][k]
        remaining = {k: max(0, capacity[k] - spent[k]) for k in capacity}
        if stage == "coder":
            legacy[task] = {
                k: legacy.get(task, dict.fromkeys(supervisor.COUNTERS, 0))[k]
                + remaining[k]
                for k in remaining
            }
        for k in unspent:
            unspent[k] += remaining[k]
    for task, record in active.items():
        allowance = records[record["allowance"] - 1]
        envelope = _payload(home, allowance)["manifest"]
        entry = next(t for t in envelope["tasks"] if t["native_task"] == task)
        spent = _consumed(records, project, task)
        capacity = additional([entry])
        if task not in completed and control_state(records, task) != "revoked":
            for k in unspent:
                unspent[k] += max(0, capacity[k] - spent[k])
    for entry in manifest["tasks"]:
        task = entry["native_task"]
        spent = _consumed(records, project, task)
        prior = active.get(task)
        if task in completed:
            continue
        if prior:
            if (
                prior["contract_digest"] != entry["contract_digest"]
                or prior["scope"] != digest({k: entry[k] for k in SCOPE})
                or control_state(records, task) != "unrestricted"
                or entry["attempts"] < prior["attempts"]
            ):
                raise ValueError("Restricted batch or immutable task scope changed.")
            delta = entry["attempts"] - prior["attempts"]
            if delta:
                amount = additional([{**entry, "attempts": delta}])
                amount["artifact_bytes"] = 0
            else:
                amount = dict.fromkeys(supervisor.COUNTERS, 0)
        else:
            if (
                spent["coder_runs"] > entry["attempts"]
                or spent["reviewer_runs"] > entry["attempts"]
            ):
                raise ValueError("Batch cannot authorize a third task attempt.")
            capacity = additional([entry])
            amount = {
                k: max(0, capacity[k] - spent[k] - legacy.get(task, {}).get(k, 0))
                for k in capacity
            }
        for k in new:
            new[k] += amount[k]
    old_scout = scout_allocated
    if manifest["scout"] and (
        old_scout
        or consumed["scout_runs"]
        or any(stage == "scout" for _, stage in enrollments)
    ):
        raise ValueError("A second Scout slot is forbidden.")
    if (
        old_scout
        and not consumed["scout_runs"]
        and any(
            r["kind"] == "batch-allowance"
            and r["project"] == project
            and state(records, r) != "revoked"
            and _payload(home, r)["manifest"]["scout"]
            for r in records
        )
    ):
        unspent["scout_runs"] += 1
        unspent["active_seconds"] += 120
        unspent["artifact_bytes"] += 65536
    if manifest["scout"]:
        new["scout_runs"] += 1
        new["active_seconds"] += 120
        new["artifact_bytes"] += 65536
    ceilings = {k: consumed[k] + unspent[k] + new[k] for k in consumed}
    return consumed, unspent, new, ceilings


def _preview(home, vault, root, manifest, policy, image, records, kb, db):
    from emuru.projects.coder_runtime import catalogue_digest
    from emuru.projects.scout import git

    _shape(manifest, policy)
    if (
        manifest["project"] != policy["project"]
        or manifest["proposal"] != policy["proposal"]
    ):
        raise ValueError("Batch project/policy mismatch.")
    current = latest(records, manifest["project"])
    if manifest["previous_allowance"] != (current["manifest"] if current else None):
        raise ValueError("Stale previous allowance.")
    if (
        git(Path(policy["path"]), "rev-parse", "--verify", "HEAD").decode().strip()
        != policy["baseline"]
        or policy["catalogue"] != catalogue_digest()
    ):
        raise ValueError("Batch baseline/catalogue changed.")
    preview = mutations.approved_project(
        home, vault, manifest["proposal"], policy["owner"], policy["bot"], readonly=True
    )
    labels = [
        json.loads(kb.get_task(db, e["native_task"]).body)["task"]["id"]
        for e in manifest["tasks"]
        if kb.get_task(db, e["native_task"])
    ]
    if len(labels) != len(manifest["tasks"]):
        raise ValueError("Batch native task missing.")
    plan = continuation._snapshot(
        home,
        vault,
        root,
        preview,
        policy["owner"],
        policy["bot"],
        labels,
        kb,
        db,
        records,
    )
    complete = next(
        (
            r
            for r in records
            if r["kind"] == "binding-complete"
            and r["project"] == manifest["project"]
            and r["binding_digest"] == manifest["binding_digest"]
        ),
        None,
    )
    if complete is None:
        raise ValueError("Complete bound selection required.")
    bound_plan = continuation._manifest(
        home, identity=records[complete["intent"] - 1]["manifest"]
    )
    for entry, label in zip(manifest["tasks"], labels, strict=True):
        mapping = plan["mappings"][label]
        old = bound_plan["mappings"].get(label)
        if (
            old is None
            or any(
                entry[k] != mapping[m] or entry[k] != old[m]
                for k, m in (
                    ("logical_key", "logical_key"),
                    ("contract_digest", "contract_digest"),
                )
            )
            or not any(
                r["kind"] == "task-binding"
                and r["intent"] == complete["intent"]
                and r["task"] == entry["native_task"]
                and r["contract_digest"] == entry["contract_digest"]
                for r in records
            )
        ):
            raise ValueError("Batch task binding or contract changed.")
        card = kb.get_task(db, entry["native_task"])
        expected = {
            next(
                r["task"]
                for r in reversed(records)
                if r["kind"] == "task-binding"
                and r["project"] == manifest["project"]
                and r["logical_key"] == key
                and r["contract_digest"] == contract
            )
            for key, contract in mapping["contract"]["dependencies"]
        }
        if set(kb.parent_ids(db, card.id)) != expected:
            raise ValueError("Batch native dependency readback changed.")
    completed = set()
    from emuru.projects import coder_board, reviewer
    from emuru.projects.coder import load_grant

    relevant_tasks = {e["native_task"] for e in manifest["tasks"]} | {
        r["task"]
        for r in records
        if r["kind"] == "batch-task" and r["project"] == manifest["project"]
    }
    for card in kb.list_tasks(db, tenant=manifest["project"], include_archived=True):
        if card.id not in relevant_tasks or card.status not in {"done", "archived"}:
            continue
        try:
            folder = coder_board._find_attempt(home, card.id)
            if folder is None:
                continue
            grant = load_grant(folder / "grant.json")
            receipt = reviewer.verify(
                home,
                vault,
                root,
                card.id,
                grant,
                folder,
                kb,
                db,
                historical=True,
                capture=True,
            )
            native_run = kb.get_run(db, receipt["run"])
            if (
                receipt["status"] == "verified"
                and native_run is not None
                and native_run.task_id == card.id
                and native_run.outcome == "completed"
                and native_run.ended_at is not None
                and json.loads(card.result or "null") == receipt
            ):
                completed.add(card.id)
        except (OSError, ValueError, TypeError, KeyError):
            # Missing old artifacts never establish success or block unrelated contracts.
            continue
    consumed, unspent, new, ceilings = _account(
        home, records, manifest, policy, completed
    )
    if manifest["ceilings"] != ceilings:
        raise ValueError(
            "Batch ceilings must equal C+U+D: " + json.dumps(ceilings, sort_keys=True)
        )
    enrollments = [
        r
        for r in records
        if r["kind"] in {"enrollment", "batch-allowance"}
        and r["project"] == manifest["project"]
    ]
    started = min((r["started_ns"] for r in enrollments), default=None)
    deadline = min((r["deadline_ns"] for r in enrollments), default=None)
    if enrollments and (
        any(r["boot_id"] != supervisor.boot_id() for r in enrollments)
        or supervisor.time.monotonic_ns() >= deadline
    ):
        raise ValueError("Original batch enrollment expired; no renewal.")
    order = []

    def visit(label):
        if label in order:
            return
        for dep in plan["mappings"][label]["task"].get("depends_on", []):
            if dep in labels:
                visit(dep)
        order.append(label)

    for label in labels:
        visit(label)
    result = {
        "manifest": manifest,
        "repository_path": policy["path"],
        "identity": supervisor.identity(root, image),
        "authority": {
            "owner": policy["owner"],
            "bot": policy["bot"],
            "vault": os.path.abspath(vault),
            "proposal": manifest["proposal"],
            "revision": plan["revision"],
            "approval": preview["approval_digest"],
        },
        "consumed": consumed,
        "unspent": unspent,
        "new": new,
        "ceilings": ceilings,
        "dependency_order": order,
        "blockers": {
            label: ["Schema-4 successor execution unavailable until Ticket 03"]
            if plan["mappings"][label]["contract"]["dependencies"]
            else []
            for label in labels
        },
        "input_policy": "Selected baseline files only; verified successor composition unavailable until Ticket 03.",
        "started_ns": started,
        "deadline_ns": deadline,
    }
    return {**result, "digest": digest(result)}


def preview(home, vault, root, manifest, policy, image):
    with (
        board.project_board_readonly(home, root) as (kb, db),
        supervisor._locked(home) as (parent, size, marker),
    ):
        if marker["schema"] != 2:
            raise ValueError("Stopped continuation upgrade required.")
        records, _ = supervisor._records(parent, size, marker)
        return _preview(home, vault, root, manifest, policy, image, records, kb, db)


def _retry_records(home, parent, size, marker, matches):
    """Only the exact explicit action may publish its one interrupted marker."""
    try:
        return supervisor._records(parent, size, marker)
    except ValueError:
        names = sorted(
            n for n in os.listdir(parent) if re.fullmatch(r"[0-9]{8}\.json", n)
        )
        if len(names) != marker["generation"] + 1:
            raise
        newest, _ = supervisor._read(parent, names[-1])
        if newest.get("kind") not in KINDS:
            raise
        candidate = {
            **marker,
            "generation": newest.get("generation"),
            "head": digest(newest),
        }
        records, size = supervisor._records(parent, size, candidate)
        allowance = (
            newest
            if newest["kind"] == "batch-allowance"
            else records[newest["allowance"] - 1]
        )
        payload = _payload(home, allowance)
        if not matches(newest, allowance, payload):
            raise ValueError(
                "Interrupted batch publication requires its exact original action."
            ) from None
        from emuru.models.gateway import write_private

        write_private(
            Path(home) / "emuru-projects/supervisor-enabled.json",
            encoded(candidate).decode(),
        )
        with mutations.directory(Path(home) / "emuru-projects") as folder:
            os.fsync(folder)
        return records, size


def enroll(home, vault, root, manifest, policy, image, expect, *, seconds=86400):
    from emuru.projects.dispatcher import dispatcher_lock
    from emuru.projects.supervisor_controls import emergency_size
    from emuru.projects.supervisor_scout import metadata_pending

    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError("Batch enrollment needs 1..86400 seconds.")
    _shape(manifest, policy)
    with (
        dispatcher_lock(home),
        board.project_board_readonly(home, root) as (kb, db),
        supervisor._locked(home, write=True) as (parent, size, marker),
    ):
        if marker["schema"] != 2:
            raise ValueError("Stopped continuation upgrade required.")
        records, size = _retry_records(
            home,
            parent,
            size,
            marker,
            lambda newest, allowance, payload: (
                newest["kind"] in {"batch-allowance", "batch-task"}
                and payload["manifest"] == manifest
                and allowance["approval"] == expect
                and allowance["identity"] == supervisor.identity(root, image)
                and allowance["authority"]["owner"] == policy["owner"]
                and allowance["authority"]["bot"] == policy["bot"]
                and allowance["authority"]["vault"] == os.path.abspath(vault)
            ),
        )
        existing = next(
            (
                r
                for r in records
                if r["kind"] == "batch-allowance"
                and _payload(home, r)["manifest"] == manifest
            ),
            None,
        )
        if existing:
            payload = _payload(home, existing)
            if (
                payload["preview"]["digest"] != expect
                or payload["preview"]["identity"] != supervisor.identity(root, image)
                or any(
                    payload["preview"]["authority"][k] != policy[k]
                    for k in ("owner", "bot")
                )
                or payload["preview"]["authority"]["vault"] != os.path.abspath(vault)
            ):
                raise ValueError("Conflicting batch enrollment retry.")
            result = existing
        else:
            from emuru.projects.supervisor_lifecycle import require_admission

            require_admission(records)
            report = _preview(
                home, vault, root, manifest, policy, image, records, kb, db
            )
            if expect != report["digest"]:
                raise ValueError("Stale batch preview digest.")
            payload = {"manifest": manifest, "preview": report}
            identity = digest(manifest)
            started = (
                report["started_ns"]
                if report["started_ns"] is not None
                else supervisor.time.monotonic_ns()
            )
            deadline = (
                report["deadline_ns"]
                if report["deadline_ns"] is not None
                else started + seconds * 10**9
            )
            result = {
                "kind": "batch-allowance",
                "project": manifest["project"],
                "manifest": identity,
                "preview": digest(report),
                "binding_digest": manifest["binding_digest"],
                "previous_allowance": manifest["previous_allowance"],
                "ceilings": report["ceilings"],
                "approval": expect,
                "authority": report["authority"],
                "identity": report["identity"],
                "boot_id": supervisor.boot_id(),
                "started_ns": started,
                "deadline_ns": deadline,
                "task_count": len(manifest["tasks"]),
            }
        generation = result.get("generation", len(records) + 1)
        tasks = [
            {
                "kind": "batch-task",
                "project": manifest["project"],
                "allowance": generation,
                "task": e["native_task"],
                "logical_key": e["logical_key"],
                "contract_digest": e["contract_digest"],
                "scope": digest({k: e[k] for k in SCOPE}),
                "attempts": e["attempts"],
            }
            for e in manifest["tasks"]
            if not any(
                r["kind"] == "batch-task"
                and r["allowance"] == generation
                and r["task"] == e["native_task"]
                for r in records
            )
        ]
        planned = records.copy()
        values = ([] if existing else [result]) + tasks
        storage = (
            Path(home)
            / "emuru-projects/continuation-manifests"
            / (digest(payload["preview"]) + ".json")
        )
        manifest_size = 0 if storage.exists() else len(encoded(payload["preview"]))
        extra = manifest_size
        if len(encoded(payload["preview"])) > 32768:
            raise ValueError(
                "Batch immutable preview exceeds 32 KiB; select fewer tasks."
            )
        for v in values:
            candidate = {
                "schema": 2,
                "generation": len(planned) + 1,
                "previous": digest(planned[-1]) if planned else None,
                **v,
            }
            validate_record(candidate, planned)
            if len(encoded(candidate)) > supervisor.RECEIPT_BYTES:
                raise ValueError("Batch receipt exceeds capacity.")
            extra += len(encoded(candidate))
            planned.append(candidate)
        if (
            size + extra + metadata_pending(planned)
            > supervisor.METADATA_BYTES - supervisor.EMERGENCY_BYTES
            or emergency_size(planned) > supervisor.EMERGENCY_BYTES
        ):
            raise ValueError(
                "Batch metadata capacity exhausted; restriction reserve retained."
            )
        if not existing:
            continuation._manifest(home, payload["preview"])
            result, size = supervisor._append(
                Path(home) / "emuru-projects/supervisor",
                records,
                size + manifest_size,
                result,
            )
        for value in tasks:
            _, size = supervisor._append(
                Path(home) / "emuru-projects/supervisor", records, size, value
            )
        return result


def require_task(home, records, grant, *, review=False, admitted=False):
    """Admission only. No claim, grant renewal, or schema-4 interpretation."""
    tasks = memberships(records, grant["task"])
    if not tasks:
        if any(
            r["kind"] == "batch-allowance" and r["project"] == grant["project"]
            for r in records
        ):
            raise ValueError("Task absent from approved batch.")
        return None
    task = tasks[-1]
    allowance = records[task["allowance"] - 1]
    if (
        (
            control_state(records, grant["task"]) == "revoked"
            or not admitted
            and control_state(records, grant["task"]) == "held"
        )
        or allowance["boot_id"] != supervisor.boot_id()
        or supervisor.time.monotonic_ns() >= allowance["deadline_ns"]
    ):
        raise ValueError("Batch held, revoked or expired; no stage admitted.")
    if (
        len(
            [
                r
                for r in records
                if r["kind"] == "batch-task"
                and r["allowance"] == allowance["generation"]
            ]
        )
        != allowance["task_count"]
    ):
        raise ValueError("Incomplete batch enrollment; explicit retry required.")
    payload = _payload(home, allowance)
    entry = next(
        t for t in payload["manifest"]["tasks"] if t["native_task"] == grant["task"]
    )
    if allowance["identity"] != supervisor.identity(
        Path(__file__).resolve().parents[3], allowance["identity"]["image"]
    ):
        raise ValueError("Batch source/image identity changed.")
    if (
        grant["path"] != payload["preview"]["repository_path"]
        or any(grant[k] != entry[k] for k in SCOPE if k != "limits")
        or any(grant["limits"][k] > entry["limits"][k] for k in entry["limits"])
    ):
        raise ValueError("Grant exceeds batch task scope.")
    if (
        any(
            grant[k] != allowance["authority"][k]
            for k in ("owner", "bot", "proposal", "revision", "approval")
        )
        or digest(allowance["authority"]["vault"]) != grant["vault"]
    ):
        raise ValueError("Grant batch authority changed.")
    count = sum(
        r["kind"] == ("reviewer-stage" if review else "coder-stage")
        and r.get("phase") == "admitted"
        and r["task"] == grant["task"]
        and r["grant_sha256"] != digest(grant)
        for r in records
    )
    if count >= task["attempts"]:
        raise ValueError("Batch task attempt cap exhausted; no third attempt.")
    from emuru.projects.spec import task_contracts

    approved = mutations.approved_project(
        home,
        Path(allowance["authority"]["vault"]),
        grant["proposal"],
        grant["owner"],
        grant["bot"],
        readonly=True,
    )
    bindings = {
        r["logical_key"]: r
        for r in records
        if r["kind"] == "task-binding" and r["project"] == grant["project"]
    }
    with board.project_board_readonly(home, Path(__file__).resolve().parents[3]) as (
        kb,
        db,
    ):
        if kb.parent_ids(db, grant["task"]):
            raise ValueError(
                "Batch native dependencies changed; successor execution unavailable."
            )
        label_keys = {
            json.loads(kb.get_task(db, r["task"]).body)["task"]["id"]: key
            for key, r in bindings.items()
        }
    contracts = task_contracts(approved["content"], logical_keys=label_keys)
    contract = next(
        (v for v in contracts.values() if v["logical_key"] == entry["logical_key"]),
        None,
    )
    if (
        contract is None
        or contract["contract_digest"] != entry["contract_digest"]
        or contract["contract"]["dependencies"]
    ):
        raise ValueError("Continuation successor requires schema-4 implementation.")
    return allowance


def token(records, allowance):
    relevant = [
        r
        for r in records
        if r["generation"] == allowance["generation"]
        or r.get("allowance") == allowance["generation"]
        or r.get("project") == allowance["project"]
        and r["kind"] == "batch-allowance"
    ]
    return digest(
        [
            allowance["manifest"],
            allowance["authority"],
            max(r["generation"] for r in relevant),
        ]
    )


def projection(home, records, project):
    allowance = latest(records, project)
    if allowance is None:
        return None
    consumed = _consumed(records, project)
    return {
        "digest": allowance["manifest"],
        "control": state(records, allowance),
        "token": token(records, allowance),
        "deadline_ns": allowance["deadline_ns"],
        "expired": allowance["boot_id"] != supervisor.boot_id()
        or supervisor.time.monotonic_ns() >= allowance["deadline_ns"],
        "consumed": consumed,
        "remaining": {k: allowance["ceilings"][k] - consumed[k] for k in consumed},
    }


def _continue_proof(home, root, batch_digest):
    """Slow process/evidence inspection stays outside the admission fence."""
    from emuru.projects.supervisor_controls import _coder_continue_proof
    from emuru.projects.supervisor_scout import projection, unresolved

    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    head = digest(records[-1]) if records else None
    if not unresolved(records):
        return head
    allowance = next(
        (
            r
            for r in records
            if r["kind"] == "batch-allowance" and r["manifest"] == batch_digest
        ),
        None,
    )
    if allowance is None:
        return None
    selected = {
        r["task"]
        for r in records
        if r["kind"] == "batch-task" and r["allowance"] == allowance["generation"]
    }
    active = {
        r["enrollment"]
        for r in records
        if r["kind"] in {"stage", "coder-stage", "reviewer-stage"}
        and r.get("phase") == "admitted"
    }
    for generation in active:
        enrollment = records[generation - 1]
        if enrollment["task"] not in selected:
            return None
        if enrollment["stage"] == "coder":
            proof = _coder_continue_proof(
                home,
                root,
                f"/project continue {enrollment['proposal']} {enrollment['task']} unused",
            )
            if proof is None or proof["projection"].get("execution") not in {
                "finished",
                "awaiting_review",
                "running",
            }:
                return None
        elif projection(home, root, records, enrollment).get("execution") != "finished":
            return None
    return head


def owner_control(home, vault, root, text, owner, bot, request, uid):
    """Called only after the existing private owner/queue admission proof."""
    from emuru.projects.supervisor_controls import CONTROL_REPLIES
    from emuru.telegram.queue import control_receipt

    fields = text.split()
    if (
        len(fields) != 4
        or fields[1] not in {"batch-hold", "batch-continue", "batch-cancel"}
        or any(not re.fullmatch("[0-9a-f]{64}", v) for v in fields[2:])
    ):
        raise ValueError(
            "Use /project batch-hold|batch-continue|batch-cancel <batch-digest> <control-token>."
        )
    action = fields[1][6:]
    proof = _continue_proof(home, root, fields[2]) if action == "continue" else None
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = _retry_records(
            home,
            parent,
            size,
            marker,
            lambda newest, allowance, payload: (
                newest["kind"] == "batch-control"
                and newest["request"] == request
                and newest["text_hash"] == hashlib.sha256(text.encode()).hexdigest()
                and allowance["authority"]["owner"] == owner
                and allowance["authority"]["bot"] == bot
                and allowance["authority"]["vault"] == os.path.abspath(vault)
            ),
        )
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        existing = next(
            (
                r
                for r in records
                if r["kind"] in {"batch-control", "control", "control-receipt"}
                and r["request"] == request
            ),
            None,
        )
        if existing:
            if existing["text_hash"] != text_hash:
                raise ValueError("Conflicting batch control request bytes.")
            return existing["response"]
        allowance = next(
            (
                r
                for r in records
                if r["kind"] == "batch-allowance" and r["manifest"] == fields[2]
            ),
            None,
        )
        if allowance is None or any(
            allowance["authority"][k] != v
            for k, v in (
                ("owner", owner),
                ("bot", bot),
                ("vault", os.path.abspath(vault)),
            )
        ):
            raise ValueError("Exact owner/bot/vault batch authority required.")
        if token(records, allowance) != fields[3]:
            raise ValueError("Stale batch control token; inspect again.")
        current = state(records, allowance)
        if current == "revoked" or action == "hold" and current == "held":
            from emuru.projects.supervisor_controls import _retain_outcome

            response = (
                "revoked; no further authorized stage."
                if current == "revoked"
                else CONTROL_REPLIES["hold"]
            )
            return _retain_outcome(
                home, request, text_hash, response, locked=(records, size)
            )
        if action == "continue":
            from emuru.projects.supervisor_lifecycle import require_admission
            from emuru.projects.supervisor_scout import unresolved

            require_admission(records)
            if (
                current != "held"
                or allowance["boot_id"] != supervisor.boot_id()
                or supervisor.time.monotonic_ns() >= allowance["deadline_ns"]
                or allowance["identity"]
                != supervisor.identity(root, allowance["identity"]["image"])
            ):
                raise ValueError(
                    "Continue requires held, unchanged, unexpired and unrevoked batch."
                )
            if unresolved(records) and proof != digest(records[-1]):
                raise ValueError(
                    "Active or uncertain stage requires unchanged stop/outcome proof before batch continue."
                )
            authority = allowance["authority"]
            preview = mutations.approved_project(
                home, vault, authority["proposal"], owner, bot, readonly=True
            )
            if (
                not preview["current"]
                or preview["after_revision"] != authority["revision"]
                or preview["approval_digest"] != authority["approval"]
            ):
                raise ValueError("Batch project approval changed.")
        value = {
            "kind": "batch-control",
            "project": allowance["project"],
            "allowance": allowance["generation"],
            "action": action,
            "request": request,
            "text_hash": text_hash,
            "token": fields[3],
            "response": CONTROL_REPLIES[action],
        }
        supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            value,
            emergency=action != "continue",
        )
        return control_receipt(
            Path(home) / "emuru-telegram/queue.sqlite3",
            bot,
            uid,
            request,
            text_hash,
            response=value["response"],
        )


def require_scout(home, records, project, task, policy):
    allowances = [
        r for r in records if r["kind"] == "batch-allowance" and r["project"] == project
    ]
    if not allowances:
        return None
    allowance = next(
        (r for r in allowances if _payload(home, r)["manifest"]["scout"]),
        None,
    )
    if allowance is None:
        legacy = next(
            (
                r
                for r in records
                if r["kind"] == "enrollment"
                and r["stage"] == "scout"
                and r["project"] == project
                and r["task"] == task
                and r["generation"] < allowances[0]["generation"]
                and r["grant_sha256"] == digest(policy)
            ),
            None,
        )
        if legacy is None:
            raise ValueError("Current unrestricted batch Scout slot required.")
        # The existing enrollment retains its own scope/deadline; batch restrictions still win.
        if not memberships(records, task):
            return None
        allowance = records[memberships(records, task)[-1]["allowance"] - 1]
    if (
        allowance is None
        or state(records, allowance) != "unrestricted"
        or control_state(records, task) != "unrestricted"
        or allowance["boot_id"] != supervisor.boot_id()
        or supervisor.time.monotonic_ns() >= allowance["deadline_ns"]
    ):
        raise ValueError("Current unrestricted batch Scout slot required.")
    if not any(
        r["kind"] == "batch-task"
        and r["allowance"] == allowance["generation"]
        and r["task"] == task
        for r in records
    ):
        raise ValueError("Scout task is outside its approved batch.")
    members = [
        r
        for r in records
        if r["kind"] == "batch-task" and r["allowance"] == allowance["generation"]
    ]
    payload = _payload(home, allowance)
    entry = next(t for t in payload["manifest"]["tasks"] if t["native_task"] == task)
    if (
        len(members) != allowance["task_count"]
        or policy["path"] != payload["preview"]["repository_path"]
        or any(policy[k] != entry[k] for k in ("repository", "baseline"))
        or not set(policy["files"]) <= set(entry["read_files"])
    ):
        raise ValueError("Incomplete batch or Scout scope exceeds approved selection.")
    if _consumed(records, project)["scout_runs"]:
        raise ValueError("A second Scout is forbidden.")
    return allowance
