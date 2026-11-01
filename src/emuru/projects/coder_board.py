"""Live task authorization at the native board boundary."""

import hashlib
import json
import os
import re
from pathlib import Path

from emuru.projects import board
from emuru.projects.coder import load_grant, validate_grant
from emuru.projects.coder_runtime import (
    LIMITS,
    _read_private,
    catalogue_digest,
    verify_evidence,
)
from emuru.projects.coder_sandbox import bounded_process
from emuru.projects.scout import digest, git
from emuru.vault import mutations
from emuru.vault.mutations import directory, private_directory, regular

CODER = "emuru-coder"
SCOUT = "emuru-scout"
ACTIVE = {"ready", "running", "blocked", "needs_input"}


def attempt_path(home, grant):
    return Path(home) / "emuru-projects/coder-attempts" / grant["grant_id"]


def _write_new(folder, name, data):
    with directory(folder) as parent:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def _append(folder, name, record):
    with directory(folder) as parent:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            regular(fd, private=True)
            if os.fstat(fd).st_size > 8192:
                raise ValueError("Coder process ledger limit exceeded.")
            data = (json.dumps(record, sort_keys=True) + "\n").encode()
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
            os.fsync(parent)
        finally:
            os.close(fd)


def _read_json(path, limit=32768):
    return json.loads(_read_private(path, limit))


def _active_work(kb, db, except_task):
    for agent in (SCOUT, CODER):
        for card in kb.list_tasks(db, assignee=agent, include_archived=True):
            if card.id != except_task and card.status in ACTIVE:
                return card
    return None


def ensure_idle(home, root, task_id):
    with board.project_board_readonly(home, root) as (kb, db):
        active = _active_work(kb, db, task_id)
    if active:
        raise ValueError("Another Scout or coder task is active or unresolved.")


def _find_attempt(home, task_id):
    root = Path(home) / "emuru-projects/coder-attempts"
    try:
        with directory(root) as fd:
            private_directory(fd)
            entries = os.listdir(fd)
    except FileNotFoundError:
        return None
    matches = []
    for entry in entries:
        if not re.fullmatch(r"[0-9a-f]{32}", entry):
            continue
        folder = root / entry
        with directory(folder) as fd:
            private_directory(fd)
            intent = _read_json(folder / "intent.json")
        if intent.get("task") == task_id:
            matches.append(folder)
    if len(matches) > 1:
        raise ValueError("Multiple coder intents exist for one tracked task.")
    return matches[0] if matches else None


def authorize(home, vault, root, task_id, grant, *, run_id=None):
    """Reopen current approval and native task before any worker or claim starts."""
    grant = validate_grant(grant)
    if task_id != grant["task"]:
        raise ValueError("Coder task does not match its grant.")
    preview = mutations.approved_project(
        home, vault, grant["proposal"], grant["owner"], grant["bot"]
    )
    project = board._digest(
        [
            str(grant["owner"]),
            str(grant["bot"]),
            os.path.abspath(vault),
            preview["path"],
        ]
    )
    if (
        not preview["current"]
        or grant["vault"] != digest(os.path.abspath(vault))
        or grant["project"] != project
        or grant["revision"] != preview["after_revision"]
        or grant["approval"] != preview["approval_digest"]
    ):
        raise ValueError("Coder grant approval is stale or mismatched.")
    if (
        git(Path(grant["path"]), "rev-parse", "--verify", "HEAD").decode().strip()
        != grant["baseline"]
    ):
        raise ValueError("Coder repository baseline changed.")
    if grant["catalogue"] != catalogue_digest():
        raise ValueError("Coder check catalogue changed.")
    with board.project_board(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        binding = json.loads(card.body) if card else {}
        task = binding.get("task")
        approved_tasks = board.spec_tasks(preview["content"])
        if (
            card is None
            or card.tenant != project
            or card.created_by != "emuru-owner"
            or (
                (card.assignee is not None or card.status != "blocked")
                if run_id is None
                else (
                    card.assignee != CODER
                    or card.status != "running"
                    or card.claim_lock != CODER
                    or card.current_run_id != run_id
                )
            )
            or binding.get("schema") != 1
            or binding.get("revision") != grant["revision"]
            or binding.get("proposal") != grant["proposal"]
            or binding.get("approval") != grant["approval"]
            or task not in approved_tasks
            or digest(task) != grant["task_digest"]
            or "scout" in binding
            or "coder" in binding
        ):
            raise ValueError("Native task does not match the approved coder grant.")
        return grant


def create_attempt(home, grant):
    """Exclusive-create the immutable grant and intent before native claim."""
    root = Path(home) / "emuru-projects"
    with directory(root) as parent:
        private_directory(parent)
        try:
            os.mkdir("coder-attempts", 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    attempts = root / "coder-attempts"
    with directory(attempts) as parent:
        private_directory(parent)
        os.mkdir(grant["grant_id"], 0o700, dir_fd=parent)
        os.fsync(parent)
    folder = attempts / grant["grant_id"]
    _write_new(folder, "grant.json", json.dumps(grant, sort_keys=True).encode())
    _write_new(
        folder,
        "intent.json",
        json.dumps(
            {
                "schema": 1,
                "task": grant["task"],
                "grant_sha256": digest(grant),
                "phase": "intent",
            },
            sort_keys=True,
        ).encode(),
    )
    return folder


def claim(home, vault, root, task_id, grant, folder):
    """Claim original task only after durable intent and shared exclusion checks."""
    authorize(home, vault, root, task_id, grant)
    with board.project_board(home, root) as (kb, db):
        active = _active_work(kb, db, task_id)
        if active:
            raise ValueError("Another Scout or coder task is active or unresolved.")
        card = kb.get_task(db, task_id)
        if not card or not kb.assign_task(db, task_id, CODER):
            raise ValueError("Native coder assignment refused.")
        if not kb.unblock_task(db, task_id):
            raise ValueError("Native coder authorization refused.")
        claimed = kb.claim_task(db, task_id, claimer=CODER, ttl_seconds=660)
        if not claimed or claimed.assignee != CODER or claimed.claim_lock != CODER:
            raise ValueError("Native coder claim refused.")
        run = {"task": task_id, "run": claimed.current_run_id}
        _write_new(folder, "run.json", json.dumps(run, sort_keys=True).encode())
        return run


def process_event(folder, name, kind, phase):
    if (
        not name.startswith("emuru-coder-")
        or len(name) > 128
        or kind not in {"inference", "check", "gateway"}
        or phase not in {"starting", "stopped", "uncertain"}
    ):
        raise ValueError("Invalid coder process identity.")
    _append(
        folder,
        "processes.jsonl",
        {"name": name, "kind": kind, "phase": phase},
    )


def _processes(folder):
    path = Path(folder) / "processes.jsonl"
    try:
        raw = _read_private(path, 8192)
    except FileNotFoundError:
        return []
    events = [json.loads(line) for line in raw.splitlines()]
    states = {}
    kinds = {}
    for event in events:
        if (
            len(events) > 20
            or set(event) != {"name", "kind", "phase"}
            or not isinstance(event["name"], str)
            or not event["name"].startswith("emuru-coder-")
            or event["kind"] not in {"inference", "check", "gateway"}
            or (event["name"] in kinds and kinds[event["name"]] != event["kind"])
            or event["phase"] not in {"starting", "stopped", "uncertain"}
            or (event["phase"] == "starting" and event["name"] in states)
            or (event["phase"] == "stopped" and states.get(event["name"]) != "starting")
            or (event["phase"] == "uncertain" and event["name"] not in states)
        ):
            raise ValueError("Coder process ledger is invalid.")
        states[event["name"]] = event["phase"]
        kinds[event["name"]] = event["kind"]
    return events


def finish(home, vault, root, task_id, grant, folder, run):
    task_run = {"task": task_id, "run": run["run"]}
    data = _read_private(Path(folder) / "evidence.json", LIMITS["artifact_bytes"])
    report = verify_evidence(
        folder, hashlib.sha256(data).hexdigest(), task_run=task_run
    )
    events = _processes(folder)
    processes = {}
    for event in events:
        processes[event["name"]] = event["phase"]
    if any(phase != "stopped" for phase in processes.values()):
        raise ValueError("Coder process termination is not verified.")
    if report["success"] and not {"gateway", "inference", "check"} <= {
        event["kind"] for event in events
    }:
        raise ValueError("Coder completion lacks required process observations.")
    if not report["success"]:
        return _record_failure(home, root, task_id, grant, folder, run, report)
    try:
        authorize(home, vault, root, task_id, grant, run_id=run["run"])
    except ValueError:
        return _record_failure(
            home,
            root,
            task_id,
            grant,
            folder,
            run,
            report,
            failure="authorization_stale",
        )
    with board.project_board(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        _require_run(card, task_id, run["run"])
        refs = _attach_artifacts(kb, db, task_id, grant, folder, run["run"])
        receipt = {
            "schema": 1,
            "status": "tested local result / awaiting owner review",
            "task": task_id,
            "run": run["run"],
            "grant_id": grant["grant_id"],
            "grant_sha256": digest(grant),
            "evidence_sha256": hashlib.sha256(data).hexdigest(),
            "diff_sha256": report["diff_sha256"],
            "attachments": refs,
        }
        _verify_artifact_rows(
            kb,
            db,
            task_id,
            grant,
            folder,
            {
                "evidence.json": receipt["evidence_sha256"],
                "diff.patch": report["diff_sha256"],
            },
        )
        if not kb.complete_task(
            db,
            task_id,
            result=json.dumps(receipt, sort_keys=True),
            metadata={"coder": receipt},
            expected_run_id=run["run"],
        ):
            raise ValueError("Native coder completion refused.")
    return receipt


def _require_run(card, task_id, run_id):
    if (
        not card
        or card.status != "running"
        or card.assignee != CODER
        or card.current_run_id != run_id
        or card.claim_lock != CODER
    ):
        raise ValueError("Native coder run ownership changed.")


def _attach_artifacts(kb, db, task_id, grant, folder, run_id):
    refs = []
    for filename in ("evidence.json", "diff.patch"):
        card = kb.get_task(db, task_id)
        _require_run(card, task_id, run_id)
        path = Path(folder) / filename
        data = _read_private(path, grant["limits"]["artifact_bytes"])
        ref = next(
            (
                item.id
                for item in kb.list_attachments(db, task_id)
                if item.filename == filename
                and item.stored_path == str(path)
                and item.size == len(data)
                and item.uploaded_by == "emuru-coder-runner"
            ),
            None,
        )
        if ref is None:
            ref = kb.add_attachment(
                db,
                task_id,
                filename=filename,
                stored_path=str(path),
                content_type="application/json"
                if filename.endswith("json")
                else "text/x-diff",
                size=len(data),
                uploaded_by="emuru-coder-runner",
            )
        refs.append(ref)
    return refs


def _verify_artifact_rows(kb, db, task_id, grant, folder, expected):
    retained = {}
    rows = kb.list_attachments(db, task_id)
    for filename in ("evidence.json", "diff.patch"):
        path = Path(folder) / filename
        data = _read_private(path, grant["limits"]["artifact_bytes"])
        if hashlib.sha256(data).hexdigest() != expected[filename]:
            raise ValueError("Coder attachment content digest changed.")
        matches = [
            item
            for item in rows
            if item.filename == filename
            and item.stored_path == str(path)
            and item.size == len(data)
            and item.uploaded_by == "emuru-coder-runner"
        ]
        if (
            len(matches) != 1
            or _read_private(
                Path(matches[0].stored_path), grant["limits"]["artifact_bytes"]
            )
            != data
        ):
            raise ValueError("Coder attachment identity does not verify.")
        retained[filename] = hashlib.sha256(data).hexdigest()
    return retained


def status(kb, db, card, home, root):
    """Project native facts only after reopening the private evidence bytes."""
    folder = _find_attempt(home, card.id)
    if folder is None:
        return {"status": "uncertain", "detail": "coder intent unavailable"}
    grant = load_grant(folder / "grant.json")
    intent = _read_json(folder / "intent.json")
    if intent.get("grant_sha256") != digest(grant) or intent.get("task") != card.id:
        raise ValueError("Coder intent binding changed.")
    try:
        run = _read_json(folder / "run.json")
    except FileNotFoundError:
        return {"status": "uncertain", "detail": "claim/run binding incomplete"}
    if (
        set(run) != {"task", "run"}
        or run["task"] != card.id
        or type(run["run"]) is not int
        or run["run"] <= 0
    ):
        raise ValueError("Coder run binding changed.")
    events = _processes(folder)
    process_state = {}
    for event in events:
        process_state[event["name"]] = event["phase"]
    receipt = None
    try:
        receipt = json.loads(card.result) if card.result else None
    except (TypeError, json.JSONDecodeError):
        pass
    if card.status == "done" and isinstance(receipt, dict):
        task_run = {"task": card.id, "run": run["run"]}
        if (
            receipt.get("status") != "tested local result / awaiting owner review"
            or receipt.get("task") != card.id
            or receipt.get("run") != run["run"]
            or receipt.get("grant_id") != grant["grant_id"]
            or receipt.get("grant_sha256") != digest(grant)
        ):
            raise ValueError("Native coder receipt does not match its grant/run.")
        report_data = _read_private(folder / "evidence.json", LIMITS["artifact_bytes"])
        report = verify_evidence(
            folder, hashlib.sha256(report_data).hexdigest(), task_run=task_run
        )
        if (
            report["success"] is not True
            or receipt.get("evidence_sha256") != hashlib.sha256(report_data).hexdigest()
            or receipt.get("diff_sha256") != report["diff_sha256"]
            or len(receipt.get("attachments", [])) != 2
        ):
            raise ValueError("Coder completion evidence no longer verifies.")
        retained = _verify_artifact_rows(
            kb,
            db,
            card.id,
            grant,
            folder,
            {
                "evidence.json": hashlib.sha256(report_data).hexdigest(),
                "diff.patch": report["diff_sha256"],
            },
        )
        for attachment_id, expected_name in zip(
            receipt["attachments"], ("evidence.json", "diff.patch"), strict=True
        ):
            attachment = kb.get_attachment(db, attachment_id)
            expected_path = str(folder / expected_name)
            expected_data = _read_private(
                folder / expected_name, grant["limits"]["artifact_bytes"]
            )
            if (
                not attachment
                or attachment.task_id != card.id
                or attachment.filename != expected_name
                or attachment.stored_path != expected_path
                or attachment.size != len(expected_data)
                or retained[expected_name] != hashlib.sha256(expected_data).hexdigest()
            ):
                raise ValueError("Coder attachment identity does not verify.")
        return {
            "status": "tested local result / awaiting owner review",
            "evidence": receipt,
        }
    if card.current_run_id != run["run"]:
        return {"status": "uncertain", "detail": "native run identity changed"}
    if card.status == "running":
        for name, phase in process_state.items():
            if phase != "starting":
                continue
            observed = bounded_process(["docker", "inspect", name], b"", 5, 65536)
            if not observed["exit_code"]:
                rows = json.loads(observed["stdout"])
                if len(rows) == 1 and rows[0].get("State", {}).get("Running"):
                    return {"status": "running", "run": run["run"]}
        if process_state and all(
            phase == "stopped" for phase in process_state.values()
        ):
            try:
                report_data = _read_private(
                    folder / "evidence.json", LIMITS["artifact_bytes"]
                )
                report = verify_evidence(
                    folder,
                    hashlib.sha256(report_data).hexdigest(),
                    task_run={"task": card.id, "run": run["run"]},
                )
            except FileNotFoundError:
                return {"status": "uncertain", "detail": "checked evidence unavailable"}
            return {"status": "awaiting reconciliation", "run": run["run"]}
        return {"status": "uncertain", "detail": "attempt process state unproven"}
    try:
        report_data = _read_private(folder / "evidence.json", LIMITS["artifact_bytes"])
        report = verify_evidence(
            folder,
            hashlib.sha256(report_data).hexdigest(),
            task_run={"task": card.id, "run": run["run"]},
        )
        if report.get("success") is False or report.get("failure"):
            retained = _verify_artifact_rows(
                kb,
                db,
                card.id,
                grant,
                folder,
                {
                    "evidence.json": hashlib.sha256(report_data).hexdigest(),
                    "diff.patch": report["diff_sha256"],
                },
            )
            return {
                "status": "tests_failed"
                if report.get("failure") == "tests_failed"
                else "blocked",
                "run": run["run"],
                "failure": report.get("failure") or "blocked",
                "evidence": {
                    "evidence_sha256": hashlib.sha256(report_data).hexdigest(),
                    "diff_sha256": report["diff_sha256"],
                    **retained,
                },
            }
    except FileNotFoundError:
        pass
    if events and all(phase == "stopped" for phase in process_state.values()):
        return {"status": "blocked", "run": run["run"]}
    return {
        "status": "uncertain",
        "detail": "attempt has no verified terminal evidence",
    }


def _record_failure(home, root, task_id, grant, folder, run, report, failure=None):
    failure = failure or report.get("failure") or "blocked"
    if not re.fullmatch(r"[a-z_]+", failure):
        failure = "blocked"
    with board.project_board(home, root) as (kb, db):
        card = kb.get_task(db, task_id)
        _require_run(card, task_id, run["run"])
        refs = _attach_artifacts(kb, db, task_id, grant, folder, run["run"])
        if not kb.block_task(
            db,
            task_id,
            reason=f"Coder attempt {failure}; explicit fresh grant and reconciliation required.",
            kind="needs_input",
            expected_run_id=run["run"],
        ):
            raise ValueError("Native coder failure transition refused.")
    return {
        "task": task_id,
        "run": run["run"],
        "status": "tests_failed" if failure == "tests_failed" else "blocked",
        "failure": failure,
        "attachments": refs,
    }
