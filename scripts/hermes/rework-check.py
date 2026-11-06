"""Disposable Ticket 2 rework and signal-boundary qualification; no real vault."""

import argparse
import contextlib
import hashlib
import json
import multiprocessing
import os
import runpy
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

HIDDEN = "def sum_prices(prices):\n    if len(prices) > 3:\n        return 0\n    return sum(prices)\n"
CORRECT = "def sum_prices(prices):\n    return sum(prices)\n"
FINDING = {
    "path": "sum_prices.py",
    "line": 3,
    "reason": "Four inputs return zero.",
    "requested_change": "Sum every input.",
}
WINDOWS = (
    "intent_before",
    "intent_after",
    "reservation_before",
    "reservation_after",
    "coder_claim_before",
    "coder_claim_after",
    "claim_before",
    "claim_after",
    "rework_link_before",
    "rework_link_after",
    "editing",
    "checking",
    "handoff_before",
    "handoff_after",
    "review_claim_before",
    "review_claim_after",
    "review_process_before",
    "review_process_after",
    "review_inference",
    "verdict_before",
    "verdict_after",
    "receipt_before",
    "receipt_after",
    "transition_before",
    "transition_after",
)


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def snapshot(folder):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.rglob("*")
        if p.is_file()
    }


def new_grant(fixture):
    """Construct only disposable owner authority; the launcher revalidates every fact."""
    from emuru.projects import coder_board, reviewer
    from emuru.projects.scout import digest

    initial = fixture["grant"]
    folder = coder_board.attempt_path(fixture["home"], initial)
    receipt = reviewer._read(folder / "review/receipt.json", reviewer.OUTPUT_BYTES)
    report = json.loads((folder / "evidence.json").read_bytes())
    return {
        **initial,
        "schema": 3,
        "grant_id": secrets.token_hex(16),
        "rework": {
            "schema": 1,
            "loop": initial["grant_id"],
            "grant_sha256": digest(initial),
            "evidence_sha256": receipt["evidence_sha256"],
            "receipt_sha256": digest(receipt),
            "findings_sha256": digest(receipt["verdict"]["blockers"]),
            "final_sha256": digest(report["final_manifest"]),
            "coder_run": receipt["coder_run"],
            "reviewer_run": receipt["run"],
        },
    }


def child(
    fixture,
    image,
    *,
    second=False,
    live=False,
    reject=False,
    window=None,
    connection=None,
):
    from emuru.projects import board, coder_board

    launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
    grant = fixture["second"] if second else fixture["grant"]
    policy = fixture["root"] / ("rework.json" if second else "initial.json")
    policy.write_text(json.dumps(grant))
    policy.chmod(0o600)

    def pause(label):
        if label == window:
            connection.send(label)
            while True:
                signal.pause()

    def observed_container(name, process_event, *, survive=False):
        pause("review_process_before")
        process_event(name, "starting")
        container = docker(
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "/bin/sleep",
            image,
            "120" if survive else "0",
        )
        docker("start", name)
        pause("review_process_after")
        if survive:
            pause("review_inference")
        docker("wait", name)
        docker("rm", name)
        process_event(name, "stopped")
        return container

    def coder(
        _image, _network, _key, _request, dispatch, *, name, process_event, **kwargs
    ):
        process_event(name, "starting")
        container = docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--entrypoint",
            "/bin/sleep",
            image,
            "120",
        )
        try:
            before = dispatch("coder_read", {"path": "sum_prices.py"})
            dispatch(
                "coder_write",
                {
                    "path": "sum_prices.py",
                    "content": ("import time\ntime.sleep(120)\n" + CORRECT)
                    if window == "checking"
                    else (
                        CORRECT
                        if second and not reject
                        else HIDDEN + ("# Second rejected attempt.\n" if second else "")
                    ),
                    "expected_sha256": before["sha256"],
                },
            )
            pause("editing")
            dispatch("coder_check", {"check_id": "python_unittest"})
        finally:
            docker("rm", "-f", container)
            process_event(name, "stopped")
        return {"success": True, "fixture_only": True, "model_calls": 0}

    def review(
        _image, _network, _key, request, dispatch, *, name, process_event, **kwargs
    ):
        observed = observed_container(
            name, process_event, survive=window == "review_inference"
        )
        value = {
            "verdict": "approve" if second and not reject else "changes_requested",
            "summary": "All inputs summed."
            if second and not reject
            else "Four-input defect.",
            "blockers": [] if second and not reject else [FINDING],
            "suggestions": [],
        }
        raw = json.dumps(value)
        final = {
            "type": "final",
            "success": True,
            "tool_calls": 0,
            "verdict": raw,
            "model_calls": 1,
        }
        return {
            "success": True,
            "tool_calls": 0,
            "model_calls": 1,
            "model_led": True,
            "verdict": raw,
            "exit_code": 0,
            "container": name,
            "output_bytes": len(json.dumps(final)),
            "native_lifecycle": {
                "image": image,
                "removed": True,
                "tool_calls": 0,
                "exit_code": 0,
                "container_id": observed,
                "final_sha256": hashlib.sha256(
                    json.dumps(final, sort_keys=True).encode()
                ).hexdigest(),
            },
        }

    @contextlib.contextmanager
    def gateway(folder, *, process_event, **kwargs):
        name = "emuru-coder-gateway-" + secrets.token_hex(8)
        process_event(name, "starting")
        docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--entrypoint",
            "/bin/true",
            image,
        )
        docker("rm", name)
        process_event(name, "stopped")
        yield "none", "synthetic", None

    kb = board.native_api(ROOT)
    write = coder_board._write_new

    def write_boundary(folder, name, data):
        labels = {
            "intent.json": "review_intent" if folder.name == "review" else "intent",
            "rework-intent.json": "rework_link",
            "loop.json": "reservation",
            "run.json": "review_claim" if folder.name == "review" else "claim",
            "outcome.json": "verdict",
            "receipt.json": "receipt",
        }
        label = labels.get(name)
        if label:
            pause(label + "_before")
        result = write(folder, name, data)
        if label:
            pause(label + "_after")
        return result

    def transition(function, label):
        def wrapped(*args, **kwargs):
            pause(label + "_before")
            result = function(*args, **kwargs)
            pause(label + "_after")
            return result

        return wrapped

    from emuru.projects.coder_sandbox import DockerSandbox

    original_check = DockerSandbox._run

    def checking(self, *args, **kwargs):
        if window == "checking":
            original_event = self.process_event

            def observe(name, phase):
                original_event(name, phase)
                if phase == "starting":

                    def await_running():
                        limit = time.monotonic() + 30
                        while time.monotonic() < limit:
                            observed = subprocess.run(
                                [
                                    "docker",
                                    "inspect",
                                    "--format",
                                    "{{.State.Running}}",
                                    name,
                                ],
                                check=False,
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            if (
                                observed.returncode == 0
                                and observed.stdout.strip() == "true"
                            ):
                                connection.send("checking")
                                return
                            time.sleep(0.02)

                    threading.Thread(target=await_running, daemon=True).start()

            self.process_event = observe
            kwargs["developer"] = True
        return original_check(self, *args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(coder_board, "_write_new", write_boundary))
        for name, label in [
            ("claim_task", "coder_claim"),
            ("request_review", "handoff"),
            ("claim_review_task", "review_claim"),
            ("complete_task", "transition"),
            ("request_changes", "transition"),
            ("block_task", "transition"),
        ]:
            stack.enter_context(
                patch.object(kb, name, transition(getattr(kb, name), label))
            )
        stack.enter_context(patch.object(DockerSandbox, "_run", checking))
        if not live:
            launch.__globals__["cloud_gateway"] = gateway
            stack.enter_context(
                patch("emuru.projects.coder_gateway.cloud_gateway", gateway)
            )
            stack.enter_context(
                patch("emuru.projects.coder_inference.run_inference", review)
            )
        if not second or not live:
            launch.__globals__["run_inference"] = coder
        result = launch(fixture["home"], fixture["vault"], policy, grant["task"], ROOT)
        connection.send({"status": result["status"]})


def invoke(fixture, image, **kwargs):
    ctx = multiprocessing.get_context("fork")
    parent, connection = ctx.Pipe()
    process = ctx.Process(
        target=child,
        args=(fixture, image),
        kwargs={
            **{k: v for k, v in kwargs.items() if k != "signum"},
            "connection": connection,
        },
    )
    process.start()
    try:
        deadline = time.monotonic() + (1000 if kwargs.get("live") else 180)
        while not parent.poll(1):
            assert process.is_alive() and time.monotonic() < deadline, (
                f"Fixture did not reach {kwargs}"
            )
        result = parent.recv()
        if kwargs.get("window"):
            assert result == kwargs["window"], result
            os.kill(process.pid, kwargs["signum"])
        process.join(15)
        assert not process.is_alive()
        assert process.exitcode == (-kwargs["signum"] if kwargs.get("window") else 0), (
            process.exitcode
        )
        return result
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        connection.close()


def run(image, *, live=False, matrix=False):
    from emuru.projects import board, coder_board, reviewer
    from emuru.projects.coder import load_grant
    from emuru.projects.scout import encoded

    source, actual = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))[
        "ensure_image"
    ](build=False)
    assert image == actual
    prepare = runpy.run_path(str(ROOT / "scripts/hermes/coder-task-check.py"))[
        "prepare"
    ]
    records = []

    def fixture():
        value = prepare(image)
        prepare.__globals__["_runtime"] = lambda *_: None
        value["grant"] = {
            **load_grant(value["policy"]),
            "schema": 2,
            "review": reviewer.authority(),
        }
        return value

    def inspect(value, grant):
        with board.project_board_readonly(value["home"], ROOT) as (kb, db):
            card = kb.get_task(db, grant["task"])
            return coder_board.status(kb, db, card, value["home"], ROOT)

    def cleanup(value, grant):
        folder = coder_board.attempt_path(value["home"], grant)
        for event in coder_board._processes(folder):
            if docker("ps", "-aq", "--filter", "name=^/" + event["name"] + "$"):
                docker("rm", "-f", event["name"])
        value["queue"].close()

    for reject in (False, True):
        use_live = live and not reject
        value = fixture()
        repo_before, vault_before = snapshot(value["repo"]), snapshot(value["vault"])
        assert invoke(value, image, live=use_live)["status"] == "changes_requested"
        assert inspect(value, value["grant"])["status"] == "changes_requested"
        launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
        before = snapshot(value["home"])
        assert (
            launch(
                value["home"],
                value["vault"],
                value["root"] / "initial.json",
                value["task_id"],
                ROOT,
            )["status"]
            == "changes_requested"
        )
        assert snapshot(value["home"]) == before
        value["second"] = new_grant(value)
        before = snapshot(value["home"])
        from copy import deepcopy

        for field, replacement in (
            ("owner", 43),
            ("baseline", "0" * 40),
            ("write_files", []),
            ("check_ids", []),
            ("catalogue", "0" * 64),
        ):
            hostile = {**value["second"], field: replacement}
            hostile_policy = value["root"] / "hostile.json"
            hostile_policy.write_bytes(encoded(hostile))
            hostile_policy.chmod(0o600)
            try:
                launch(
                    value["home"],
                    value["vault"],
                    hostile_policy,
                    value["task_id"],
                    ROOT,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Changed rework authority accepted: " + field)
            assert snapshot(value["home"]) == before
        for field in (
            "grant_sha256",
            "evidence_sha256",
            "receipt_sha256",
            "findings_sha256",
            "final_sha256",
            "coder_run",
            "reviewer_run",
        ):
            hostile = deepcopy(value["second"])
            hostile["rework"][field] = 999 if field.endswith("_run") else "0" * 64
            hostile_policy.write_bytes(encoded(hostile))
            try:
                launch(
                    value["home"],
                    value["vault"],
                    hostile_policy,
                    value["task_id"],
                    ROOT,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Wrong parent accepted: " + field)
            assert snapshot(value["home"]) == before
        result = invoke(value, image, second=True, live=use_live, reject=reject)
        expected = "review_rejected" if reject else "verified"
        assert result["status"] == expected, result
        assert inspect(value, value["second"])["status"] == expected
        assert (
            snapshot(value["repo"]) == repo_before
            and snapshot(value["vault"]) == vault_before
        )
        with board.project_board_readonly(value["home"], ROOT) as (kb, db):
            assert len(kb.list_runs(db, value["task_id"])) == 4
        before = snapshot(value["home"])
        repeat = launch(
            value["home"],
            value["vault"],
            value["root"] / "rework.json",
            value["task_id"],
            ROOT,
        )
        assert repeat["status"] == expected
        assert snapshot(value["home"]) == before
        third = {**value["second"], "grant_id": secrets.token_hex(16)}
        hostile_policy.write_bytes(encoded(third))
        try:
            launch(
                value["home"], value["vault"], hostile_policy, value["task_id"], ROOT
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Third attempt authorized")
        assert snapshot(value["home"]) == before
        records.append(
            {"fixture": str(value["root"]), "status": expected, "live": use_live}
        )
        cleanup(value, value["second"])
        print(json.dumps(records[-1]), flush=True)

    if matrix:
        for second in (False, True):
            for window in WINDOWS:
                if not second and window.startswith("rework_link"):
                    continue
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    value = fixture()
                    grant = value["grant"]
                    try:
                        if second:
                            assert invoke(value, image)["status"] == "changes_requested"
                            value["second"] = new_grant(value)
                            grant = value["second"]
                        invoke(
                            value, image, second=second, window=window, signum=signum
                        )
                        before, repo_before, vault_before = (
                            snapshot(value["home"]),
                            snapshot(value["repo"]),
                            snapshot(value["vault"]),
                        )
                        policy = value["root"] / (
                            "rework.json" if second else "initial.json"
                        )
                        with board.project_board_readonly(value["home"], ROOT) as (
                            kb,
                            db,
                        ):
                            counts = (
                                len(kb.list_runs(db, grant["task"])),
                                len(kb.list_attachments(db, grant["task"])),
                            )
                        result = launch(
                            value["home"], value["vault"], policy, grant["task"], ROOT
                        )
                        expected_statuses = {"uncertain"}
                        if window in {"review_process_after", "review_inference"}:
                            expected_statuses.add("awaiting_review")
                        if window == "transition_after":
                            expected_statuses = {
                                "verified" if second else "changes_requested"
                            }
                        assert result["status"] in expected_statuses, result
                        assert snapshot(value["home"]) == before
                        assert (
                            snapshot(value["repo"]) == repo_before
                            and snapshot(value["vault"]) == vault_before
                        )
                        if result["status"] != "verified":
                            try:
                                coder_board.ensure_idle(
                                    value["home"], ROOT, "t_competitor"
                                )
                            except (OSError, ValueError):
                                pass
                            else:
                                raise AssertionError(
                                    "Unresolved intent released exclusion"
                                )
                        owner_status = value["project_command"](
                            "/project status " + value["proposal"]
                        )
                        assert "coder " + result["status"] in owner_status, owner_status
                        folder = coder_board.attempt_path(value["home"], grant)
                        events = coder_board._processes(folder)
                        if events:
                            # Exercise operator cleanup, including any surviving child.
                            coder_board.cleanup(
                                value["home"], ROOT, grant["task"], grant
                            )
                            assert not any(
                                docker(
                                    "ps", "-aq", "--filter", "name=^/" + e["name"] + "$"
                                )
                                for e in events
                            )
                        recovery = "uncertain"
                        try:
                            reconciled = coder_board.reconcile(
                                value["home"],
                                value["vault"],
                                ROOT,
                                grant["task"],
                                grant,
                            )
                            recovery = reconciled["status"]
                        except (ValueError, FileNotFoundError):
                            pass
                        if window in {
                            "receipt_before",
                            "receipt_after",
                            "transition_before",
                            "transition_after",
                        }:
                            assert recovery == (
                                "verified" if second else "changes_requested"
                            ), recovery
                        else:
                            assert recovery == "uncertain", recovery
                        with board.project_board_readonly(value["home"], ROOT) as (
                            kb,
                            db,
                        ):
                            assert counts == (
                                len(kb.list_runs(db, grant["task"])),
                                len(kb.list_attachments(db, grant["task"])),
                            )
                        records.append(
                            {
                                "fixture": str(value["root"]),
                                "second": second,
                                "window": window,
                                "signal": signum.name,
                                "status": result["status"],
                                "readonly": True,
                                "recovery": recovery,
                                "native_counts": counts,
                            }
                        )
                        print(json.dumps(records[-1]), flush=True)
                    finally:
                        cleanup(value, grant)
    output = ROOT / ".runtime" / ("v055-ticket2-" + str(time.time_ns()) + ".json")
    output.write_bytes(
        encoded(
            {
                "source": source,
                "image": image,
                "records": records,
                "deterministic_verdicts": not live,
            }
        )
    )
    print(str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()
    run(args.image, live=args.live, matrix=args.matrix)
