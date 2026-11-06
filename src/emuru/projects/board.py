"""Owner-only tracking through pinned native Kanban APIs; no execution surface.

Bindings live in native card bodies (schema 1), not a second task database.
Each native create commits independently. A serialized retry fills missing cards
using stable project/revision/task keys, including archived cards.
"""

import fcntl
import hashlib
import importlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from emuru.vault import mutations


def spec_tasks(content):
    """Read the bounded M1 task format from an approved snapshot, never prose commands."""
    identities = re.findall(r"(?m)^- Spec: ([a-z][a-z0-9-]{0,63})\r?$", content)
    sections = re.findall(r"(?ms)^## Tasks\r?\n(.*?)(?=^## |\Z)", content)
    if len(identities) != 1 or len(sections) != 1:
        raise ValueError("Spec needs one M1 Spec ID and one Tasks section.")
    spec_id = identities[0]
    tasks = []
    for line in sections[0].splitlines():
        if not line.strip():
            continue
        task = re.fullmatch(
            r"- \[ \] (.{1,1000}) — status: todo \^task-"
            + re.escape(spec_id)
            + r"-([a-z][a-z0-9-]{0,63})",
            line,
        )
        check = re.fullmatch(
            r"  - Planned check: (.{1,1000}); expected: (.{1,1000})", line
        )
        if task:
            tasks.append({"id": task[2], "description": task[1], "acceptance": []})
        elif check and tasks and line.count("; expected:") == 1:
            tasks[-1]["acceptance"].append(
                {"command": check[1], "expected_result": check[2]}
            )
        else:
            raise ValueError(
                "Tasks must use the M1 unchecked task and planned-check format."
            )
    if (
        not 1 <= len(tasks) <= 20
        or len({task["id"] for task in tasks}) != len(tasks)
        or any(not 1 <= len(task["acceptance"]) <= 20 for task in tasks)
    ):
        raise ValueError(
            "Spec needs 1..20 unique tasks, each with 1..20 planned checks."
        )
    return tasks


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def native_api(root):
    """Fail closed on a changed native board/dispatcher implementation."""
    contract = json.loads(
        (root / "infra/hermes/kanban-native-contract.json").read_text()
    )
    kb = importlib.import_module("hermes_cli.kanban_db")
    native = Path(kb.__file__).parents[1]
    for name, expected in contract["source_sha256"].items():
        source = native / (name.replace(".", "/") + ".py")
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned native Kanban contract changed.")
    return kb


@contextmanager
def project_board(home, root):
    """One private board under the persistent profile; serialize binder retries."""
    kb = native_api(root)
    from hermes_cli.kanban_db_connect import connect_closing

    with mutations.directory(home) as profile:
        mutations.private_directory(profile)
        try:
            os.mkdir("emuru-projects", 0o700, dir_fd=profile)
        except FileExistsError:
            pass
    folder = home / "emuru-projects"
    with mutations.directory(folder) as parent:
        mutations.private_directory(parent)
        lock = os.open(
            "binder.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        try:
            mutations.regular(lock, private=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name in (
                "kanban.db",
                "kanban.db.init.lock",
                "kanban.db-journal",
                "kanban.db-wal",
                "kanban.db-shm",
            ):
                try:
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    if name not in {"kanban.db", "kanban.db.init.lock"}:
                        continue
                    fd = os.open(
                        name,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                try:
                    mutations.regular(fd, private=True)
                finally:
                    os.close(fd)
            with connect_closing(Path(f"/proc/self/fd/{parent}/kanban.db")) as db:
                yield kb, db
        finally:
            os.close(lock)


@contextmanager
def project_board_readonly(home, root):
    """Inspect pinned DB/WAL snapshots without touching native state or sidecars."""
    from emuru.telegram.queue import QueueDiagnosticsError, _diagnostic_snapshot

    kb = native_api(root)
    path = Path(home) / "emuru-projects/kanban.db"
    descriptors = [None, None]
    try:
        with mutations.directory(path.parent) as parent:
            mutations.private_directory(parent)
            for suffix in ("", ".init.lock", "-journal", "-wal", "-shm"):
                try:
                    fd = os.open(
                        path.name + suffix,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    if not suffix:
                        raise
                    continue
                try:
                    mutations.regular(fd, private=True)
                    if suffix == "-journal" and os.fstat(fd).st_size:
                        raise ValueError("Database recovery required before lookup.")
                    if suffix in {"", "-wal"}:
                        descriptors[0 if not suffix else 1] = fd
                        fd = None
                finally:
                    if fd is not None:
                        os.close(fd)
            with _diagnostic_snapshot(path, descriptors=descriptors) as snapshot:
                try:
                    journal = os.open(
                        path.name + "-journal",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    pass
                else:
                    try:
                        mutations.regular(journal, private=True)
                        if os.fstat(journal).st_size:
                            raise ValueError(
                                "Database recovery required before lookup."
                            )
                    finally:
                        os.close(journal)
                if descriptors[1] is None:
                    try:
                        os.stat(
                            path.name + "-wal", dir_fd=parent, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError("Database changed during lookup; query again.")
                with closing(
                    sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
                ) as db:
                    db.execute("PRAGMA query_only=ON")
                    db.row_factory = sqlite3.Row
                    yield kb, db
    except QueueDiagnosticsError:
        raise ValueError("Database changed during lookup; query again.") from None
    finally:
        for fd in descriptors:
            if fd is not None:
                os.close(fd)


def project_command(home, vault, root, text, owner, bot):
    """Called only by the admitted owner transport, behind its tools-started fence."""
    fields = text.split()
    if (
        len(fields) != 3
        or fields[0].split("@")[0] != "/project"
        or fields[1] not in {"track", "status"}
        or not re.fullmatch(r"[0-9a-f]{32}", fields[2])
    ):
        raise ValueError(
            "Use /project track <applied-spec-proposal-id> or /project status <applied-spec-proposal-id>."
        )
    request = mutations.authenticated_request(home, vault)
    with mutations.journal(home) as journal:
        binding = journal.execute(
            "SELECT owner,bot,vault,text_hash FROM owner_requests WHERE id=?",
            (request,),
        ).fetchone()
    if binding != (
        str(owner),
        str(bot),
        os.path.abspath(vault),
        hashlib.sha256(text.encode()).hexdigest(),
    ):
        raise ValueError("Explicit authenticated owner project command required.")
    preview = mutations.approved_project(home, vault, fields[2], owner, bot)
    project = _digest([str(owner), str(bot), os.path.abspath(vault), preview["path"]])
    revision = preview["after_revision"]
    creating = fields[1] == "track"
    if creating and not preview["current"]:
        raise ValueError(
            "Approved spec is stale. Approve the current note before tracking."
        )
    tasks = spec_tasks(preview["content"])
    context = project_board if creating else project_board_readonly
    with context(home, root) as (kb, db):
        cards = kb.list_tasks(db, tenant=project, include_archived=True)
        if creating:
            for card in cards:
                binding = json.loads(card.body)
                if binding.get("schema") != 1 or binding.get("revision") != revision:
                    raise ValueError(
                        "Project already tracks another spec revision. Explicit reconciliation required; no cards changed."
                    )
            known = {card.idempotency_key: card for card in cards}
            for task in tasks:
                key = "emuru:" + _digest([project, revision, task["id"]])
                if key not in known:
                    identity = kb.create_task(
                        db,
                        title=task["description"],
                        body=json.dumps(
                            {
                                "schema": 1,
                                "revision": revision,
                                "proposal": preview["proposal_id"],
                                "approval": preview["approval_digest"],
                                "task": task,
                            },
                            sort_keys=True,
                        ),
                        tenant=project,
                        idempotency_key=key,
                        created_by="emuru-owner",
                        initial_status="blocked",
                        workspace_kind="scratch",
                        project_id="",
                        max_retries=0,
                    )
                    known[key] = kb.get_task(db, identity)
            cards = kb.list_tasks(db, tenant=project, include_archived=True)
        lines = [
            f"Project: {preview['path']}",
            "Tracking does not authorize execution. Native automatic dispatch remains disabled.",
            f"Approved revision: {revision}",
        ]
        if not preview["current"]:
            lines.append(
                "Note changed since approval; tracked cards have not been reconciled."
            )
        if not cards:
            lines.append(
                "No tracked tasks. Use /project track with this applied spec receipt."
            )
        revisions = {json.loads(card.body)["revision"] for card in cards}
        if revisions and revisions != {revision}:
            lines.append(
                "Tracked cards belong to another spec revision; explicit reconciliation required."
            )
        elif cards and sum(
            "scout" not in json.loads(card.body) for card in cards
        ) < len(tasks):
            lines.append(
                "Partial task set. Repeat /project track with this receipt to fill missing cards."
            )
        for card in cards:
            parents = ", ".join(kb.parent_ids(db, card.id)) or "none"
            binding = json.loads(card.body)
            if "scout" in binding:
                from emuru.projects.scout_board import status

                report = status(kb, db, card, home)
                lines.append(
                    f"{card.id}: read-only scout {report['status']}; source task: {binding['scout']['source_task']}"
                )
                if "evidence" in report:
                    evidence = report["evidence"]
                    lines.append(
                        f"Repository: {evidence['repository']}; baseline: {evidence['baseline']}; report SHA-256: {evidence['sha256']}"
                    )
                continue
            from emuru.projects.coder_board import _find_attempt

            try:
                coder_intent = _find_attempt(home, card.id) is not None
            except (OSError, ValueError, TypeError, KeyError):
                coder_intent = True
            if (
                coder_intent
                or card.assignee == "emuru-coder"
                or card.result
                and '"grant_id"' in card.result
            ):
                from emuru.projects.coder_board import status

                report = status(kb, db, card, home, root)
                lines.append(
                    f"{card.id} ({binding['task']['id']}): coder {report['status']}; parents: {parents}"
                )
                if report.get("actor"):
                    lines.append(f"Actor: {report['actor']}; run: {report['run']}")
                evidence = report.get("evidence")
                if evidence:
                    lines.append(
                        f"Evidence SHA-256: {evidence['evidence_sha256']}; diff SHA-256: {evidence['diff_sha256']}"
                    )
                    if evidence.get("reviewer_profile"):
                        lines.append(
                            f"Reviewer: {evidence['reviewer_profile']}; coder run: {evidence['coder_run']}; review run: {evidence['run']}; verdict: {evidence['verdict']['verdict']}"
                        )
                        lines.append(f"Review reason: {evidence['verdict']['summary']}")
                elif report.get("detail"):
                    lines.append(f"Coder detail: {report['detail']}")
                continue
            lines.append(
                f"{card.id} ({binding['task']['id']}): native {card.status}; parents: {parents}"
            )
        lines.append(
            "Native done does not establish independent review or project completion."
        )
        return "\n".join(lines)
