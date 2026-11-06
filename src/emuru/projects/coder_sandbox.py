"""Fixed offline candidate execution; authoritative assertions stay outside it."""

import json
import os
import selectors
import signal
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

CASES = (([], 0), ([1, 2, 3], 6), ([-2, 5], 3), ([0, 0], 0), ([4, 4], 8))


def install_exec_guard():
    """Irreversibly deny execve/execveat in the qualified Linux x86-64 process."""
    import ctypes
    import errno

    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError(
            "Coding syscall confinement is unqualified on this platform."
        )

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    # Linux seccomp_data offsets and UAPI constants; reject alternate syscall ABIs.
    rules = (Filter * 9)(
        Filter(0x20, 0, 0, 4),
        Filter(0x15, 1, 0, 0xC000003E),
        Filter(0x06, 0, 0, 0x80000000),
        Filter(0x20, 0, 0, 0),
        Filter(0x15, 3, 0, 59),
        Filter(0x15, 2, 0, 322),
        Filter(0x45, 1, 0, 0x40000000),
        Filter(0x06, 0, 0, 0x7FFF0000),
        Filter(0x06, 0, 0, 0x00050000 | errno.EACCES),
    )
    program = Program(len(rules), rules)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.addressof(program), 0, 0):
        error = ctypes.get_errno()
        raise OSError(error, "Coding syscall confinement unavailable.")


def bounded_process(command, payload, seconds, output_bytes, *, env=None):
    """Stop overflowing producers while they run; never buffer unlimited output."""
    if seconds <= 0 or output_bytes <= 0 or len(payload) > 4096:
        raise ValueError("Invalid coding process bounds.")
    deadline = time.monotonic() + seconds
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    try:
        process.stdin.write(payload)
        process.stdin.close()
        with selectors.DefaultSelector() as poller:
            poller.register(process.stdout, selectors.EVENT_READ, "stdout")
            poller.register(process.stderr, selectors.EVENT_READ, "stderr")
            while poller.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Coding process exceeded its deadline.")
                for key, _ in poller.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        poller.unregister(key.fileobj)
                        continue
                    if sum(map(len, streams.values())) + len(chunk) > output_bytes:
                        raise ValueError("Coding process output limit exceeded.")
                    streams[key.data].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Coding process exceeded its deadline.")
        code = process.wait(timeout=remaining)
        return {
            "exit_code": code,
            **{name: bytes(data).decode() for name, data in streams.items()},
        }
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        process.stdin.close()


def run_acceptance(invoke):
    """Run fixed cases through the host-controlled Monty callable boundary."""
    cases = []
    for prices, expected in CASES:
        value = invoke(prices)
        record = {
            "input": prices,
            "expected": expected,
            "host_call_returned": True,
            "returned_type": type(value).__name__,
            "value": value if type(value) in {int, str, bool, type(None)} else None,
            "accepted": type(value) is int and value == expected,
        }
        cases.append(record)
    return {
        "success": len(cases) == len(CASES) and all(row["accepted"] for row in cases),
        "case_count": len(cases),
        "cases": cases,
    }


def candidate_main():
    """Evaluate source and fixed calls in Monty; no guest callback or serializer."""
    from pydantic_monty import Monty

    source = Path("/repo/sum_prices.py").read_text(encoding="utf-8")

    def invoke(prices):
        with (
            Monty() as pool,
            pool.checkout(
                limits={"max_memory": 32_000_000, "max_feed_duration_secs": 2.0}
            ) as session,
        ):
            session.feed_run(source)
            return session.feed_run(
                "sum_prices(prices)",
                inputs={"prices": prices},
                print_callback=lambda _stream, _text: None,
            )

    result = run_acceptance(invoke)
    print(json.dumps(result))


def developer_main():
    install_exec_guard()
    suite = unittest.defaultTestLoader.discover("/repo", pattern="test_*.py")
    result = unittest.TextTestRunner().run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


def sandbox_configuration(root, image, snapshot, uid, gid, limits=None):
    from emuru.projects.coder import LIMITS

    if uid <= 0 or gid <= 0:
        raise ValueError("Coding checks require an unprivileged UID and GID.")
    limits = LIMITS if limits is None else limits
    if set(limits) != set(LIMITS) or any(
        type(value) is not int or not 1 <= value <= LIMITS[key]
        for key, value in limits.items()
    ):
        raise ValueError("Invalid coding resource limits.")
    import yaml

    config = yaml.safe_load(
        (Path(root) / "infra/docker/coder.compose.yaml").read_text()
    )
    service = config["services"]["check"]
    service["image"] = image
    service["user"] = f"{uid}:{gid}"
    service["mem_limit"] = service["memswap_limit"] = limits["memory_bytes"]
    service["pids_limit"] = limits["pids"]
    service["cpus"] = limits["cpus"]
    service["entrypoint"][-1] = str(limits["check_seconds"])
    service["tmpfs"][0] = (
        "/tmp:rw,noexec,nosuid,nodev,"
        f"size={limits['scratch_bytes']},nr_inodes={limits['scratch_inodes']},mode=1777"
    )
    service["volumes"] = [
        {
            "type": "bind",
            "source": str(snapshot),
            "target": "/repo",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    return config


def inspect_confinement(record, snapshot, uid, gid, limits):
    """Verify actual daemon state rather than treating Compose text as proof."""
    host, config = record["HostConfig"], record["Config"]
    if (
        host["NetworkMode"] != "none"
        or not host["ReadonlyRootfs"]
        or host["Privileged"]
        or host["PidMode"]
        or host["IpcMode"] == "host"
        or set(host["CapDrop"] or []) != {"ALL"}
        or host["CapAdd"]
        or "no-new-privileges:true" not in host["SecurityOpt"]
        or host["Memory"] != limits["memory_bytes"]
        or host["MemorySwap"] != limits["memory_bytes"]
        or host["PidsLimit"] != limits["pids"]
        or host["NanoCpus"] != limits["cpus"] * 1_000_000_000
        or config["User"] != f"{uid}:{gid}"
        or uid <= 0
        or gid <= 0
        or host["LogConfig"]["Type"] != "none"
    ):
        raise ValueError("Actual coding container confinement differs from policy.")
    bindings = [mount for mount in record["Mounts"] if mount["Type"] == "bind"]
    if len(bindings) != 1 or any(
        mount["Destination"] != "/repo"
        or mount["RW"]
        or mount["Source"] != str(snapshot)
        for mount in bindings
    ):
        raise ValueError("Unexpected coding container mounts.")
    if any(mount["Type"] not in {"bind", "tmpfs"} for mount in record["Mounts"]):
        raise ValueError("Unexpected coding container storage.")
    if set(host["Tmpfs"]) != {"/tmp", "/dev/shm"}:
        raise ValueError("Unexpected coding writable scratch.")
    scratch = host["Tmpfs"]["/tmp"].split(",")
    if not {
        "rw",
        "noexec",
        "nosuid",
        "nodev",
        f"size={limits['scratch_bytes']}",
        f"nr_inodes={limits['scratch_inodes']}",
    } <= set(scratch):
        raise ValueError("Coding scratch limits are missing.")
    if "ro" not in host["Tmpfs"]["/dev/shm"].split(","):
        raise ValueError("Coding shared memory is writable.")
    environment = dict(item.split("=", 1) for item in config["Env"])
    allowed = {
        "HOME",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "PATH",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "EMURU_HERMES_ROOT",
        "HERMES_DISABLE_LAZY_INSTALLS",
        "UV_OFFLINE",
        "UV_NO_SYNC",
        "XDG_STATE_HOME",
    }
    if set(environment) - allowed or environment.get("HOME") != "/tmp":
        raise ValueError("Unexpected coding environment.")
    return {
        "network": "none",
        "readonly_source": True,
        "unprivileged": True,
        "memory_bytes": host["Memory"],
        "pids": host["PidsLimit"],
        "cpus": limits["cpus"],
        "environment_names": sorted(environment),
        "mounts": [
            {"target": m["Destination"], "readonly": not m["RW"]} for m in bindings
        ],
        "scratch_bytes": limits["scratch_bytes"],
        "scratch_inodes": limits["scratch_inodes"],
    }


class DockerSandbox:
    """Trusted fixed-check adapter; no repository command or environment input."""

    def __init__(
        self,
        root,
        image,
        folder,
        limits,
        daemon_root=None,
        process_event=None,
        output_usage=None,
    ):
        import re

        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Coding checks require an immutable image identity.")
        self.root, self.image, self.folder = Path(root), image, Path(folder)
        self.limits = dict(limits)
        self.daemon_root = Path(daemon_root) if daemon_root else self.root
        self.process_event = process_event
        self.total_output = 0
        self.output_usage = (
            output_usage if output_usage is not None else {"native": 0, "checks": 0}
        )
        self.executions = 0
        self.processes = []
        self.failed = False
        self.failure = None

    def _fail(self, reason):
        self.failed = True
        self.failure = self.failure or reason

    def _run(self, snapshot, payload, deadline, remaining_output, *, developer=False):
        name = "emuru-coder-check-" + uuid.uuid4().hex
        daemon_snapshot = snapshot
        if self.daemon_root != self.root:
            daemon_snapshot = self.daemon_root / snapshot.relative_to(self.root)
        config = sandbox_configuration(
            self.root,
            self.image,
            daemon_snapshot,
            os.getuid(),
            os.getgid(),
            self.limits,
        )
        service = config["services"]["check"]
        service["entrypoint"][-1] = str(
            max(1, min(self.limits["check_seconds"], int(deadline - time.monotonic())))
        )
        if developer:
            service["command"] = [
                "/opt/emuru/.venv/bin/python",
                "-I",
                "-B",
                "-c",
                (
                    "import sys; sys.path.insert(0,'/opt/emuru/src'); "
                    "from emuru.projects.coder_sandbox import developer_main; developer_main()"
                ),
            ]
        path = self.folder / (name + ".json")
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        command = [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            name,
            "--file",
            str(path),
            "run",
            "--no-deps",
            "-T",
            "--name",
            name,
            "check",
        ]
        observed = {"container": name, "command": service["command"]}
        self.processes.append(observed)
        if self.process_event:
            self.process_event(name, "starting")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Coding check deadline exhausted.")
            result = bounded_process(command, payload, remaining, remaining_output)
            inspected = bounded_process(["docker", "inspect", name], b"", 5, 65536)
            if inspected["exit_code"]:
                raise ValueError("Coding container inspection unavailable.")
            actual = json.loads(inspected["stdout"])[0]
            observed["confinement"] = inspect_confinement(
                actual, daemon_snapshot, os.getuid(), os.getgid(), self.limits
            )
            if (
                actual["State"]["Running"]
                or actual["State"]["ExitCode"] != result["exit_code"]
            ):
                raise ValueError(
                    "Coding container exit is not independently confirmed."
                )
            observed["oom_killed"] = actual["State"]["OOMKilled"]
            observed.update(result)
            if observed["oom_killed"] or result["exit_code"] in {124, 137}:
                self._fail("oom" if observed["oom_killed"] else "timeout")
            return observed
        except BaseException as error:
            reason = (
                "timeout" if isinstance(error, TimeoutError) else "execution_failed"
            )
            if (
                isinstance(error, ValueError)
                and str(error) == "Coding process output limit exceeded."
            ):
                reason = "output_limit"
            self._fail(reason)
            raise
        finally:
            # A Docker client exit does not terminate its container.
            try:
                removed = bounded_process(
                    ["docker", "rm", "--force", name], b"", 10, 4096
                )
                absent = bounded_process(
                    ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"], b"", 5, 4096
                )
                if absent["exit_code"] or absent["stdout"].strip():
                    raise RuntimeError("Coding container termination is uncertain.")
            except BaseException:
                self._fail("termination_uncertain")
                raise
            observed["stopped"] = True
            observed["removal_exit_code"] = removed["exit_code"]
            if self.process_event:
                self.process_event(name, "stopped")
            try:
                path.unlink()
            except OSError:
                self._fail("execution_failed")
                raise

    def check(self, snapshot, check_id, *, remaining_seconds=None):
        if check_id != "python_unittest":
            raise ValueError("Unknown coding check identifier.")
        if self.failed:
            raise ValueError("Coding attempt already failed closed.")
        if self.executions >= self.limits["check_executions"]:
            raise ValueError("Coding check budget exhausted.")
        self.executions += 1
        seconds = self.limits["check_seconds"]
        if remaining_seconds is not None:
            seconds = min(seconds, remaining_seconds)
        deadline = time.monotonic() + seconds
        allowance = min(
            self.limits["check_output_bytes"],
            self.limits["total_output_bytes"]
            - self.total_output
            - self.output_usage["native"],
        )
        execution = self._run(Path(snapshot), b"", deadline, allowance)
        used = len(execution["stdout"].encode()) + len(execution["stderr"].encode())
        self.total_output += used
        self.output_usage["checks"] = self.total_output
        try:
            result = json.loads(execution["stdout"])
            if (
                not isinstance(result, dict)
                or set(result) != {"success", "case_count", "cases"}
                or type(result["success"]) is not bool
                or type(result["case_count"]) is not int
                or not isinstance(result["cases"], list)
            ):
                raise ValueError
        except (ValueError, TypeError):
            self._fail("authoritative_evidence_missing")
            result = {"success": False, "case_count": 0, "cases": []}
        result["host_observation"] = {
            "stopped": execution["stopped"],
            "confinement": execution["confinement"],
        }
        result["behavioral_cases_passed"] = result["success"]
        if result["success"] and list(Path(snapshot).rglob("test_*.py")):
            allowance = min(
                self.limits["check_output_bytes"] - used,
                self.limits["total_output_bytes"]
                - self.total_output
                - self.output_usage["native"],
            )
            developer = self._run(
                Path(snapshot), b"", deadline, allowance, developer=True
            )
            self.total_output += len(developer["stdout"].encode()) + len(
                developer["stderr"].encode()
            )
            self.output_usage["checks"] = self.total_output
            result["developer_tests"] = developer
            result["developer_tests"]["completion_verified"] = False
            result["success"] = result["success"] and developer["exit_code"] == 0
        if not result["success"]:
            self._fail("tests_failed")
        return {
            "check_id": check_id,
            **result,
            "failure": self.failure,
            "developer_test_count": None,
        }
