"""Actual native/Docker lifecycle refusal and drain; synthetic inference only."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import hashlib
import json
import multiprocessing
import os
import runpy
import subprocess
import time

import pytest
from test_coder_supervisor_native import (
    IMAGE,
    ROOT,
    facts,
    files,
    native_fixture,  # noqa: F401
    retain,
    wait_active,
)
from test_reviewer_supervisor_native import control, invoke, stop

from emuru.models.gateway import write_private
from emuru.projects import supervisor
from emuru.projects import supervisor_lifecycle as lifecycle

pytestmark = pytest.mark.skipif(
    "EMURU_CODER_TEST_IMAGE" not in os.environ,
    reason="Matching-image service lifecycle harness supplies Docker/native proof",
)


def service(fixture, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["sleep"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", fixture["root"] / "deployment.env")
    monkeypatch.setitem(
        scope, "deployment_metadata", lambda *_: {"EMURU_PROFILE": str(fixture["home"])}
    )
    compose_path = fixture["root"] / "service-compose.json"
    write_private(scope["DEPLOYMENT_ENV"], "")
    write_private(
        compose_path,
        json.dumps(
            {
                "services": {
                    name: {
                        "image": IMAGE,
                        "restart": "no",
                        "network_mode": "none",
                        "entrypoint": [
                            "/opt/emuru/.venv/bin/python",
                            "-c",
                            "import signal,time,sys; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); time.sleep(3600)",
                        ],
                    }
                    for name in ("emuru", "litellm", "ollama")
                }
            }
        ),
    )
    monkeypatch.setitem(scope, "COMPOSE_FILE", compose_path)
    monkeypatch.setitem(scope, "PROJECT_NAME", fixture["root"].name)
    calls = []
    original = scope["docker_compose"]
    original("up", "-d")

    def compose(*args):
        if args[0] in {"stop", "down"}:
            calls.append(args)
        return original(*args)

    monkeypatch.setitem(scope, "docker_compose", compose)
    return module, calls, lambda: original("down", "--timeout", "5", "--remove-orphans")


@pytest.mark.parametrize(
    "native_fixture,window",
    [(1, "cancel-inference"), (2, "cancel-review")],
    indirect=["native_fixture"],
)
def test_actual_worker_sleep_refusal_cancel_and_retry(
    native_fixture, window, monkeypatch
):
    fixture = native_fixture
    module, calls, unwind = service(fixture, monkeypatch)
    services_before = module["containers"]()
    assert set(services_before) == {"emuru", "litellm", "ollama"}
    assert all(r["status"] == "running" for r in services_before.values())
    process, parent, child = invoke(fixture, window)
    try:
        if window == "cancel-inference":
            wait_active(fixture, process, window)
        else:
            limit = time.monotonic() + 90
            while time.monotonic() < limit:
                assert process.is_alive()
                if any(
                    r["kind"] == "reviewer-stage" and r.get("phase") == "model-admitted"
                    for r in lifecycle.snapshot(fixture["home"])
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Reviewer model wait not reached")
        with pytest.raises((ValueError, BlockingIOError)):
            module["sleep"](remove=True)
        assert not calls
        assert module["containers"]() == services_before
        records = lifecycle.snapshot(fixture["home"])
        assert lifecycle.fenced(records)
        before = files(fixture["home"])
        assert lifecycle.wake_inspection(fixture["home"], ROOT)
        assert files(fixture["home"]) == before
        assert "stop_requested" in control(fixture, "cancel")
        process.join(15)
        assert not process.is_alive()
        launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
        launch(
            fixture["home"],
            fixture["vault"],
            fixture["policy"],
            fixture["task_id"],
            ROOT,
            cleanup=True,
        )
        count = facts(fixture)[2]
        with pytest.raises(
            ValueError, match="active or uncertain|outcome uncertain|intent unresolved"
        ):
            module["sleep"]()
        assert not calls
        assert facts(fixture)[2] == count
        assert lifecycle.clear_after_wake(fixture["home"], ROOT, image=IMAGE)
        retain(
            fixture,
            "service-" + window,
            zero_stop_calls=True,
            services_before=services_before,
            services_after_refusal=module["containers"](),
            fence_generation=next(
                r["generation"] for r in records if r["kind"] == "sleep-fence"
            ),
        )
    finally:
        stop(process, parent, child)
        unwind()


def scout_child(fixture):
    runner = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
    backend = runner["DockerScout"]
    original = backend.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        config = json.loads(self.config_path.read_text())
        config["services"]["scout"]["command"] = [
            (
                "import sys,time; sys.path.insert(0,'/opt/emuru/src'); "
                "import emuru.projects.scout as s; original=s.inspect_snapshot; "
                "s.inspect_snapshot=lambda path: (time.sleep(5),original(path))[1]; s.worker_main()"
            )
        ]
        self.config_path.write_text(json.dumps(config))

    backend.__init__ = initialize
    result = runner["launch"](
        fixture["home"],
        fixture["scout_policy"],
        fixture["task_id"],
        ROOT,
        vault=fixture["vault"],
    )
    (fixture["root"] / "scout-result.json").write_text(json.dumps(result))
    assert result["status"] == "done"


@pytest.mark.parametrize("death", [False, True])
def test_actual_scout_sleep_refusal_then_drain_or_launcher_death(
    native_fixture, monkeypatch, death
):
    fixture = native_fixture
    grant = fixture["grant"]
    policy = {
        "schema": 1,
        "repository": grant["repository"],
        "path": grant["path"],
        "baseline": grant["baseline"],
        "files": grant["read_files"],
        "tasks": [fixture["task_id"]],
        "timeout_seconds": 120,
    }
    path = fixture["root"] / "scout-policy.json"
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    fixture["scout_policy"] = path
    supervisor.enroll(
        fixture["home"],
        fixture["vault"],
        ROOT,
        "scout",
        path,
        fixture["task_id"],
        IMAGE,
        owner=42,
        bot=7,
    )
    module, calls, unwind = service(fixture, monkeypatch)
    services_before = module["containers"]()
    assert set(services_before) == {"emuru", "litellm", "ollama"}
    assert all(r["status"] == "running" for r in services_before.values())
    process = multiprocessing.get_context("fork").Process(
        target=scout_child, args=(fixture,)
    )
    process.start()
    try:
        limit = time.monotonic() + 90
        identity = None
        while time.monotonic() < limit:
            assert process.is_alive(), process.exitcode
            records = lifecycle.snapshot(fixture["home"])
            plans = {r["name"]: r for r in records if r.get("phase") == "planned"}
            for created in (r for r in records if r.get("phase") == "created"):
                if plans[created["name"]]["service"] != "scout":
                    continue
                result = subprocess.run(
                    ["docker", "inspect", created["container_id"]],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                actual = json.loads(result.stdout)[0]
                if actual["State"]["Running"]:
                    subprocess.run(
                        [
                            "docker",
                            "kill",
                            "--signal",
                            "SIGSTOP",
                            created["container_id"],
                        ],
                        check=True,
                        capture_output=True,
                    )
                    identity = created["container_id"]
                    break
            if identity:
                break
            time.sleep(0.05)
        assert identity
        with pytest.raises((ValueError, BlockingIOError)):
            module["sleep"]()
        assert not calls
        if death:
            process.kill()
            process.join(10)
            with pytest.raises(ValueError, match="active or uncertain"):
                module["sleep"]()
            assert not calls
            assert "stop_requested" in control(fixture, "cancel")
            cleanup = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))["launch"]
            cleanup(
                fixture["home"],
                path,
                fixture["task_id"],
                ROOT,
                vault=fixture["vault"],
                cleanup=True,
            )
            assert lifecycle.wake_inspection(fixture["home"], ROOT)
        else:
            subprocess.run(
                ["docker", "kill", "--signal", "SIGCONT", identity],
                check=True,
                capture_output=True,
            )
            process.join(30)
            assert process.exitcode == 0
            assert module["containers"]() == services_before
            module["sleep"](remove=True)
            assert calls == [
                ("stop", "--timeout", "660", "emuru"),
                ("stop", "litellm", "ollama"),
                ("down", "--remove-orphans"),
            ]
            assert module["containers"]() == {}
            assert lifecycle.wake_inspection(fixture["home"], ROOT) is None
            assert (
                lifecycle.clear_after_wake(fixture["home"], ROOT, image=IMAGE) is None
            )
            assert facts(fixture)[0]["sleep_fence"] is False
        retain(
            fixture,
            "service-scout-" + ("death" if death else "drain"),
            zero_stop_calls_before_refusal=True,
            ordered_idle_stops=calls,
            services_before=services_before,
            services_after=module["containers"](),
            exact_worker=identity,
        )
    finally:
        unwind()
        if process.is_alive():
            process.kill()
            process.join(10)
        if process.exitcode != 0:
            control(fixture, "cancel")
            cleanup = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))["launch"]
            cleanup(
                fixture["home"],
                path,
                fixture["task_id"],
                ROOT,
                vault=fixture["vault"],
                cleanup=True,
            )


def test_active_native_rollback_and_mutation_refuse_without_state_loss(native_fixture):
    from test_coder_supervisor_native import crash

    from emuru import deployment

    fixture = native_fixture
    process = multiprocessing.get_context("fork").Process(
        target=crash, args=(fixture, "claim")
    )
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(10)
        pytest.fail("Native claim barrier stalled")
    assert process.exitcode == 17
    assert facts(fixture)[2] == 1
    old = fixture["root"] / "old-executor"
    before = files(fixture["home"])
    for operation in (
        lambda: deployment.check_review_state(fixture["home"], old),
        lambda: deployment.sync_profile(old, fixture["home"], apply=True),
    ):
        with pytest.raises(ValueError, match="incompatible.*rollback"):
            operation()
        assert files(fixture["home"]) == before
    retain(
        fixture,
        "service-native-rollback",
        unchanged_files={
            name: hashlib.sha256(data).hexdigest() for name, data in before.items()
        },
        native_claims=1,
    )
