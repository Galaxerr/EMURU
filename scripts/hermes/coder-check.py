"""Disposable Ticket 1 qualification; never dispatches a native board task."""

import argparse
import json
import os
import runpy
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from emuru.models.gateway import write_private
from emuru.projects.coder_gateway import cloud_gateway
from emuru.projects.coder_inference import run_inference
from emuru.projects.coder_runtime import Qualification, qualification_fixture
from emuru.projects.coder_sandbox import DockerSandbox


def completion_regression(image, folder, daemon_root):
    """Native early exit stays supplemental while Monty authorizes callable cases."""
    folder.mkdir(mode=0o700)
    _, grant = qualification_fixture(folder / "fixture")
    attempt = Qualification(folder / "attempt", grant, image)
    source = attempt.workspace.read("sum_prices.py")
    attempt.workspace.write(
        "sum_prices.py",
        "import os, sys\ndef sum_prices(prices):\n"
        "    if sys.platform == 'monty':\n        return sum(prices)\n"
        "    print('Ran 5 tests\\nOK', flush=True)\n    os._exit(0)\n",
        source["sha256"],
    )
    snapshot = attempt.folder / "probe-source"
    attempt.workspace.snapshot(snapshot)
    sandbox = DockerSandbox(ROOT, image, folder, grant["limits"], daemon_root)
    result = sandbox.check(snapshot, "python_unittest")
    observations = {
        "expected_acceptance": True,
        "result": result,
        "all_stopped": all(p.get("stopped") is True for p in sandbox.processes),
    }
    write_private(
        folder / "observations.json", json.dumps(observations, sort_keys=True)
    )
    assert result["success"], "Host-controlled callable acceptance failed: " + str(
        folder
    )
    assert result["behavioral_cases_passed"] and result["case_count"] == 5
    assert result["developer_tests"]["exit_code"] == 0
    assert result["developer_tests"]["completion_verified"] is False
    assert all(case["host_call_returned"] for case in result["cases"])
    assert observations["all_stopped"]
    return {
        "monty_callable_pass": True,
        "native_completion_unverified": True,
        "native_exit_zero_not_authority": True,
        "evidence": str(folder),
    }


def sandbox_self_check(image, folder, daemon_root):
    print(
        json.dumps(
            completion_regression(image, folder / "completion-regression", daemon_root)
        ),
        flush=True,
    )
    repo, grant = qualification_fixture(folder / "fixture")
    original = (repo / "sum_prices.py").read_bytes()
    attempt = Qualification(folder / "attempt", grant, image)
    sandbox = DockerSandbox(ROOT, image, folder, grant["limits"], daemon_root)

    def fixture_edit(dispatch):
        source = dispatch("coder_read", {"path": "sum_prices.py"})
        dispatch(
            "coder_write",
            {
                "path": "sum_prices.py",
                "content": "def sum_prices(prices):\n    return sum(prices)\n",
                "expected_sha256": source["sha256"],
            },
        )
        return {"success": True, "model_led": False, "mode": "controller_fixture"}

    result = attempt.run(fixture_edit, sandbox)
    assert result["success"] and (repo / "sum_prices.py").read_bytes() == original
    return {
        "sandbox": "PASS",
        "model_led": False,
        "evidence": str(attempt.folder),
        "sha256": attempt.evidence_sha256,
        "containers": len(sandbox.processes),
    }


def isolation_probes(image, folder, daemon_root):
    """Run hostile fixture source through the actual fixed offline check path."""
    folder.mkdir(mode=0o700)
    sentinel = folder / "host-private-canary"
    sentinel.write_text(secrets.token_hex(32))
    sentinel.chmod(0o600)
    boundary = f"""
import errno, os, socket
def sum_prices(prices):
    assert os.getuid() != 0
    assert not any("KEY" in name or "TOKEN" in name or "SECRET" in name for name in os.environ if name != "GPG_KEY")
    for name in ({str(sentinel)!r}, '/var/run/docker.sock', '/run/secrets', '/vault', '/board'):
        assert not os.path.exists(name)
    for name in ('/repo/sum_prices.py', '/etc/coder-probe', '/dev/shm/coder-probe'):
        try:
            with open(name, 'w') as stream: stream.write('escape')
        except OSError: pass
        else: raise AssertionError('unexpected write')
    try: os.execve('/bin/sh', ['sh', '-c', 'exit 0'], {{}})
    except PermissionError: pass
    else: raise AssertionError('unexpected exec')
    for address in ('1.1.1.1', 'litellm'):
        try:
            with socket.create_connection((address, 4000), timeout=0.2): pass
        except OSError: pass
        else: raise AssertionError('unexpected egress')
    return sum(prices)
"""
    scratch_bytes = """
import errno
def sum_prices(prices):
    try:
        with open('/tmp/large', 'wb') as stream: stream.write(b'x' * 2097152)
    except OSError as error: assert error.errno == errno.ENOSPC
    else: raise AssertionError('scratch byte limit missing')
    return sum(prices)
"""
    scratch_inodes = """
import errno
def sum_prices(prices):
    try:
        for index in range(100): open('/tmp/file' + str(index), 'w').close()
    except OSError as error: assert error.errno == errno.ENOSPC
    else: raise AssertionError('scratch inode limit missing')
    return sum(prices)
"""
    processes = """
import errno, os, signal, time
def sum_prices(prices):
    children = []
    try:
        for _ in range(32):
            pid = os.fork()
            if pid == 0:
                time.sleep(10)
                os._exit(0)
            children.append(pid)
    except OSError as error: assert error.errno == errno.EAGAIN
    else: raise AssertionError('process limit missing')
    finally:
        for pid in children:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    return sum(prices)
"""
    scenarios = (
        ("boundary", boundary, {}, True),
        ("scratch_bytes", scratch_bytes, {"scratch_bytes": 1048576}, True),
        ("scratch_inodes", scratch_inodes, {"scratch_inodes": 16}, True),
        ("processes", processes, {"pids": 8}, True),
        ("wrong_result", "def sum_prices(prices): return 99\n", {}, False),
        ("early_exit", "import os\nos._exit(0)\n", {}, False),
        (
            "forged_counts",
            "print('Ran 5 tests\\nOK')\nraise SystemExit(0)\n",
            {},
            False,
        ),
        (
            "memory",
            "data = bytearray(256 * 1024 * 1024)\n",
            {"memory_bytes": 67108864},
            False,
        ),
        ("timeout", "import time\ntime.sleep(20)\n", {"check_seconds": 8}, False),
        ("output", "print('x' * 100000)\n", {"check_output_bytes": 1024}, False),
    )
    records = []
    for label, content, overrides, expected in scenarios:
        probe = folder / label
        probe.mkdir(mode=0o700)
        repo, grant = qualification_fixture(probe / "fixture")
        original = (repo / "sum_prices.py").read_bytes()
        grant["limits"].update(overrides)
        attempt = Qualification(probe / "attempt", grant, image)
        source = attempt.workspace.read("sum_prices.py")
        attempt.workspace.write("sum_prices.py", content, source["sha256"])
        snapshot = attempt.folder / "probe-source"
        attempt.workspace.snapshot(snapshot)
        sandbox = DockerSandbox(ROOT, image, probe, grant["limits"], daemon_root)
        try:
            result = sandbox.check(snapshot, "python_unittest")
            observed = result["success"] and not sandbox.failed
        except (ValueError, TimeoutError):
            result, observed = None, False
        assert observed is expected, label
        assert sandbox.processes and all(
            p.get("stopped") is True for p in sandbox.processes
        ), label
        assert (repo / "sum_prices.py").read_bytes() == original, label
        if label == "memory":
            assert any(p.get("oom_killed") for p in sandbox.processes)
        if label in {"timeout", "output"}:
            assert sandbox.failed and len(sandbox.processes) == 1
        record = {
            "probe": label,
            "expected_acceptance": expected,
            "passed": True,
            "result": result,
            "processes": sandbox.processes,
        }
        write_private(probe / "observations.json", json.dumps(record, sort_keys=True))
        records.append(
            {"probe": label, "passed": True, "containers": len(sandbox.processes)}
        )
        print(json.dumps(records[-1]), flush=True)
    return {"isolation_probes": records, "evidence": str(folder)}


def live_check(image, folder, daemon_root):
    repo, grant = qualification_fixture(folder / "fixture")
    original = (repo / "sum_prices.py").read_bytes()
    with cloud_gateway(folder / "gateway", daemon_root) as (network, key, primary):
        attempt = Qualification(folder / "attempt", grant, image)
        sandbox = DockerSandbox(ROOT, image, folder, grant["limits"], daemon_root)
        request = {
            "prompt": "Implement sum_prices(prices) in sum_prices.py to return the integer sum, including empty and negative lists. Authorized readable files: sum_prices.py, test_sum_prices.py. Only sum_prices.py is writable; no new files. Read both files with coder_read, replace the implementation with coder_write using the returned digest, and run python_unittest through coder_check. If the check fails or reports completion_unverified, end your turn reporting that blocked outcome without any more tool calls. Do not retry, use other tools or edit tests.",
            "model": "emuru",
            "max_iterations": grant["limits"]["model_iterations"],
            "deadline_seconds": grant["limits"]["attempt_seconds"],
        }
        result = attempt.run(
            lambda dispatch: run_inference(image, network, key, request, dispatch),
            sandbox,
        )
        assert result["success"] and result["native"]["model_led"]
        assert (repo / "sum_prices.py").read_bytes() == original
        return {
            "model_led": "PASS",
            "primary": primary,
            "evidence": str(attempt.folder),
            "sha256": attempt.evidence_sha256,
            "tool_calls": result["native"]["tool_calls"],
            "check_executions": sandbox.executions,
            "containers": len(sandbox.processes),
        }


def native_self_check(image):
    expression = (
        "import sys; sys.path.insert(0,'/opt/emuru/src'); "
        "from emuru.projects.coder_native import run_native; "
        "print(run_native({'prompt':'__emuru_native_self_check__',"
        "'model':'emuru','max_iterations':12,'deadline_seconds':60},"
        "lambda *args:'{}'))"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--pids-limit",
            "64",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m,nr_inodes=1024,mode=1777",
            "--env",
            "EMURU_GATEWAY_KEY=synthetic-native-probe",
            "--env",
            "HOME=/tmp",
            "--env",
            "HERMES_DISABLE_LAZY_INSTALLS=1",
            "--entrypoint",
            "/usr/bin/timeout",
            image,
            "--signal=KILL",
            "60",
            "/opt/hermes/.venv/bin/python",
            "-I",
            "-B",
            "-c",
            expression,
        ],
        check=True,
        timeout=70,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Immutable disposable qualification image")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--native-self-check", action="store_true")
    parser.add_argument("--sandbox-self-check", action="store_true")
    parser.add_argument("--isolation-probes", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the selected cloud route on disposable source only",
    )
    args = parser.parse_args()
    os.umask(0o077)
    if not any(
        (
            args.native_self_check,
            args.sandbox_self_check,
            args.isolation_probes,
            args.live,
        )
    ):
        parser.error("Select a disposable qualification mode.")
    image = args.image
    if not image:
        _, image = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))[
            "ensure_image"
        ](build=args.build)
    if args.native_self_check:
        native_self_check(image)
        print(json.dumps({"native_self_check": "PASS", "image": image}))
    if args.sandbox_self_check or args.isolation_probes or args.live:
        daemon_root = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))[
            "docker_workspace"
        ](ROOT)
        folder = Path(
            tempfile.mkdtemp(prefix="coder-qualification-", dir=ROOT / ".runtime")
        )
        if args.sandbox_self_check:
            print(
                json.dumps(sandbox_self_check(image, folder, daemon_root)), flush=True
            )
        if args.isolation_probes:
            print(
                json.dumps(isolation_probes(image, folder / "probes", daemon_root)),
                flush=True,
            )
        if args.live:
            if args.sandbox_self_check:
                folder = Path(
                    tempfile.mkdtemp(
                        prefix="coder-live-qualification-", dir=ROOT / ".runtime"
                    )
                )
            print(json.dumps(live_check(image, folder, daemon_root)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
