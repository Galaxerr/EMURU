"""Scout admission/reservation mechanics; native/Docker proof remains mandatory."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import fcntl
import json
import os
import runpy
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml
from test_supervisor import (  # noqa: F401 -- public enrollment fixtures
    ROOT,
    durable,
    enrollment,
    scout_enrollment,
)


def test_supervised_create_keeps_stdin_and_approved_confinement():
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    service = yaml.safe_load((ROOT / "infra/docker/scout.compose.yaml").read_text())[
        "services"
    ]["scout"]
    service.update(image="sha256:" + "a" * 64, labels={"io.emuru.scout.stage": "bound"})
    service["volumes"] = [
        {
            "type": "bind",
            "source": "/private/source",
            "target": "/repo",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    command = runner["_create_command"](service, "emuru-scout-fixed")
    assert command[:3] == ["docker", "create", "--interactive"]
    for flag, value in (
        ("--network", "none"),
        ("--user", "1000:1000"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--pids-limit", "16"),
        ("--memory", "128m"),
        ("--cpus", "1"),
        ("--restart", "no"),
        ("--stop-timeout", "2"),
        ("--log-driver", "none"),
    ):
        assert command[command.index(flag) + 1] == value
    assert "--read-only" in command
    assert (
        command[command.index("--mount") + 1]
        == "type=bind,src=/private/source,dst=/repo,readonly"
    )
    assert command[-3:] == ["-I", "-c", service["command"][0]]


from emuru.projects import board, coder_board, scout_board, supervisor, supervisor_scout
from emuru.projects.dispatcher import dispatcher_lock
from emuru.projects.scout import digest

ENSURE_IDLE = coder_board.ensure_idle
MONOTONIC_NS = time.monotonic_ns


@pytest.fixture
def registered_scout(scout_enrollment):
    home, _coder_policy, grant, card, scout = scout_enrollment
    policy = json.loads(scout.read_text())
    supervisor.enroll(
        home,
        home,
        ROOT,
        "scout",
        scout,
        card.id,
        "sha256:" + "a" * 64,
        owner=grant["owner"],
        bot=grant["bot"],
    )
    worker_grant = {
        key: policy[key]
        for key in ("repository", "baseline", "files", "timeout_seconds")
    }
    worker_grant.update(
        source_task=card.id,
        repository_id=digest([policy["repository"], policy["path"]]),
        report_sha256="f" * 64,
    )
    return home, grant, card, policy, worker_grant


def test_scout_reserves_once_before_claim_and_restart_never_refunds(registered_scout):
    home, grant, card, policy, worker_grant = registered_scout
    stage = supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker_grant
    )
    assert stage["phase"] == "admitted"
    current = supervisor.status(home, ROOT, grant["project"], card)
    assert current["reserved"]["scout_runs"] == 1
    assert current["reserved"]["active_seconds"] == 120
    assert current["reserved"]["artifact_bytes"] == 65536
    before = durable(home)
    with pytest.raises(ValueError, match="ownership uncertain"):
        supervisor_scout.reserve(
            home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker_grant
        )
    assert durable(home) == before


def test_completed_legacy_coder_preflight_can_wait_only_before_short_claim_lock(
    registered_scout, monkeypatch
):
    home, _grant, card, policy, worker_grant = registered_scout
    stage = supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker_grant
    )
    attempt = home / "emuru-projects/coder-attempts" / ("e" * 32)
    attempt.mkdir(mode=0o700, parents=True)
    attempt.parent.chmod(0o700)
    (attempt / "intent.json").write_text(json.dumps({"task": "t_deed"}))
    (attempt / "intent.json").chmod(0o600)
    checked = []

    class Native:
        @staticmethod
        def get_task(_db, task):
            assert task == "t_deed"
            return SimpleNamespace(id=task, status="done", assignee="emuru-coder")

        @staticmethod
        def list_tasks(_db, **_kwargs):
            return []

    @contextmanager
    def readonly(*_):
        yield Native(), None

    def completed_status(*_):
        with (home / "emuru-projects/supervisor/control.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=5)
        checked.append("completed legacy coder checked outside short lock")
        return {"status": "tested local result / awaiting owner review"}

    def native_claim(*_):
        fd = os.open(home / "emuru-projects/supervisor/control.lock", os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return {"status": "claimed", "task": "t_abcde", "run": 1}

    monkeypatch.setattr(coder_board, "ensure_idle", ENSURE_IDLE)
    monkeypatch.setattr(coder_board, "status", completed_status)
    monkeypatch.setattr(board, "project_board_readonly", readonly)
    monkeypatch.setattr(scout_board, "_claim_native", native_claim)
    with dispatcher_lock(home):
        assert (
            supervisor_scout.claim(home, ROOT, worker_grant, digest(stage))["status"]
            == "claimed"
        )
    assert checked == ["completed legacy coder checked outside short lock"]


def test_lost_native_claim_ack_never_replays(registered_scout, monkeypatch):
    home, _grant, card, policy, worker = registered_scout
    stage = supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker
    )
    calls = []

    def interrupted(*_):
        calls.append("native")
        raise InterruptedError("Injected acknowledgement loss")

    monkeypatch.setattr(scout_board, "_claim_native", interrupted)
    with pytest.raises(InterruptedError):
        supervisor_scout.claim(home, ROOT, worker, digest(stage))
    before = durable(home)
    with pytest.raises(ValueError, match="already attempted"):
        supervisor_scout.claim(home, ROOT, worker, digest(stage))
    assert calls == ["native"] and durable(home) == before


def test_attempted_launch_refuses_before_source_preparation(
    registered_scout, monkeypatch
):
    home, _grant, card, policy, worker = registered_scout
    supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker
    )
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda _: {"ensure_image": lambda **kw: ("source", "sha256:" + "a" * 64)},
    )
    monkeypatch.setitem(
        runner["launch"].__globals__,
        "snapshot",
        lambda *_: pytest.fail("Attempted work replayed source preparation"),
    )
    before = durable(home)
    with pytest.raises(ValueError, match="ownership uncertain"):
        runner["launch"](home, home.parent / "scout-policy.json", card.id, vault=home)
    assert durable(home) == before


def test_cleanup_refuses_live_launcher_before_docker(tmp_path, monkeypatch):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    stage = {
        "image": "sha256:" + "a" * 64,
        "launcher": supervisor_scout.launcher_identity(),
    }
    monkeypatch.setattr(supervisor_scout, "load", lambda *_: (stage, []))
    monkeypatch.setitem(
        runner["cleanup_scout"].__globals__,
        "_inspect",
        lambda *_: pytest.fail("Live-launcher cleanup reached Docker"),
    )
    with pytest.raises(ValueError, match="launcher may still execute"):
        runner["cleanup_scout"](tmp_path, ROOT, {}, "t_abc", stage["image"], tmp_path)


@pytest.mark.parametrize("exited_before_signal", [False, True])
def test_cleanup_signals_only_rechecked_pidfd(
    monkeypatch, tmp_path, exited_before_signal
):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    stage = {"image": "sha256:" + "a" * 64, "launcher": {"pid": 123}, "generation": 1}
    process = {"pid": 456}
    events = [
        {"admission": 1, "phase": "client-planned", "name": "bound"},
        {"admission": 1, "phase": "client-bound", "name": "bound", "process": process},
        {"admission": 1, "phase": "client-planned", "name": "unacknowledged"},
    ]
    opened, signalled, closed, recorded = [], [], [], []

    def stopped(value):
        if value is stage["launcher"]:
            return True
        return bool(signalled) or bool(opened) and exited_before_signal

    monkeypatch.setattr(supervisor_scout, "load", lambda *_: (stage, events))
    monkeypatch.setattr(coder_board, "launcher_stopped", stopped)
    monkeypatch.setattr(os, "pidfd_open", lambda pid: opened.append(pid) or 789)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(
        runner["signal"], "pidfd_send_signal", lambda fd, sig: signalled.append(fd)
    )
    monkeypatch.setattr(
        os, "killpg", lambda *_: pytest.fail("Cleanup signalled reusable process group")
    )
    monkeypatch.setattr(
        supervisor_scout, "event", lambda *a, **kw: recorded.append((a, kw))
    )
    result = runner["cleanup_scout"](
        tmp_path, ROOT, {}, "t_abc", stage["image"], tmp_path
    )
    assert result["stop"] == "unproven"
    assert opened == [456] and closed == [789]
    assert signalled == ([] if exited_before_signal else [789])
    assert recorded[0][0][2] == "client-stopped"
    assert recorded[0][1]["process"] == process


def test_cleanup_stops_exact_live_client_with_pidfd(monkeypatch, tmp_path):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json,time; from emuru.projects.supervisor_scout import launcher_identity; "
                "print(json.dumps(launcher_identity())); time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        binding = json.loads(process.stdout.readline())
        stage = {
            "image": "sha256:" + "a" * 64,
            "generation": 1,
            "launcher": {**binding, "start_time": "0"},
        }
        events = [
            {"admission": 1, "phase": "client-planned", "name": "bound"},
            {
                "admission": 1,
                "phase": "client-bound",
                "name": "bound",
                "process": binding,
            },
            {"admission": 1, "phase": "client-planned", "name": "unacknowledged"},
        ]
        monkeypatch.setattr(supervisor_scout, "load", lambda *_: (stage, events))
        monkeypatch.setattr(supervisor_scout, "event", lambda *a, **kw: None)
        result = runner["cleanup_scout"](
            tmp_path, ROOT, {}, "t_abc", stage["image"], tmp_path
        )
        assert result["stop"] == "unproven"
        assert process.wait(timeout=5) == -runner["signal"].SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize(
    "message",
    [
        "Cannot connect to Docker daemon",
        "permission denied while trying to connect to Docker daemon socket",
        "error: no such object: " + "b" * 64,
        "error: no such object: " + "a" * 64 + "-other",
    ],
)
def test_docker_inspection_error_never_means_absent(monkeypatch, message):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    monkeypatch.setitem(
        runner["_inspect"].__globals__,
        "_docker",
        lambda *_: (1, {"stdout": b"", "stderr": message.encode()}),
    )
    with pytest.raises(ValueError, match="ownership remains uncertain"):
        runner["_inspect"]("a" * 64)


@pytest.mark.parametrize("kind", ["object", "container"])
@pytest.mark.parametrize("prefix", ["", "error: ", "Error response from daemon: "])
@pytest.mark.parametrize("words", ["no such", "No SuCh"])
def test_docker_inspection_accepts_only_exact_missing_identity(
    monkeypatch, kind, prefix, words
):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    monkeypatch.setitem(
        runner["_inspect"].__globals__,
        "_docker",
        lambda *_: (
            1,
            {
                "stdout": b"",
                "stderr": f"\n{prefix}{words} {kind}: {'a' * 64}\n".encode(),
            },
        ),
    )
    assert runner["_inspect"]("a" * 64) is None


def test_blocked_wait_observes_stop_and_reaps_exact_client():
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    events = []
    started = time.monotonic()
    with pytest.raises(InterruptedError, match="stop requested"):
        runner["_bounded_call"](
            [sys.executable, "-c", "import time; time.sleep(10)"],
            b"",
            time.monotonic_ns() + 10 * 10**9,
            lambda: time.monotonic() - started >= 0.15,
            lambda phase, value: events.append((phase, value)),
        )
    assert time.monotonic() - started < 1
    assert [phase for phase, _ in events] == [
        "client-bound",
        "stop-observed",
        "client-stopped",
    ]
    assert events[0][1] == events[-1][1]
    assert coder_board.launcher_stopped(events[0][1])


@pytest.mark.parametrize("boundary", ["cancel", "deadline"])
def test_active_supervised_inspect_observes_cancel_and_original_deadline(
    scout_enrollment, monkeypatch, boundary
):
    from emuru.projects.supervisor_controls import owner_control
    from emuru.telegram.queue import TelegramQueue, load_telegram_settings
    from emuru.vault import mutations

    monkeypatch.setattr(time, "monotonic_ns", MONOTONIC_NS)
    home, _coder, grant, card, policy_path = scout_enrollment
    policy = json.loads(policy_path.read_text())
    if boundary == "deadline":
        policy["timeout_seconds"] = 1
        policy_path.write_text(json.dumps(policy))
    supervisor.enroll(
        home,
        home,
        ROOT,
        "scout",
        policy_path,
        card.id,
        "sha256:" + "a" * 64,
        owner=grant["owner"],
        bot=grant["bot"],
    )
    worker = {
        key: policy[key]
        for key in ("repository", "baseline", "files", "timeout_seconds")
    }
    worker.update(
        source_task=card.id,
        repository_id=digest([policy["repository"], policy["path"]]),
        report_sha256="f" * 64,
    )
    stage = supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker
    )
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    backend = object.__new__(runner["DockerScout"])
    backend.home, backend.image, backend.admission = home, stage["image"], digest(stage)
    backend.cleanup, backend.deadline_ns = False, stage["deadline_ns"]
    backend.config_path = home / "inspect-compose.json"
    service = yaml.safe_load((ROOT / "infra/docker/scout.compose.yaml").read_text())[
        "services"
    ]["scout"]
    service.update(image=backend.image, user="1000:1000", volumes=[])
    backend.config_path.write_text(json.dumps({"services": {"scout": service}}))
    backend.command = ["docker", "compose"]
    real_call, entered, observed = runner["_bounded_call"], threading.Event(), []
    inspected, removed, errors = [], [], []

    def call(command, payload, deadline_ns, stopped, process_event=None):
        if command[:2] == ["docker", "create"]:
            return 0, {"stdout": b"", "stderr": b""}
        if command[1] == "rm":
            removed.append(command[-1])
            return 0, {"stdout": b"", "stderr": b""}
        assert command[1] == "inspect"
        if not inspected:
            inspected.append(command[-1])

            def stop():
                result = stopped()
                if result:
                    observed.append(time.monotonic())
                return result

            return real_call(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                payload,
                deadline_ns,
                stop,
                lambda phase, _: entered.set() if phase == "client-bound" else None,
            )
        if removed:
            return 1, {
                "stdout": b"",
                "stderr": f"No such object: {command[-1]}".encode(),
            }
        record = {
            "Id": "c" * 64,
            "Image": stage["image"],
            "Config": {
                "Labels": {
                    "io.emuru.scout.stage": backend.admission,
                    "io.emuru.scout.operation": inspected[0],
                }
            },
        }
        return 0, {"stdout": json.dumps(record).encode(), "stderr": b""}

    monkeypatch.setitem(runner["_bounded_call"].__globals__, "_bounded_call", call)

    def invoke():
        try:
            backend._supervised_call("scout", None)
        except (InterruptedError, TimeoutError, ValueError) as error:
            errors.append(error)

    thread = threading.Thread(target=invoke)
    started = time.monotonic()
    thread.start()
    assert entered.wait(2)
    if boundary == "cancel":
        monkeypatch.setattr(
            mutations,
            "approved_project",
            lambda *a, **kw: {
                "current": True,
                "after_revision": grant["revision"],
                "proposal_id": grant["proposal"],
            },
        )
        queue = TelegramQueue(
            home / "emuru-telegram/queue.sqlite3",
            load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
            grant["owner"],
            clock=lambda: 100000,
        )
        token = supervisor.status(home, ROOT, grant["project"], card)["token"]
        text = f"/project cancel {grant['proposal']} {card.id} {token}"
        queue.stage(
            grant["bot"],
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 100000,
                        "chat": {"id": grant["owner"], "type": "private"},
                        "from": {"id": grant["owner"], "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        row = queue.claim(grant["bot"], "emuru")
        request, _ = mutations.begin_request(
            home, home, grant["owner"], grant["bot"], 1, text, fresh=True
        )
        cancelled = time.monotonic()
        assert "revoked" in owner_control(
            home, home, ROOT, text, grant["owner"], grant["bot"], request, 1
        )
        queue.finish(row["sequence"], True)
        queue.close()
    thread.join(5)
    assert not thread.is_alive()
    if boundary == "cancel":
        assert observed and observed[0] - cancelled < 1
        assert isinstance(errors[0], InterruptedError)
    else:
        assert isinstance(errors[0], TimeoutError)
        assert time.monotonic() - started < 1.5
    assert removed == ["c" * 64]


def test_worker_uses_original_absolute_deadline():
    deadline = time.monotonic_ns() + 250000000
    code = "import sys,time; from emuru.projects.scout import limit_runtime; limit_runtime(120,deadline_ns=int(sys.argv[1])); time.sleep(10)"
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", code, str(deadline)],
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert result.returncode != 0 and b"Scout stopped or exceeded" in result.stderr
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("length", [65536, 65537])
def test_durable_scout_report_exact_limit_and_overflow(registered_scout, length):
    from emuru.projects.scout import encoded

    home, _grant, card, policy, worker_grant = registered_scout
    report = {"text": "x" * (length - len(encoded({"text": ""})))}
    assert len(encoded(report)) == length
    worker_grant["report_sha256"] = digest(report)
    stage = supervisor_scout.reserve(
        home, home, ROOT, policy, card.id, "sha256:" + "a" * 64, worker_grant
    )
    before = durable(home)
    if length == 65536:
        supervisor_scout.save_report(home, digest(stage), report)
        target = home / "emuru-projects/scout-artifacts" / (digest(report) + ".json")
        assert target.read_bytes() == encoded(report)
        supervisor_scout.save_report(home, digest(stage), report)
        assert target.read_bytes() == encoded(report)
    else:
        with pytest.raises(ValueError, match="report bound or digest invalid"):
            supervisor_scout.save_report(home, digest(stage), report)
        assert durable(home) == before
