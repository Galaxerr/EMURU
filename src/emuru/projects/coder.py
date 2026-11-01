"""Controller-owned selected-file workspace for disposable coder qualification.

Grant shape validation is not live spec/receipt authorization. Repository Python
never runs here; the caller serializes editing and offline check execution.
"""

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from emuru.projects.scout import git, selected_path
from emuru.vault.mutations import directory, regular

LIMITS = {
    "concurrent_tasks": 1,
    "attempt_seconds": 600,
    "model_iterations": 12,
    "check_executions": 4,
    "check_seconds": 60,
    "cpus": 1,
    "memory_bytes": 512 * 1024 * 1024,
    "pids": 64,
    "files": 100,
    "file_bytes": 256 * 1024,
    "total_bytes": 2 * 1024 * 1024,
    "check_output_bytes": 64 * 1024,
    "total_output_bytes": 256 * 1024,
    "artifact_bytes": 8 * 1024 * 1024,
    "scratch_bytes": 64 * 1024 * 1024,
    "scratch_inodes": 1024,
}
_FIELDS = {
    "schema",
    "grant_id",
    "owner",
    "bot",
    "vault",
    "project",
    "proposal",
    "approval",
    "revision",
    "task",
    "task_digest",
    "repository",
    "path",
    "baseline",
    "read_files",
    "write_files",
    "create_files",
    "check_ids",
    "catalogue",
    "network",
    "limits",
}


def validate_grant(grant):
    """Validate frozen scope/ceilings, without consulting real board or vault state."""
    if not isinstance(grant, dict) or set(grant) != _FIELDS:
        raise ValueError("Invalid coder grant fields.")
    patterns = {
        "grant_id": r"[0-9a-f]{32}",
        "proposal": r"[0-9a-f]{32}",
        "vault": r"[0-9a-f]{64}",
        "project": r"[0-9a-f]{64}",
        "approval": r"[0-9a-f]{64}",
        "revision": r"[0-9a-f]{64}",
        "task_digest": r"[0-9a-f]{64}",
        "catalogue": r"[0-9a-f]{64}",
        "baseline": r"[0-9a-f]{40}",
        "task": r"t_[0-9a-f]+",
        "repository": r"[a-z][a-z0-9-]{0,63}",
    }
    if (
        type(grant["schema"]) is not int
        or grant["schema"] != 1
        or any(
            type(grant[key]) is not int or grant[key] <= 0 for key in ("owner", "bot")
        )
        or any(
            not isinstance(grant[key], str) or not re.fullmatch(pattern, grant[key])
            for key, pattern in patterns.items()
        )
        or grant["network"] != "none"
        or grant["check_ids"] != ["python_unittest"]
        or not isinstance(grant["limits"], dict)
        or set(grant["limits"]) != set(LIMITS)
        or any(
            type(grant["limits"][key]) is not int
            or not 1 <= grant["limits"][key] <= ceiling
            for key, ceiling in LIMITS.items()
        )
    ):
        raise ValueError("Invalid coder grant binding or limits.")
    for key in ("read_files", "write_files", "create_files"):
        names = grant[key]
        if not isinstance(names, list) or len(names) > grant["limits"]["files"]:
            raise ValueError("Invalid coder file scope.")
        for name in names:
            selected_path(name)
        if len(set(names)) != len(names):
            raise ValueError("Coder file scopes must be unique.")
    reads, writes, creates = (
        set(grant[key]) for key in ("read_files", "write_files", "create_files")
    )
    if not reads or not writes <= reads or not creates <= reads or writes & creates:
        raise ValueError("Coder write/create scope exceeds read scope.")
    if any(a.startswith(b + "/") for a in reads for b in reads):
        raise ValueError("Coder file scopes overlap directories.")
    path = grant["path"]
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or str(Path(path)) != path
        or path == "/"
    ):
        raise ValueError("Coder repository path must be canonical and absolute.")
    with directory(Path(path)), directory(Path(path) / ".git"):
        pass
    return copy.deepcopy(grant)


def load_grant(path):
    """Open an operator-owned mode-0600, single-link bounded JSON grant."""
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as stream:
            regular(stream.fileno(), private=True)
            data = stream.read(32769)
    if len(data) > 32768:
        raise ValueError("Coder grant is too large.")

    def unique_fields(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate coder grant fields.")
        return result

    return validate_grant(json.loads(data, object_pairs_hook=unique_fields))


def source_diff(before, after):
    import difflib

    result = []
    for name in sorted(after):
        for line in difflib.unified_diff(
            before.get(name, b"").decode("utf-8").splitlines(keepends=True),
            after[name].decode("utf-8").splitlines(keepends=True),
            fromfile="a/" + name if name in before else "/dev/null",
            tofile="b/" + name,
        ):
            result.append(line)
            if not line.endswith("\n"):
                result.append("\n\\ No newline at end of file\n")
    return "".join(result)


class Workspace:
    """Fresh private source; only the controller may hold filesystem authority."""

    def __init__(self, folder, grant, *, reserve=None):
        self.grant = validate_grant(grant)
        self.folder = Path(folder)
        self._baseline = {}
        self.reserve = reserve
        repo = Path(self.grant["path"])
        if not self.folder.is_absolute() or self.folder.is_relative_to(repo):
            raise ValueError("Coder workspace must be outside the original repository.")
        baseline = self.grant["baseline"]
        if git(repo, "rev-parse", "--verify", "HEAD").decode().strip() != baseline:
            raise ValueError("Coder baseline differs from repository HEAD.")
        total = 0
        for name in self.grant["read_files"]:
            row = git(repo, "ls-tree", "-z", "--full-tree", baseline, "--", name)
            if name in self.grant["create_files"]:
                if row:
                    raise ValueError(
                        "Coder creation target already exists at baseline."
                    )
                continue
            entry = row.split(b"\t", 1)[0].decode().split() if row else None
            if not entry or entry[:2] not in (["100644", "blob"], ["100755", "blob"]):
                raise ValueError("Coder source is missing or not a regular blob.")
            size = int(git(repo, "cat-file", "-s", entry[2]))
            total += size
            if (
                size > self.grant["limits"]["file_bytes"]
                or total > self.grant["limits"]["total_bytes"]
            ):
                raise ValueError("Coder source size limit exceeded.")
            data = git(repo, "cat-file", "blob", entry[2])
            if len(data) != size:
                raise ValueError("Coder source blob changed.")
            self._text(data)
            self._baseline[name] = (data, int(entry[0][-3:], 8))
        if self.reserve:
            self.reserve(total)
        with directory(self.folder.parent) as parent:
            os.mkdir(self.folder.name, 0o700, dir_fd=parent)
        for name, (data, mode) in self._baseline.items():
            self._publish(name, data, mode, create=True)
        self.initial_manifest = self.manifest()

    def _text(self, data):
        if b"\0" in data:
            raise ValueError("Coder accepts UTF-8 text only.")
        data.decode("utf-8")

    def _parent(self, name, *, create=False):
        from contextlib import ExitStack

        stack = ExitStack()
        try:
            root = stack.enter_context(directory(self.folder))
            from emuru.vault.mutations import private_directory

            private_directory(root)
            parent = root
            for part in name.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                stack.callback(os.close, child)
                private_directory(child)
                parent = child
            return stack, parent
        except BaseException:
            stack.close()
            raise

    def _read(self, name):
        stack, parent = self._parent(name)
        with stack:
            fd = os.open(
                name.split("/")[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            with os.fdopen(fd, "rb") as stream:
                regular(stream.fileno())
                metadata = os.fstat(stream.fileno())
                if metadata.st_uid != os.getuid():
                    raise ValueError("Coder source owner changed.")
                data = stream.read(self.grant["limits"]["file_bytes"] + 1)
                if len(data) > self.grant["limits"]["file_bytes"]:
                    raise ValueError("Coder source size limit exceeded.")
                self._text(data)
                return data, metadata.st_mode & 0o7777

    def _publish(self, name, data, mode, *, create, expected=None):
        import ctypes
        import uuid

        if self.reserve:
            self.reserve(len(data))
        stack, parent = self._parent(name, create=True)
        staging = ".coder-" + uuid.uuid4().hex
        target = name.split("/")[-1]
        with stack:
            try:
                fd = os.open(
                    staging,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                if create:
                    libc = ctypes.CDLL(None, use_errno=True)
                    if (
                        libc.renameat2(
                            parent, os.fsencode(staging), parent, os.fsencode(target), 1
                        )
                        != 0
                    ):
                        error = ctypes.get_errno()
                        raise OSError(error, os.strerror(error), name)
                else:
                    if hashlib.sha256(self._read(name)[0]).hexdigest() != expected:
                        raise ValueError("Stale coder replacement digest.")
                    os.replace(staging, target, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(staging, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def read(self, name):
        selected_path(name)
        if name not in self.grant["read_files"]:
            raise ValueError("Coder read path is outside grant.")
        data, _ = self._read(name)
        return {
            "path": name,
            "content": data.decode("utf-8"),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def write(self, name, content, expected_sha256=None):
        selected_path(name)
        if not isinstance(content, str):
            raise TypeError("Coder replacement must be text.")
        data = content.encode("utf-8")
        self._text(data)
        current = self.manifest()
        create = name not in current
        if create:
            if name not in self.grant["create_files"] or expected_sha256 is not None:
                raise ValueError("Coder creation path is outside grant.")
            mode = 0o644
        else:
            if (
                name not in self.grant["write_files"] + self.grant["create_files"]
                or expected_sha256 != current[name]["sha256"]
            ):
                raise ValueError("Coder replacement is unauthorized or stale.")
            mode = current[name]["mode"]
        if (
            len(data) > self.grant["limits"]["file_bytes"]
            or sum(item["size"] for item in current.values())
            - current.get(name, {}).get("size", 0)
            + len(data)
            > self.grant["limits"]["total_bytes"]
        ):
            raise ValueError("Coder source size limit exceeded.")
        self._publish(name, data, mode, create=create, expected=expected_sha256)
        return hashlib.sha256(data).hexdigest()

    def manifest(self):
        import stat

        found = set()
        allowed_dirs = {str(Path(name).parent) for name in self.grant["read_files"]}
        allowed_dirs |= {
            str(parent)
            for name in self.grant["read_files"]
            for parent in Path(name).parents
            if str(parent) != "."
        }

        def visit(fd, prefix=""):
            for entry in os.listdir(fd):
                name = prefix + entry
                info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and name in allowed_dirs:
                    child = os.open(
                        entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        visit(child, name + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and name in self.grant["read_files"]:
                    found.add(name)
                else:
                    raise ValueError("Unexpected coder workspace path or file type.")

        with directory(self.folder) as root:
            visit(root)
        if (
            not set(self._baseline) <= found
            or len(found) > self.grant["limits"]["files"]
        ):
            raise ValueError("Coder source missing or file limit exceeded.")
        manifest = {}
        for name in sorted(found):
            data, mode = self._read(name)
            initial = self._baseline.get(name)
            if mode != (initial[1] if initial else 0o644):
                raise ValueError("Coder source mode changed.")
            if (
                name not in self.grant["write_files"] + self.grant["create_files"]
                and data != initial[0]
            ):
                raise ValueError("Coder read-only source changed.")
            manifest[name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
        if (
            sum(item["size"] for item in manifest.values())
            > self.grant["limits"]["total_bytes"]
        ):
            raise ValueError("Coder source size limit exceeded.")
        return manifest

    def snapshot(self, destination):
        """Copy validated bytes to a fresh read-only check tree; never hardlink."""
        destination = Path(destination)
        if any(
            destination.is_relative_to(root)
            for root in (self.folder, Path(self.grant["path"]))
        ):
            raise ValueError(
                "Coder snapshot must be separate from mutable/source trees."
            )
        manifest = self.manifest()
        if self.reserve:
            self.reserve(sum(item["size"] for item in manifest.values()))
        with directory(destination.parent) as parent:
            os.mkdir(destination.name, 0o700, dir_fd=parent)
        for name, item in manifest.items():
            data, _ = self._read(name)
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("Coder snapshot source changed.")
            target = destination / name
            for parent in reversed(target.parent.relative_to(destination).parents):
                (destination / parent).mkdir(mode=0o700, exist_ok=True)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with directory(target.parent) as parent:
                fd = os.open(
                    target.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o444)
                    os.fsync(stream.fileno())
        if self.manifest() != manifest:
            raise ValueError("Coder snapshot source changed.")
        for root, dirs, _ in os.walk(destination, topdown=False):
            Path(root).chmod(0o555)
        return manifest

    def diff(self):
        """Inspectable scoped content diff; no synthetic Git commit or task HEAD."""
        manifest = self.manifest()
        return source_diff(
            {name: item[0] for name, item in self._baseline.items()},
            {name: self._read(name)[0] for name in manifest},
        )
