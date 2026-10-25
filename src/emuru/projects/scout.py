"""Fixed, offline reconnaissance of explicitly selected committed Git blobs.

No repository code, Git hooks, filters, configuration or dependency scripts run.
Reports contain file metadata, never source text or model-generated claims.
"""

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
from pathlib import Path, PurePosixPath

from emuru.models.gateway import private_path

MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024
SCOUT = "emuru-scout"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def selected_path(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value)
        or value.startswith(("/", "-"))
        or any(
            part in {"", ".", ".."} or part.startswith(".") for part in value.split("/")
        )
        or PurePosixPath(value).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
    ):
        raise ValueError("Scout file path is forbidden.")
    return value


def load_policy(path, task):
    path = private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 32768
        ):
            raise ValueError("Scout policy must be private, owner-owned and bounded.")
        policy = json.load(stream)
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {
            "schema",
            "repository",
            "path",
            "baseline",
            "files",
            "tasks",
            "timeout_seconds",
        }
        or type(policy["schema"]) is not int
        or policy["schema"] != 1
        or not isinstance(policy["path"], str)
        or not isinstance(policy["repository"], str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", policy["repository"])
        or not isinstance(policy["baseline"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", policy["baseline"])
        or not isinstance(policy["tasks"], list)
        or not 1 <= len(policy["tasks"]) <= 20
        or any(
            not isinstance(item, str) or not re.fullmatch(r"t_[0-9a-f]+", item)
            for item in policy["tasks"]
        )
        or task not in policy["tasks"]
        or type(policy["timeout_seconds"]) is not int
        or not 1 <= policy["timeout_seconds"] <= 120
        or not isinstance(policy["files"], list)
        or not 1 <= len(policy["files"]) <= MAX_FILES
    ):
        raise ValueError("Scout policy does not authorize this bounded task.")
    files = [selected_path(name) for name in policy["files"]]
    if len(set(files)) != len(files):
        raise ValueError("Scout files must be unique.")
    repo = private_path(policy["path"])
    if repo == Path("/") or not repo.is_dir():
        raise ValueError("Scout repository unavailable.")
    private_path(repo / ".git")
    if not (repo / ".git").is_dir():
        raise ValueError(
            "Scout requires a standalone Git repository, not a linked worktree."
        )
    return policy


def git(repo, *arguments):
    # Ignore ambient Git configuration, replacements, credentials and exec overrides.
    result = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "--git-dir",
            str(repo / ".git"),
            *arguments,
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout


def snapshot(policy, destination):
    """Export only approved regular blobs; never copy .git or working-tree files."""
    repo = Path(policy["path"])
    baseline = policy["baseline"]
    if git(repo, "rev-parse", "--verify", "HEAD").decode().strip() != baseline:
        raise ValueError("Scout baseline differs from repository HEAD.")
    total = 0
    for index, name in enumerate(policy["files"]):
        row = git(repo, "ls-tree", "-z", "--full-tree", baseline, "--", name)
        entry = row.split(b"\t", 1)[0].decode().split() if row else None
        if not entry or entry[:2] not in (["100644", "blob"], ["100755", "blob"]):
            raise ValueError("Scout selection is missing, a symlink or a submodule.")
        size = int(git(repo, "cat-file", "-s", entry[2]))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Scout source size limit exceeded.")
        data = git(repo, "cat-file", "blob", entry[2])
        if len(data) != size or b"\0" in data:
            raise ValueError("Scout accepts bounded text blobs only.")
        data.decode("utf-8")
        # Numbered blobs prevent selected names from becoming worker filesystem paths.
        target = destination / str(index)
        target.write_bytes(data)
        target.chmod(0o400)
    manifest = {
        "schema": 1,
        "repository": policy["repository"],
        "repository_id": digest([policy["repository"], str(repo)]),
        "baseline": baseline,
        "files": policy["files"],
        "timeout_seconds": policy["timeout_seconds"],
    }
    (destination / "manifest.json").write_bytes(encoded(manifest))
    (destination / "manifest.json").chmod(0o400)
    return inspect_snapshot(destination)


def inspect_snapshot(folder):
    """Reviewed worker instruction: inventory selected blobs; run no project code."""
    manifest = json.loads((folder / "manifest.json").read_bytes())
    files = []
    total = 0
    if not 1 <= len(manifest["files"]) <= MAX_FILES:
        raise ValueError("Invalid scout manifest.")
    for index, name in enumerate(manifest["files"]):
        selected_path(name)
        fd = os.open(folder / str(index), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Scout blob must be a regular file.")
            data = stream.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Scout source size limit exceeded.")
        files.append(
            {
                "path": name,
                "bytes": len(data),
                "lines": len(data.splitlines()),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        **manifest,
        "files": files,
        "directories": sorted(
            {str(PurePosixPath(name).parent) for name in manifest["files"]}
        ),
        "suggested_work": [
            "Review selected files against the approved task acceptance criteria.",
            "Authorize coding and tests separately; reconnaissance executes neither.",
        ],
        "coverage": "Explicitly selected committed files only; no semantic review or test execution.",
    }


def _stopped(signum, frame):
    raise TimeoutError("Scout stopped or exceeded its time budget.")


def limit_runtime(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 120:
        raise ValueError("Invalid scout time budget.")
    # Container PID 1 ignores default dispositions; explicit handlers enforce deadlines.
    signal.signal(signal.SIGTERM, _stopped)
    signal.signal(signal.SIGALRM, _stopped)
    signal.alarm(seconds)


def worker_main():
    limit_runtime(
        json.loads(Path("/repo/manifest.json").read_bytes())["timeout_seconds"]
    )
    print(encoded(inspect_snapshot(Path("/repo"))).decode())
