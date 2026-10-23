"""Private owner-bound proposals, deterministic approval, and crash recovery."""

import ctypes
import difflib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_DIRECTORIES = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_DENIED = {"99_Private", "40_Journal", "90_Archive", "99_System"}
_LOCAL = threading.local()
_THREADS = threading.RLock()


def relative_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        raise ValueError("Invalid mutation path.")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Path traversal is not allowed.")
    return parts


@contextmanager
def directory(path: Path):
    """Open every absolute ancestor without following links."""
    if ".." in Path(path).parts:
        raise ValueError("Path traversal is not allowed.")
    path = Path(os.path.abspath(path))
    fd = os.open("/", _DIRECTORIES)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORIES, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def private_directory(fd: int):
    metadata = os.fstat(fd)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("Private mutation directory permissions invalid.")


def regular(fd: int, *, private=False):
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Mutation file must be regular and have one link.")
    if private and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("Private mutation file permissions invalid.")


@contextmanager
def writer_lock(vault_root: Path):
    """All cooperating captures and index writers use this persistent inode."""
    key = os.path.abspath(vault_root)
    with _THREADS:
        held = getattr(_LOCAL, "held", set())
        if key in held:
            yield
            return
        with directory(vault_root) as root:
            fd = os.open(
                ".emuru-writer.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root,
            )
            try:
                regular(fd, private=True)
                fcntl.flock(fd, fcntl.LOCK_EX)
                _LOCAL.held = held | {key}
                try:
                    yield
                finally:
                    _LOCAL.held = held
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


@contextmanager
def mutation_parent(vault_root: Path, path: str, *, capture=False, roots=()):
    parts = relative_parts(path)
    allowed = ("00_Inbox",) if capture else roots
    if any(part.startswith((".", "_")) or part in _DENIED for part in parts):
        raise ValueError("Mutation location denied.")
    if len(parts) < 2 or not parts[-1].lower().endswith(".md"):
        raise ValueError("Agent-created notes must be Markdown files.")
    if not any(
        parts[: len(relative_parts(root))] == relative_parts(root) for root in allowed
    ):
        raise ValueError("Agent writes are restricted to 00_Inbox/ or approved roots.")
    with directory(vault_root) as root:
        parent = os.dup(root)
        try:
            for part in parts[:-1]:
                if capture:
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIRECTORIES, dir_fd=parent)
                os.close(parent)
                parent = child
            try:
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                pass
            else:
                try:
                    regular(fd)
                finally:
                    os.close(fd)
            yield parent, parts[-1]
        finally:
            os.close(parent)


def load_policy(home: Path) -> dict:
    """Absent policy denies all broader mutations; malformed records fail closed."""
    with directory(home) as parent:
        private_directory(parent)
        try:
            fd = os.open(
                "mutation-policy.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return {"enabled": False, "roots": []}
        try:
            regular(fd, private=True)
            if os.fstat(fd).st_size > 4096:
                raise ValueError("Mutation policy too large.")
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = None
                policy = json.load(stream)
        finally:
            if fd is not None:
                os.close(fd)
    if (
        not isinstance(policy, dict)
        or set(policy) not in ({"enabled", "roots"}, {"enabled", "roots", "vault_path"})
        or type(policy.get("enabled")) is not bool
        or not isinstance(policy["roots"], list)
    ):
        raise ValueError("Broader writes disabled; mutation policy invalid.")
    if policy["enabled"]:
        target = policy.get("vault_path")
        if (
            not isinstance(target, str)
            or not Path(target).is_absolute()
            or ".." in Path(target).parts
        ):
            raise ValueError(
                "Broader writes disabled; policy requires an absolute vault_path."
            )
        from emuru.vault.target import _read_record

        selected = _read_record(home / "vault-target.json")
        if selected != {"mode": "real", "vault_path": target}:
            raise ValueError(
                "Broader writes disabled; policy differs from selected vault."
            )
    for root in policy["roots"]:
        parts = relative_parts(root)
        if parts[0] not in {"10_Projects", "20_Areas", "30_Resources"}:
            raise ValueError("Mutation root denied.")
        if any(part.startswith((".", "_")) or part in _DENIED for part in parts):
            raise ValueError("Mutation root denied.")
    return policy


@contextmanager
def journal(home: Path):
    """Private durable proposals and receipts, independent of Telegram pruning."""
    with directory(home) as profile:
        private_directory(profile)
        try:
            os.mkdir("emuru-mutations", 0o700, dir_fd=profile)
        except FileExistsError:
            pass
        parent = os.open("emuru-mutations", _DIRECTORIES, dir_fd=profile)
        try:
            private_directory(parent)
            fd = os.open(
                "mutations.sqlite",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            try:
                regular(fd, private=True)
            finally:
                os.close(fd)
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    sidecar = os.open(
                        "mutations.sqlite" + suffix,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    continue
                try:
                    regular(sidecar, private=True)
                finally:
                    os.close(sidecar)
            # Private parent is owner-only; keep it pinned for SQLite and sidecars.
            connection = sqlite3.connect(f"/proc/self/fd/{parent}/mutations.sqlite")
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("""CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    vault TEXT NOT NULL, target TEXT NOT NULL,
                    before_hash TEXT, after_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('proposed','cancelled')))
                """)
                connection.execute("""CREATE TABLE IF NOT EXISTS receipts (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                    outcome TEXT NOT NULL, before_hash TEXT, after_hash TEXT NOT NULL)
                """)
                connection.execute("""CREATE TABLE IF NOT EXISTS validated_proposals (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                    approval_digest TEXT NOT NULL)
                """)
                connection.execute("""CREATE TABLE IF NOT EXISTS intents (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                    staging TEXT NOT NULL)
                """)
                connection.execute("""CREATE TABLE IF NOT EXISTS proposal_requests (
                    vault TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL, target TEXT NOT NULL,
                    proposal_id TEXT REFERENCES proposals(id),
                    PRIMARY KEY(vault, request_id))
                """)
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS proposal_edits (
                        proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                        anchor TEXT NOT NULL, replacement TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS owner_requests (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL, bot TEXT NOT NULL,
                        vault TEXT NOT NULL, text_hash TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'started');
                    CREATE INDEX IF NOT EXISTS owner_request_text ON owner_requests(owner,bot,vault,text_hash);
                    CREATE TABLE IF NOT EXISTS request_updates (
                        owner TEXT NOT NULL, bot TEXT NOT NULL, vault TEXT NOT NULL,
                        update_id TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES owner_requests(id),
                        PRIMARY KEY(owner,bot,vault,update_id));
                    CREATE TABLE IF NOT EXISTS proposal_bindings (
                        proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                        request_id TEXT NOT NULL REFERENCES owner_requests(id),
                        policy_digest TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS active_request (
                        slot INTEGER PRIMARY KEY CHECK(slot=1), request_id TEXT NOT NULL,
                        queue_path TEXT NOT NULL, bot INTEGER NOT NULL, update_id INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS preview_deliveries (
                        proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                        approval_digest TEXT NOT NULL, complete INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS application_requests (
                        proposal_id TEXT NOT NULL REFERENCES proposals(id),
                        request_id TEXT NOT NULL REFERENCES owner_requests(id),
                        PRIMARY KEY(proposal_id,request_id));
                    CREATE TABLE IF NOT EXISTS capture_requests (
                        request_id TEXT PRIMARY KEY REFERENCES owner_requests(id),
                        result TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS proposal_lifecycle (
                        proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                        created_at REAL NOT NULL, interacted INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS revision_requests (
                        request_id TEXT PRIMARY KEY REFERENCES owner_requests(id),
                        proposal_id TEXT NOT NULL REFERENCES proposals(id),
                        approval_digest TEXT NOT NULL);
                """)
                # Existing proposals have no trusted creation time; start their grace period at migration.
                connection.execute(
                    "INSERT OR IGNORE INTO proposal_lifecycle(proposal_id,created_at) SELECT id,? FROM proposals",
                    (time.time(),),
                )
                connection.commit()
                os.fsync(parent)
                os.fsync(profile)
                yield connection
            finally:
                connection.close()
        finally:
            os.close(parent)


def store_proposal(
    home: Path, vault_root: Path, path: str, payload: str, *, expected_before_hash=...
) -> str:
    """Internal preparation only; no model tool and no publication permission."""
    policy = load_policy(home)
    with (
        writer_lock(vault_root),
        mutation_parent(vault_root, path, roots=policy["roots"]) as (parent, name),
    ):
        before = None
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, "rb") as stream:
                regular(stream.fileno())
                before = hashlib.sha256(stream.read()).hexdigest()
        if expected_before_hash is not ... and before != expected_before_hash:
            raise ValueError("Stale revision during proposal preparation.")
        identity = uuid.uuid4().hex
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with journal(home) as db:
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')",
                (
                    identity,
                    payload,
                    digest,
                    os.path.abspath(vault_root),
                    path,
                    before,
                    digest,
                ),
            )
            db.execute(
                "INSERT INTO proposal_lifecycle(proposal_id,created_at) VALUES(?,?)",
                (identity, time.time()),
            )
            db.commit()
        return identity


def cancel_proposal(home: Path, identity: str, *, approval_digest=None):
    with journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        proposal = _proposal(db, identity)
        if (
            approval_digest is not None
            and _bound_approval(db, proposal) != approval_digest
        ):
            raise ValueError("Proposal changed; use its latest preview.")
        receipt = _receipt(db, identity)
        if receipt is not None:
            if receipt["outcome"] != "cancelled":
                raise ValueError("An intent or completed effect cannot be cancelled.")
            return receipt
        db.execute("UPDATE proposals SET outcome='cancelled' WHERE id=?", (identity,))
        return _record_receipt(db, proposal, "cancelled")


def read_note(parent: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        regular(stream.fileno())
        return stream.read()


def require_synthetic(home: Path, vault_root: Path):
    """No production apply switch: only isolated /tmp fixtures can publish."""
    from emuru.vault.target import _read_record

    home, vault_root = Path(home).absolute(), Path(vault_root).absolute()
    if any(
        Path("/tmp") not in path.parents or ".." in path.parts
        for path in (home, vault_root)
    ):
        raise ValueError(
            "Broader writes disabled; application requires disposable /tmp fixtures."
        )
    if home == vault_root or home in vault_root.parents or vault_root in home.parents:
        raise ValueError("Mutation profile must be outside the synthetic vault.")
    record = _read_record(home / "vault-target.json")
    if record is not None and record != {"mode": "synthetic"}:
        raise ValueError("Broader writes disabled; explicit real targets cannot apply.")


def require_authority(home: Path, vault_root: Path):
    home, vault_root = Path(home).absolute(), Path(vault_root).absolute()
    policy = load_policy(home)
    if not policy["enabled"]:
        require_synthetic(home, vault_root)
        return policy
    if policy["vault_path"] != os.path.abspath(vault_root):
        raise ValueError("Mutation policy belongs to another vault.")
    if home == vault_root or home in vault_root.parents or vault_root in home.parents:
        raise ValueError("Mutation profile must be outside the vault.")
    with directory(vault_root):
        pass
    return policy


def _policy_digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def begin_request(home, vault_root, owner, bot, update_id, text, *, fresh=False):
    """Trusted adapter/CLI entrypoint. Identity never comes from tool arguments."""
    vault, owner, bot = os.path.abspath(vault_root), str(owner), str(bot)
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    with journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        scope = (owner, bot, vault)
        previous = db.execute(
            "SELECT request_id FROM request_updates WHERE owner=? AND bot=? AND vault=? AND update_id=?",
            (*scope, str(update_id)),
        ).fetchone()
        if previous:
            return previous[0], True
        previous = (
            None
            if fresh
            else db.execute(
                """SELECT id FROM owner_requests r WHERE owner=? AND bot=? AND vault=? AND text_hash=?
            AND (EXISTS(SELECT 1 FROM proposal_bindings b WHERE b.request_id=r.id)
                OR EXISTS(SELECT 1 FROM capture_requests c WHERE c.request_id=r.id)
                OR EXISTS(SELECT 1 FROM application_requests a WHERE a.request_id=r.id)
                OR EXISTS(SELECT 1 FROM proposal_requests p WHERE p.request_id=r.id AND p.vault=r.vault)
                OR EXISTS(SELECT 1 FROM revision_requests x JOIN validated_proposals v ON v.proposal_id=x.proposal_id
                    WHERE x.request_id=r.id AND x.approval_digest!=v.approval_digest))
            ORDER BY rowid LIMIT 1""",
                (*scope, text_hash),
            ).fetchone()
        )
        identity = previous[0] if previous else uuid.uuid4().hex
        if not previous:
            db.execute(
                "INSERT INTO owner_requests VALUES(?,?,?,?,?,'started')",
                (identity, *scope, text_hash),
            )
        db.execute(
            "INSERT INTO request_updates VALUES(?,?,?,?,?)",
            (*scope, str(update_id), identity),
        )
        db.commit()
        return identity, previous is not None


def finish_request(home, identity):
    with journal(home) as db:
        db.execute(
            "UPDATE owner_requests SET state=CASE WHEN state='mutation' THEN state ELSE 'completed' END WHERE id=?",
            (identity,),
        )
        db.execute("DELETE FROM active_request WHERE request_id=?", (identity,))
        db.commit()


def activate_request(home, identity, queue_path, bot, update_id):
    with journal(home) as db:
        db.execute(
            "INSERT OR REPLACE INTO active_request VALUES(1,?,?,?,?)",
            (identity, str(Path(queue_path).absolute()), bot, update_id),
        )
        db.commit()


def authenticated_request(home, vault_root):
    """MCP reads private adapter state and verifies the still-live durable claim."""
    with journal(home) as db:
        row = db.execute(
            "SELECT request_id,queue_path,bot,update_id FROM active_request WHERE slot=1"
        ).fetchone()
        if row is None:
            raise ValueError("An active authenticated request claim is required.")
        identity, queue_path, bot, update_id = row
        request = db.execute(
            "SELECT owner,bot,vault FROM owner_requests WHERE id=?", (identity,)
        ).fetchone()
    with directory(Path(queue_path).parent):
        fd = os.open(queue_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            regular(fd, private=True)
        finally:
            os.close(fd)
    with sqlite3.connect(Path(queue_path).as_uri() + "?mode=ro", uri=True) as queue:
        claim = queue.execute(
            "SELECT user_id,chat_id FROM updates WHERE bot_id=? AND update_id=? AND state='started'",
            (bot, update_id),
        ).fetchone()
    if (
        request is None
        or claim is None
        or request != (str(claim[0]), str(bot), os.path.abspath(vault_root))
        or claim[0] != claim[1]
    ):
        raise ValueError("Authenticated request claim is invalid.")
    return identity


def request_previews(home, identity):
    with journal(home) as db:
        previews = [
            _preview(db, row[0])
            for row in db.execute(
                """SELECT proposal_id FROM proposal_bindings WHERE request_id=?
                UNION SELECT proposal_id FROM proposal_requests WHERE request_id=? AND proposal_id IS NOT NULL
                UNION SELECT x.proposal_id FROM revision_requests x JOIN validated_proposals v ON v.proposal_id=x.proposal_id
                WHERE x.request_id=? AND x.approval_digest!=v.approval_digest""",
                (identity, identity, identity),
            ).fetchall()
        ]
        capture = db.execute(
            "SELECT result FROM capture_requests WHERE request_id=?", (identity,)
        ).fetchone()
        if capture is not None:
            previews.append(json.loads(capture[0]))
        if not previews:
            for vault, target in db.execute(
                "SELECT vault,target FROM proposal_requests WHERE request_id=? AND proposal_id IS NULL",
                (identity,),
            ).fetchall():
                previews.append(
                    {
                        "path": target,
                        "outcome": "exists",
                        "message": "Existing note retained; no mutation applied.",
                    }
                )
        return previews


def capture_note(home, vault_root, path, content):
    """Record capture intent before effects; repeated requests never recapture."""
    from emuru.vault.indexer import vault_write

    identity = authenticated_request(home, vault_root)
    with writer_lock(vault_root):
        with journal(home) as db:
            previous = db.execute(
                "SELECT result FROM capture_requests WHERE request_id=?", (identity,)
            ).fetchone()
            if previous is not None:
                return json.loads(previous[0])
            db.execute(
                "INSERT INTO capture_requests VALUES(?,?)",
                (
                    identity,
                    json.dumps(
                        {
                            "path": path,
                            "outcome": "uncertain",
                            "message": "Capture attempted; inspect effects before /propose-new.",
                        }
                    ),
                ),
            )
            db.execute(
                "UPDATE owner_requests SET state='mutation' WHERE id=?", (identity,)
            )
            db.commit()
        result = vault_write(vault_root, path, content)
        with journal(home) as db:
            db.execute(
                "UPDATE capture_requests SET result=? WHERE request_id=?",
                (json.dumps(result), identity),
            )
            db.commit()
        return result


def application_outcomes(home, vault_root, owner, bot, update_id):
    with journal(home) as db:
        identities = db.execute(
            """SELECT a.proposal_id FROM application_requests a
            JOIN request_updates u ON u.request_id=a.request_id
            WHERE u.owner=? AND u.bot=? AND u.vault=? AND u.update_id=?""",
            (str(owner), str(bot), os.path.abspath(vault_root), str(update_id)),
        ).fetchall()
        return [_receipt(db, row[0]) for row in identities]


def owner_preview(home, vault_root, identity, owner, bot):
    expire_proposals(home, vault_root)
    with journal(home) as db:
        _check_owner(db, identity, vault_root, owner, bot)
        return _preview(db, identity)


def approved_project(home, vault_root, identity, owner, bot):
    """Read an owner-bound applied snapshot; Markdown cannot assert approval."""
    policy = require_authority(home, vault_root)
    if not policy["enabled"]:
        raise ValueError("Project tracking requires a bound vault and enabled policy.")
    with journal(home) as db:
        _check_owner(db, identity, vault_root, owner, bot)
        preview = _preview(db, identity)
    if preview["outcome"] not in {"applied", "index_pending"}:
        raise ValueError("Project tracking requires an applied approved spec receipt.")
    with mutation_parent(vault_root, preview["path"], roots=policy["roots"]) as (
        parent,
        name,
    ):
        current = read_note(parent, name)
    preview["current"] = (
        current is not None
        and hashlib.sha256(current).hexdigest() == preview["after_revision"]
    )
    return preview


def expire_proposals(home, vault_root, *, now=None):
    """Remove untouched draft content after one day; retain a replay-safe tombstone."""
    now = time.time() if now is None else now
    with writer_lock(vault_root), journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        identities = db.execute(
            """SELECT p.id FROM proposals p JOIN proposal_lifecycle l ON l.proposal_id=p.id
            WHERE p.vault=? AND p.outcome='proposed' AND l.interacted=0 AND l.created_at<=?
            AND NOT EXISTS(SELECT 1 FROM receipts r WHERE r.proposal_id=p.id)
            AND NOT EXISTS(SELECT 1 FROM intents i WHERE i.proposal_id=p.id)""",
            (os.path.abspath(vault_root), now - 86400),
        ).fetchall()
        for (identity,) in identities:
            proposal = _proposal(db, identity)
            db.execute(
                "INSERT INTO receipts VALUES(?, 'expired', ?, ?)",
                (identity, proposal["before_hash"], proposal["after_hash"]),
            )
            empty = _hash(b"")
            db.execute(
                "UPDATE proposals SET payload='',digest=?,after_hash=?,outcome='cancelled' WHERE id=?",
                (empty, empty, identity),
            )
            db.execute("DELETE FROM proposal_edits WHERE proposal_id=?", (identity,))
            db.execute(
                "DELETE FROM preview_deliveries WHERE proposal_id=?", (identity,)
            )
            db.execute(
                "UPDATE validated_proposals SET approval_digest=? WHERE proposal_id=?",
                (_bound_approval(db, _proposal(db, identity)), identity),
            )
        db.commit()
        return [row[0] for row in identities]


def pending_previews(home, vault_root, owner, bot):
    with journal(home) as db:
        identities = db.execute(
            """SELECT p.id FROM proposals p JOIN proposal_bindings b ON b.proposal_id=p.id
            JOIN owner_requests r ON r.id=b.request_id
            WHERE r.owner=? AND r.bot=? AND r.vault=? AND p.outcome='proposed'
            AND NOT EXISTS(SELECT 1 FROM receipts x WHERE x.proposal_id=p.id)""",
            (str(owner), str(bot), os.path.abspath(vault_root)),
        ).fetchall()
        return [_preview(db, row[0]) for row in identities]


def note_owner_activity(home, vault_root, owner, bot, update_id):
    with journal(home) as db:
        db.execute(
            """UPDATE proposal_lifecycle SET interacted=1 WHERE proposal_id IN (
            SELECT b.proposal_id FROM proposal_bindings b JOIN owner_requests r ON r.id=b.request_id
            JOIN request_updates u ON u.request_id=r.id
            WHERE r.owner=? AND r.bot=? AND r.vault=? AND CAST(u.update_id AS INTEGER)<?)
            AND created_at>?""",
            (
                str(owner),
                str(bot),
                os.path.abspath(vault_root),
                update_id,
                time.time() - 86400,
            ),
        )
        db.commit()


def begin_revision(home, vault_root, request_id, preview, owner, bot):
    with journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        identity = preview["proposal_id"]
        _check_owner(db, identity, vault_root, owner, bot)
        current = _preview(db, identity)
        if (
            current["outcome"] != "proposed"
            or current["approval_digest"] != preview["approval_digest"]
        ):
            raise ValueError("Only the current pending proposal can be revised.")
        request = db.execute(
            "SELECT owner,bot,vault FROM owner_requests WHERE id=?", (request_id,)
        ).fetchone()
        if request != (str(owner), str(bot), os.path.abspath(vault_root)):
            raise ValueError("Revision request belongs to another owner.")
        db.execute(
            "INSERT INTO revision_requests VALUES(?,?,?)",
            (request_id, identity, current["approval_digest"]),
        )
        db.execute(
            "UPDATE proposal_lifecycle SET interacted=1 WHERE proposal_id=?",
            (identity,),
        )
        db.commit()


def _check_owner(db, identity, vault_root, owner, bot):
    row = db.execute(
        """SELECT r.owner,r.bot,r.vault FROM proposal_bindings b
        JOIN owner_requests r ON r.id=b.request_id WHERE b.proposal_id=?""",
        (identity,),
    ).fetchone()
    if row != (str(owner), str(bot), os.path.abspath(vault_root)):
        raise ValueError("Proposal has no matching authenticated owner/vault binding.")


def record_preview(home, identity, digest, *, complete=False):
    with journal(home) as db:
        if _preview(db, identity)["approval_digest"] != digest:
            raise ValueError("Preview approval digest changed.")
        db.execute(
            "INSERT OR REPLACE INTO preview_deliveries VALUES(?,?,?)",
            (identity, digest, int(complete)),
        )
        db.commit()


def preview_text(preview):
    text = (
        f"Proposal {preview['proposal_id']}\nPath: {preview['path']}\n"
        f"Revision: {preview['before_revision'] or 'absent'}\n"
        f"Outcome: {preview['outcome']}\n"
    )
    if "changes" in preview:
        changes = preview["changes"]
        text += f"Exact anchor:\n{changes['anchor']}\nExact replacement:\n{changes['replacement']}"
    else:
        text += f"Exact creation content:\n{preview['content']}"
    return text


def preview_parts(preview, limit=3500):
    return message_parts(preview_text(preview), limit, label="Preview")


def applied_diff_parts(preview, receipt):
    """Report only the approved change; edit line numbers refer to its exact fragment."""
    changes = preview.get("changes")
    before = changes["anchor"] if changes else ""
    after = changes["replacement"] if changes else preview["content"]
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=preview["path"] + " (approved fragment)"
            if changes
            else "/dev/null",
            tofile=preview["path"],
        )
    )
    diff = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )
    label = (
        "Previously applied proposal; no mutation replayed"
        if preview["outcome"] in {"applied", "index_pending"}
        else "Applied proposal"
    )
    text = f"{label} {preview['proposal_id']}\nPath: {preview['path']}\nApplied revision: {receipt['after_revision']}\n"
    if receipt["outcome"] == "index_pending":
        text += "saved; indexing pending\n"
    text += "Approved diff" + (" (changed fragment)" if changes else "") + ":\n" + diff
    return message_parts(text, label="Applied diff")


def message_parts(text, limit=3500, *, label):
    # UTF-16 bounds also keep astral characters within Telegram's message limit.
    chunks, chunk, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-16-le")) // 2
        if size + width > limit:
            chunks.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += width
    chunks.append("".join(chunk))
    return [f"{label} {i}/{len(chunks)}\n{part}" for i, part in enumerate(chunks, 1)]


def _hash(data: bytes | None):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _approval(proposal, request_binding=None):
    binding = {
        key: proposal[key]
        for key in ("id", "vault", "target", "before_hash", "after_hash", "digest")
    }
    if request_binding is not None:
        binding["request_binding"] = request_binding
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _bound_approval(db, proposal):
    row = db.execute(
        """SELECT b.request_id,b.policy_digest,r.owner,r.bot,r.vault
        FROM proposal_bindings b JOIN owner_requests r ON r.id=b.request_id
        WHERE b.proposal_id=?""",
        (proposal["id"],),
    ).fetchone()
    edit = db.execute(
        "SELECT anchor,replacement FROM proposal_edits WHERE proposal_id=?",
        (proposal["id"],),
    ).fetchone()
    version = db.execute(
        "SELECT version FROM proposal_lifecycle WHERE proposal_id=?", (proposal["id"],)
    ).fetchone()
    return _approval(
        proposal,
        {
            "request": row,
            "edit": edit,
            **({"version": version[0]} if version and version[0] else {}),
        }
        if row is not None or edit is not None or version and version[0]
        else None,
    )


def _no_merge_markers(text: str):
    if re.search(r"(?m)^(?:<{7,}|={7,}|>{7,})(?:[ \t].*)?\r?$", text):
        raise ValueError("Unresolved merge markers refused.")


def _preview(db, identity):
    proposal = _proposal(db, identity)
    approved = db.execute(
        "SELECT approval_digest FROM validated_proposals WHERE proposal_id=?",
        (identity,),
    ).fetchone()
    if approved is None or approved[0] != _bound_approval(db, proposal):
        raise ValueError("Stored proposal approval binding changed.")
    result = {
        "proposal_id": identity,
        "approval_digest": approved[0],
        "path": proposal["target"],
        "before_revision": proposal["before_hash"],
        "after_revision": proposal["after_hash"],
        "content": proposal["payload"],
        "outcome": "proposed",
    }
    edit = db.execute(
        "SELECT anchor,replacement FROM proposal_edits WHERE proposal_id=?", (identity,)
    ).fetchone()
    if edit is not None:
        result["changes"] = {"anchor": edit[0], "replacement": edit[1]}
    receipt = _receipt(db, identity)
    if receipt is not None:
        result.update(receipt)
    if result["outcome"] == "index_pending":
        result["message"] = "saved; indexing pending"
    return result


def _existing(parent, name, path):
    content = read_note(parent, name)
    if content is None:
        raise ValueError(
            "Existing result is no longer present; use a new request identity."
        )
    return {
        "path": path,
        "revision": _hash(content),
        "content": content.decode("utf-8"),
        "outcome": "exists",
    }


def propose_note(
    home: Path,
    vault_root: Path,
    path: str,
    *,
    content: str | None = None,
    expected_revision: str | None = None,
    anchor: str | None = None,
    replacement: str | None = None,
    request_id: str | None = None,
    authenticated_id: str | None = None,
) -> dict:
    """Repeated owner request identities reuse durable proposals or outcomes."""
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 256
        or "\0" in request_id
    ):
        raise ValueError("Invalid request identity.")
    if any(
        value is not None and not isinstance(value, str)
        for value in (content, expected_revision, anchor, replacement)
    ):
        raise ValueError("Proposal fields must be strings or null.")
    policy = require_authority(home, vault_root)
    if policy["enabled"] and authenticated_id is None:
        raise ValueError("Authenticated request required for production proposals.")
    if authenticated_id is not None:
        with journal(home) as db:
            row = db.execute(
                "SELECT vault,state FROM owner_requests WHERE id=?", (authenticated_id,)
            ).fetchone()
            if (
                row is None
                or row[0] != os.path.abspath(vault_root)
                or row[1] not in {"started", "mutation"}
            ):
                raise ValueError("Invalid authenticated request.")
            db.execute(
                "UPDATE owner_requests SET state='mutation' WHERE id=?",
                (authenticated_id,),
            )
            db.commit()
        request_id = authenticated_id
    with (
        writer_lock(vault_root),
        mutation_parent(vault_root, path, roots=policy["roots"]) as (parent, name),
    ):
        vault = os.path.abspath(vault_root)
        fields = {
            "path": path,
            "content": content,
            "expected_revision": expected_revision,
            "anchor": anchor,
            "replacement": replacement,
        }
        digest = hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if request_id is not None:
            with journal(home) as db:
                previous = db.execute(
                    "SELECT request_digest, target, proposal_id FROM proposal_requests WHERE vault=? AND request_id=?",
                    (vault, request_id),
                ).fetchone()
                if previous is not None:
                    if previous[0] != digest:
                        raise ValueError(
                            "Request identity already binds different proposal inputs."
                        )
                    if previous[2] is not None:
                        return _preview(db, previous[2])
                    return _existing(parent, name, previous[1])
        with journal(home) as db:
            revision = db.execute(
                "SELECT proposal_id,approval_digest FROM revision_requests WHERE request_id=?",
                (authenticated_id,),
            ).fetchone()
        result = _prepare_note(
            home,
            vault_root,
            path,
            **{key: value for key, value in fields.items() if key != "path"},
            reuse_unbound=authenticated_id is None,
            revision=revision,
        )
        if (
            authenticated_id is not None
            and result.get("proposal_id")
            and revision is None
        ):
            with journal(home) as db:
                bound = db.execute(
                    "SELECT request_id FROM proposal_bindings WHERE proposal_id=?",
                    (result["proposal_id"],),
                ).fetchone()
                if bound is not None and bound[0] != authenticated_id:
                    raise ValueError("Proposal already belongs to another request.")
                db.execute(
                    "INSERT OR IGNORE INTO proposal_bindings VALUES(?,?,?)",
                    (result["proposal_id"], authenticated_id, _policy_digest(policy)),
                )
                db.execute(
                    "UPDATE validated_proposals SET approval_digest=? WHERE proposal_id=?",
                    (
                        _bound_approval(db, _proposal(db, result["proposal_id"])),
                        result["proposal_id"],
                    ),
                )
                db.commit()
                result = _preview(db, result["proposal_id"])
        if request_id is not None:
            with journal(home) as db:
                db.execute(
                    "INSERT INTO proposal_requests VALUES (?, ?, ?, ?, ?)",
                    (vault, request_id, digest, path, result.get("proposal_id")),
                )
                db.commit()
        return result


def _prepare_note(
    home: Path,
    vault_root: Path,
    path: str,
    *,
    content: str | None = None,
    expected_revision: str | None = None,
    anchor: str | None = None,
    replacement: str | None = None,
    reuse_unbound=True,
    revision=None,
) -> dict:
    """One creation or one unique exact replacement; no note is published."""
    if any(
        value is not None and not isinstance(value, str)
        for value in (content, expected_revision, anchor, replacement)
    ):
        raise ValueError("Proposal fields must be strings or null.")
    policy = load_policy(home)
    with (
        writer_lock(vault_root),
        mutation_parent(vault_root, path, roots=policy["roots"]) as (parent, name),
    ):
        before = read_note(parent, name)
        update_fields = (expected_revision, anchor, replacement)
        if content is not None and not all(
            value is not None for value in update_fields
        ):
            if any(
                value is not None for value in (expected_revision, anchor, replacement)
            ):
                raise ValueError("Creation accepts no update fields.")
            if before is not None:
                return _existing(parent, name, path)
            after = content
        else:
            if (
                before is None
                or expected_revision is None
                or expected_revision != _hash(before)
            ):
                raise ValueError(
                    "Stale or missing full-file revision. "
                    "Do not repeat this failed proposal. Reopen the same path at most once; "
                    "copy the exact revision field from that vault_open response into "
                    "expected_revision and reassess the unique anchor before a corrected proposal. "
                    "If recovery fails, stop and report the path and error."
                )
            text = before.decode("utf-8")
            _no_merge_markers(text)
            if not anchor or replacement is None:
                raise ValueError(
                    "Update requires a nonempty exact anchor and replacement."
                )
            start = text.find(anchor)
            if start < 0 or text.find(anchor, start + 1) >= 0:
                raise ValueError("Exact anchor must match once.")
            after = text[:start] + replacement + text[start + len(anchor) :]
            if content is not None and content != after:
                raise ValueError("Content must match the exact anchored update.")
        if not after.strip():
            raise ValueError("Cannot propose an empty note.")
        _no_merge_markers(after)
        if after.encode("utf-8") == before:
            raise ValueError("Proposal has no change.")
        if revision is not None:
            identity, expected_digest = revision
            with journal(home) as db:
                db.execute("BEGIN IMMEDIATE")
                current = _preview(db, identity)
                if (
                    current["outcome"] != "proposed"
                    or current["approval_digest"] != expected_digest
                ):
                    raise ValueError("Proposal changed during revision.")
                digest = _hash(after.encode())
                db.execute(
                    "UPDATE proposals SET target=?,payload=?,digest=?,before_hash=?,after_hash=? WHERE id=?",
                    (path, after, digest, _hash(before), digest, identity),
                )
                db.execute(
                    "DELETE FROM proposal_edits WHERE proposal_id=?", (identity,)
                )
                if anchor is not None:
                    db.execute(
                        "INSERT INTO proposal_edits VALUES(?,?,?)",
                        (identity, anchor, replacement),
                    )
                db.execute(
                    "UPDATE proposal_lifecycle SET version=version+1,interacted=1 WHERE proposal_id=?",
                    (identity,),
                )
                db.execute(
                    "DELETE FROM preview_deliveries WHERE proposal_id=?", (identity,)
                )
                db.execute(
                    "UPDATE validated_proposals SET approval_digest=? WHERE proposal_id=?",
                    (_bound_approval(db, _proposal(db, identity)), identity),
                )
                db.commit()
                return _preview(db, identity)
        with journal(home) as db:
            previous = db.execute(
                """SELECT p.id FROM proposals p
                JOIN validated_proposals v ON v.proposal_id=p.id
                LEFT JOIN receipts r ON r.proposal_id=p.id
                WHERE p.vault=? AND p.target=? AND p.before_hash IS ?
                AND p.after_hash=? AND p.outcome='proposed' AND r.proposal_id IS NULL
                AND NOT EXISTS(SELECT 1 FROM proposal_bindings b WHERE b.proposal_id=p.id)
                ORDER BY p.rowid LIMIT 1""",
                (
                    os.path.abspath(vault_root),
                    path,
                    _hash(before),
                    _hash(after.encode()),
                ),
            ).fetchone()
            if reuse_unbound and previous is not None:
                return _preview(db, previous[0])
        identity = store_proposal(
            home, vault_root, path, after, expected_before_hash=_hash(before)
        )
        with journal(home) as db:
            proposal = _proposal(db, identity)
            if anchor is not None:
                db.execute(
                    "INSERT INTO proposal_edits VALUES(?,?,?)",
                    (identity, anchor, replacement),
                )
            approval = _bound_approval(db, proposal)
            db.execute(
                "INSERT INTO validated_proposals VALUES (?, ?)", (identity, approval)
            )
            db.commit()
        with journal(home) as db:
            return _preview(db, identity)


def _proposal(db, identity):
    cursor = db.execute("SELECT * FROM proposals WHERE id=?", (identity,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Unknown proposal.")
    proposal = dict(zip((column[0] for column in cursor.description), row, strict=True))
    if (
        _hash(proposal["payload"].encode()) != proposal["digest"]
        or proposal["digest"] != proposal["after_hash"]
    ):
        raise ValueError("Proposal payload digest changed.")
    return proposal


def _receipt(db, identity):
    row = db.execute(
        "SELECT outcome, before_hash, after_hash FROM receipts WHERE proposal_id=?",
        (identity,),
    ).fetchone()
    if row is None:
        return None
    return {
        "proposal_id": identity,
        "outcome": row[0],
        "before_revision": row[1],
        "after_revision": row[2],
    }


def _record_receipt(db, proposal, outcome):
    db.execute(
        "INSERT OR REPLACE INTO receipts VALUES (?, ?, ?, ?)",
        (proposal["id"], outcome, proposal["before_hash"], proposal["after_hash"]),
    )
    db.commit()
    result = {**_receipt(db, proposal["id"]), "path": proposal["target"]}
    if outcome == "index_pending":
        result["message"] = "saved; indexing pending"
    return result


def _publish_note(parent, staging, name, *, create):
    if create:
        # Linux renameat2 avoids both overwrites and a crash-visible hard-link pair.
        libc = ctypes.CDLL(None, use_errno=True)
        if (
            libc.renameat2(parent, os.fsencode(staging), parent, os.fsencode(name), 1)
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)
    else:
        os.replace(staging, name, src_dir_fd=parent, dst_dir_fd=parent)


def publish_note(parent, name, content: bytes, *, before: bytes | None, staging=None):
    """Stage privately, verify destination again, publish and fsync its directory."""
    staging = staging or ".emuru-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as stream:
            regular(stream.fileno())
            staged_identity = os.fstat(stream.fileno())
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if read_note(parent, name) != before:
            raise ValueError("Stale destination; prepare a new proposal.")
        source = os.open(
            staging, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(source, "rb") as stream:
            regular(stream.fileno(), private=True)
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (
                staged_identity.st_dev,
                staged_identity.st_ino,
            ) or stream.read() != content:
                raise ValueError("Mutation staging file changed before publication.")
        _publish_note(parent, staging, name, create=before is None)
        os.fsync(parent)
    finally:
        try:
            os.unlink(staging, dir_fd=parent)
        except FileNotFoundError:
            pass


def _recover_one(db, proposal, parent, name):
    """Recognize disk effects. Never replay a prepared intent."""
    try:
        current = _hash(read_note(parent, name))
    except (OSError, ValueError):
        return _record_receipt(db, proposal, "quarantined")
    stage = db.execute(
        "SELECT staging FROM intents WHERE proposal_id=?", (proposal["id"],)
    ).fetchone()
    if stage is not None and re.fullmatch(r"\.emuru-[0-9a-f]{32}\.tmp", stage[0]):
        try:
            if _hash(read_note(parent, stage[0])) == proposal["after_hash"]:
                os.unlink(stage[0], dir_fd=parent)
                os.fsync(parent)
        except (OSError, ValueError):
            pass
    # Incomplete staging files remain for inspection; they are never replayed.
    if current == proposal["after_hash"]:
        os.fsync(parent)
        return _record_receipt(db, proposal, "index_pending")
    if current == proposal["before_hash"]:
        return _record_receipt(db, proposal, "not_applied")
    return _record_receipt(db, proposal, "quarantined")


def _refresh_receipt(db, proposal, vault_root):
    from emuru.vault.indexer import index_vault

    try:
        index_vault(vault_root)
    except (OSError, ValueError, sqlite3.Error):
        return _record_receipt(db, proposal, "index_pending")
    return _record_receipt(db, proposal, "applied")


def apply_proposal(
    home: Path,
    vault_root: Path,
    identity: str,
    approval_digest: str,
    *,
    writers_paused=False,
    owner=None,
    bot=None,
    application_request=None,
) -> dict:
    require_authority(home, vault_root)
    expire_proposals(home, vault_root)
    if writers_paused is not True:
        raise ValueError("Confirm other writers are paused before application.")
    with writer_lock(vault_root), journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        policy = load_policy(home)
        proposal = _proposal(db, identity)
        bound = db.execute(
            "SELECT policy_digest FROM proposal_bindings WHERE proposal_id=?",
            (identity,),
        ).fetchone()
        if policy["enabled"] or bound is not None:
            _check_owner(db, identity, vault_root, owner, bot)
            if bound is None or bound[0] != _policy_digest(policy):
                raise ValueError("Mutation policy changed; prepare a new proposal.")
            delivered = db.execute(
                "SELECT approval_digest,complete FROM preview_deliveries WHERE proposal_id=?",
                (identity,),
            ).fetchone()
            if delivered != (approval_digest, 1):
                raise ValueError(
                    "Complete preview delivery required before application."
                )
            request = db.execute(
                "SELECT owner,bot,vault FROM owner_requests WHERE id=?",
                (application_request,),
            ).fetchone()
            if request != (str(owner), str(bot), os.path.abspath(vault_root)):
                raise ValueError("Active application request required.")
            db.execute(
                "INSERT OR IGNORE INTO application_requests VALUES(?,?)",
                (identity, application_request),
            )
        if proposal["vault"] != os.path.abspath(vault_root):
            raise ValueError("Proposal belongs to another vault.")
        validated = db.execute(
            "SELECT approval_digest FROM validated_proposals WHERE proposal_id=?",
            (identity,),
        ).fetchone()
        if (
            validated is None
            or validated[0] != approval_digest
            or _bound_approval(db, proposal) != approval_digest
        ):
            raise ValueError("Approval does not bind the exact validated proposal.")
        db.commit()
        with mutation_parent(vault_root, proposal["target"], roots=policy["roots"]) as (
            parent,
            name,
        ):
            receipt = _receipt(db, identity)
            if receipt is not None and receipt["outcome"] not in {
                "prepared",
                "index_pending",
            }:
                return {**receipt, "path": proposal["target"]}
            if receipt is not None:
                receipt = _recover_one(db, proposal, parent, name)
                if receipt["outcome"] == "index_pending":
                    return _refresh_receipt(db, proposal, vault_root)
                return receipt
            if proposal["outcome"] != "proposed":
                raise ValueError("Proposal is cancelled.")
            before = read_note(parent, name)
            if _hash(before) != proposal["before_hash"]:
                raise ValueError("Stale full-file revision; prepare a new proposal.")
            staging = ".emuru-" + uuid.uuid4().hex + ".tmp"
            db.execute("INSERT INTO intents VALUES (?, ?)", (identity, staging))
            _record_receipt(db, proposal, "prepared")
            try:
                publish_note(
                    parent,
                    name,
                    proposal["payload"].encode(),
                    before=before,
                    staging=staging,
                )
            except (OSError, ValueError):
                receipt = _recover_one(db, proposal, parent, name)
                if receipt["outcome"] == "index_pending":
                    return receipt
                raise
            _record_receipt(db, proposal, "index_pending")
            return _refresh_receipt(db, proposal, vault_root)


def recover_proposals(home: Path, vault_root: Path) -> list[dict]:
    require_authority(home, vault_root)
    results = []
    with writer_lock(vault_root), journal(home) as db:
        policy = load_policy(home)
        identities = db.execute(
            "SELECT proposal_id FROM receipts WHERE outcome IN ('prepared','index_pending')"
        ).fetchall()
        for (identity,) in identities:
            proposal = _proposal(db, identity)
            if proposal["vault"] != os.path.abspath(vault_root):
                continue
            try:
                with mutation_parent(
                    vault_root, proposal["target"], roots=policy["roots"]
                ) as (parent, name):
                    receipt = _recover_one(db, proposal, parent, name)
            except (OSError, ValueError):
                receipt = _record_receipt(db, proposal, "quarantined")
            if receipt["outcome"] == "index_pending":
                receipt = _refresh_receipt(db, proposal, vault_root)
            results.append(receipt)
    return results
