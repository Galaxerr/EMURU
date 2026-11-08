"""Disposable Ticket 3 crash matrix: native records, real signals and Docker workers."""

import argparse
import hashlib
import json
import multiprocessing
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
WINDOWS = (
    "intent",
    "claim",
    "gateway",
    "coding",
    "check",
    "evidence",
    "attachment",
    "done",
)


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def paused(connection, window):
    connection.send(window)
    while True:
        signal.pause()


def worker(fixture, image, window, connection):
    from emuru.projects import board, coder_board
    from emuru.projects.coder_runtime import Qualification
    from emuru.projects.coder_sandbox import DockerSandbox

    home, vault, grant = fixture["home"], fixture["vault"], fixture["grant"]
    task = grant["task"]
    folder = coder_board.create_attempt(home, grant)
    coder_board.record_launcher(folder)
    if window == "intent":
        paused(connection, window)
    native_board = board.native_api(ROOT)
    native_board.time = SimpleNamespace(time=lambda: time.time() - 1000)
    # Reproduce a committed native claim whose run-binding write was interrupted.
    if window == "claim":
        write = coder_board._write_new

        def before_binding(folder, name, data):
            if name == "run.json":
                paused(connection, window)
            return write(folder, name, data)

        coder_board._write_new = before_binding
    run = coder_board.claim(home, vault, ROOT, task, grant, folder)
    attempt = Qualification(folder, grant, image, task_run=run)
    if window == "gateway":
        gateway = (
            ROOT / ".runtime" / ("emuru-coder-gateway-" + grant["grant_id"]) / "gateway"
        )
        gateway.mkdir(mode=0o700, parents=True)
        gateway.parent.chmod(0o700)
        for filename in ("provider.key", "gateway.key"):
            (gateway / filename).write_text("synthetic-key")
            (gateway / filename).chmod(0o600)
        coder_board._write_new(
            folder, "gateway.json", json.dumps({"folder": str(gateway)}).encode()
        )
        suffix = grant["grant_id"][:16]
        name = "emuru-coder-gateway-" + suffix
        coder_board.process_event(folder, name, "gateway", "starting")
        docker("network", "create", "--internal", "emuru-coder-model-" + suffix)
        docker("network", "create", "emuru-coder-egress-" + suffix)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "emuru-coder-model-" + suffix,
            "--entrypoint",
            "/usr/bin/timeout",
            image,
            "30",
            "/bin/sleep",
            "25",
        )
        paused(connection, window)
    if window in {"coding", "check"}:

        def observe(name, phase):
            kind = "inference" if window == "coding" else "check"
            coder_board.process_event(folder, name, kind, phase)
            if phase != "starting":
                return

            def await_running():
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    observed = subprocess.run(
                        ["docker", "inspect", "--format", "{{.State.Running}}", name],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if observed.returncode == 0 and observed.stdout.strip() == "true":
                        connection.send(window)
                        return
                    time.sleep(0.02)

            threading.Thread(target=await_running, daemon=True).start()

        if window == "coding":
            from emuru.projects.coder_inference import run_inference

            run_inference(
                image,
                "none",
                "synthetic-no-provider-access",
                {
                    "prompt": "Implement the authorized sum_prices task.",
                    "deadline_seconds": 30,
                    "max_iterations": 1,
                },
                lambda *_: None,
                process_event=observe,
            )
        else:
            read = attempt.workspace.read("sum_prices.py")
            attempt.workspace.write(
                "sum_prices.py", "import time\ntime.sleep(25)\n", read["sha256"]
            )
            check_source = folder / "interrupted-check-source"
            attempt.workspace.snapshot(check_source)
            sandbox = DockerSandbox(
                ROOT, image, folder, grant["limits"], process_event=observe
            )
            sandbox._run(
                check_source, b"", time.monotonic() + 40, 65536, developer=True
            )
        raise AssertionError("Worker completed before injected interruption")

    # Deterministic editing backend; provider compatibility is checked separately.
    def native(dispatch):
        read = dispatch("coder_read", {"path": "sum_prices.py"})
        dispatch(
            "coder_write",
            {
                "path": "sum_prices.py",
                "content": "def sum_prices(prices):\n    return sum(prices)\n",
                "expected_sha256": read["sha256"],
            },
        )
        return {"success": True}

    # Record actual disposable inference/gateway stand-ins, never fake process observations.
    for kind in ("gateway", "inference"):
        name = (
            "emuru-coder-"
            + kind
            + "-"
            + (grant["grant_id"][:16] if kind == "gateway" else grant["grant_id"])
        )
        coder_board.process_event(folder, name, kind, "starting")
        docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "/bin/true",
            image,
        )
        docker("rm", name)
        coder_board.process_event(folder, name, kind, "stopped")
    sandbox = DockerSandbox(
        ROOT,
        image,
        folder,
        grant["limits"],
        process_event=lambda name, phase: coder_board.process_event(
            folder, name, "check", phase
        ),
    )
    report = attempt.run(native, sandbox)
    assert report["success"]
    if window == "evidence":
        paused(connection, window)
    if window == "attachment":
        with board.project_board(home, ROOT) as (kb, db):
            coder_board._attach_artifacts(kb, db, task, grant, folder, run["run"])
        paused(connection, window)
    coder_board.finish(home, vault, ROOT, task, grant, folder, run)
    paused(connection, window)


def snapshot(folder):
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in folder.rglob("*")
        if path.is_file()
    }


def run(image):
    from emuru.projects import board, coder_board
    from emuru.projects.dispatcher import dispatcher_lock

    fixture_module = runpy.run_path(str(ROOT / "scripts/hermes/coder-task-check.py"))
    prepare = fixture_module["prepare"]
    results = []
    output = ROOT / ".runtime" / ("coder-lifecycle-" + str(time.time_ns()))
    output.mkdir(mode=0o700)
    context = multiprocessing.get_context("fork")
    for window in WINDOWS:
        for signum in (signal.SIGTERM, signal.SIGKILL):
            fixture = prepare(image)
            # Runtime imports remain loaded; avoid extracting the same pinned tree again.
            prepare.__globals__["_runtime"] = lambda *_: None
            home, vault, grant = fixture["home"], fixture["vault"], fixture["grant"]
            with dispatcher_lock(home):
                pass
            parent, child = context.Pipe()
            process = context.Process(
                target=worker, args=(fixture, image, window, child)
            )
            process.start()
            names = []
            try:
                assert parent.poll(120), f"Crash boundary not reached: {window}"
                assert parent.recv() == window
                os.kill(process.pid, signum)
                process.join(10)
                assert process.exitcode == -signum
                folder = coder_board.attempt_path(home, grant)
                events = coder_board._processes(folder)
                names = list({event["name"] for event in events})
                from emuru.projects import scout_board

                with board.project_board(home, ROOT) as (kb, db):
                    original_card = kb.get_task(db, grant["task"])
                    competitor = kb.create_task(
                        db,
                        title="Competing scout",
                        body=original_card.body,
                        tenant=original_card.tenant,
                        created_by="emuru-owner",
                        initial_status="blocked",
                        project_id="",
                        workspace_kind="scratch",
                    )
                    if window != "intent":
                        assert window == "done" or original_card.claim_expires < int(
                            time.time()
                        )
                if window != "done":
                    try:
                        scout_board.claim(home, ROOT, {"source_task": competitor})
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Scout bypassed unresolved coder intent")
                    try:
                        coder_board.ensure_idle(home, ROOT, competitor)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Coder bypassed unresolved intent")
                before = snapshot(home)
                source_before = snapshot(fixture["repo"])
                prior = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"](
                    home, vault, fixture["policy"], grant["task"], ROOT
                )
                assert snapshot(home) == before, "Restart inspection wrote state"
                assert snapshot(fixture["repo"]) == source_before
                expected = (
                    "tested local result / awaiting owner review"
                    if window == "done"
                    else (
                        "running" if window in {"gateway", "coding", "check"} else None
                    )
                )
                if expected:
                    assert prior["status"] == expected, prior
                else:
                    assert prior["status"] in {
                        "uncertain",
                        "awaiting reconciliation",
                    }, prior
                with board.project_board_readonly(home, ROOT) as (kb, db):
                    card = kb.get_task(db, grant["task"])
                    run_count = len(kb.list_runs(db, card.id))
                    attachments = len(kb.list_attachments(db, card.id))
                if window in {"coding", "check"}:
                    assert (
                        docker("inspect", "--format", "{{.State.Running}}", names[0])
                        == "true"
                    )
                    try:
                        with dispatcher_lock(home):
                            coder_board.reconcile(
                                home, vault, ROOT, grant["task"], grant
                            )
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Reconciled a live worker")
                if window == "gateway":
                    with dispatcher_lock(home):
                        coder_board.cleanup(home, ROOT, grant["task"], grant)
                    suffix = grant["grant_id"][:16]
                    for prefix in ("emuru-coder-model-", "emuru-coder-egress-"):
                        assert not docker(
                            "network",
                            "ls",
                            "-q",
                            "--filter",
                            "name=^" + prefix + suffix + "$",
                        )
                    with board.project_board_readonly(home, ROOT) as (kb, db):
                        assert len(kb.list_runs(db, grant["task"])) == run_count
                        assert kb.get_task(db, grant["task"]).status == "running"
                    assert snapshot(fixture["repo"]) == source_before
                if window in {"evidence", "attachment"}:
                    with dispatcher_lock(home):
                        receipt = coder_board.reconcile(
                            home, vault, ROOT, grant["task"], grant
                        )
                    assert (
                        receipt["status"]
                        == "tested local result / awaiting owner review"
                    )
                    with board.project_board_readonly(home, ROOT) as (kb, db):
                        assert len(kb.list_runs(db, grant["task"])) == run_count
                        assert len(kb.list_attachments(db, grant["task"])) == 2
                results.append(
                    {
                        "window": window,
                        "signal": signum.name,
                        "status": prior["status"],
                        "root": str(fixture["root"]),
                        "runs": run_count,
                        "attachments_before": attachments,
                        "inspection_readonly": True,
                        "original_source_unchanged": True,
                    }
                )
                print(f"{window} {signum.name}: PASS", flush=True)
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(10)
                folder = coder_board.attempt_path(home, grant)
                if folder.exists():
                    names = list(
                        {event["name"] for event in coder_board._processes(folder)}
                    )
                for name in names:
                    if docker("ps", "-aq", "--filter", "name=^/" + name + "$"):
                        docker("rm", "-f", name)
                    assert not docker("ps", "-aq", "--filter", "name=^/" + name + "$")
                if window == "gateway":
                    for prefix in ("emuru-coder-model-", "emuru-coder-egress-"):
                        network = prefix + grant["grant_id"][:16]
                        if docker(
                            "network", "ls", "-q", "--filter", "name=^" + network + "$"
                        ):
                            docker("network", "rm", network)
                fixture["queue"].close()
                parent.close()
                child.close()
    (output / "matrix.json").write_text(json.dumps(results, indent=2))
    print(f"Ticket 3 crash matrix: PASS ({output})", flush=True)


def run_supervised(image):
    """Matching-image M6 seams, using the same disposable native fixture and workers."""
    import pytest

    from emuru.projects.scout import digest

    source, current = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))[
        "ensure_image"
    ](build=False)
    if image != current:
        raise ValueError(
            "Supervised qualification requires the current verified image."
        )
    output = ROOT / ".runtime" / ("coder-supervisor-" + str(time.time_ns()))
    output.mkdir(mode=0o700)
    prepare = runpy.run_path(str(ROOT / "scripts/hermes/coder-task-check.py"))[
        "prepare"
    ]
    fixture = prepare(image)
    fixture["queue"].close()
    os.environ["EMURU_CODER_TEST_IMAGE"] = image
    os.environ["EMURU_CODER_QUALIFICATION"] = str(output)
    code = pytest.main(["-q", str(ROOT / "tests/test_coder_supervisor_native.py")])
    cases = {path.stem: json.loads(path.read_text()) for path in output.glob("*.json")}
    report = {
        "status": "PASS" if code == 0 and len(cases) == 13 else "FAIL",
        "source": source,
        "image": image,
        "runtime": str(fixture["root"]),
        "cases": cases,
        "cases_sha256": digest(cases),
        "pytest_exit": int(code),
        "limitations": "Synthetic SDK transport and isolated gateway stand-in prove mechanics; no provider/model usefulness claim.",
    }
    (output / "matrix.json").write_text(json.dumps(report, sort_keys=True, indent=2))
    print(f"Supervised coder qualification: {report['status']} ({output})", flush=True)
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--supervised", action="store_true")
    args = parser.parse_args()
    (run_supervised if args.supervised else run)(args.image)
