"""SDK-independent admission, atomic staging, claims, and crash recovery."""

import copy
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from emuru.telegram_queue import QueueFull, TelegramQueue, admit_update


def update(uid=1, text="hello", date=100000):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": date,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": text,
        },
    }


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
    path = Path(__file__).resolve().parents[2] / "src/emuru/telegram_queue.py"
    code = "import runpy,sys; runpy.run_path(sys.argv[1]); assert not any(n == 'mcp' or n.startswith('mcp.') for n in sys.modules)"
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(path)], check=True, timeout=10
    )


def test_real_process_crash_preserves_committed_input(tmp_path, policy):
    path = tmp_path / "queue.sqlite3"
    source = Path(__file__).resolve().parents[2] / "src/emuru/telegram_queue.py"
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
