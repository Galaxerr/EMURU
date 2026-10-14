"""Private proposals and crash recovery; broader publication is synthetic-only."""

import ctypes
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
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
        or set(policy) != {"enabled", "roots"}
        or policy["enabled"] is not False
        or not isinstance(policy["roots"], list)
    ):
        raise ValueError("Broader writes disabled; mutation policy invalid.")
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
            db.commit()
        return identity


def cancel_proposal(home: Path, identity: str):
    with journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        proposal = _proposal(db, identity)
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


def _hash(data: bytes | None):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _approval(proposal):
    binding = {
        key: proposal[key]
        for key in ("id", "vault", "target", "before_hash", "after_hash", "digest")
    }
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _no_merge_markers(text: str):
    if re.search(r"(?m)^(?:<{7,}|={7,}|>{7,})(?:[ \t].*)?\r?$", text):
        raise ValueError("Unresolved merge markers refused.")


def _preview(db, identity):
    proposal = _proposal(db, identity)
    approved = db.execute(
        "SELECT approval_digest FROM validated_proposals WHERE proposal_id=?",
        (identity,),
    ).fetchone()
    if approved is None or approved[0] != _approval(proposal):
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
    policy = load_policy(home)
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
        result = _prepare_note(
            home,
            vault_root,
            path,
            **{key: value for key, value in fields.items() if key != "path"},
        )
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
        if content is not None:
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
                raise ValueError("Stale or missing full-file revision.")
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
        if not after.strip():
            raise ValueError("Cannot propose an empty note.")
        _no_merge_markers(after)
        if after.encode("utf-8") == before:
            raise ValueError("Proposal has no change.")
        with journal(home) as db:
            previous = db.execute(
                """SELECT p.id FROM proposals p
                JOIN validated_proposals v ON v.proposal_id=p.id
                LEFT JOIN receipts r ON r.proposal_id=p.id
                WHERE p.vault=? AND p.target=? AND p.before_hash IS ?
                AND p.after_hash=? AND p.outcome='proposed' AND r.proposal_id IS NULL
                ORDER BY p.rowid LIMIT 1""",
                (
                    os.path.abspath(vault_root),
                    path,
                    _hash(before),
                    _hash(after.encode()),
                ),
            ).fetchone()
            if previous is not None:
                return _preview(db, previous[0])
        identity = store_proposal(
            home, vault_root, path, after, expected_before_hash=_hash(before)
        )
        with journal(home) as db:
            proposal = _proposal(db, identity)
            approval = _approval(proposal)
            db.execute(
                "INSERT INTO validated_proposals VALUES (?, ?)", (identity, approval)
            )
            db.commit()
        return {
            "proposal_id": identity,
            "approval_digest": approval,
            "path": path,
            "before_revision": _hash(before),
            "after_revision": _hash(after.encode()),
            "content": after,
            "outcome": "proposed",
        }


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
) -> dict:
    require_synthetic(home, vault_root)
    if writers_paused is not True:
        raise ValueError("Confirm other writers are paused before application.")
    with writer_lock(vault_root), journal(home) as db:
        db.execute("BEGIN IMMEDIATE")
        policy = load_policy(home)
        proposal = _proposal(db, identity)
        if proposal["vault"] != os.path.abspath(vault_root):
            raise ValueError("Proposal belongs to another vault.")
        validated = db.execute(
            "SELECT approval_digest FROM validated_proposals WHERE proposal_id=?",
            (identity,),
        ).fetchone()
        if (
            validated is None
            or validated[0] != approval_digest
            or _approval(proposal) != approval_digest
        ):
            raise ValueError("Approval does not bind the exact validated proposal.")
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
    require_synthetic(home, vault_root)
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
