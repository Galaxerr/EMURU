"""Owner-operated, one-shot scout dispatcher; no bot, model, daemon or second queue."""

import argparse
import fcntl
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import yaml

from emuru.models.gateway import private_path
from emuru.projects.scout import (
    MAX_REPORT_BYTES,
    digest,
    encoded,
    load_policy,
    snapshot,
)
from emuru.vault import mutations


@contextmanager
def dispatcher_lock(home):
    """A dead dispatcher releases the lock, but never automatically releases its claim."""
    with mutations.directory(home) as parent:
        mutations.private_directory(parent)
    folder = home / "emuru-projects"
    with mutations.directory(folder) as parent:
        mutations.private_directory(parent)
        fd = os.open(
            "scout.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        try:
            mutations.regular(fd, private=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


class DockerScout:
    """Host owns Docker; neither container receives its socket or host environment."""

    def __init__(self, root, image, home, staging):
        self.home = home
        config = yaml.safe_load((root / "infra/docker/scout.compose.yaml").read_text())
        if os.getuid() == 0 or os.getgid() == 0:
            raise ValueError("Scout dispatcher must run as an unprivileged owner.")
        for service in config["services"].values():
            service["image"] = image
            service["user"] = f"{os.getuid()}:{os.getgid()}"
        config["services"]["scout"]["volumes"] = [
            {
                "type": "bind",
                "source": str(staging / "snapshot"),
                "target": "/repo",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ]
        ledger = config["services"]["ledger"]
        ledger["environment"]["EMURU_SCOUT_HOME"] = "/control"
        ledger["tmpfs"].append(
            f"/control:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid={os.getuid()},gid={os.getgid()}"
        )
        ledger["volumes"] = [
            {
                "type": "bind",
                "source": str(home / "emuru-projects"),
                "target": "/control/emuru-projects",
                "bind": {"create_host_path": False},
            }
        ]
        config_path = staging / "compose.json"
        config_path.write_bytes(encoded(config))
        self.command = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            "emuru-scout-" + uuid.uuid4().hex[:12],
            "--file",
            str(config_path),
        ]

    def call(self, service, request=None, timeout=30):
        name = "emuru-scout-" + uuid.uuid4().hex
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                try:
                    subprocess.run(
                        [
                            *self.command,
                            "run",
                            "--rm",
                            "--no-deps",
                            "-T",
                            "--name",
                            name,
                            service,
                        ],
                        input=None if request is None else encoded(request),
                        stdout=output,
                        stderr=errors,
                        check=True,
                        timeout=timeout,
                    )
                except subprocess.CalledProcessError as error:
                    errors.seek(0)
                    error.stderr = errors.read(16384)
                    raise
                output.seek(0)
                data = output.read(MAX_REPORT_BYTES + 1)
            if len(data) > MAX_REPORT_BYTES:
                raise ValueError("Scout output limit exceeded.")
            return json.loads(data)
        finally:
            # Docker client death does not stop its container; always terminate by fixed identity.
            subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )


def execute(backend, policy, task, expected):
    grant = {
        key: policy[key]
        for key in ("repository", "baseline", "files", "timeout_seconds")
    }
    grant.update(
        {
            "source_task": task,
            "repository_id": expected["repository_id"],
            "report_sha256": digest(expected),
        }
    )
    claimed = backend.call("ledger", {"action": "claim", "grant": grant})
    if claimed["status"] != "claimed":
        return claimed
    request = {"action": "finish", "task": claimed["task"], "run": claimed["run"]}
    try:
        report = backend.call("scout", timeout=policy["timeout_seconds"])
        if encoded(report) != encoded(expected):
            raise ValueError("Scout report failed independent snapshot verification.")
        request["report"] = report
    except (Exception, KeyboardInterrupt):
        # Report a known failed attempt without retrying or manufacturing completion.
        backend.call("ledger", request)
        raise
    return backend.call("ledger", request)


def interrupted(signum, frame):
    raise InterruptedError("Scout dispatcher interrupted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, required=True, help="Existing private M2 profile home"
    )
    parser.add_argument(
        "--policy",
        type=Path,
        required=True,
        help="Private operator-approved repository/task policy",
    )
    parser.add_argument("--task", required=True, help="Existing M2 tracked card ID")
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    policy = load_policy(args.policy, args.task)
    home = private_path(args.home)
    with dispatcher_lock(home):
        launcher = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
        _, image = launcher["ensure_image"](build=False)
        with tempfile.TemporaryDirectory(prefix="emuru-scout-") as temporary:
            staging = Path(temporary)
            (staging / "snapshot").mkdir(mode=0o700)
            expected = snapshot(policy, staging / "snapshot")
            backend = DockerScout(ROOT, image, home, staging)
            result = execute(backend, policy, args.task, expected)
            print(encoded(result).decode())
            return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print(
            "Scout refused or interrupted; inspect native card state. No automatic retry.",
            file=sys.stderr,
        )
        raise SystemExit(1)
