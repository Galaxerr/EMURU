"""Attempt deadlines cover gateway startup and inference, with cleanup grace."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from emuru.projects import coder_gateway, coder_inference


def test_expired_attempt_cannot_start_inference(monkeypatch):
    monkeypatch.setattr(coder_inference.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        coder_inference.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("expired attempt started inference"),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        coder_inference.run_inference(
            "sha256:" + "a" * 64,
            "fixture-network",
            "fixture-key",
            {"prompt": "fixture", "deadline_seconds": 600},
            lambda *_: None,
            deadline=9.0,
        )


def test_gateway_startup_uses_attempt_budget_and_cleanup_has_grace(
    monkeypatch, tmp_path
):
    now = [10.0]
    calls = []
    monkeypatch.setattr(coder_gateway.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        coder_gateway.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(coder_gateway, "ROOT", tmp_path)
    monkeypatch.setattr(
        coder_gateway, "read_route", lambda *_: {"primary": {"provider": "ollama"}}
    )
    monkeypatch.setattr(
        coder_gateway, "read_env", lambda *_: {"OLLAMA_API_KEY": "synthetic"}
    )
    monkeypatch.setattr(coder_gateway, "render", lambda *_: "synthetic")
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / "infra/docker/deployment-lock.json").write_text('{"litellm":"fixture"}')

    def process(command, payload, seconds, output_bytes):
        calls.append((command, seconds, now[0]))
        if command[1] == "exec":
            now[0] += seconds
            return {"exit_code": 1, "stdout": "", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(coder_gateway, "bounded_process", process)
    with (
        pytest.raises(TimeoutError, match="deadline"),
        coder_gateway.cloud_gateway(tmp_path / "gateway", deadline=12.0),
    ):
        pytest.fail("expired startup yielded inference authority")
    startup = calls[
        : next(i for i, call in enumerate(calls) if call[0][1] == "exec") + 1
    ]
    assert all(seconds <= 12.0 - started for _, seconds, started in startup)
    assert any(started >= 12.0 and seconds > 0 for _, seconds, started in calls)
    assert not (tmp_path / "gateway/provider.key").exists()
    assert not (tmp_path / "gateway/gateway.key").exists()


def test_launcher_shares_original_deadline_after_gateway_startup(monkeypatch, tmp_path):
    import contextlib
    import runpy

    launch = runpy.run_path("scripts/hermes/coder.py")["launch"]
    namespace = launch.__globals__
    grant = {
        "task": "t_fixture",
        "read_files": ["sum_prices.py"],
        "write_files": ["sum_prices.py"],
        "create_files": [],
        "limits": {"attempt_seconds": 600, "model_iterations": 12},
    }
    folder = tmp_path / "attempt"
    (tmp_path / ".runtime").mkdir()
    monkeypatch.setitem(namespace, "private_path", lambda path: path)
    monkeypatch.setitem(namespace, "load_grant", lambda _: grant)
    monkeypatch.setitem(
        namespace, "dispatcher_lock", lambda _: contextlib.nullcontext()
    )
    monkeypatch.setattr(namespace["os"], "getuid", lambda: 1000)
    monkeypatch.setattr(namespace["os"], "getgid", lambda: 1000)
    monkeypatch.setattr(
        namespace["runpy"],
        "run_path",
        lambda _: {"ensure_image": lambda **_: ("source", "image")},
    )
    controller = namespace["coder_board"]
    for name in ("authorize", "ensure_idle", "record_launcher", "_write_new"):
        monkeypatch.setattr(controller, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_find_attempt", lambda *_: None)
    monkeypatch.setattr(controller, "attempt_path", lambda *_: folder)
    monkeypatch.setattr(controller, "create_attempt", lambda *_: folder)
    monkeypatch.setattr(controller, "claim", lambda *_: {"run": 1})
    monkeypatch.setitem(namespace, "_task", lambda *_: {})
    monkeypatch.setitem(namespace, "DockerSandbox", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        namespace,
        "Qualification",
        lambda *args, **kwargs: SimpleNamespace(
            deadline=12.0, run=lambda native, _: native(None)
        ),
    )
    observed = []

    @contextlib.contextmanager
    def gateway(*args, deadline, **kwargs):
        observed.append(("gateway", deadline))
        yield "network", "key", "provider"

    def inference(*args, deadline, **kwargs):
        observed.append(("inference", deadline))
        raise TimeoutError("fixture deadline")

    monkeypatch.setitem(namespace, "cloud_gateway", gateway)
    monkeypatch.setitem(namespace, "run_inference", inference)
    with (
        patch.dict("os.environ"),
        pytest.raises(TimeoutError, match="fixture deadline"),
    ):
        launch(
            tmp_path / "home",
            tmp_path / "vault",
            tmp_path / "grant",
            "t_fixture",
            tmp_path,
        )
    assert observed == [("gateway", 12.0), ("inference", 12.0)]


def test_inference_container_timeout_uses_remaining_attempt_budget(monkeypatch):
    monkeypatch.setattr(coder_inference.time, "monotonic", lambda: 100.0)

    def launch(command, **kwargs):
        assert command[command.index("--signal=KILL") + 1] == "0.25"
        raise RuntimeError("fixture launch inspected")

    monkeypatch.setattr(coder_inference.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="fixture launch inspected"):
        coder_inference.run_inference(
            "sha256:" + "a" * 64,
            "fixture-network",
            "fixture-key",
            {"prompt": "fixture", "deadline_seconds": 600},
            lambda *_: None,
            deadline=100.25,
        )
