"""SDK-independent admission, atomic staging, claims, and crash recovery."""

import copy
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from telegram_helpers import callback_update, update

from emuru.telegram import queue as queue_module
from emuru.telegram.queue import (
    QueueDiagnosticsError,
    QueueFull,
    TelegramQueue,
    admit_update,
    diagnostics,
)


@pytest.mark.parametrize("text", ["/reject", "/reject@emuru", "/reject " + "a" * 32])
def test_reject_command_keeps_owner_and_forward_guards(policy, text):
    payload = update(1, text)
    assert admit_update(payload, 42, policy, 100000, "emuru", 7) is not None
    payload["message"]["forward_origin"] = {"type": "hidden_user"}
    assert admit_update(payload, 42, policy, 100000, "emuru", 7) is None
    del payload["message"]["forward_origin"]
    payload["message"]["from"]["id"] = 43
    assert admit_update(payload, 42, policy, 100000, "emuru", 7) is None


@pytest.mark.parametrize(
    "action,command",
    [
        ("a", "/apply " + "a" * 32 + " writers-paused " + "b" * 16),
        ("r", "/cancel " + "a" * 32 + " " + "b" * 16),
    ],
)
def test_buttons_survive_queue_reopen_without_replay(tmp_path, policy, action, command):
    payload = callback_update(1, "a" * 32, "b" * 16, action)
    path = tmp_path / "queue.sqlite3"
    queue = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    assert queue.stage(7, [payload], "emuru") == ["queued"]
    queue.close()
    queue = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    try:
        row = queue.claim(7, "emuru")
        assert row["text"] == command
        assert queue.stage(7, [payload], "emuru") == ["started"]
        assert queue.claim(7, "emuru") is None
    finally:
        queue.close()


@pytest.mark.parametrize(
    "mismatch",
    [
        "sender",
        "bot",
        "group",
        "chat",
        "inline",
        "data",
        "forward",
        "message_id",
        "missing_author",
    ],
)
def test_button_admission_rejects_untrusted_envelopes(policy, mismatch):
    payload = callback_update(1, "a" * 32, "b" * 16)
    query = payload["callback_query"]
    if mismatch == "sender":
        query["from"]["id"] = 99
    elif mismatch == "bot":
        query["message"]["from"]["id"] = 99
    elif mismatch == "group":
        query["message"]["chat"]["type"] = "group"
    elif mismatch == "chat":
        query["message"]["chat"]["id"] = 99
    elif mismatch == "inline":
        del query["message"]
    elif mismatch == "data":
        query["data"] = "emuru:a:anything"
    elif mismatch == "forward":
        query["message"]["forward_origin"] = {"type": "hidden_user"}
    elif mismatch == "missing_author":
        query["message"]["from"] = None
    else:
        query["message"]["message_id"] = 0
    assert queue_module.admit_update(payload, 42, policy, 100000, "emuru", 7) is None


@pytest.mark.parametrize(
    "response,command",
    [
        ("I accept the proposal", "/apply"),
        ("Yes, I approve these changes.", "/apply"),
        ("I reject the proposal", "/cancel"),
        ("I do not accept", "/revise"),
        ("Accept, but change the goal", "/revise"),
    ],
)
def test_plain_proposal_decisions_are_explicit(response, command):
    assert queue_module.proposal_response("a" * 32, "b" * 16, response).startswith(
        command + " "
    )


@pytest.mark.parametrize("response", ["I accept", "reject"])
def test_forwarded_plain_decisions_cannot_approve_latest_proposal(policy, response):
    payload = update(text=response)
    payload["message"]["forward_origin"] = {"type": "hidden_user"}
    assert queue_module.admit_update(payload, 42, policy, 100000, "emuru", 7) is None


@pytest.mark.parametrize("field", ["text", "caption"])
@pytest.mark.parametrize(
    "response,expected",
    [("I accept.", "apply"), ("I reject", "cancel"), ("Change the goal", "revise")],
)
def test_versioned_proposal_replies_work_for_text_and_attachments(
    policy, field, response, expected
):
    payload = update(text=response)
    payload["message"]["reply_to_message"] = {
        "message_id": 500,
        "from": {"id": 7, "is_bot": True},
        "chat": {"id": 42, "type": "private"},
        field: "EMURU approval: " + "a" * 32 + "\nRevision: " + "b" * 16,
    }
    text = queue_module.admit_update(payload, 42, policy, 100000, "emuru", 7)[
        "message"
    ]["text"]
    assert text.startswith(f"/{expected} " + "a" * 32)
    if expected != "revise":
        assert text.endswith("b" * 16)


def test_admission_rejects_unauthorized_media_commands_and_stale_input(policy):
    valid = update()
    assert admit_update(valid, 42, policy, 100000)["message"]["text"] == "hello"
    bad = []
    for path, value in (
        (("update_id",), True),
        (("message", "message_id"), True),
        (("message", "date"), True),
        (("message", "date"), 100301),
        (("message", "date"), 13599),
        (("message", "chat", "id"), 43),
        (("message", "chat", "type"), "group"),
        (("message", "from", "id"), 43),
        (("message", "from", "is_bot"), True),
        (("message", "from", "is_bot"), 0),
        (("message", "text"), "x" * 8001),
        (("message", "text"), "  "),
        (("message", "text"), "/terminal ls"),
        (("message", "text"), "/new@other"),
        (("message", "text"), " /stop"),
        (("message", "text"), "/STOP"),
        (("message", "photo"), [{}]),
        (("message", "document"), {}),
    ):
        raw = copy.deepcopy(valid)
        parent = raw
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = value
        bad.append(raw)
    bad += [
        {"update_id": 1, "edited_message": valid["message"]},
        {**valid, "callback_query": {}},
        {"update_id": 1, "message": None},
    ]
    for raw in bad:
        assert admit_update(raw, 42, policy, 100000) is None, raw
    assert admit_update(update(text="x" * 8000, date=13600), 42, policy, 100000)
    assert admit_update(update(date=100300), 42, policy, 100000)
    for cmd in policy["allowed_commands"]:
        assert admit_update(update(text=f"/{cmd}@emuru"), 42, policy, 100000, "emuru")
    # Reply/forward reference data is not an admission grant or a media download.
    reply = {
        **valid["message"],
        "reply_to_message": {"photo": [{}], "text": "/terminal"},
    }
    admitted = admit_update({**valid, "message": reply}, 42, policy, 100000)
    assert "reply_to_message" not in admitted["message"]


def test_atomic_batch_full_duplicate_and_cross_connection_claims(tmp_path, policy):
    policy = {**policy, "max_pending_updates": 2}
    path = tmp_path / "queue/queue.sqlite3"
    q = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    other = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    with pytest.raises(QueueFull):
        q.stage(7, [update(1), update(2), update(3)])
    assert q.status() == {}
    q.stage(7, [update(1), update(1), update(2)])
    q.stage(7, [update(1)])
    assert q.status() == {"queued": 2}
    first = q.claim(7)
    assert first["update_id"] == 1
    assert other.claim(7) is None
    q.finish(first["sequence"], True)
    assert other.claim(7)["update_id"] == 2
    q.close()
    other.close()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_recovery_never_replays_claimed_or_reporting_input(tmp_path, policy):
    path = tmp_path / "queue.sqlite3"
    q = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    q.stage(7, [update(1), update(2)])
    q.claim(7)
    q.close()
    q = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    q.recover()
    report = q.claim_report(7)
    assert report["update_id"] == 1
    q.close()  # Crash while reporting: notice must not be resent either.
    q = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    q.recover()
    assert q.claim_report(7) is None
    assert q.claim(7)["update_id"] == 2
    q.stage(7, [update(1)])
    assert q.status() == {"interrupted": 1, "started": 1}
    q.close()


def test_clock_and_current_authorization_checked_again_before_dispatch(
    tmp_path, policy
):
    now = [100000]
    path = tmp_path / "queue.sqlite3"
    q = TelegramQueue(path, policy, 42, clock=lambda: now[0])
    q.stage(7, [update()])
    now[0] += 86401
    assert q.claim(7) is None
    assert q.status() == {"expired": 1}
    q.stage(7, [update(2, date=now[0])])
    q.close()
    q = TelegramQueue(path, policy, 43, clock=lambda: now[0])
    assert q.claim(7) is None
    assert q.status() == {"expired": 1, "rejected": 1}
    q.close()


def test_completed_receipts_ttl_bound_and_per_bot_keys(tmp_path, policy):
    now = [100000]
    q = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: now[0])
    q.db.execute("BEGIN IMMEDIATE")
    q.db.executemany(
        "INSERT INTO updates(bot_id,update_id,chat_id,user_id,message_id,message_date,received_at,state,finished_at) VALUES(7,?,42,42,1,100000,100000,'completed',100000)",
        [(uid,) for uid in range(1, 4100)],
    )
    q.db.execute("COMMIT")
    q.stage(7, [])  # Prune after the last completion.
    assert q.status() == {"completed": 4099}
    q.stage(8, [update(4099)])
    row = q.claim(8)
    assert row["update_id"] == 4099
    now[0] += 86401
    q.stage(7, [])
    assert q.status() == {"completed": 4099, "started": 1}
    now[0] = 100000 + 7 * 86400 + 1
    q.stage(7, [update(1)])  # Old input is stale even after its receipt is purged.
    assert q.status() == {"started": 1}
    q.close()


def test_queue_imports_in_an_isolated_interpreter_without_mcp():
    path = Path(__file__).resolve().parents[2] / "src/emuru/telegram/queue.py"
    code = "import runpy,sys; runpy.run_path(sys.argv[1]); assert not any(n == 'mcp' or n.startswith('mcp.') for n in sys.modules)"
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(path)], check=True, timeout=10
    )


def test_real_process_crash_preserves_committed_input(tmp_path, policy):
    path = tmp_path / "queue.sqlite3"
    source = Path(__file__).resolve().parents[2] / "src/emuru/telegram/queue.py"
    code = """import runpy,sys,json,os
module=runpy.run_path(sys.argv[1])
q=module['TelegramQueue'](sys.argv[2],json.loads(sys.argv[3]),42,clock=lambda:100000)
q.stage(7,[json.loads(sys.argv[4])])
os._exit(9)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            code,
            str(source),
            str(path),
            json.dumps(policy),
            json.dumps(update()),
        ],
        timeout=10,
        check=False,
    )
    assert result.returncode == 9
    q = TelegramQueue(path, policy, 42, clock=lambda: 100000)
    assert q.claim(7)["update_id"] == 1
    q.close()


def test_schema_permissions_and_recovery_lock(tmp_path, policy):
    q = TelegramQueue(
        tmp_path / "queue/queue.sqlite3", policy, 42, clock=lambda: 100000
    )
    q.stage(7, [update()])
    row = q.claim(7, session_key="native:emuru:owner")
    assert row["session_key"] == "native:emuru:owner"
    assert q.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert q.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert q.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert q.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    for suffix in ("", "-wal", "-shm"):
        assert Path(str(q.path) + suffix).stat().st_mode & 0o777 == 0o600
    fd = os.open(q.path.parent / "instance.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            q.recover()
        assert q.status() == {"started": 1}
    finally:
        os.close(fd)
    q.recover()
    recovered = q.db.execute("SELECT * FROM updates").fetchone()
    assert recovered["state"] == "interrupted" and recovered["text"] is None
    q.close()


def test_envelopes_random_ids_duplicate_states_and_payload_shedding(tmp_path, policy):
    q = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: 100000)
    envelope = {
        "bot_id": 7,
        "update_id": 9001,
        "chat_id": 42,
        "chat_type": "private",
        "user_id": 42,
        "is_bot": False,
        "message_id": 101,
        "message_date": 100000,
        "text": "same text",
    }
    assert (
        q.stage(7, [envelope, {**envelope, "update_id": 3}, envelope]) == ["queued"] * 3
    )
    first = q.claim(7)
    assert first["update_id"] == 9001
    assert q.stage(7, [envelope]) == ["started"]
    q.finish(first["sequence"], False, "provider_failed")
    assert q.stage(7, [envelope]) == ["failed"]
    second = q.claim(7)
    assert second["update_id"] == 3 and second["text"] == "same text"
    q.finish(second["sequence"], True)
    assert q.stage(8, [{**envelope, "bot_id": 8}]) == ["queued"]
    assert (
        q.db.execute(
            "SELECT count(*) FROM updates WHERE state IN ('failed','completed') AND text IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    q.close()


def test_fractional_age_and_ptb_service_defaults(policy):
    assert admit_update(update(date=13600), 42, policy, 100000.001) is None
    raw = update()
    raw["message"]["group_chat_created"] = False
    assert admit_update(raw, 42, policy, 100000)
    raw["message"]["group_chat_created"] = True
    assert admit_update(raw, 42, policy, 100000) is None


@pytest.fixture
def diagnostic_queue(tmp_path, policy):
    q = TelegramQueue(
        tmp_path / "queue/queue.sqlite3", policy, 424242, clock=lambda: 100000
    )
    # Deliberately stale active input and old terminal receipts must survive inspection.
    q.db.executemany(
        """INSERT INTO updates(bot_id,update_id,chat_id,user_id,message_id,
        message_date,received_at,state,text,error_code,finished_at)
        VALUES(7,?,424242,424242,?,0,0,?,?,?,-1000000)""",
        [
            (
                number,
                number,
                state,
                "SENSITIVE_NOTE" if state in {"queued", "started"} else None,
                {
                    "failed": "provider_failed:reported",
                    "interrupted": "restart_interrupted:reported",
                    "rejected": "SENSITIVE_ERROR",
                }.get(state),
            )
            for number, state in enumerate(queue_module.QUEUE_STATES, 1)
        ],
    )
    try:
        yield q
    finally:
        q.close()


def test_diagnostics_real_database_redacts_receipts_and_merges_separate_health(
    diagnostic_queue,
):
    q = diagnostic_queue
    health = q.path.parent / "health.json"
    health.write_text(
        json.dumps({"health": ["consumer_exited", "SENSITIVE_HEALTH", ["secret"]]})
    )
    health.chmod(0o600)
    report = diagnostics(
        q.path, process_health=["consumer_running"], health_file=health
    )
    assert report == {
        "queue": {state: 1 for state in queue_module.QUEUE_STATES},
        "health": [
            "consumer_exited",
            "consumer_failed",
            "consumer_running",
            "provider_failed",
            "restart_interrupted",
            "turn_failed",
        ],
    }
    serialized = json.dumps(report)
    assert "SENSITIVE" not in serialized and "424242" not in serialized
    assert health.read_text() == json.dumps(
        {"health": ["consumer_exited", "SENSITIVE_HEALTH", ["secret"]]}
    )


def test_diagnostics_never_constructs_recovers_prunes_or_rewrites_rows(
    diagnostic_queue, monkeypatch
):
    q = diagnostic_queue
    before = list(q.db.iterdump())
    files = {path: path.read_bytes() for path in (q.path, Path(str(q.path) + "-wal"))}

    def forbidden(*args, **kwargs):
        pytest.fail("Diagnostics invoked live queue mutation")

    for name in ("__init__", "recover", "_prune"):
        monkeypatch.setattr(TelegramQueue, name, forbidden)
    with patch.object(
        queue_module.sqlite3, "connect", wraps=sqlite3.connect
    ) as connect:
        assert diagnostics(q.path)["queue"] == {
            state: 1 for state in queue_module.QUEUE_STATES
        }
    connect.assert_called_once()
    assert connect.call_args.kwargs == {"uri": True}
    assert connect.call_args.args[0].endswith("/queue.sqlite3?mode=ro")
    assert connect.call_args.args[0] != q.path.as_uri() + "?mode=ro"
    assert list(q.db.iterdump()) == before
    assert {path: path.read_bytes() for path in files} == files
    assert not (q.path.parent / "instance.lock").exists()


@pytest.mark.parametrize("version", [0, 2, 999])
def test_diagnostics_unsupported_schema_is_not_initialized_or_repaired(
    diagnostic_queue, version
):
    q = diagnostic_queue
    q.db.execute(f"PRAGMA user_version={version}")
    before = list(q.db.iterdump())
    with pytest.raises(QueueDiagnosticsError, match="^queue_schema_unsupported$"):
        diagnostics(q.path)
    assert list(q.db.iterdump()) == before


def test_diagnostics_rejects_unknown_states_without_printing_them(diagnostic_queue):
    q = diagnostic_queue
    q.db.execute("PRAGMA ignore_check_constraints=ON")
    q.db.execute("UPDATE updates SET state='SENSITIVE_STATE' WHERE update_id=1")
    with pytest.raises(QueueDiagnosticsError, match="^queue_schema_unsupported$"):
        diagnostics(q.path)


@pytest.mark.parametrize("target", ["directory", "database", "wal", "shm", "health"])
def test_diagnostics_rejects_permissions_without_repair(diagnostic_queue, target):
    q = diagnostic_queue
    health = q.path.parent / "health.json"
    health.write_text('{"health":[]}')
    health.chmod(0o600)
    path = {
        "directory": q.path.parent,
        "database": q.path,
        "wal": Path(str(q.path) + "-wal"),
        "shm": Path(str(q.path) + "-shm"),
        "health": health,
    }[target]
    mode = 0o755 if target == "directory" else 0o644
    path.chmod(mode)
    with pytest.raises(QueueDiagnosticsError, match="^queue_permissions_invalid$"):
        diagnostics(q.path, health_file=health)
    assert path.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("target", ["directory", "database", "wal", "shm", "health"])
@pytest.mark.parametrize("broken", [False, True])
def test_diagnostics_rejects_symlinks(diagnostic_queue, tmp_path, target, broken):
    q = diagnostic_queue
    health = q.path.parent / "health.json"
    outside = tmp_path / "outside"
    if not broken:
        if target == "directory":
            outside.mkdir(mode=0o700)
        else:
            outside.write_text("SENSITIVE_DATA")
            outside.chmod(0o600)
    if target == "directory":
        alias = tmp_path / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        path = alias / "queue.sqlite3"
    elif target == "database":
        path = q.path.parent / "alias.sqlite3"
        path.symlink_to(outside)
    else:
        path = q.path
        candidate = health if target == "health" else Path(str(q.path) + "-" + target)
        if candidate.exists():
            candidate.unlink()
        candidate.symlink_to(outside)
    with pytest.raises(QueueDiagnosticsError, match="^queue_permissions_invalid$"):
        diagnostics(path, health_file=health)


@pytest.mark.parametrize(
    "content",
    [
        "SENSITIVE_INVALID_JSON",
        "[]",
        "{}",
        '{"health":null}',
        '{"health":"SENSITIVE_HEALTH"}',
    ],
)
def test_diagnostics_malformed_health_has_content_free_error(diagnostic_queue, content):
    q = diagnostic_queue
    health = q.path.parent / "health.json"
    health.write_text(content)
    health.chmod(0o600)
    with pytest.raises(QueueDiagnosticsError, match="^queue_unavailable$"):
        diagnostics(q.path, health_file=health)


def test_diagnostics_missing_database_does_not_create_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        queue_module.sqlite3,
        "connect",
        lambda *args, **kwargs: pytest.fail("Missing database opened"),
    )
    path = tmp_path / "missing/queue.sqlite3"
    assert diagnostics(path, health_file=path.parent / "health.json") == {
        "queue": "not_started",
        "health": ["not_started"],
    }
    assert not path.parent.exists()


def test_diagnostics_corrupt_database_error_is_redacted(tmp_path):
    path = tmp_path / "queue.sqlite3"
    path.write_bytes(b"SENSITIVE_DATABASE_CONTENT")
    path.chmod(0o600)
    with pytest.raises(QueueDiagnosticsError, match="^queue_unavailable$"):
        diagnostics(path)


def test_diagnostics_closed_wal_database_creates_no_sidecars(diagnostic_queue):
    q = diagnostic_queue
    q.close()
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in q.path.parent.iterdir()
        if path.is_file()
    }
    assert not Path(str(q.path) + "-wal").exists()
    assert diagnostics(q.path)["queue"] == {
        state: 1 for state in queue_module.QUEUE_STATES
    }
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in q.path.parent.iterdir()
        if path.is_file()
    } == before


def test_diagnostics_active_wal_does_not_modify_shared_memory(diagnostic_queue):
    q = diagnostic_queue
    files = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in q.path.parent.iterdir()
        if path.is_file()
    }
    assert diagnostics(q.path)["queue"] == {
        state: 1 for state in queue_module.QUEUE_STATES
    }
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files
    } == files


@pytest.mark.parametrize(
    "text,expected",
    [
        ("approve, writers paused", "/apply " + "a" * 32 + " writers-paused"),
        ("reject", "/cancel " + "a" * 32),
        ("cancel", "/cancel " + "a" * 32),
        ("yes", "/apply " + "a" * 32),
        ("Change the goal", "/revise " + "a" * 32 + " Change the goal"),
    ],
)
def test_bot_preview_reply_is_bound_before_durable_queueing(
    tmp_path, policy, text, expected
):
    payload = update(text=text)
    payload["message"]["reply_to_message"] = {
        "message_id": 200,
        "from": {"id": 7, "is_bot": True},
        "chat": {"id": 42, "type": "private"},
        "text": "EMURU approval: " + "a" * 32 + "\nApply?",
    }
    assert (
        admit_update(payload, 42, policy, 100000, "emuru", 7)["message"]["text"]
        == expected
    )
    queue = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: 100000)
    queue.stage(7, [payload], "emuru")
    queue.close()
    queue = TelegramQueue(tmp_path / "queue.sqlite3", policy, 42, clock=lambda: 100000)
    try:
        row = queue.claim(7, "emuru")
        assert row["text"] == expected
        assert queue.stage(7, [payload], "emuru") == ["started"]
        assert queue.claim(7, "emuru") is None
    finally:
        queue.close()


@pytest.mark.parametrize(
    "mismatch",
    ["author", "chat", "forward", "outer_forward", "message_id", "header", "bot_id"],
)
def test_untrusted_reply_cannot_become_an_apply_command(policy, mismatch):
    payload = update(text="approve, writers paused")
    reply = {
        "message_id": 200,
        "from": {"id": 7, "is_bot": True},
        "chat": {"id": 42, "type": "private"},
        "text": "EMURU approval: " + "a" * 32,
    }
    payload["message"]["reply_to_message"] = reply
    if mismatch == "author":
        reply["from"]["id"] = 99
    elif mismatch == "chat":
        reply["chat"]["id"] = 99
    elif mismatch == "forward":
        reply["forward_origin"] = {"type": "user"}
    elif mismatch == "outer_forward":
        payload["message"]["forward_origin"] = {"type": "user"}
    elif mismatch == "message_id":
        reply["message_id"] = False
    elif mismatch == "header":
        reply["text"] = "EMURU approval: not-an-id"
    result = admit_update(
        payload, 42, policy, 100000, "emuru", None if mismatch == "bot_id" else 7
    )
    assert result is None
