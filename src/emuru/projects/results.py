"""Deterministic result capture; no execution or publication authority."""

import re
from contextlib import contextmanager


def is_capture_status(text):
    if not isinstance(text, str):
        return False
    fields = text.split()
    return (
        len(fields) >= 2
        and fields[0].split("@")[0] == "/project"
        and fields[1] == "capture-status"
    )


def format_capture(note, anchor, detail):
    """Preserve every existing byte, adding one bounded task detail."""
    if (
        not isinstance(anchor, str)
        or len(anchor.encode()) > 4096
        or not re.fullmatch(r"- \[[ xX]\] [^\r\n]+", anchor)
        or not isinstance(detail, str)
        or len(detail.encode()) > 4096
        or any(ord(c) < 32 for c in detail)
    ):
        raise ValueError("A bounded, exact existing task anchor is required.")
    lines = note.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == anchor]
    if len(matches) != 1:
        raise ValueError("Task anchor must match exactly one complete line.")
    index = matches[0]
    line = lines[index]
    newline = "\r\n" if line.endswith("\r\n") else "\n"
    lines[index] = (
        line + ("" if line.endswith("\n") else newline) + "  - " + detail + newline
    )
    return "".join(lines)


def verify_completion(home, vault, root, spec, task, run, owner, bot):
    """Reopen a stopped native completion, never infer it from a model summary."""
    import json

    from emuru.projects import board, coder_board, reviewer
    from emuru.projects.coder import load_grant
    from emuru.projects.scout import digest
    from emuru.vault import mutations as m

    folder = coder_board._find_attempt(home, task)
    if folder is None:
        raise ValueError("No verified coding attempt.")
    grant = load_grant(folder / "grant.json")
    if (grant["proposal"], str(grant["owner"]), str(grant["bot"])) != (
        spec,
        str(owner),
        str(bot),
    ):
        raise ValueError("Capture owner/spec binding mismatch.")
    with board.project_board_readonly(home, root) as (kb, db):
        receipt = reviewer.verify(
            home, vault, root, task, grant, folder, kb, db, capture=True
        )
        card = kb.get_task(db, task)
        native_run = kb.get_run(db, run)
        if (
            receipt["status"] != "verified"
            or receipt["run"] != run
            or card.status != "done"
            or native_run is None
            or native_run.outcome != "completed"
            or native_run.ended_at is None
            or json.loads(card.result or "null") != receipt
            or reviewer._read(folder / "review/receipt.json", reviewer.OUTPUT_BYTES)
            != receipt
        ):
            raise ValueError("Capture requires matching terminal review evidence.")
        task_binding = json.loads(card.body)["task"]
    preview = m.approved_project(home, vault, spec, owner, bot, readonly=True)
    binding = {
        "schema": 1,
        "owner": str(owner),
        "bot": str(bot),
        "vault": str(vault.resolve()),
        "project": grant["project"],
        "spec": spec,
        "revision": grant["revision"],
        "task": task,
        "coder_run": receipt["coder_run"],
        "review_run": run,
        "grant_sha256": digest(grant),
        **{
            k: receipt[k]
            for k in (
                "evidence_sha256",
                "diff_sha256",
                "packet_sha256",
                "outcome_sha256",
            )
        },
    }
    ids = re.findall(r"(?m)^- Spec: ([a-z][a-z0-9-]{0,63})\r?$", preview["content"])
    if len(ids) != 1:
        raise ValueError("Invalid approved spec identity.")
    block = "^task-" + ids[0] + "-" + task_binding["id"]
    detail = (
        f"Verified artifact-only result; task {task}, coder run {receipt['coder_run']}, "
        f"review run {run}; evidence SHA-256 {receipt['evidence_sha256']}; "
        f"diff SHA-256 {receipt['diff_sha256']}. Fixed Monty checks passed; "
        "native unittest completion and native/Monty equivalence unverified. "
        "Next action: approve this capture separately; confirm indexed readback after application."
    )
    return binding, preview["path"], block, detail


def command(home, vault, root, text, owner, bot):
    """Admitted private-owner command; return existing preview or observational status."""
    import hashlib
    import json
    import os
    import sys

    from emuru.projects.scout import digest, encoded
    from emuru.vault import mutations as m
    from emuru.vault.indexer import vault_open

    lines = text.splitlines()
    fields = lines[0].split() if lines else []
    if (
        len(fields) != 5
        or fields[0].split("@")[0] != "/project"
        or fields[1] not in {"capture", "capture-status", "capture-new"}
        or not re.fullmatch("[0-9a-f]{32}", fields[2])
        or not re.fullmatch(r"t_[0-9a-f]+", fields[3])
        or not re.fullmatch(r"[1-9][0-9]{0,18}", fields[4])
        or len(lines) > 2
        or len(lines) == 2
        and not lines[1].startswith("anchor: ")
    ):
        raise ValueError(
            "Use /project capture, capture-new or capture-status <spec-proposal-id> <task-id> <review-run-id>."
        )
    spec, task, run = fields[2], fields[3], int(fields[4])
    request = m.authenticated_request(home, vault, read_only=True)
    database = home / "emuru-mutations/mutations.sqlite"
    with m._read_database(database) as db:
        m.check_capture_metadata(db)
        identity = db.execute(
            "SELECT owner,bot,vault,text_hash FROM owner_requests WHERE id=?",
            (request,),
        ).fetchone()
        if identity != (
            str(owner),
            str(bot),
            os.path.abspath(vault),
            hashlib.sha256(text.encode()).hexdigest(),
        ):
            raise ValueError("Explicit authenticated owner capture required.")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='project_result_captures'"
        ).fetchone():
            raise ValueError(
                "Explicit proposal schema upgrade required before capture."
            )
        rows = db.execute(
            "SELECT proposal_id,binding,request_id FROM project_result_captures ORDER BY generation"
        ).fetchall()
        matches = []
        for proposal_id, raw, original_request in rows:
            value = json.loads(raw)
            if (
                value["owner"],
                value["bot"],
                value["vault"],
                value["spec"],
                value["task"],
                value["review_run"],
            ) == (str(owner), str(bot), str(vault.resolve()), spec, task, run):
                matches.append((proposal_id, raw, original_request))
        if len({row[1] for row in matches}) > 1:
            raise ValueError("Conflicting capture history; preserve evidence.")
        if matches:
            replay = next((row for row in matches if row[2] == request), None)
            selected = replay or matches[-1]
            proposal_id = selected[0]
            m._check_owner(db, proposal_id, vault, owner, bot)
            result = m._preview(db, proposal_id)
            if fields[1] == "capture-status":
                return {k: result[k] for k in ("proposal_id", "path", "outcome")}
            if (replay and result["outcome"] != "proposed") or result["outcome"] in {
                "applied",
                "index_pending",
                "prepared",
                "quarantined",
            }:
                return result
            if fields[1] != "capture-new" and result["outcome"] != "proposed":
                return {k: result[k] for k in ("proposal_id", "path", "outcome")}
        elif fields[1] == "capture-status":
            return {"state": "not_captured"}
        elif fields[1] == "capture-new":
            raise ValueError("No previous capture generation; use capture first.")
    binding, path, block, detail = verify_completion(
        home, vault, root, spec, task, run, owner, bot
    )
    raw_binding = encoded(binding).decode()
    if len(raw_binding.encode()) > 4096:
        raise ValueError("Capture binding exceeds 4 KiB.")
    key = digest(binding)
    if matches:
        with m._read_database(database) as db:
            stored = db.execute(
                "SELECT binding FROM project_result_captures WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if stored is None or stored[0] != raw_binding:
                raise ValueError(
                    "Capture evidence changed; explicit fresh capture required."
                )
            if fields[1] != "capture-new":
                return m._preview(db, proposal_id)
    with m.writer_lock(vault):
        current = vault_open(vault, path, sys.maxsize)
        if len(lines) == 2:
            anchor = lines[1][len("anchor: ") :]
        else:
            anchors = [
                line for line in current["content"].splitlines() if line.endswith(block)
            ]
            if len(anchors) != 1:
                raise ValueError(
                    "Select one exact existing task using a second anchor: line."
                )
            anchor = anchors[0]
        after = format_capture(current["content"], anchor, detail)
        return m.prepare_result_capture(
            home,
            vault,
            request,
            key,
            raw_binding,
            path,
            current["revision"],
            anchor,
            after,
            renew=fields[1] == "capture-new",
        )


def verify_application(home, vault, proposal_id):
    """H3: first-write admission only; recovery remains receipt-first."""
    import json

    from emuru.projects.scout import encoded
    from emuru.vault import mutations as m

    with m._read_database(home / "emuru-mutations/mutations.sqlite") as db:
        m.check_capture_metadata(db)
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='project_result_captures'"
        ).fetchone():
            return
        row = db.execute(
            "SELECT binding FROM project_result_captures WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None or m._receipt(db, proposal_id) is not None:
            return
        old = json.loads(row[0])
    fresh = operator_verification(home, vault, old)
    if encoded(fresh) != encoded(old):
        raise ValueError("Capture evidence changed; no note was applied.")


@contextmanager
def application_guard(home, vault, proposal_id):
    from emuru.vault import mutations as m

    with m.writer_lock(vault):
        verify_application(home, vault, proposal_id)
        yield


def operator_verification(home, vault, binding):
    """No credentials, worker tools or mutation capability cross this IPC boundary."""
    import json
    import os
    from pathlib import Path

    from emuru.hermes.profile import installed_runtime
    from emuru.projects.coder_sandbox import bounded_process
    from emuru.projects.scout import encoded

    root = Path(__file__).resolve().parents[3]
    runtime = installed_runtime(root)
    payload = encoded(
        {
            "home": str(home),
            "vault": str(vault),
            "spec": binding["spec"],
            "task": binding["task"],
            "run": binding["review_run"],
            "owner": binding["owner"],
            "bot": binding["bot"],
        }
    )
    observed = bounded_process(
        [
            str(runtime / ".venv/bin/python"),
            "-I",
            "-B",
            str(root / "scripts/hermes/result-verifier.py"),
        ],
        payload,
        30,
        8192,
        env={"PATH": os.defpath},
    )
    if observed["exit_code"] != 0:
        raise ValueError(
            "Pinned-runtime capture verification refused; no note was applied."
        )
    try:
        fresh = json.loads(observed["stdout"])
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid capture verifier response.") from error
    if not isinstance(fresh, dict) or len(encoded(fresh)) > 4096:
        raise ValueError("Invalid capture verifier binding.")
    return fresh
