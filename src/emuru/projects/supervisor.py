"""Private opt-in supervisor facts. Native Kanban remains authoritative."""

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from emuru.projects.scout import digest, encoded
from emuru.vault import mutations

STATE_SCHEMA = 1
METADATA_BYTES = 1024 * 1024
RECEIPT_BYTES = 4096
EMERGENCY_BYTES = 4096
COUNTERS = (
    "coder_runs",
    "reviewer_runs",
    "scout_runs",
    "active_seconds",
    "coder_iterations",
    "reviewer_iterations",
    "checks",
    "output_bytes",
    "artifact_bytes",
)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def enabled(home):
    folder = Path(home) / "emuru-projects"
    try:
        with mutations.directory(folder) as parent:
            for name in ("supervisor-enabled.json", "supervisor"):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                return True
    except FileNotFoundError:
        return False
    return False


def refuse_unsupported(home):
    if enabled(home):
        raise ValueError("Supervised execution adapter unavailable; launch refused.")


def _read(parent, name):
    from emuru.projects.reviewer import unique_fields

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        mutations.regular(stream.fileno(), private=True)
        data = stream.read(RECEIPT_BYTES + 1)
    if len(data) > RECEIPT_BYTES:
        raise ValueError("Supervisor receipt exceeds 4 KiB.")
    return json.loads(data, object_pairs_hook=unique_fields), len(data)


@contextmanager
def _locked(home, *, write=False):
    folder = Path(home) / "emuru-projects"
    with mutations.directory(folder / "supervisor") as parent:
        mutations.private_directory(parent)
        fd = os.open(
            "control.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            mutations.regular(fd, private=True)
            fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            with mutations.directory(folder) as profile:
                mutations.private_directory(profile)
                marker, _ = _read(profile, "supervisor-enabled.json")
            if (
                not isinstance(marker, dict)
                or set(marker) != {"schema", "generation", "head"}
                or type(marker["schema"]) is not int
                or marker["schema"] != STATE_SCHEMA
                or type(marker["generation"]) is not int
                or marker["generation"] < 0
            ):
                raise ValueError("Unsupported supervisor opt-in schema.")
            yield parent, RECEIPT_BYTES + os.fstat(fd).st_size, marker
        finally:
            os.close(fd)


def _records(parent, size, marker):
    names = sorted(os.listdir(parent))
    records = []
    for name in names:
        if name == "control.lock":
            continue
        if not re.fullmatch(r"[0-9]{8}\.json", name):
            raise ValueError("Unknown supervisor metadata; preserve newest state.")
        value, length = _read(parent, name)
        size += length
        if (
            size > METADATA_BYTES - EMERGENCY_BYTES
            or type(value) is not dict
            or type(value.get("schema")) is not int
            or value.get("schema") != STATE_SCHEMA
            or type(value.get("generation")) is not int
            or value.get("generation") != len(records) + 1
            or value.get("previous") != (digest(records[-1]) if records else None)
            or name != f"{len(records) + 1:08d}.json"
        ):
            raise ValueError("Supervisor metadata schema, history or capacity invalid.")
        if value.get("kind") == "project":
            if (
                set(value)
                != {"schema", "kind", "generation", "previous", "project", "reserved"}
                or value["reserved"] != dict.fromkeys(COUNTERS, 0)
                or any(type(v) is not int for v in value["reserved"].values())
                or any(
                    r.get("project") == value["project"] and r["kind"] == "project"
                    for r in records
                )
            ):
                raise ValueError("Supervisor initial project reservation invalid.")
        elif value.get("kind") == "enrollment":
            required = {
                "schema",
                "kind",
                "generation",
                "previous",
                "project",
                "task",
                "proposal",
                "grant_sha256",
                "stage",
                "boot_id",
                "started_ns",
                "deadline_ns",
                "identity",
            }
            if (
                set(value) != required
                or value["stage"] not in {"coder", "scout"}
                or not isinstance(value["boot_id"], str)
                or not value["boot_id"]
                or any(type(value[k]) is not int for k in ("started_ns", "deadline_ns"))
                or value["started_ns"] < 0
                or not 0 < value["deadline_ns"] - value["started_ns"] <= 86400 * 10**9
                or not any(
                    r["kind"] == "project" and r["project"] == value["project"]
                    for r in records
                )
            ):
                raise ValueError("Supervisor enrollment invalid.")
            identity = value["identity"]
            if (
                not isinstance(identity, dict)
                or set(identity) != {"source", "image", "pin", "native", "schema"}
                or type(identity["schema"]) is not int
                or identity["schema"] != STATE_SCHEMA
                or any(
                    not isinstance(identity[key], str)
                    or not re.fullmatch(pattern, identity[key])
                    for key, pattern in (
                        ("source", "[0-9a-f]{64}"),
                        ("image", "sha256:[0-9a-f]{64}"),
                        ("pin", "[0-9a-f]{40}"),
                        ("native", "[0-9a-f]{64}"),
                    )
                )
            ):
                raise ValueError("Supervisor execution identity invalid.")
        else:
            raise ValueError("Unknown supervisor record; preserve newest state.")
        for key, pattern in (
            ("project", "[0-9a-f]{64}"),
            ("task", "t_[0-9a-f]+"),
            ("proposal", "[0-9a-f]{32}"),
            ("grant_sha256", "[0-9a-f]{64}"),
        ):
            if key in value and (
                not isinstance(value[key], str) or not re.fullmatch(pattern, value[key])
            ):
                raise ValueError("Invalid supervisor binding identity.")
        records.append(value)
    if marker["generation"] != len(records) or marker["head"] != (
        digest(records[-1]) if records else None
    ):
        raise ValueError(
            "Supervisor committed history missing or interrupted; preserve newest state."
        )
    return records, size


def identity(root, image):
    from emuru.deployment import source_identity

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Exact verified image identity required.")
    pin = json.loads((Path(root) / "infra/hermes/runtime-lock.json").read_text())[
        "commit"
    ]
    native = json.loads(
        (Path(root) / "infra/hermes/kanban-native-contract.json").read_text()
    )
    return {
        "source": source_identity(root),
        "image": image,
        "pin": pin,
        "native": digest(native),
        "schema": STATE_SCHEMA,
    }


def _initialize(home):
    """Commit opt-in first: interruption must never restore legacy launch authority."""
    from emuru.projects.coder_board import _write_new

    folder = Path(home) / "emuru-projects"
    with mutations.directory(folder) as parent:
        mutations.private_directory(parent)
        if not enabled(home):
            _write_new(
                folder,
                "supervisor-enabled.json",
                encoded({"schema": STATE_SCHEMA, "generation": 0, "head": None}),
            )
            os.mkdir("supervisor", 0o700, dir_fd=parent)
            os.fsync(parent)
            _write_new(folder / "supervisor", "control.lock", b"")


def _append(folder, records, size, value):
    from emuru.projects.coder_board import _write_new

    value = {
        "schema": STATE_SCHEMA,
        "generation": len(records) + 1,
        "previous": digest(records[-1]) if records else None,
        **value,
    }
    data = encoded(value)
    if len(data) > RECEIPT_BYTES or size + len(data) > METADATA_BYTES - EMERGENCY_BYTES:
        raise ValueError(
            "Supervisor metadata admission exhausted; 4 KiB emergency reserve retained."
        )
    # Reserve compact hold/cancel/fence facts for every admitted binding before enrollment.
    bindings = {
        (r["project"], r["task"])
        for r in [*records, value]
        if r["kind"] == "enrollment"
    }
    emergency = {
        "sleep": True,
        "bindings": [
            [p, t, "hold", "cancel", "stop_requested"] for p, t in sorted(bindings)
        ],
    }
    if len(encoded(emergency)) > EMERGENCY_BYTES:
        raise ValueError("Supervisor emergency stop/fence capacity exhausted.")
    _write_new(folder, f"{value['generation']:08d}.json", data)
    from emuru.models.gateway import write_private

    write_private(
        folder.parent / "supervisor-enabled.json",
        encoded(
            {
                "schema": STATE_SCHEMA,
                "generation": value["generation"],
                "head": digest(value),
            }
        ).decode(),
    )
    with mutations.directory(folder.parent) as parent:
        os.fsync(parent)
    records.append(value)
    return value, size + len(data)


def enroll(
    home,
    vault,
    root,
    stage,
    policy,
    task,
    image,
    *,
    seconds=86400,
    owner=None,
    bot=None,
):
    """Operator-only registration; never claim, initialize native state or launch work."""
    from emuru.projects import board, coder_board
    from emuru.projects.coder import load_grant
    from emuru.projects.dispatcher import dispatcher_lock
    from emuru.projects.scout import load_policy

    if (
        stage not in {"coder", "scout"}
        or type(seconds) is not int
        or not 1 <= seconds <= 86400
    ):
        raise ValueError("Enrollment needs a supported stage and 1..86400 seconds.")
    grant = load_grant(policy) if stage == "coder" else load_policy(policy, task)
    current_identity = identity(root, image)
    with dispatcher_lock(home):
        if stage == "coder":
            coder_board.authorize(home, vault, root, task, grant, readonly=True)
            project, proposal = grant["project"], grant["proposal"]
        else:
            if any(type(v) is not int or v <= 0 for v in (owner, bot)):
                raise ValueError("Scout enrollment needs exact owner and bot IDs.")
            with board.project_board_readonly(home, root) as (kb, db):
                card = kb.get_task(db, task)
                if (
                    not card
                    or card.created_by != "emuru-owner"
                    or card.assignee is not None
                    or card.status != "blocked"
                ):
                    raise ValueError(
                        "Scout enrollment requires an unchanged tracked task."
                    )
                binding = json.loads(card.body)
                prior_scope = {
                    **{
                        key: grant[key]
                        for key in (
                            "repository",
                            "baseline",
                            "files",
                            "timeout_seconds",
                        )
                    },
                    "source_task": task,
                    "repository_id": digest(
                        [grant["repository"], str(Path(grant["path"]))]
                    ),
                }
                for child in kb.list_tasks(
                    db, tenant=card.tenant, include_archived=True
                ):
                    child_binding = json.loads(child.body)
                    if (
                        not isinstance(child_binding, dict)
                        or child_binding.get("schema") != 1
                    ):
                        raise ValueError(
                            "Legacy Scout binding invalid; explicit inspection required."
                        )
                    prior = child_binding.get("scout")
                    if prior is not None and not isinstance(prior, dict):
                        raise ValueError(
                            "Legacy Scout grant invalid; explicit inspection required."
                        )
                    if prior is not None and all(
                        prior.get(key) == val for key, val in prior_scope.items()
                    ):
                        raise ValueError(
                            "Scout identity already attempted; enrollment cannot renew historical work."
                        )
            preview = mutations.approved_project(
                home, vault, binding["proposal"], owner, bot, readonly=True
            )
            project = board._digest(
                [str(owner), str(bot), os.path.abspath(vault), preview["path"]]
            )
            proposal = binding["proposal"]
            if (
                not preview["current"]
                or card.tenant != project
                or binding.get("schema") != 1
                or "scout" in binding
                or "coder" in binding
                or binding.get("revision") != preview["after_revision"]
                or binding.get("approval") != preview["approval_digest"]
                or binding.get("task") not in board.spec_tasks(preview["content"])
            ):
                raise ValueError("Scout approval or project binding changed.")
        coder_board.ensure_idle(
            home,
            root,
            task,
            rework_grant=grant
            if grant.get("schema") == 3 and stage == "coder"
            else None,
        )
        if stage == "coder" and coder_board.attempt_path(home, grant).exists():
            raise ValueError("Attempted work cannot be enrolled or renewed.")
        _initialize(home)
        with _locked(home, write=True) as (parent, size, marker):
            records, size = _records(parent, size, marker)
            previous = next(
                (
                    r
                    for r in reversed(records)
                    if r["kind"] == "enrollment"
                    and r["task"] == task
                    and r["stage"] == stage
                ),
                None,
            )
            if (
                previous is not None
                and previous["grant_sha256"] != digest(grant)
                and not (
                    stage == "coder"
                    and grant["schema"] == 3
                    and grant["rework"]["grant_sha256"] == previous["grant_sha256"]
                )
            ):
                raise ValueError(
                    "Enrollment task already bound to another grant; explicit reconciliation required."
                )
            folder = Path(home) / "emuru-projects/supervisor"
            if not any(
                r["kind"] == "project" and r["project"] == project for r in records
            ):
                _, size = _append(
                    folder,
                    records,
                    size,
                    {
                        "kind": "project",
                        "project": project,
                        "reserved": dict.fromkeys(COUNTERS, 0),
                    },
                )
            started = time.monotonic_ns()
            return _append(
                folder,
                records,
                size,
                {
                    "kind": "enrollment",
                    "project": project,
                    "task": task,
                    "proposal": proposal,
                    "grant_sha256": digest(grant),
                    "stage": stage,
                    "boot_id": boot_id(),
                    "started_ns": started,
                    "deadline_ns": started + seconds * 10**9,
                    "identity": current_identity,
                },
            )[0]


def status(home, root, project, card):
    if not enabled(home):
        return None
    with _locked(home) as (parent, size, marker):
        records, _ = _records(parent, size, marker)
        value = next(
            (
                r
                for r in reversed(records)
                if r["kind"] == "enrollment"
                and r["project"] == project
                and r["task"] == card.id
            ),
            None,
        )
        if value is None:
            return {
                "task": card.id,
                "authority": "not enrolled",
                "execution": "unavailable",
            }
        if value["identity"] != identity(root, value["identity"]["image"]):
            raise ValueError(
                "Supervisor source/image/pin/schema incompatible; preserve newest state."
            )
        binding = json.loads(card.body)
        if card.tenant != project or binding.get("proposal") != value["proposal"]:
            raise ValueError("Supervisor native project/task/proposal mismatch.")
        budget = next(
            r["reserved"]
            for r in records
            if r["kind"] == "project" and r["project"] == project
        )
        token = digest(
            [
                value["generation"],
                project,
                card.id,
                value["grant_sha256"],
                card.current_run_id,
            ]
        )
        expired = (
            value["boot_id"] != boot_id() or time.monotonic_ns() >= value["deadline_ns"]
        )
        return {
            "task": card.id,
            "native": card.status,
            "run": card.current_run_id,
            "authority": "expired" if expired else "enrolled",
            "generation": value["generation"],
            "token": token,
            "grant_sha256": value["grant_sha256"],
            "deadline_ns": value["deadline_ns"],
            "reserved": budget,
            "execution": "unstarted"
            if card.current_run_id is None
            and card.assignee is None
            and card.status == "blocked"
            else "uncertain",
            "verified_outcome": "unavailable",
            "stop_proof": "unavailable",
            "provider_usage": "unavailable",
            "proposal": value["proposal"],
            "control": "unrestricted",
            "adapter": "unavailable",
            "stage": value["stage"],
        }


def projection(home, root, project, card, *, preview=None):
    value = status(home, root, project, card)
    if value is None:
        return ""
    value["proposal_facts"] = (
        {
            key: preview[key]
            for key in ("proposal_id", "outcome", "current", "after_revision")
        }
        if preview is not None
        else "unavailable"
    )
    text = "Supervision: " + json.dumps(value, sort_keys=True)
    if len(text) > 3500:
        raise ValueError("Supervisor projection exceeds 3500 characters.")
    return text
