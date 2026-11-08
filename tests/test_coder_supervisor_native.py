"""Native-board fault seams; also run in matching-image lifecycle qualification."""

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
from contextlib import contextmanager
from pathlib import Path

import pytest

from emuru.projects import board, coder_board, supervisor, supervisor_coder
from emuru.projects.dispatcher import dispatcher_lock

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("EMURU_CODER_TEST_IMAGE", "sha256:" + "a" * 64)


@pytest.fixture
def native_fixture(monkeypatch, request):
    pytest.importorskip("hermes_cli.kanban_db")
    prepare = runpy.run_path(str(ROOT / "scripts/hermes/coder-task-check.py"))[
        "prepare"
    ]
    monkeypatch.setitem(prepare.__globals__, "_runtime", lambda *_: None)
    fixture = prepare(IMAGE)
    mode = getattr(request, "param", 1)
    if mode == "check":
        test = fixture["repo"] / "test_sum_prices.py"
        test.write_text("import time\ntime.sleep(10)\n" + test.read_text())
        for args in (
            ["add", "test_sum_prices.py"],
            [
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "synthetic blocked check",
            ],
        ):
            subprocess.run(
                ["git", "-C", str(fixture["repo"]), *args],
                check=True,
                capture_output=True,
            )
        fixture["grant"]["baseline"] = subprocess.run(
            ["git", "-C", str(fixture["repo"]), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        fixture["policy"].write_text(json.dumps(fixture["grant"]))
    if mode == 2:
        from emuru.projects import reviewer

        fixture["grant"] = {
            **fixture["grant"],
            "schema": 2,
            "review": reviewer.authority(),
        }
        fixture["policy"].write_text(json.dumps(fixture["grant"]))
    supervisor.enroll(
        fixture["home"],
        fixture["vault"],
        ROOT,
        "coder",
        fixture["policy"],
        fixture["task_id"],
        IMAGE,
    )
    yield fixture
    fixture["queue"].close()


def retain(fixture, name, **extra):
    target = os.environ.get("EMURU_CODER_QUALIFICATION")
    if target is None:
        return
    from emuru.deployment import source_identity
    from emuru.projects.scout import digest

    with supervisor._locked(fixture["home"]) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    detail, card, count = facts(fixture)
    result = {
        "status": "PASS",
        "case": name,
        "fixture": str(fixture["root"]),
        "source": source_identity(ROOT),
        "image": IMAGE,
        "grant": fixture["grant"],
        "grant_sha256": digest(fixture["grant"]),
        "records": records,
        "detail": detail,
        "native": {
            "task": card.id,
            "status": card.status,
            "run": card.current_run_id,
            "run_count": count,
        },
        **extra,
    }
    (Path(target) / (name + ".json")).write_text(
        json.dumps(result, sort_keys=True, indent=2)
    )


def facts(fixture):
    home = fixture["home"]
    with board.project_board_readonly(home, ROOT) as (kb, db):
        card = kb.get_task(db, fixture["task_id"])
        return (
            supervisor.status(home, ROOT, fixture["project"], card),
            card,
            len(kb.list_runs(db, card.id)),
        )


def files(folder):
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


def crash(fixture, boundary):
    home, vault, grant = fixture["home"], fixture["vault"], fixture["grant"]
    with dispatcher_lock(home):
        supervisor_coder.reserve(home, vault, ROOT, grant, IMAGE)
        if boundary == "reservation":
            os._exit(17)
        stage = supervisor_coder.CoderStage(home, vault, ROOT, grant)
        folder = coder_board.create_attempt(home, grant, supervision=stage)
        coder_board.record_launcher(folder)
        original = coder_board._write_new

        def lost_ack(folder, name, data):
            if name == "run.json":
                os._exit(17)
            return original(folder, name, data)

        coder_board._write_new = lost_ack
        coder_board.claim(
            home, vault, ROOT, grant["task"], grant, folder, supervision=stage
        )
        raise AssertionError("Crash boundary missed")


@pytest.mark.parametrize("boundary", ["reservation", "claim"])
def test_native_crash_retains_charge_status_readonly_and_reconcile_never_reclaims(
    native_fixture, boundary
):
    fixture = native_fixture
    process = multiprocessing.get_context("fork").Process(
        target=crash, args=(fixture, boundary)
    )
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("Native crash seam stalled")
    assert process.exitcode == 17
    before = files(fixture["home"])
    detail, card, count = facts(fixture)
    assert detail["reserved"]["coder_runs"] == 1
    assert detail["execution"] == "uncertain"
    assert files(fixture["home"]) == before
    assert count == (1 if boundary == "claim" else 0)
    with dispatcher_lock(fixture["home"]):
        result = coder_board.reconcile(
            fixture["home"],
            fixture["vault"],
            ROOT,
            fixture["task_id"],
            fixture["grant"],
        )
    assert result["status"] == "uncertain" and result["stop"] == "proven"
    detail, card, after_count = facts(fixture)
    assert after_count == count
    assert detail["reserved"]["active_seconds"] == 600
    assert card.status == "blocked"
    assert "stop" in detail["stop_proof"]
    before = files(fixture["home"])
    with pytest.raises(ValueError, match="attempted|uncertain|approved coder grant"):
        supervisor_coder.reserve(
            fixture["home"], fixture["vault"], ROOT, fixture["grant"], IMAGE
        )
    assert files(fixture["home"]) == before
    retain(fixture, "native-" + boundary, inspection_readonly=True, stop=result)


@pytest.mark.parametrize("native_fixture", [2], indirect=True)
def test_schema_two_uses_same_deadline_as_existing_loop_reservation(native_fixture):
    from emuru.projects import reviewer

    fixture = native_fixture
    grant = fixture["grant"]
    with dispatcher_lock(fixture["home"]):
        admitted = supervisor_coder.reserve(
            fixture["home"], fixture["vault"], ROOT, grant, IMAGE
        )
        stage = supervisor_coder.CoderStage(
            fixture["home"], fixture["vault"], ROOT, grant
        )
        folder = coder_board.create_attempt(fixture["home"], grant, supervision=stage)
        assert (
            coder_board._read_json(folder / "coder-stage.json")["deadline_ns"]
            == admitted["deadline_ns"]
        )
        reopened = supervisor_coder.CoderStage(
            fixture["home"], fixture["vault"], ROOT, grant
        )
        assert reopened.deadline == stage.deadline
        assert reviewer.check_loop(folder, grant)["coder_runs"] == 1
    retain(fixture, "schema-two-reservation")


def test_continue_after_claim_returns_without_locking_out_cancel(native_fixture):
    fixture = native_fixture
    home, vault, grant = fixture["home"], fixture["vault"], fixture["grant"]
    parent, child = multiprocessing.get_context("fork").Pipe()

    def claimed_worker():
        with dispatcher_lock(home):
            supervisor_coder.reserve(home, vault, ROOT, grant, IMAGE)
            stage = supervisor_coder.CoderStage(home, vault, ROOT, grant)
            folder = coder_board.create_attempt(home, grant, supervision=stage)
            coder_board.record_launcher(folder)
            coder_board.claim(
                home, vault, ROOT, grant["task"], grant, folder, supervision=stage
            )
            child.send("claimed")
            child.recv()
            with pytest.raises(InterruptedError):
                stage.check()

    worker = multiprocessing.get_context("fork").Process(target=claimed_worker)
    worker.start()
    assert parent.poll(10) and parent.recv() == "claimed"
    detail, _, _ = facts(fixture)
    fixture["project_command"](
        f"/project hold {fixture['proposal']} {grant['task']} {detail['token']}"
    )
    held, _, _ = facts(fixture)

    def continued():
        result = fixture["project_command"](
            f"/project continue {fixture['proposal']} {grant['task']} {held['token']}"
        )
        detail, _, _ = facts(fixture)
        cancelled = fixture["project_command"](
            f"/project cancel {fixture['proposal']} {grant['task']} {detail['token']}"
        )
        (fixture["root"] / "continue-result.json").write_text(
            json.dumps({"continue": result, "cancel": cancelled})
        )

    process = multiprocessing.get_context("fork").Process(target=continued)
    process.start()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(3)
        pytest.fail("Continue retained the control lock")
    assert process.exitcode == 0
    response = json.loads((fixture["root"] / "continue-result.json").read_text())
    assert "uncertain" in response["continue"]
    assert "stop_requested" in response["cancel"]
    parent.send("finish")
    worker.join(5)
    if worker.is_alive():
        worker.kill()
        worker.join(3)
        pytest.fail("Claimed worker did not stop")
    assert worker.exitcode == 0
    with dispatcher_lock(home):
        coder_board.reconcile(home, vault, ROOT, grant["task"], grant)
    parent.close()
    child.close()
    retain(fixture, "continue-lock")


def test_lifecycle_capacity_preserves_cancel_and_independent_stop(native_fixture):
    import sys

    fixture = native_fixture
    home, vault, grant = fixture["home"], fixture["vault"], fixture["grant"]
    parent, child = multiprocessing.get_context("fork").Pipe()

    def bounded_worker():
        with dispatcher_lock(home):
            supervisor_coder.reserve(home, vault, ROOT, grant, IMAGE)
            stage = supervisor_coder.CoderStage(home, vault, ROOT, grant)
            for count in range(150):
                try:
                    stage.command([sys.executable, "-c", "pass"])
                except ValueError as error:
                    assert "capacity" in str(error)
                    child.send(count)
                    break
            else:
                raise AssertionError("Lifecycle cap did not refuse ordinary commands")
            child.recv()
            with pytest.raises(InterruptedError):
                stage.check()
            assert (
                stage.command([sys.executable, "-c", "pass"], maintenance=True)[
                    "exit_code"
                ]
                == 0
            )

    worker = multiprocessing.get_context("fork").Process(target=bounded_worker)
    worker.start()
    try:
        assert parent.poll(60), "Lifecycle cap boundary stalled"
        commands = parent.recv()
        assert commands >= 80
        detail, _, _ = facts(fixture)
        assert "stop_requested" in fixture["project_command"](
            f"/project cancel {fixture['proposal']} {grant['task']} {detail['token']}"
        )
        parent.send("cancel")
        worker.join(10)
        assert worker.exitcode == 0
        with dispatcher_lock(home):
            stopped = coder_board.reconcile(home, vault, ROOT, grant["task"], grant)
        assert stopped["stop"] == "proven"
        retain(fixture, "lifecycle-cap", ordinary_commands=commands, stop=stopped)
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5)
        parent.close()
        child.close()


def bootstrap(fixture, blocked, *, review=False):
    source_path = fixture["repo"] / "sum_prices.py"
    if fixture["grant"]["schema"] == 3:
        source_path = (
            fixture["home"]
            / "emuru-projects/coder-attempts"
            / fixture["grant"]["rework"]["loop"]
            / "final-source/sum_prices.py"
        )
    initial = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = fixture.get(
        "coder_source", "def sum_prices(prices):\n    return sum(prices)\n"
    )
    verdict = fixture.get(
        "review_verdict",
        {
            "verdict": "approve",
            "summary": "Synthetic checked review.",
            "blockers": [],
            "suggestions": [],
        },
    )
    return f"""import sys,json,time
sys.path.insert(0,'/opt/emuru/src')
from emuru.projects import coder_native as native
import httpx
agent_class=native._load_native()
original=agent_class._create_openai_client
calls=0
def respond(request):
    global calls
    calls+=1
    if {blocked is True!r}: time.sleep(10)
    if {review!r}:
        message={{'role':'assistant','content':{json.dumps(verdict)!r}}}
        return httpx.Response(200,json={{'id':'synthetic','object':'chat.completion','created':0,'model':'emuru','choices':[{{'index':0,'message':message,'finish_reason':'stop'}}],'usage':{{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}}})
    if calls==1:
        name,args='coder_read',{{'path':'sum_prices.py'}}
    elif calls==2:
        name,args='coder_write',{{'path':'sum_prices.py','content':{source!r},'expected_sha256':{initial!r}}}
    elif {blocked == "cap-check"!r} and calls<=5:
        name,args='coder_check',{{'check_id':'python_unittest'}}
    else:
        name,args=None,None
    message={{'role':'assistant','content':'Synthetic completion' if name is None else None}}
    if name: message['tool_calls']=[{{'id':'synthetic-'+str(calls),'type':'function','function':{{'name':name,'arguments':json.dumps(args)}}}}]
    return httpx.Response(200,json={{'id':'synthetic','object':'chat.completion','created':0,'model':'emuru','choices':[{{'index':0,'message':message,'finish_reason':'tool_calls' if name else 'stop'}}],'usage':{{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}}})
def client(agent,kwargs,**kw):
    return original(agent,{{**kwargs,'http_client':httpx.Client(transport=httpx.MockTransport(respond))}},**kw)
agent_class._create_openai_client=client
native._load_native=lambda: agent_class
raise SystemExit(native.main())
"""


def launch_child(fixture, window, connection):
    """Private external-API/failure injections; actual launcher, native runtime and checks."""
    launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
    create = supervisor_coder.CoderStage.create
    bind = supervisor_coder.CoderStage.bind

    def created(self, command, name, service, image, **kwargs):
        if service == "inference":
            command = [*command]
            command[-1] = bootstrap(
                fixture,
                "cap-check"
                if window == "cap-check"
                else (
                    window == "cancel-inference"
                    or self.stage["kind"] == "reviewer-stage"
                    and window in {"cancel-review", "kill-review"}
                ),
                review=self.stage["kind"] == "reviewer-stage",
            )
        identity = create(self, command, name, service, image, **kwargs)
        if (
            window == "cap-check"
            and service == "check"
            and "developer_main" in command[-1]
            and sum(r.get("phase") == "check-admitted" for r in self.records()) == 4
        ):

            def fill():
                while True:
                    actual = subprocess.run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{.State.Running}}",
                            identity,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    )
                    if actual.returncode == 0 and actual.stdout.strip() == "true":
                        break
                    time.sleep(0.02)
                for _ in range(150):
                    try:
                        self.command([sys.executable, "-c", "pass"])
                    except ValueError as error:
                        assert "capacity" in str(error)
                        (fixture["root"] / "capacity-reached").touch()
                        return
                raise AssertionError("Ordinary lifecycle admission did not stop")

            threading.Thread(target=fill, daemon=True).start()
        return identity

    def bound(self, name, actual):
        result = bind(self, name, actual)
        with (fixture["root"] / "actual-containers.jsonl").open("a") as stream:
            stream.write(json.dumps(actual) + "\n")
        return result

    supervisor_coder.CoderStage.create = created
    supervisor_coder.CoderStage.bind = bound

    @contextmanager
    def gateway(folder, *, process_event, supervision, deadline):
        assert deadline == supervision.deadline
        suffix = fixture["grant"]["grant_id"][:12] + str(
            supervision.stage["generation"]
        )
        name = "emuru-coder-gateway-" + suffix
        process_event(name, "starting")
        network_names = [
            "emuru-coder-model-" + suffix,
            "emuru-coder-egress-" + suffix,
        ]
        for network in network_names:
            supervision.create_network(network, internal=True)
        identity = supervision.create(
            [
                "docker",
                "run",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/bin/true",
                IMAGE,
            ],
            name,
            "gateway",
            IMAGE,
        )
        try:
            assert (
                supervision.command(["docker", "start", "--attach", identity])[
                    "exit_code"
                ]
                == 0
            )
            yield network_names[0], "synthetic-no-provider-access", "synthetic"
        finally:
            supervision.remove(name)
            for network in reversed(network_names):
                supervision.remove_network(network)
            process_event(name, "stopped")

    launch.__globals__["cloud_gateway"] = gateway
    from emuru.projects import coder_gateway, supervisor_reviewer

    coder_gateway.cloud_gateway = gateway
    if window in {"handoff", "schema-two"}:
        original_handoff = supervisor_reviewer.handoff

        def handoff(*args, **kwargs):
            connection.send("handoff")
            assert connection.recv() == "proceed"
            return original_handoff(*args, **kwargs)

        supervisor_reviewer.handoff = handoff
    if window in {"review-request", "review-claim", "review-ack", "rework-claim"}:
        kb = board.native_api(ROOT)
        name = {
            "review-request": "request_review",
            "review-claim": "claim_review_task",
            "review-ack": "complete_task",
            "rework-claim": "claim_task",
        }[window]
        original_native = getattr(kb, name)

        def native(*args, **kwargs):
            result = original_native(*args, **kwargs)
            connection.send(window)
            connection.recv()
            return result

        setattr(kb, name, native)
    if window == "verdict":
        original_complete = supervisor_reviewer.complete

        def complete(*args, **kwargs):
            connection.send("verdict")
            connection.recv()
            return original_complete(*args, **kwargs)

        supervisor_reviewer.complete = complete
    if window == "hold":
        original = coder_board.create_attempt

        def attempt(*args, **kwargs):
            connection.send("admitted")
            assert connection.recv() == "proceed"
            return original(*args, **kwargs)

        coder_board.create_attempt = attempt
    if window == "evidence":
        original_run = launch.__globals__["Qualification"].run

        def run(self, *args):
            result = original_run(self, *args)
            assert result["success"]
            connection.send("evidence")
            connection.recv()
            raise AssertionError("Crash injection resumed")

        launch.__globals__["Qualification"].run = run
    if window == "native-ack":
        original_complete = coder_board._complete_success

        def complete(*args):
            result = original_complete(*args)
            connection.send("native-ack")
            connection.recv()
            return result

        coder_board._complete_success = complete
    try:
        result = launch(
            fixture["home"],
            fixture["vault"],
            fixture["policy"],
            fixture["task_id"],
            ROOT,
            review=fixture.get("first_review", False),
        )
        (fixture["root"] / "child-result.json").write_text(json.dumps(result))
    except BaseException as error:
        (fixture["root"] / "child-result.json").write_text(
            json.dumps({"error": type(error).__name__})
        )
        raise


def wait_active(fixture, process, window):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        assert process.is_alive(), (
            (fixture["root"] / "child-result.json").read_text()
            if (fixture["root"] / "child-result.json").exists()
            else process.exitcode
        )
        with supervisor._locked(fixture["home"]) as (parent, size, marker):
            records, _ = supervisor._records(parent, size, marker)
        plans = {r["name"]: r for r in records if r.get("phase") == "planned"}
        for created in (r for r in records if r.get("phase") == "created"):
            plan = plans[created["name"]]
            if plan["service"] != (
                "inference" if window == "cancel-inference" else "check"
            ):
                continue
            actual = subprocess.run(
                ["docker", "inspect", created["container_id"]],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if actual.returncode:
                continue
            record = json.loads(actual.stdout)[0]
            if (
                window == "cap-check"
                and not (fixture["root"] / "capacity-reached").exists()
            ):
                continue
            if record["State"]["Running"] and (
                window == "cancel-inference"
                and any(r.get("phase") == "model-admitted" for r in records)
                or window != "cancel-inference"
                and "developer_main" in " ".join(record["Config"]["Cmd"])
            ):
                return record
        time.sleep(0.05)
    raise AssertionError("Controlled active wait not reached")


@pytest.mark.skipif(
    "EMURU_CODER_TEST_IMAGE" not in os.environ,
    reason="Matching-image lifecycle harness supplies Docker/native qualification",
)
@pytest.mark.parametrize(
    "native_fixture,window",
    [
        (1, "hold"),
        (1, "cancel-inference"),
        ("check", "cancel-check"),
        ("check", "kill-check"),
        ("check", "cap-check"),
        (1, "evidence"),
        (1, "native-ack"),
        (2, "schema-two"),
    ],
    indirect=["native_fixture"],
)
def test_supervised_launcher_native_docker_matrix(native_fixture, window):
    fixture = native_fixture
    launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
    original_repo, original_vault = files(fixture["repo"]), files(fixture["vault"])
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(
        target=launch_child, args=(fixture, window, child)
    )
    process.start()
    observation = None
    try:
        if window in {"hold", "schema-two", "evidence", "native-ack"}:
            assert parent.poll(90), "Controlled crash/admission barrier not reached"
            assert parent.recv() == (
                "admitted"
                if window == "hold"
                else "handoff"
                if window == "schema-two"
                else window
            )
        elif window in {"cancel-inference", "cancel-check", "kill-check", "cap-check"}:
            observation = wait_active(fixture, process, window)
            before = files(fixture["home"])
            with pytest.raises((ValueError, BlockingIOError)):
                launch(
                    fixture["home"],
                    fixture["vault"],
                    fixture["policy"],
                    fixture["task_id"],
                    ROOT,
                    cleanup=True,
                )
            assert files(fixture["home"]) == before
        if (
            window in {"hold", "schema-two"}
            or window.startswith("cancel")
            or window == "cap-check"
        ):
            detail, _, _ = facts(fixture)
            requested = time.monotonic_ns()
            response = fixture["project_command"](
                f"/project {'hold' if window in {'hold', 'schema-two'} else 'cancel'} {fixture['proposal']} {fixture['task_id']} {detail['token']}"
            )
            assert (
                "held" in response
                if window in {"hold", "schema-two"}
                else "stop_requested" in response
            )
            if window in {"hold", "schema-two"}:
                parent.send("proceed")
        if window in {"kill-check", "evidence", "native-ack"}:
            os.kill(process.pid, signal.SIGKILL)
        process.join(90)
        assert not process.is_alive()
        if window in {"hold", "schema-two"}:
            assert process.exitcode == 0, (
                fixture["root"] / "child-result.json"
            ).read_text()
        before = files(fixture["home"])
        detail, card, count = facts(fixture)
        assert count == 1 and detail["reserved"]["coder_runs"] == 1
        assert files(fixture["home"]) == before
        with supervisor._locked(fixture["home"]) as (directory, size, marker):
            records, _ = supervisor._records(directory, size, marker)
        if window.startswith("cancel") or window == "cap-check":
            observed = next(
                r["observed_ns"] for r in records if r.get("phase") == "stop-observed"
            )
            assert observed - requested < 10**9
        if window not in {"hold", "schema-two"}:
            if window == "kill-check":
                before_card = (card.status, card.current_run_id)
                old_host = os.environ.get("DOCKER_HOST")
                os.environ["DOCKER_HOST"] = "unix:///tmp/emuru-ticket3-unavailable.sock"
                try:
                    with pytest.raises(ValueError, match="inspection unavailable"):
                        launch(
                            fixture["home"],
                            fixture["vault"],
                            fixture["policy"],
                            fixture["task_id"],
                            ROOT,
                            cleanup=True,
                        )
                finally:
                    if old_host is None:
                        os.environ.pop("DOCKER_HOST")
                    else:
                        os.environ["DOCKER_HOST"] = old_host
                assert (
                    facts(fixture)[1].status,
                    facts(fixture)[1].current_run_id,
                ) == before_card
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                cleanup=True,
            )
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                reconcile=True,
            )
        detail, card, after_count = facts(fixture)
        assert after_count == 1
        if window in {"hold", "evidence", "native-ack"}:
            assert (
                card.status == "done"
                and detail["verified_outcome"]
                == "tested local result / awaiting owner review"
            )
        elif window == "schema-two":
            assert card.status == "running" and card.assignee == "emuru-coder"
            assert detail["execution"] == "awaiting_review"
            assert not (
                coder_board.attempt_path(fixture["home"], fixture["grant"]) / "review"
            ).exists()
        else:
            assert card.status == "blocked" and detail["execution"] == "uncertain"
        assert (
            files(fixture["repo"]) == original_repo
            and files(fixture["vault"]) == original_vault
        )
        before = files(fixture["home"])
        prior = launch(
            fixture["home"],
            fixture["vault"],
            fixture["policy"],
            fixture["task_id"],
            ROOT,
        )
        assert files(fixture["home"]) == before
        retain(
            fixture,
            window,
            inspection_readonly=True,
            restart_result=prior,
            active_container=observation,
            cancellation_ns=None
            if not (window.startswith("cancel") or window == "cap-check")
            else observed - requested,
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        try:
            launch(
                fixture["home"],
                fixture["vault"],
                fixture["policy"],
                fixture["task_id"],
                ROOT,
                cleanup=True,
            )
        finally:
            parent.close()
            child.close()
