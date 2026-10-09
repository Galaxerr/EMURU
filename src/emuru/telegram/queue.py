"""Durable Telegram queue, transport policy, and read-only redacted diagnostics."""

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
QUEUE_STATES = (
    "queued",
    "started",
    "completed",
    "failed",
    "interrupted",
    "expired",
    "rejected",
)
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
ERROR_CODES = frozenset(
    {
        "turn_timeout",
        "queue_database_error",
        "turn_failed",
        "provider_failed",
        "tool_failed",
        "native_failure",
        "native_cancelled",
        "dispatch_admission",
        "restart_interrupted",
        "turn_interrupted",
        "consumer_failed",
        "consumer_exited",
        "consumer_not_running",
        "status_reply_failed",
    }
)
PROCESS_HEALTH_CODES = ERROR_CODES | {"consumer_running"}

_SCHEMA = f"""CREATE TABLE IF NOT EXISTS updates (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL, update_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, message_date INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({",".join(repr(state) for state in QUEUE_STATES)})),
    text TEXT, session_key TEXT, error_code TEXT, finished_at INTEGER,
    UNIQUE (bot_id, update_id))"""


class QueueDiagnosticsError(RuntimeError):
    """Content-free diagnostic code; never expose database or file error text."""


def _private_files(path):
    return path, Path(str(path) + "-wal"), Path(str(path) + "-shm")


def _check_private_file(path):
    if path.is_symlink():
        raise QueueDiagnosticsError("queue_permissions_invalid")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
    ):
        raise QueueDiagnosticsError("queue_permissions_invalid")


def _schema_supported(db, *, allow_empty=False):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return True
    return (
        allow_empty
        and version == 0
        and not db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )


def _receipt_error(code):
    code = code.removesuffix(":reported") if isinstance(code, str) else None
    return code if code in ERROR_CODES else "turn_failed"


def _process_health(codes):
    if not isinstance(codes, (list, tuple)):
        raise QueueDiagnosticsError("queue_unavailable")
    return {
        code
        if isinstance(code, str) and code in PROCESS_HEALTH_CODES
        else "consumer_failed"
        for code in codes
    }


def diagnostics(path: Path, *, process_health=(), health_file: Path | None = None):
    """Read durable receipts; merge separate process health without queue mutation.

    No live queue, recovery, pruning, permission repair, or schema initialization.
    The optional health file is an input owned by the runtime, never an output.
    """
    try:
        path = Path(path).absolute()
        if path.is_symlink() or path.parent.is_symlink():
            raise QueueDiagnosticsError("queue_permissions_invalid")
        if not path.exists():
            return {"queue": "not_started", "health": ["not_started"]}
        metadata = path.parent.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            raise QueueDiagnosticsError("queue_permissions_invalid")
        for candidate in _private_files(path):
            if candidate.exists() or candidate.is_symlink():
                _check_private_file(candidate)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if not _schema_supported(db):
                raise QueueDiagnosticsError("queue_schema_unsupported")
            counts = dict(
                db.execute("SELECT state,count(*) FROM updates GROUP BY state")
            )
            if set(counts) - set(QUEUE_STATES):
                raise QueueDiagnosticsError("queue_schema_unsupported")
            health = {
                _receipt_error(raw)
                for (raw,) in db.execute(
                    "SELECT DISTINCT error_code FROM updates WHERE error_code IS NOT NULL"
                )
            }
        health.update(_process_health(process_health))
        if health_file is not None:
            health_file = Path(health_file)
            if health_file.exists() or health_file.is_symlink():
                _check_private_file(health_file)
                health.update(
                    _process_health(json.loads(health_file.read_text())["health"])
                )
        return {"queue": counts, "health": sorted(health)}
    except QueueDiagnosticsError:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        raise QueueDiagnosticsError("queue_unavailable") from None


def load_telegram_settings(path: Path) -> dict:
    settings = json.loads(path.read_text(encoding="utf-8"))
    fixed = {
        "transport": "polling",
        "private_chat_only": True,
        "text_only": True,
        "max_active_turns": 1,
        "allowed_commands": ["start", "help", "status", "new", "stop"],
        "ambiguous_turn_recovery": "report_without_replay",
    }
    ceilings = {
        "max_message_age_seconds": 86400,
        "max_input_chars": 8000,
        "max_pending_updates": 1000,
        "poll_timeout_seconds": 25,
        "reconnect_delay_seconds": 5,
    }
    if type(settings) is not dict or set(settings) != fixed.keys() | ceilings.keys():
        raise ValueError("Unexpected Telegram transport policy fields")
    for key, expected in fixed.items():
        if type(settings[key]) is not type(expected) or settings[key] != expected:
            raise ValueError(f"Unsafe Telegram transport policy: {key}")
    for key, ceiling in ceilings.items():
        if type(settings[key]) is not int or not 1 <= settings[key] <= ceiling:
            raise ValueError(f"Telegram transport bound must be 1..{ceiling}: {key}")
    return settings


class QueueFull(RuntimeError):
    """Do not return a polling batch whose accepted input cannot be committed."""


def admit_update(update, owner_id, settings, now, bot_username=""):
    """Return minimal text-only Bot API JSON, or reject before any Hermes handler."""
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("A single positive owner ID is required")
    if type(update) is not dict or type(update.get("update_id")) is not int:
        return None
    if update["update_id"] < 0 or set(update) != {"update_id", "message"}:
        return None
    msg = update.get("message")
    if type(msg) is not dict:
        return None
    chat, sender = msg.get("chat"), msg.get("from")
    if type(chat) is not dict or type(sender) is not dict:
        return None
    if (
        chat.get("type") != "private"
        or type(chat.get("id")) is not int
        or chat["id"] != owner_id
        or type(sender.get("id")) is not int
        or sender["id"] != owner_id
        or sender.get("is_bot") is not False
    ):
        return None
    text, date, mid = msg.get("text"), msg.get("date"), msg.get("message_id")
    if (
        type(text) is not str
        or not text.strip()
        or len(text) > settings["max_input_chars"]
        or type(mid) is not int
        or mid <= 0
        or type(date) is not int
        or not -300 <= now - date <= settings["max_message_age_seconds"]
    ):
        return None
    # Telegram service/media/edit payloads must never become agent prompts.
    # PTB serializes these absent service flags as false even for plain text.
    service_flags = {
        "channel_chat_created",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
    }
    if any(msg.get(flag, False) is not False for flag in service_flags):
        return None
    permitted = {
        "message_id",
        "date",
        "chat",
        "from",
        "text",
        "entities",
        "reply_to_message",
        "forward_origin",
        "has_protected_content",
    } | service_flags
    if set(msg) - permitted:
        return None
    entities = []
    if text.lstrip().startswith("/"):
        command = re.fullmatch(
            r"/([a-z]+)(?:@([A-Za-z0-9_]+))?(?:\s.*)?", text, re.DOTALL
        )
        if (
            command is None
            or command[1] not in settings["allowed_commands"]
            or command[2]
            and command[2].lower() != bot_username.lower()
        ):
            return None
        entities = [
            {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
        ]
    return {
        "update_id": update["update_id"],
        "message": {
            "message_id": mid,
            "date": date,
            "chat": {"id": owner_id, "type": "private"},
            "from": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
            "text": text,
            "entities": entities,
        },
    }


def as_update(row):
    """Rebuild minimal native Telegram JSON from a trusted queue envelope."""
    text = row["text"]
    entities = (
        [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if text.startswith("/")
        else []
    )
    return {
        "update_id": row["update_id"],
        "message": {
            "message_id": row["message_id"],
            "date": row["message_date"],
            "chat": {"id": row["chat_id"], "type": "private"},
            "from": {"id": row["user_id"], "is_bot": False, "first_name": "Owner"},
            "text": text,
            "entities": entities,
        },
    }


def safe_error_code(error):
    # Exception messages can contain credentials or request text.
    if isinstance(error, TimeoutError):
        return "turn_timeout"
    if isinstance(error, sqlite3.Error):
        return "queue_database_error"
    return "turn_failed"


class TelegramQueue:
    """Durable FIFO admission, keyed only by (bot_id, update_id).

    Terminal receipts live seven days; active work is never pruned or replayed.
    Recovery holds the launcher's process lock (or acquires the same lock itself).
    """

    def __init__(self, path, settings, owner_id, *, clock=time.time, lock_fd=None):
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("A single positive owner ID is required")
        self.settings, self.owner_id, self.clock = settings, owner_id, clock
        self.path = Path(path)
        self._lock_fd, self._owns_lock = lock_fd, False
        os.umask(0o077)
        self.path.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError("Queue directory must not be a symlink")
        os.chmod(self.path.parent, PRIVATE_DIRECTORY_MODE)
        for private in _private_files(self.path):
            if private.is_symlink():
                raise ValueError("Queue files must not be symlinks")
            if private.exists():
                os.chmod(private, PRIVATE_FILE_MODE)
        fd = os.open(
            self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, PRIVATE_FILE_MODE
        )
        os.fchmod(fd, PRIVATE_FILE_MODE)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=5000")
            if not _schema_supported(self.db, allow_empty=True):
                raise RuntimeError(
                    "Unsupported Telegram queue schema; preserve the database for review"
                )
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                self.db.execute(_SCHEMA)
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS turn_effects (sequence INTEGER PRIMARY KEY, tools_started INTEGER NOT NULL)"
                )
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS pending_updates ON updates(bot_id,state,sequence)"
                )
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Invalid transport clock")
        return now

    def _prune(self, now):
        self.db.execute(
            """DELETE FROM updates WHERE state NOT IN ('queued','started')
            AND finished_at < ?""",
            (now - 7 * 86400,),
        )
        self.db.execute(
            "DELETE FROM turn_effects WHERE sequence NOT IN (SELECT sequence FROM updates)"
        )

    def stage(self, bot, updates, bot_username=""):
        """Commit a whole fetched batch in returned order, or none. Return admission states."""
        if type(bot) is not int or bot <= 0:
            raise ValueError("Invalid bot identity")
        now, states = self.now(), []
        with self.transaction():
            self._prune(now)
            for raw in updates:
                # The raw boundary normalizes Telegram data; callers may also supply
                # the small trusted envelope directly, never a forwarded identity.
                if type(raw) is dict and "bot_id" in raw:
                    expected = {
                        "bot_id",
                        "update_id",
                        "chat_id",
                        "chat_type",
                        "user_id",
                        "is_bot",
                        "message_id",
                        "message_date",
                        "text",
                    }
                    if (
                        set(raw) != expected
                        or type(raw["bot_id"]) is not int
                        or raw["bot_id"] != bot
                    ):
                        states.append(None)
                        continue
                    raw = {
                        "update_id": raw["update_id"],
                        "message": {
                            "message_id": raw["message_id"],
                            "date": raw["message_date"],
                            "chat": {"id": raw["chat_id"], "type": raw["chat_type"]},
                            "from": {"id": raw["user_id"], "is_bot": raw["is_bot"]},
                            "text": raw["text"],
                        },
                    }
                payload = admit_update(
                    raw, self.owner_id, self.settings, now, bot_username
                )
                if payload is None:
                    states.append(None)
                    continue
                existing = self.db.execute(
                    "SELECT state FROM updates WHERE bot_id=? AND update_id=?",
                    (bot, payload["update_id"]),
                ).fetchone()
                if existing:
                    states.append(existing["state"])
                    continue
                count = self.db.execute(
                    "SELECT count(*) FROM updates WHERE state IN ('queued','started')"
                ).fetchone()[0]
                if count >= self.settings["max_pending_updates"]:
                    raise QueueFull("Telegram durable queue is full; intake stopped")
                msg = payload["message"]
                self.db.execute(
                    """INSERT INTO updates(bot_id,update_id,chat_id,user_id,message_id,
                    message_date,received_at,state,text) VALUES(?,?,?,?,?,?,?,'queued',?)""",
                    (
                        bot,
                        payload["update_id"],
                        msg["chat"]["id"],
                        msg["from"]["id"],
                        msg["message_id"],
                        msg["date"],
                        int(now),
                        msg["text"],
                    ),
                )
                states.append("queued")
        return states

    def recover(self):
        """Lock before changing any crash-interrupted claims. Never requeue them."""
        if self._lock_fd is None:
            self._lock_fd = os.open(
                self.path.parent / "instance.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                PRIVATE_FILE_MODE,
            )
            self._owns_lock = True
        expected = (self.path.parent / "instance.lock").stat()
        actual = os.fstat(self._lock_fd)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("Recovery requires the queue's lifetime process lock")
        os.fchmod(self._lock_fd, PRIVATE_FILE_MODE)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.transaction():
            self.db.execute(
                """UPDATE updates SET state='interrupted',text=NULL,
                error_code='restart_interrupted',finished_at=? WHERE state='started'""",
                (int(self.now()),),
            )

    def claim(self, bot, bot_username="", *, session_key=None, local_status=False):
        """Atomically start the oldest fresh request with a stable native session route."""
        with self.transaction():
            if (
                not local_status
                and self.db.execute(
                    "SELECT 1 FROM updates WHERE state='started' LIMIT 1"
                ).fetchone()
            ):
                return None
            while True:
                rows = self.db.execute(
                    "SELECT * FROM updates WHERE bot_id=? AND state='queued' ORDER BY sequence",
                    (bot,),
                )
                # ponytail: bounded pending queue scan; index commands if this bound grows.
                row = next(
                    (
                        row
                        for row in rows
                        if not local_status
                        or row["text"].split(maxsplit=1)[0].lower()
                        in {"/status", "/status@" + bot_username.lower()}
                    ),
                    None,
                )
                if row is None:
                    return None
                now = self.now()
                payload = admit_update(
                    as_update(row), self.owner_id, self.settings, now, bot_username
                )
                if payload is None:
                    state = (
                        "expired"
                        if now - row["message_date"]
                        > self.settings["max_message_age_seconds"]
                        else "rejected"
                    )
                    self.db.execute(
                        "UPDATE updates SET state=?,text=NULL,error_code='dispatch_admission',finished_at=? WHERE sequence=?",
                        (state, int(now), row["sequence"]),
                    )
                    continue
                route = session_key(payload) if callable(session_key) else session_key
                if route is not None and (type(route) is not str or not route):
                    raise ValueError("Invalid native session route")
                self.db.execute(
                    "UPDATE updates SET state='started',session_key=? WHERE sequence=? AND state='queued'",
                    (route, row["sequence"]),
                )
                self.db.execute(
                    "INSERT INTO turn_effects(sequence,tools_started) VALUES(?,0)",
                    (row["sequence"],),
                )
                return {**dict(row), "state": "started", "session_key": route}

    def mark_tools_started(self, bot, uid):
        """Commit before dispatch, including executor threads; unknown effects stay unsafe."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "UPDATE turn_effects SET tools_started=1 WHERE sequence IN "
                "(SELECT sequence FROM updates WHERE bot_id=? AND update_id=? AND state='started')",
                (bot, uid),
            )
            db.commit()

    def terminal_notice(self, bot, uid):
        row = self.db.execute(
            "SELECT tools_started FROM turn_effects JOIN updates USING(sequence) "
            "WHERE bot_id=? AND update_id=?",
            (bot, uid),
        ).fetchone()
        if row is not None and row["tools_started"] == 0:
            return "I can’t reach a model right now, so I couldn’t carry out this request. Please try again later."
        return "I couldn’t finish this request. Some actions may already have completed; please check before trying again."

    def finish(self, sequence, success, error_code=None):
        state = "completed" if success else "failed"
        self.db.execute(
            """UPDATE updates SET state=?,text=NULL,error_code=?,finished_at=?
            WHERE sequence=? AND state='started'""",
            (
                state,
                None if success else (error_code or "turn_failed"),
                int(self.now()),
                sequence,
            ),
        )

    def mark_interrupted(self, sequence):
        self.db.execute(
            """UPDATE updates SET state='interrupted',text=NULL,
            error_code='turn_interrupted',finished_at=? WHERE sequence=? AND state='started'""",
            (int(self.now()), sequence),
        )

    def claim_report(self, bot):
        """One notice attempt per terminal failure, containing identity but no request body."""
        with self.transaction():
            row = self.db.execute(
                """SELECT * FROM updates WHERE bot_id=? AND user_id=? AND chat_id=?
                AND state IN ('failed','interrupted') AND error_code NOT LIKE '%:reported'
                ORDER BY sequence LIMIT 1""",
                (bot, self.owner_id, self.owner_id),
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE updates SET error_code=error_code || ':reported' WHERE sequence=?",
                    (row["sequence"],),
                )
            return dict(row) if row else None

    def status(self):
        return dict(
            self.db.execute("SELECT state,count(*) FROM updates GROUP BY state")
        )

    def close(self):
        self.db.close()
        if self._owns_lock and self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
