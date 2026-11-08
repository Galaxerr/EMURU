"""Service lifecycle fencing through the existing admission and deployment boundaries."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import pytest
from test_project_controls import controls  # noqa: F401
from test_reviewer_supervisor import checked  # noqa: F401
from test_scout_supervisor import registered_scout  # noqa: F401
from test_supervisor import ROOT, durable, enrollment, scout_enrollment  # noqa: F401

from emuru.projects import coder_board, supervisor, supervisor_coder


def test_sleep_fence_precedes_inspection_and_blocks_coder(enrollment):
    from emuru.projects import supervisor_lifecycle as lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    grant = _grant
    lifecycle.fence(home)
    before = durable(home)
    with pytest.raises(ValueError, match="sleep fence"):
        supervisor_coder.reserve(home, home, ROOT, grant, "sha256:" + "a" * 64)
    assert durable(home) == before
    lifecycle.fence(home)
    assert durable(home) == before


@pytest.fixture(params=["scout", "coder", "reviewer"])
def admission(request):
    from emuru.projects import supervisor_reviewer, supervisor_scout

    image = "sha256:" + "a" * 64
    if request.param == "scout":
        home, grant, card, policy, worker = request.getfixturevalue("registered_scout")
        launch = lambda: supervisor_scout.reserve(
            home, home, ROOT, policy, card.id, image, worker
        )
    elif request.param == "reviewer":
        coder, card = request.getfixturevalue("checked")
        home, grant = coder.home, coder.grant
        launch = lambda: supervisor_reviewer.reserve(
            home, home, ROOT, grant, coder.stage, image
        )
    else:
        home, policy, grant, card = request.getfixturevalue("enrollment")
        supervisor.enroll(home, home, ROOT, "coder", policy, card.id, image)
        launch = lambda: supervisor_coder.reserve(home, home, ROOT, grant, image)
    return home, launch, request.param


@pytest.mark.parametrize("first", ["fence", "admission"])
def test_sleep_admission_race_both_orders(admission, monkeypatch, first):
    import threading

    from emuru.projects import supervisor_lifecycle as lifecycle

    home, launch, kind = admission
    entered, release = threading.Event(), threading.Event()
    append = supervisor._append
    results = {}

    def blocked(folder, records, size, value, **kwargs):
        target = (
            value["kind"] == "sleep-fence"
            if first == "fence"
            else value.get("phase") == "admitted"
        )
        if target:
            entered.set()
            assert release.wait(5)
        return append(folder, records, size, value, **kwargs)

    def run(name, operation):
        try:
            results[name] = operation()
        except (ValueError, OSError, AssertionError) as error:
            results[name] = error

    monkeypatch.setattr(supervisor, "_append", blocked)
    operations = {"fence": lambda: lifecycle.fence(home), "admission": launch}
    second = "admission" if first == "fence" else "fence"
    one = threading.Thread(target=run, args=(first, operations[first]))
    two = threading.Thread(target=run, args=(second, operations[second]))
    one.start()
    assert entered.wait(5)
    two.start()
    release.set()
    one.join(10)
    two.join(10)
    assert not one.is_alive() and not two.is_alive()
    assert results["fence"] is None
    assert lifecycle.fenced(lifecycle.snapshot(home))
    if first == "fence":
        assert isinstance(results["admission"], ValueError)
        assert "sleep fence" in str(results["admission"])
    else:
        assert results["admission"]["phase"] == "admitted", (kind, results)
    before = durable(home)
    if first == "fence" and kind != "reviewer":
        with lifecycle.sleeping(home, ROOT):
            pass
    else:
        with (
            pytest.raises(ValueError, match="active or uncertain"),
            lifecycle.sleeping(home, ROOT),
        ):
            pytest.fail("Service stop reached active or uncertain work")
    assert durable(home) == before


def test_wake_clears_only_fence_after_idle_and_keeps_history(controls):
    from emuru.projects import supervisor_lifecycle as lifecycle

    home, grant, card, _queue, deliver = controls
    detail = supervisor.status(home, ROOT, grant["project"], card)
    deliver("hold", detail["token"], 1)
    lifecycle.fence(home)
    records = lifecycle.snapshot(home)
    assert lifecycle.clear_after_wake(home, ROOT, image="sha256:" + "a" * 64) is None
    assert lifecycle.snapshot(home)[:-1] == records
    detail = supervisor.status(home, ROOT, grant["project"], card)
    assert detail["control"] == "held"
    assert detail["reserved"] == dict.fromkeys(supervisor.COUNTERS, 0)
    assert detail["sleep_fence"] is False
    assert detail["execution"] == "unstarted"


@pytest.mark.parametrize("failure", ["source", "inspection", "lock", "interrupted"])
def test_failed_or_uncertain_wake_retains_fence(enrollment, monkeypatch, failure):
    from emuru.projects import supervisor_lifecycle as lifecycle
    from emuru.projects.dispatcher import dispatcher_lock

    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    lifecycle.fence(home)
    before = durable(home)
    if failure == "source":
        monkeypatch.setattr(supervisor, "identity", lambda *_: {})
    elif failure == "inspection":
        monkeypatch.setattr(
            coder_board,
            "ensure_idle",
            lambda *_: (_ for _ in ()).throw(OSError("inspect failed")),
        )
    elif failure == "interrupted":
        monkeypatch.setattr(
            lifecycle,
            "inspect_idle",
            lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            lifecycle.clear_after_wake(home, ROOT, image="sha256:" + "a" * 64)
        assert durable(home) == before
        return
    if failure == "lock":
        with dispatcher_lock(home):
            assert lifecycle.clear_after_wake(home, ROOT, image="sha256:" + "a" * 64)
    else:
        assert lifecycle.clear_after_wake(home, ROOT, image="sha256:" + "a" * 64)
    assert durable(home) == before


def test_rollback_unknown_or_old_executor_preserves_every_byte(enrollment, tmp_path):
    from emuru.deployment import check_review_state
    from emuru.projects import supervisor_lifecycle as lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    lifecycle.fence(home)
    for name in (
        "board.sqlite",
        "board.sqlite-wal",
        "board.sqlite-shm",
        "artifact",
        "receipt",
    ):
        (home / name).write_bytes(b"newest durable state")
    before = durable(home)
    with pytest.raises(ValueError, match="incompatible.*rollback"):
        check_review_state(home, tmp_path / "old-executor")
    assert durable(home) == before
    marker = home / "emuru-projects/supervisor-enabled.json"
    marker.write_text('{"schema":99}')
    before = durable(home)
    with pytest.raises(ValueError, match="schema"):
        check_review_state(home, ROOT)
    assert durable(home) == before


@pytest.mark.parametrize("remove", [False, True])
def test_sleep_refuses_all_stage_work_before_any_service_io(
    admission, tmp_path, monkeypatch, remove
):
    import runpy

    from emuru.projects import supervisor_lifecycle as lifecycle

    home, launch, _kind = admission
    launch()
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["sleep"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", tmp_path / "deployment.env")
    monkeypatch.setitem(
        scope, "deployment_metadata", lambda *_: {"EMURU_PROFILE": str(home)}
    )
    monkeypatch.setitem(
        scope, "docker_compose", lambda *_: pytest.fail("Service I/O before idle proof")
    )
    with pytest.raises(ValueError, match="active or uncertain"):
        module["sleep"](remove=remove)
    assert lifecycle.fenced(lifecycle.snapshot(home))


@pytest.mark.parametrize("interrupted", [False, True])
def test_idle_sleep_releases_short_lock_and_retains_fence_on_failure(
    enrollment, tmp_path, monkeypatch, interrupted
):
    import fcntl
    import runpy

    from emuru.projects import supervisor_lifecycle as lifecycle
    from emuru.projects.dispatcher import dispatcher_lock

    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["sleep"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", tmp_path / "deployment.env")
    monkeypatch.setitem(
        scope, "deployment_metadata", lambda *_: {"EMURU_PROFILE": str(home)}
    )
    monkeypatch.setitem(scope, "containers", dict)
    calls = []

    def stop(*args):
        assert lifecycle.fenced(lifecycle.snapshot(home))
        with (home / "emuru-projects/supervisor/control.lock").open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError), dispatcher_lock(home):
            pytest.fail("Service stop lost lifetime exclusion")
        calls.append(args)
        if interrupted:
            raise KeyboardInterrupt

    monkeypatch.setitem(scope, "docker_compose", stop)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            module["sleep"]()
        assert calls == [("stop", "--timeout", "660", "emuru")]
    else:
        module["sleep"](remove=True)
        assert calls == [
            ("stop", "--timeout", "660", "emuru"),
            ("stop", "litellm", "ollama"),
            ("down", "--remove-orphans"),
        ]
    assert lifecycle.fenced(lifecycle.snapshot(home))


@pytest.mark.parametrize(
    "stderr,exit_code", [("daemon unavailable", 1), ("", 0), ("", 1)]
)
def test_resource_inspection_failure_never_proves_stop(monkeypatch, stderr, exit_code):
    from emuru.projects import coder_sandbox
    from emuru.projects import supervisor_lifecycle as lifecycle

    monkeypatch.setattr(
        coder_sandbox,
        "bounded_process",
        lambda *_: {"exit_code": exit_code, "stderr": stderr},
    )
    with pytest.raises(ValueError, match="stop unproven"):
        lifecycle.require_absent("container", "a" * 64)


def test_wake_image_mismatch_preserves_fence(enrollment):
    from emuru.projects import supervisor_lifecycle as lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    lifecycle.fence(home)
    before = durable(home)
    assert lifecycle.clear_after_wake(home, ROOT, image="sha256:" + "b" * 64)
    assert durable(home) == before


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("image_matches", [True, False])
def test_wake_checks_actual_release_image_before_clearing(
    enrollment, tmp_path, monkeypatch, capsys, running, image_matches
):
    import runpy
    from types import SimpleNamespace

    from emuru.projects import supervisor_lifecycle as lifecycle

    home, policy, _grant, card = enrollment
    enrolled_image = "sha256:" + "a" * 64
    actual_image = enrolled_image if image_matches else "sha256:" + "b" * 64
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, enrolled_image)
    lifecycle.fence(home)
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["wake"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", tmp_path / "deployment.env")
    monkeypatch.setitem(
        scope, "saved_deployment", lambda *_: {"EMURU_PROFILE": str(home)}
    )
    monkeypatch.setitem(scope, "ensure_image", lambda **_: ("f" * 64, actual_image))
    monkeypatch.setitem(
        scope,
        "prepare_update",
        lambda *_: {"EMURU_SOURCE_ROOT": str(ROOT), "EMURU_IMAGE": actual_image},
    )
    monkeypatch.setitem(scope, "verify_release", lambda *_: None)
    monkeypatch.setitem(scope, "record_release", lambda *_: None)
    # Work may drain between the first observation and the final release check.
    monkeypatch.setattr(
        lifecycle, "wake_inspection", lambda *_: "initial fence retained"
    )
    worker_running = running
    calls = []

    def run(command, **_kwargs):
        nonlocal worker_running
        calls.append(command)
        output = ""
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=3, stdout="inactive")
        if "--services" in command:
            output = "emuru" if worker_running else ""
        elif "up" in command and command[-1] == "emuru":
            worker_running = True
        elif "logs" in command:
            output = module["READY_CHECKPOINT"]
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(module["subprocess"], "run", run)
    module["wake"]()
    output = capsys.readouterr().out
    assert lifecycle.fenced(lifecycle.snapshot(home)) is not image_matches
    assert ("fence retained" in output) is not image_matches
    assert not any("scout.py" in str(call) or "coder.py" in str(call) for call in calls)


def test_full_ordinary_metadata_retains_sleep_hold_and_cancel_capacity(
    controls, monkeypatch
):
    from emuru.projects import supervisor_lifecycle as lifecycle

    home, grant, card, _queue, deliver = controls
    with supervisor._locked(home) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
    monkeypatch.setattr(supervisor, "METADATA_BYTES", size + supervisor.EMERGENCY_BYTES)
    detail = supervisor.status(home, ROOT, grant["project"], card)
    before = durable(home)
    # An ordinary receipt cannot consume the reserved restrictive-control space.
    with (
        supervisor._locked(home, write=True),
        pytest.raises(ValueError, match="admission exhausted"),
    ):
        supervisor._append(
            home / "emuru-projects/supervisor",
            records,
            size,
            {
                "kind": "control-receipt",
                "request": "a" * 32,
                "text_hash": "b" * 64,
                "response": "unchanged",
            },
        )
    assert durable(home) == before
    lifecycle.fence(home)
    assert "held" in deliver("hold", detail["token"], 1)[0]
    detail = supervisor.status(home, ROOT, grant["project"], card)
    assert "revoked" in deliver("cancel", detail["token"], 2)[0]
    final = supervisor.status(home, ROOT, grant["project"], card)
    assert final["sleep_fence"] and final["control"] == "revoked"
    assert final["reserved"] == dict.fromkeys(supervisor.COUNTERS, 0)
