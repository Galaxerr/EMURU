"""Owner-operated, one-shot scout dispatcher; no bot, model, daemon or second queue."""

import argparse
import csv
import io
import json
import os
import runpy
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from emuru.models.gateway import private_path
from emuru.projects.dispatcher import dispatcher_lock
from emuru.projects.scout import (
    MAX_REPORT_BYTES,
    digest,
    encoded,
    load_policy,
    snapshot,
)


def _bounded_call(command, payload, deadline_ns, stopped, process_event=None):
    """Poll stop intent during fixed Docker waits; bound stdin and both output streams."""
    if len(payload) > MAX_REPORT_BYTES + 32768 or stopped():
        raise InterruptedError("Scout stop requested or input bound exceeded.")
    if time.monotonic_ns() >= deadline_ns:
        raise TimeoutError("Scout original deadline exhausted.")
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    with tempfile.TemporaryFile() as source:
        source.write(payload)
        source.seek(0)
        process = subprocess.Popen(
            command,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        identity = None
        try:
            if process_event is not None:
                from emuru.projects import supervisor

                identity = {
                    "pid": process.pid,
                    "start_time": Path(f"/proc/{process.pid}/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()[19],
                    "boot_id": supervisor.boot_id(),
                }
                process_event("client-bound", identity)
            with selectors.DefaultSelector() as poller:
                poller.register(process.stdout, selectors.EVENT_READ, "stdout")
                poller.register(process.stderr, selectors.EVENT_READ, "stderr")
                while poller.get_map() or process.poll() is None:
                    if stopped():
                        if process_event is not None:
                            process_event("stop-observed", time.monotonic_ns())
                        raise InterruptedError(
                            "Scout stop requested; effects remain uncertain."
                        )
                    remaining = (deadline_ns - time.monotonic_ns()) / 10**9
                    if remaining <= 0:
                        raise TimeoutError("Scout original deadline exhausted.")
                    if not poller.get_map():
                        time.sleep(min(remaining, 0.1))
                    for key, _ in poller.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            poller.unregister(key.fileobj)
                            continue
                        limit = MAX_REPORT_BYTES if key.data == "stdout" else 16384
                        if len(streams[key.data]) + len(chunk) > limit:
                            raise ValueError(
                                "Scout command/report output bound exceeded."
                            )
                        streams[key.data].extend(chunk)
            return process.wait(), {key: bytes(value) for key, value in streams.items()}
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            if process_event is not None and identity is not None:
                process_event("client-stopped", identity)


def _docker(command, *, seconds=10, deadline_ns=None, stopped=None):
    bounded_deadline = time.monotonic_ns() + seconds * 10**9
    code, streams = _bounded_call(
        ["docker", *command],
        b"",
        bounded_deadline if deadline_ns is None else min(deadline_ns, bounded_deadline),
        (lambda: False) if stopped is None else stopped,
    )
    return code, streams


def _inspect(name, *, deadline_ns=None, stopped=None):
    limits = (
        {} if deadline_ns is None else {"deadline_ns": deadline_ns, "stopped": stopped}
    )
    code, streams = _docker(["inspect", "--format", "{{json .}}", name], **limits)
    if code:
        prefix, _, missing_name = streams["stderr"].decode().strip().rpartition(": ")
        missing_errors = {
            f"{intro}no such {kind}"
            for intro in ("", "error: ", "error response from daemon: ")
            for kind in ("object", "container")
        }
        if missing_name == name and prefix.casefold() in missing_errors:
            return None
        raise ValueError("Docker inspection unavailable; ownership remains uncertain.")
    return json.loads(streams["stdout"])


def _create_command(service, name):
    """Compose create omits AttachStdin; preserve its confinement with Docker create."""
    command = ["docker", "create", "--interactive", "--pull", "never", "--name", name]
    for flag, key in (
        ("--user", "user"),
        ("--network", "network_mode"),
        ("--pids-limit", "pids_limit"),
        ("--memory", "mem_limit"),
        ("--cpus", "cpus"),
        ("--restart", "restart"),
    ):
        command.extend([flag, str(service[key])])
    if service["read_only"]:
        command.append("--read-only")
    if "stop_grace_period" in service:
        command.extend(
            ["--stop-timeout", str(int(service["stop_grace_period"].removesuffix("s")))]
        )
    for flag, key in (
        ("--cap-drop", "cap_drop"),
        ("--security-opt", "security_opt"),
        ("--tmpfs", "tmpfs"),
    ):
        for value in service[key]:
            command.extend([flag, value])
    for flag, key in (("--env", "environment"), ("--label", "labels")):
        for key_name, value in service[key].items():
            command.extend([flag, f"{key_name}={value}"])
    command.extend(["--log-driver", service["logging"]["driver"]])
    for mount in service["volumes"]:
        if (
            mount["type"] != "bind"
            or mount["bind"].get("create_host_path") is not False
        ):
            raise ValueError("Scout requires existing bind mounts.")
        fields = ["type=bind", "src=" + mount["source"], "dst=" + mount["target"]]
        if mount.get("read_only"):
            fields.append("readonly")
        encoded_mount = io.StringIO()
        csv.writer(encoded_mount, lineterminator="").writerow(fields)
        command.extend(["--mount", encoded_mount.getvalue()])
    return [
        *command,
        "--entrypoint",
        service["entrypoint"][0],
        service["image"],
        *service["entrypoint"][1:],
        *service["command"],
    ]


class DockerScout:
    """Host owns Docker; neither container receives its socket or host environment."""

    def __init__(
        self,
        root,
        image,
        home,
        staging,
        *,
        admission=None,
        deadline_ns=None,
        cleanup=False,
    ):
        self.home = home
        self.image, self.admission, self.deadline_ns = image, admission, deadline_ns
        self.cleanup = cleanup
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
        ledger["environment"]["EMURU_SCOUT_HOME"] = str(home)
        ledger["tmpfs"].append(
            f"{home}:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid={os.getuid()},gid={os.getgid()}"
        )
        ledger["volumes"] = [
            {
                "type": "bind",
                "source": str(home / "emuru-projects"),
                "target": str(home / "emuru-projects"),
                "bind": {"create_host_path": False},
            }
        ]
        attempts = home / "emuru-projects/coder-attempts"
        if attempts.exists():
            from emuru.projects.coder import load_grant
            from emuru.vault.mutations import directory, private_directory

            with directory(attempts) as parent:
                private_directory(parent)
                entries = os.listdir(parent)
            repositories = {
                load_grant(attempts / entry / "grant.json")["path"] for entry in entries
            }
            for repository in sorted(repositories):
                ledger["volumes"].append(
                    {
                        "type": "bind",
                        "source": repository,
                        "target": repository,
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                )
        config_path = staging / "compose.json"
        self.config_path = config_path
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
        if self.admission is not None:
            return self._supervised_call(service, request)
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

    def _supervised_call(self, service, request):
        from emuru.projects import supervisor_scout

        name = "emuru-scout-" + uuid.uuid4().hex
        operation = request["action"] if request is not None else "inspect"
        if self.cleanup and (
            service != "ledger" or operation not in {"inspect-stage", "stop"}
        ):
            raise ValueError(
                "Cleanup permits existing native metadata inspection/stop only."
            )
        deadline_ns = (
            time.monotonic_ns() + 30 * 10**9 if self.cleanup else self.deadline_ns
        )
        stopped = (
            (lambda: False)
            if self.cleanup
            else (lambda: supervisor_scout.stop_requested(self.home, self.admission))
        )
        supervisor_scout.event(
            self.home,
            self.admission,
            "planned",
            name=name,
            operation=operation,
            service=service,
        )
        config = json.loads(self.config_path.read_text())
        config["services"][service]["container_name"] = name
        config["services"][service]["stdin_open"] = True
        config["services"][service]["environment"]["EMURU_SCOUT_DEADLINE_NS"] = str(
            deadline_ns
        )
        config["services"][service]["labels"] = {
            "io.emuru.scout.stage": self.admission,
            "io.emuru.scout.operation": name,
        }
        self.config_path.write_bytes(encoded(config))
        identity = None
        try:
            if stopped():
                raise InterruptedError(
                    "Scout stop requested before container creation."
                )
            code, _ = _bounded_call(
                _create_command(config["services"][service], name),
                b"",
                min(deadline_ns, time.monotonic_ns() + 10 * 10**9),
                stopped,
            )
            if code:
                raise ValueError(
                    "Scout container creation refused; inspect retained intent."
                )
            record = _inspect(name, deadline_ns=deadline_ns, stopped=stopped)
            if (
                record is None
                or record["Image"] != self.image
                or record["Config"]["Labels"].get("io.emuru.scout.stage")
                != self.admission
                or record["Config"]["Labels"].get("io.emuru.scout.operation") != name
            ):
                raise ValueError("Scout created container identity mismatched.")
            identity = record["Id"]
            supervisor_scout.event(
                self.home, self.admission, "created", name=name, container_id=identity
            )
            supervisor_scout.event(
                self.home, self.admission, "client-planned", name=name
            )
            if request is not None:
                request = {**request, "controller": name}
            code, streams = _bounded_call(
                ["docker", "start", "--attach", "--interactive", identity],
                b"" if request is None else encoded(request),
                deadline_ns,
                stopped,
                lambda phase, process: supervisor_scout.event(
                    self.home,
                    self.admission,
                    phase,
                    name=name,
                    **(
                        {"observed_ns": process}
                        if phase == "stop-observed"
                        else {"process": process}
                    ),
                ),
            )
            if code:
                raise ValueError("Scout container failed; no automatic retry.")
            return json.loads(streams["stdout"])
        finally:
            record = _inspect(identity or name)
            if record is not None:
                if (
                    record["Image"] != self.image
                    or record["Config"]["Labels"].get("io.emuru.scout.stage")
                    != self.admission
                    or record["Config"]["Labels"].get("io.emuru.scout.operation")
                    != name
                ):
                    raise ValueError(
                        "Scout cleanup identity changed; retain ownership."
                    )
                identity = record["Id"]
                supervisor_scout.event(
                    self.home,
                    self.admission,
                    "created",
                    name=name,
                    container_id=identity,
                )
                _docker(["rm", "--force", identity])
                if _inspect(identity) is not None:
                    raise ValueError("Scout container stop unproven; retain ownership.")
                supervisor_scout.event(
                    self.home,
                    self.admission,
                    "container-stopped",
                    name=name,
                    container_id=identity,
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
    if getattr(backend, "admission", None) is not None:
        from emuru.projects import supervisor_scout

        claimed = backend.call(
            "ledger",
            {"action": "claim", "grant": grant, "admission": backend.admission},
        )
        if claimed["status"] != "claimed":
            raise ValueError("Supervised Scout native claim unproven.")
        report = backend.call("scout")
        if encoded(report) != encoded(expected):
            raise ValueError("Scout report failed independent snapshot verification.")
        supervisor_scout.save_report(backend.home, backend.admission, report)
        result = backend.call(
            "ledger",
            {
                "action": "finish",
                "task": claimed["task"],
                "run": claimed["run"],
                "report": report,
                "admission": backend.admission,
            },
        )
        if (
            result.get("status") != "done"
            or result.get("task") != claimed["task"]
            or result.get("evidence", {}).get("sha256") != digest(expected)
        ):
            raise ValueError("Scout native completion/evidence unproven.")
        supervisor_scout.event(
            backend.home,
            backend.admission,
            "verified",
            native_task=claimed["task"],
            run=claimed["run"],
            report_sha256=digest(expected),
        )
        return result
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


def cleanup_scout(home, root, policy, task, image, staging):
    from emuru.projects import supervisor_scout
    from emuru.projects.coder_board import launcher_stopped

    stage, records = supervisor_scout.load(home, policy, task)
    admission = digest(stage)
    if stage["image"] != image:
        raise ValueError("Cleanup source/image mismatch; preserve recorded resources.")
    if not launcher_stopped(stage["launcher"]):
        raise ValueError("Original Scout launcher may still execute; cleanup refused.")
    final = next(
        (
            r
            for r in records
            if r.get("admission") == stage["generation"]
            and r.get("phase") in {"verified", "stopped"}
        ),
        None,
    )
    if final is not None:
        return {
            "status": "done" if final["phase"] == "verified" else "uncertain",
            "task": final["native_task"],
            "run": final["run"],
            "detail": "Existing terminal result retained; no work replayed.",
        }
    incomplete = False
    events = [r for r in records if r.get("admission") == stage["generation"]]
    for planned in (r for r in events if r["phase"] == "client-planned"):
        bound = next(
            (
                r
                for r in events
                if r["phase"] == "client-bound" and r["name"] == planned["name"]
            ),
            None,
        )
        if bound is None:
            incomplete = True
            continue
        process = bound["process"]
        if not launcher_stopped(process):
            os.killpg(process["pid"], signal.SIGKILL)
            until = time.monotonic() + 5
            while not launcher_stopped(process) and time.monotonic() < until:
                time.sleep(0.05)
            if not launcher_stopped(process):
                raise ValueError("Scout client stop unproven; retain ownership.")
        supervisor_scout.event(
            home, admission, "client-stopped", name=planned["name"], process=process
        )
    for planned in (r for r in events if r["phase"] == "planned"):
        bound = next(
            (
                r
                for r in events
                if r["phase"] == "created" and r["name"] == planned["name"]
            ),
            None,
        )
        record = _inspect(bound["container_id"] if bound else planned["name"])
        identity = bound["container_id"] if bound else None
        if record is not None:
            if (
                record["Image"] != image
                or record["Config"]["Labels"].get("io.emuru.scout.stage") != admission
                or record["Config"]["Labels"].get("io.emuru.scout.operation")
                != planned["name"]
            ):
                raise ValueError(
                    "Cleanup container identity changed; retain ownership."
                )
            identity = record["Id"]
            if bound is None:
                supervisor_scout.event(
                    home,
                    admission,
                    "created",
                    name=planned["name"],
                    container_id=identity,
                )
            _docker(["rm", "--force", identity])
            if _inspect(identity) is not None:
                raise ValueError("Scout container stop unproven.")
        if identity is None:
            incomplete = True
        else:
            supervisor_scout.event(
                home,
                admission,
                "container-stopped",
                name=planned["name"],
                container_id=identity,
            )
    if incomplete:
        return {
            "status": "uncertain",
            "stop": "unproven",
            "detail": "A planned process/container lacks an acknowledged identity; no claim cleared.",
        }
    if not any(
        r.get("admission") == stage["generation"]
        and r.get("phase") == "launcher-stopped"
        for r in records
    ):
        supervisor_scout.event(
            home,
            admission,
            "launcher-stopped",
            observer=supervisor_scout.launcher_identity(),
        )
    backend = DockerScout(
        root,
        image,
        home,
        staging,
        admission=admission,
        deadline_ns=stage["deadline_ns"],
        cleanup=True,
    )
    result = backend.call("ledger", {"action": "stop", "admission": admission})
    if result["task"] is None or result["run"] is None:
        return {
            "status": "uncertain",
            "detail": "Stopped resources; native claim outcome unproven. Reservation retained.",
        }
    if not any(
        r.get("admission") == stage["generation"] and r.get("phase") == "claimed"
        for r in records
    ):
        supervisor_scout.event(
            home, admission, "claimed", native_task=result["task"], run=result["run"]
        )
    if result["status"] == "done":
        supervisor_scout.event(
            home,
            admission,
            "verified",
            native_task=result["task"],
            run=result["run"],
            report_sha256=stage["report_sha256"],
        )
        return result
    if result.get("stop") != "proven":
        raise ValueError("Native Scout stop transition unproven.")
    supervisor_scout.event(
        home, admission, "stopped", native_task=result["task"], run=result["run"]
    )
    return result


def launch(home, policy_path, task, root=ROOT, *, vault=None, cleanup=False):
    from emuru.projects import supervisor, supervisor_scout

    policy = load_policy(policy_path, task)
    home = private_path(home)
    with dispatcher_lock(home):
        supervised = supervisor.enabled(home)
        if supervised and vault is None:
            raise ValueError("Supervised Scout requires its exact --vault binding.")
        if cleanup and not supervised:
            raise ValueError("No supervised Scout to clean up.")
        _, image = runpy.run_path(str(root / "scripts/hermes/container.py"))[
            "ensure_image"
        ](build=False)
        with tempfile.TemporaryDirectory(
            prefix="emuru-scout-", dir=root / ".runtime"
        ) as temporary:
            staging = Path(temporary)
            (staging / "snapshot").mkdir(mode=0o700)
            if cleanup:
                return cleanup_scout(home, root, policy, task, image, staging)
            if supervised:
                supervisor_scout.preflight(home, vault, root, policy, task, image)
            expected = snapshot(policy, staging / "snapshot")
            stage = None
            if supervised:
                grant = {
                    key: policy[key]
                    for key in ("repository", "baseline", "files", "timeout_seconds")
                }
                grant.update(
                    source_task=task,
                    repository_id=expected["repository_id"],
                    report_sha256=digest(expected),
                )
                stage = supervisor_scout.reserve(
                    home, vault, root, policy, task, image, grant
                )
            backend = DockerScout(
                root,
                image,
                home,
                staging,
                **(
                    {"admission": digest(stage), "deadline_ns": stage["deadline_ns"]}
                    if stage
                    else {}
                ),
            )
            try:
                return execute(backend, policy, task, expected)
            except (Exception, KeyboardInterrupt):
                if not supervised:
                    raise
                return {
                    "status": "uncertain",
                    "detail": "Scout interrupted; reservation and ownership retained. Explicit cleanup required.",
                }


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
    parser.add_argument(
        "--vault",
        type=Path,
        help="Exact registered vault; required for supervised Scout",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Inspect and stop recorded resources only after original launcher death",
    )
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    result = launch(
        args.home, args.policy, args.task, vault=args.vault, cleanup=args.cleanup
    )
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
