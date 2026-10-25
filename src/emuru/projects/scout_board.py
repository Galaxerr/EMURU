"""Trusted scout ledger bridge. The inspection container never mounts this board."""

import json
import os
import re
from pathlib import Path

from emuru.models.gateway import write_private
from emuru.projects.board import project_board
from emuru.projects.scout import MAX_REPORT_BYTES, SCOUT, digest, encoded, limit_runtime
from emuru.vault import mutations


def claim(home, root, grant):
    """One immutable grant/card per source task, baseline, selection and budget."""
    with project_board(home, root) as (kb, db):
        source = kb.get_task(db, grant["source_task"])
        binding = json.loads(source.body) if source else {}
        if (
            not source
            or source.created_by != "emuru-owner"
            or source.assignee is not None
            or source.status != "blocked"
            or binding.get("schema") != 1
            or "scout" in binding
            or not all(
                re.fullmatch(r"[0-9a-f]{64}", binding.get(key, ""))
                for key in ("revision", "approval")
            )
            or not re.fullmatch(r"[0-9a-f]{32}", binding.get("proposal", ""))
        ):
            raise ValueError("Scout requires an unchanged M2 tracked task.")
        body = {**binding, "scout": grant}
        key = "emuru-scout:" + digest([source.id, source.tenant, body])
        cards = kb.list_tasks(db, tenant=source.tenant, include_archived=True)
        existing = next((card for card in cards if card.idempotency_key == key), None)
        if existing:
            return status(kb, db, existing, home)
        for active in kb.list_tasks(db, include_archived=True):
            if active.assignee == SCOUT and active.status in {
                "running",
                "ready",
                "blocked",
            }:
                return status(kb, db, active, home)
        identity = kb.create_task(
            db,
            title="Read-only repository reconnaissance",
            body=json.dumps(body, sort_keys=True),
            assignee=SCOUT,
            created_by="emuru-scout-owner",
            tenant=source.tenant,
            initial_status="blocked",
            workspace_kind="scratch",
            project_id="",
            idempotency_key=key,
            max_retries=0,
            max_runtime_seconds=grant["timeout_seconds"],
            skills=[],
        )
        if not kb.unblock_task(db, identity):
            raise ValueError("Native scout authorization refused.")
        card = kb.claim_task(
            db, identity, claimer=SCOUT, ttl_seconds=grant["timeout_seconds"] + 60
        )
        if not card:
            raise ValueError("Native scout claim refused.")
        return {"task": card.id, "run": card.current_run_id, "status": "claimed"}


def status(kb, db, card, home):
    result = {
        "task": card.id,
        "run": card.current_run_id,
        "status": "uncertain" if card.status in {"running", "ready"} else card.status,
    }
    if card.status == "done":
        receipt = json.loads(card.result)
        grant = json.loads(card.body)["scout"]
        if (
            any(
                receipt[key] != grant[key]
                for key in ("repository", "repository_id", "baseline")
            )
            or receipt["sha256"] != grant["report_sha256"]
        ):
            raise ValueError("Scout receipt no longer matches its grant.")
        attachment = kb.get_attachment(db, receipt["attachment"])
        if not attachment or attachment.task_id != card.id:
            raise ValueError("Scout report attachment missing.")
        path = home / "emuru-projects/scout-artifacts" / (receipt["sha256"] + ".json")
        with mutations.directory(path.parent) as parent:
            mutations.private_directory(parent)
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as stream:
                mutations.regular(stream.fileno(), private=True)
                data = stream.read(MAX_REPORT_BYTES + 1)
        if (
            len(data) > MAX_REPORT_BYTES
            or digest(json.loads(data)) != receipt["sha256"]
        ):
            raise ValueError("Scout report digest mismatch.")
        result["evidence"] = receipt
    return result


def finish(home, root, request):
    with project_board(home, root) as (kb, db):
        card = kb.get_task(db, request["task"])
        if not card or card.assignee != SCOUT or card.created_by != "emuru-scout-owner":
            raise ValueError("Not an authorized scout card.")
        if (
            card.status != "running"
            or card.current_run_id != request["run"]
            or card.claim_lock != SCOUT
        ):
            raise ValueError("Scout run ownership changed; no completion recorded.")
        if "report" not in request:
            kb.block_task(
                db,
                card.id,
                reason="Scout interrupted or inspection failed; effects uncertain. No automatic retry.",
                expected_run_id=request["run"],
            )
            return {
                "task": card.id,
                "status": "blocked",
                "detail": "uncertain; explicit reconciliation required",
            }
        report = request["report"]
        grant = json.loads(card.body)["scout"]
        if (
            digest(report) != grant["report_sha256"]
            or len(encoded(report)) > MAX_REPORT_BYTES
        ):
            raise ValueError("Scout evidence does not match the authorized snapshot.")
        # Resolve reports relative to the private board across host/container mount paths.
        folder = home / "emuru-projects/scout-artifacts"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / (digest(report) + ".json")
        write_private(path, encoded(report).decode())
        attachment = kb.add_attachment(
            db,
            card.id,
            filename=path.name,
            stored_path=str(path),
            content_type="application/json",
            size=len(encoded(report)),
            uploaded_by="emuru-scout-runner",
        )
        receipt = {
            "repository": grant["repository"],
            "repository_id": grant["repository_id"],
            "baseline": grant["baseline"],
            "sha256": digest(report),
            "attachment": attachment,
            "run": request["run"],
        }
        if not kb.complete_task(
            db,
            card.id,
            result=json.dumps(receipt, sort_keys=True),
            metadata={"scout": receipt},
            expected_run_id=request["run"],
        ):
            raise ValueError("Native scout completion refused.")
        return status(kb, db, kb.get_task(db, card.id), home)


def main():
    import sys

    os.umask(0o077)
    limit_runtime(30)
    request = json.loads(sys.stdin.buffer.read(MAX_REPORT_BYTES + 32768))
    home, root = Path(os.environ["EMURU_SCOUT_HOME"]), Path("/opt/emuru")
    if request["action"] == "claim":
        result = claim(home, root, request["grant"])
    elif request["action"] == "finish":
        result = finish(home, root, request)
    else:
        raise ValueError("Unknown scout ledger action.")
    print(encoded(result).decode())
