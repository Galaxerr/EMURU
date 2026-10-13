"""Mutation prerequisites. Broader publication has no entrypoint in v0.4.1."""

import fcntl
import hashlib
import json
import os
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
                connection.commit()
                yield connection
            finally:
                connection.close()
        finally:
            os.close(parent)


def store_proposal(home: Path, vault_root: Path, path: str, payload: str) -> str:
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
        db.execute(
            "UPDATE proposals SET outcome='cancelled' WHERE id=? AND outcome='proposed'",
            (identity,),
        )
        db.execute(
            """INSERT OR IGNORE INTO receipts
            SELECT id, outcome, before_hash, after_hash FROM proposals
            WHERE id=? AND outcome='cancelled'""",
            (identity,),
        )
        db.commit()
