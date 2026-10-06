"""Resolve an owner's private live target; never select targets for test modes."""

import json
import os
import stat
from pathlib import Path


class TargetError(ValueError):
    """Content-free target diagnostic safe to display."""


def profile_home() -> Path:
    raw = os.environ.get(
        "EMURU_HERMES_PROFILE_HOME",
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes/profiles/emuru")),
    )
    path = Path(raw)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name != "emuru"
        or path.parent.name != "profiles"
    ):
        raise TargetError("profile_location_invalid")
    _no_symlinks(path)
    return path


def _no_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise TargetError("target_symlink_refused")


def _read_record(path: Path):
    _no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 4096
        ):
            raise TargetError("target_record_permissions_invalid")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            data = json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(data, dict):
        raise TargetError("target_record_invalid")
    return data


def load_target(repo: Path, home: Path) -> tuple[str, Path]:
    """Absent record means synthetic. Invalid explicit records fail closed."""
    try:
        repo = repo.resolve()
        data = _read_record(home / "vault-target.json")
        if data is None or data == {"mode": "synthetic"}:
            return "synthetic", repo / ".runtime/vault"
        if (
            set(data) != {"mode", "vault_path"}
            or data["mode"] != "real"
            or not isinstance(data["vault_path"], str)
            or not data["vault_path"].strip()
        ):
            raise TargetError("target_record_invalid")
        root = Path(data["vault_path"])
        if not root.is_absolute() or ".." in root.parts:
            raise TargetError("target_root_invalid")
        _no_symlinks(root)
        if not root.is_dir():
            raise TargetError("target_root_unavailable")
        root = root.resolve()
        for other in (repo, home.resolve()):
            if root == other or root in other.parents or other in root.parents:
                raise TargetError("target_repository_overlap")
        git = root / ".git"
        if git.is_symlink() or not (git.is_dir() or git.is_file()):
            raise TargetError("target_git_root_missing")
        inbox = root / "00_Inbox"
        if inbox.is_symlink() or not inbox.is_dir():
            raise TargetError("target_inbox_unavailable")
        for name in ("_index", "99_System"):
            folder = root / name
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                raise TargetError("target_derived_directory_invalid")
        for name in (
            "_index/graph.json",
            "_index/graph.json.tmp",
            "_index/notes.sqlite",
            "_index/notes.sqlite.tmp",
            "_index/notes.sqlite.tmp-journal",
            "_index/notes.sqlite.tmp-wal",
            "_index/notes.sqlite.tmp-shm",
            "99_System/INDEX.md",
        ):
            output = root / name
            if output.is_symlink() or (output.exists() and not output.is_file()):
                raise TargetError("target_derived_file_invalid")
        return "real", root
    except TargetError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError, RuntimeError) as error:
        raise TargetError("target_record_unreadable") from error
