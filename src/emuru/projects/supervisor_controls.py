"""Authenticated deterministic project restrictions, independent of conversation turns."""

import hashlib
import json
import os
import re
from pathlib import Path

from emuru.projects import supervisor
from emuru.vault import mutations

CONTROL_REPLIES = {
    "hold": "held; admitted stage may still consume its original remaining allowance.",
    "continue": "ready / awaiting operator launch; no work launched.",
    "cancel": "stop_requested; enrollment revoked; ownership and effects remain uncertain until independent stop proof.",
}


def emergency_size(records):
    """Reserve actual worst-case restrictive receipts plus a bounded sleep-fence receipt."""
    from emuru.projects.scout import encoded

    current = {
        (r["project"], r["task"]): r for r in records if r["kind"] == "enrollment"
    }
    size = 512
    for value in current.values():
        for action in ("hold", "cancel"):
            receipt = {
                "schema": 1,
                "generation": 99999999,
                "previous": "f" * 64,
                "kind": "control",
                "project": value["project"],
                "task": value["task"],
                "enrollment": value["generation"],
                "action": action,
                "request": "f" * 32,
                "text_hash": "f" * 64,
                "response": CONTROL_REPLIES[action],
                "token": "f" * 64,
                "grant_sha256": value["grant_sha256"],
            }
            size += len(encoded(receipt))
    return size


def validate_record(value, records):
    if value["kind"] == "control-receipt":
        if (
            set(value)
            != {
                "schema",
                "generation",
                "previous",
                "kind",
                "request",
                "text_hash",
                "response",
            }
            or not isinstance(value["request"], str)
            or not re.fullmatch("[0-9a-f]{32}", value["request"])
            or not isinstance(value["text_hash"], str)
            or not re.fullmatch("[0-9a-f]{64}", value["text_hash"])
            or not isinstance(value["response"], str)
            or len(value["response"]) > 3500
        ):
            raise ValueError("Supervisor control outcome receipt invalid.")
        return
    fields = {
        "schema",
        "generation",
        "previous",
        "kind",
        "project",
        "task",
        "enrollment",
        "action",
        "request",
        "text_hash",
        "response",
        "token",
        "grant_sha256",
    }
    if (
        set(value) != fields
        or value["action"] not in {"hold", "continue", "cancel"}
        or type(value["enrollment"]) is not int
        or not isinstance(value["request"], str)
        or not re.fullmatch("[0-9a-f]{32}", value["request"])
        or not isinstance(value["response"], str)
        or len(value["response"]) > 3500
        or any(
            not isinstance(value[k], str) or not re.fullmatch("[0-9a-f]{64}", value[k])
            for k in ("text_hash", "token")
        )
        or not any(
            r["kind"] == "enrollment"
            and r["generation"] == value["enrollment"]
            and all(r[k] == value[k] for k in ("project", "task", "grant_sha256"))
            for r in records
        )
    ):
        raise ValueError("Supervisor control receipt invalid.")


def control_state(records, enrollment):
    state = "unrestricted"
    for record in records:
        if (
            record["kind"] == "control"
            and record["enrollment"] == enrollment["generation"]
        ):
            if state == "revoked":
                raise ValueError("Revoked enrollment history cannot change.")
            state = {"hold": "held", "continue": "unrestricted", "cancel": "revoked"}[
                record["action"]
            ]
    return state


def authenticate(home, vault, text, owner, bot, request, uid):
    queue = Path(home) / "emuru-telegram/queue.sqlite3"
    with mutations._read_database(
        Path(home) / "emuru-mutations/mutations.sqlite"
    ) as db:
        bound = db.execute(
            "SELECT owner,bot,vault,text_hash FROM owner_requests WHERE id=?",
            (request,),
        ).fetchone()
        update = db.execute(
            "SELECT request_id FROM request_updates WHERE owner=? AND bot=? AND vault=? AND update_id=?",
            (str(owner), str(bot), os.path.abspath(vault), str(uid)),
        ).fetchone()
    with mutations._read_database(queue) as db:
        claim = db.execute(
            "SELECT user_id,chat_id,text,state FROM updates WHERE bot_id=? AND update_id=?",
            (bot, uid),
        ).fetchone()
    if (
        bound
        != (
            str(owner),
            str(bot),
            os.path.abspath(vault),
            hashlib.sha256(text.encode()).hexdigest(),
        )
        or update != (request,)
        or claim != (owner, owner, text, "started")
    ):
        raise ValueError(
            "Explicit admitted owner/bot/private-chat/request bytes required."
        )


def owner_control(home, vault, root, text, owner, bot, request, uid):
    from emuru.telegram.queue import control_receipt

    authenticate(home, vault, text, owner, bot, request, uid)
    queue = Path(home) / "emuru-telegram/queue.sqlite3"
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    cached = control_receipt(queue, bot, uid, request, text_hash)
    if cached is not None:
        return cached
    try:
        return _owner_control(home, vault, root, text, owner, bot, request, uid)
    except ValueError as error:
        response = _retain_outcome(home, request, text_hash, str(error))
        return control_receipt(queue, bot, uid, request, text_hash, response=response)


def _retain_outcome(home, request, text_hash, response, *, locked=None):
    """Permanent bounded supervisor history; expiring queue copies are only transport."""
    if locked is None:
        try:
            with supervisor._locked(home, write=True) as (parent, size, marker):
                records, size = supervisor._records(parent, size, marker)
                return _retain_outcome(
                    home, request, text_hash, response, locked=(records, size)
                )
        except ValueError as error:
            raise OSError(
                "Control outcome could not be durably retained; inspect status."
            ) from error
    records, size = locked
    existing = next(
        (
            r
            for r in records
            if r["kind"] in {"control", "control-receipt"} and r["request"] == request
        ),
        None,
    )
    if existing is not None:
        if existing["text_hash"] != text_hash:
            raise ValueError("Control request identity reused with conflicting bytes.")
        return existing["response"]
    supervisor._append(
        Path(home) / "emuru-projects/supervisor",
        records,
        size,
        {
            "kind": "control-receipt",
            "request": request,
            "text_hash": text_hash,
            "response": response,
        },
    )
    return response


def _owner_control(home, vault, root, text, owner, bot, request, uid):
    from emuru.projects import board
    from emuru.telegram.queue import control_receipt

    fields = text.split()
    if (
        len(fields) != 5
        or fields[0].split("@")[0] != "/project"
        or fields[1] not in {"hold", "continue", "cancel"}
        or not re.fullmatch("[0-9a-f]{32}", fields[2])
        or not re.fullmatch("t_[0-9a-f]+", fields[3])
        or not re.fullmatch("[0-9a-f]{64}", fields[4])
    ):
        raise ValueError(
            "Use /project hold|continue|cancel <applied-spec-proposal-id> <native-task-id> <control-token>."
        )
    action, proposal, task, token = fields[1:]
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        existing = next(
            (
                r
                for r in records
                if r["kind"] in {"control", "control-receipt"}
                and r["request"] == request
            ),
            None,
        )
        if existing:
            if existing["text_hash"] != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError(
                    "Control request identity reused with conflicting bytes."
                )
            return existing["response"]
        queue_path = Path(home) / "emuru-telegram/queue.sqlite3"
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        cached = control_receipt(queue_path, bot, uid, request, text_hash)
        if cached is not None:
            return cached
        enrollment = next(
            (
                r
                for r in reversed(records)
                if r["kind"] == "enrollment" and r["task"] == task
            ),
            None,
        )
        if enrollment is None or enrollment["proposal"] != proposal:
            raise ValueError("Exact current project/task enrollment required.")
        authority = enrollment.get("authority")
        if not authority or any(
            authority[k] != v
            for k, v in (
                ("owner", owner),
                ("bot", bot),
                ("vault", os.path.abspath(vault)),
            )
        ):
            raise ValueError(
                "Control authority does not match this private enrollment."
            )
        if enrollment["identity"] != supervisor.identity(
            root, enrollment["identity"]["image"]
        ):
            raise ValueError("Supervisor source/image/pin/schema incompatible.")
        preview = mutations.approved_project(
            home, vault, proposal, owner, bot, readonly=True
        )
        if action == "continue" and (
            not preview["current"] or preview["after_revision"] != authority["revision"]
        ):
            raise ValueError("Project approval changed; no control committed.")
        with board.project_board_readonly(home, root) as (kb, db):
            card = kb.get_task(db, task)
            binding = json.loads(card.body) if card else {}
            if (
                not card
                or card.tenant != enrollment["project"]
                or any(
                    binding.get(key) != expected
                    for key, expected in (
                        ("proposal", proposal),
                        ("revision", authority["revision"]),
                        ("approval", authority["approval"]),
                    )
                )
            ):
                raise ValueError("Cross-project native task refused.")
            claimed = next(
                (
                    r
                    for r in reversed(records)
                    if r.get("enrollment") == enrollment["generation"]
                    and r.get("phase") == "claimed"
                ),
                None,
            )
            if claimed:
                child = kb.get_task(db, claimed["native_task"])
                if child is None or child.current_run_id not in (None, claimed["run"]):
                    raise ValueError("Native run superseded; no control committed.")
            _, expected, _ = supervisor.token_binding(records, enrollment, card)
        if token != expected:
            raise ValueError("Stale control token; inspect status again.")
        state = control_state(records, enrollment)
        if card.status in {"done", "archived"}:
            if action == "continue":
                raise ValueError("Terminal work cannot be reopened.")
            response = "Terminal outcome unchanged; no further stage authorized."
            _retain_outcome(home, request, text_hash, response, locked=(records, size))
            return control_receipt(
                queue_path,
                bot,
                uid,
                request,
                text_hash,
                response=response,
            )
        if action == "continue":
            if enrollment["stage"] == "scout" and any(
                r.get("enrollment") == enrollment["generation"]
                and r.get("phase") == "admitted"
                for r in records
            ):
                from emuru.projects.supervisor_scout import projection

                proven = projection(home, root, records, enrollment)
                if (
                    proven.get("execution") != "finished"
                    or proven.get("verified_outcome")
                    != "verified read-only scout report"
                ):
                    raise ValueError(
                        "Scout ownership/outcome uncertain; continue cannot replay attempted work."
                    )
            if (
                state != "held"
                or enrollment["boot_id"] != supervisor.boot_id()
                or supervisor.time.monotonic_ns() >= enrollment["deadline_ns"]
            ):
                raise ValueError(
                    "Continue requires held, unchanged, unexpired and unrevoked authority."
                )
            response = CONTROL_REPLIES["continue"]
        elif action == "hold":
            response = CONTROL_REPLIES["hold"]
        else:
            response = (
                CONTROL_REPLIES["cancel"]
                if any(
                    r.get("enrollment") == enrollment["generation"]
                    and r.get("phase") == "admitted"
                    for r in records
                )
                else "revoked / unstarted; no further authorized stage."
            )
        if state == "revoked":
            response = "revoked; no further authorized stage."
        if state == "revoked" or action == "hold" and state == "held":
            _retain_outcome(home, request, text_hash, response, locked=(records, size))
            return control_receipt(
                queue_path, bot, uid, request, text_hash, response=response
            )
        supervisor._append(
            Path(home) / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "control",
                "project": enrollment["project"],
                "task": task,
                "enrollment": enrollment["generation"],
                "action": action,
                "request": request,
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "response": response,
                "token": token,
                "grant_sha256": enrollment["grant_sha256"],
            },
            emergency=action in {"hold", "cancel"},
        )
        return control_receipt(
            queue_path, bot, uid, request, text_hash, response=response
        )


def outcome(home, vault, owner, bot, uid):
    """Read the committed transport outcome after failed delivery; never replay controls."""
    from emuru.telegram.queue import control_receipt

    with mutations._read_database(
        Path(home) / "emuru-mutations/mutations.sqlite"
    ) as db:
        row = db.execute(
            "SELECT r.id,r.text_hash FROM owner_requests r JOIN request_updates u ON u.request_id=r.id WHERE u.owner=? AND u.bot=? AND u.vault=? AND u.update_id=?",
            (str(owner), str(bot), os.path.abspath(vault), str(uid)),
        ).fetchone()
    if row is None:
        return None
    cached = control_receipt(
        Path(home) / "emuru-telegram/queue.sqlite3", bot, uid, *row
    )
    if cached is not None:
        return cached
    if not supervisor.enabled(home):
        return None
    with supervisor._locked(home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
        return next(
            (
                r["response"]
                for r in records
                if r["kind"] in {"control", "control-receipt"}
                and r["request"] == row[0]
                and r["text_hash"] == row[1]
            ),
            None,
        )
