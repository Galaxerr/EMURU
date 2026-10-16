"""Disposable lifecycle state and strictly observational status checks."""

import json
import os
import runpy
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from emuru.models import observations
from emuru.models.gateway import write_private
from emuru.telegram.queue import TelegramQueue

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    globals_ = module["status"].__globals__
    metadata = tmp_path / "deployment.env"
    monkeypatch.setitem(globals_, "DEPLOYMENT_ENV", metadata)
    monkeypatch.delenv("EMURU_CONTAINER_HOME", raising=False)
    home = tmp_path / "home"
    values = module["prepare_deployment"](home, tmp_path / "absent-vault", 42)
    write_private(
        metadata, "".join(f"{key}={value}\n" for key, value in values.items())
    )
    route = {
        "schema_version": 1,
        "primary": {"provider": "ollama", "model": "cloud"},
        "fallback": {
            "provider": "ollama",
            "model": "local",
            "digest": "a" * 64,
            "context_tokens": 65536,
            "output_reserve_tokens": 4096,
            "qualification": None,
        },
    }
    write_private(values["EMURU_ROUTE"], json.dumps(route))
    policy = json.loads((ROOT / "infra/hermes/telegram-settings.json").read_text())
    queue = TelegramQueue(
        Path(values["EMURU_PROFILE"]) / "emuru-telegram/queue.sqlite3", policy, 42
    )
    now = time.time()
    items = {
        name: {
            "id": name + "-id",
            "instance": name + "-host",
            "service": name,
            "project": "emuru",
            "oneoff": "False",
            "status": "running",
            "started": datetime.fromtimestamp(now - 10, UTC).isoformat(),
            "health": "healthy",
        }
        for name in module["SERVICES"]
    }
    record = {
        "at": now,
        "instance": "litellm-host",
        "route": observations.fingerprint(route),
        "state": "LOCAL_UNQUALIFIED",
        "used": "primary",
    }
    logs = {"emuru-id": module["READY_CHECKPOINT"] + "\n", "litellm-id": ""}
    calls = []
    data = NS(
        module=module,
        globals=globals_,
        home=home,
        queue=queue,
        items=items,
        record=record,
        logs=logs,
        calls=calls,
        fail=None,
        extra=[],
        route=route,
    )

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs.get("capture_output") and kwargs.get("check")
        if data.fail:
            raise subprocess.CalledProcessError(
                1, command, stderr="SENSITIVE_TOKEN /vault/secret"
            )
        if command[:2] == ["docker", "inspect"]:
            return NS(
                stderr="",
                stdout="\n".join(
                    json.dumps(item) for item in [*items.values(), *data.extra]
                ),
            )
        if command[:2] == ["docker", "logs"]:
            if command[-1] == "litellm-id":
                return NS(
                    stderr="",
                    stdout=observations.PREFIX
                    + json.dumps(record)
                    + "\n"
                    + logs["litellm-id"],
                )
            return NS(stderr="", stdout=logs[command[-1]])
        assert command[:2] == ["docker", "compose"]
        if "ps" in command:
            return NS(
                stderr="", stdout="\n".join(item["id"] for item in items.values())
            )
        if "stop" in command:
            for name in command[command.index("stop") + 1 :]:
                if name in items:
                    items[name]["status"] = "exited"
            return NS(stderr="", stdout="")
        pytest.fail("Status crossed its read-only boundary: " + repr(command))

    monkeypatch.setattr(module["subprocess"], "run", run)
    try:
        yield data
    finally:
        queue.close()


@pytest.mark.parametrize(
    "state",
    ["READY", "LOCAL_UNQUALIFIED", "DEGRADED_PRIMARY", "UNAVAILABLE", "UNKNOWN"],
)
def test_current_observed_states(deployment, monkeypatch, state):
    d = deployment
    d.record["state"] = state
    d.record["used"] = "local" if state == "DEGRADED_PRIMARY" else "primary"
    monkeypatch.setitem(d.globals, "valid_qualification", lambda route: True)
    report = d.module["status"](d.home)
    assert report["state"] == state
    assert report["last_used"]["route"] == d.record["used"]
    assert all("stop" not in call and "run" not in call for call in d.calls)


@pytest.mark.parametrize(
    "change", ["old", "future", "instance", "route", "pre_restart", "malformed"]
)
def test_stale_or_unbound_observations_are_unknown(deployment, change):
    d = deployment
    if change == "old":
        d.items["litellm"]["started"] = datetime.fromtimestamp(
            time.time() - 1000, UTC
        ).isoformat()
        d.record["at"] -= observations.FRESH_SECONDS + 1
    elif change == "future":
        d.record["at"] += 1000
    elif change in {"instance", "route"}:
        d.record[change] = "previous-deployment"
    elif change == "pre_restart":
        d.record["at"] -= 60
    else:
        d.record["state"] = {"secret": "TOKEN"}
    report = d.module["status"](d.home)
    assert report["state"] == "UNKNOWN"
    assert (report["last_used"] is not None) == (change in {"old", "pre_restart"})


def test_last_used_is_separate_from_current_failure(deployment):
    d = deployment
    d.logs["litellm-id"] = observations.PREFIX + json.dumps(
        {**d.record, "at": time.time(), "state": "UNAVAILABLE", "used": None}
    )
    report = d.module["status"](d.home)
    assert report["state"] == "UNAVAILABLE"
    assert report["last_used"]["route"] == "primary"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("stopped", "STOPPED"),
        ("absent", "STOPPED"),
        ("worker_absent", "UNAVAILABLE"),
        ("unhealthy", "UNAVAILABLE"),
        ("not_guarded", "UNKNOWN"),
        ("draining", "UNAVAILABLE"),
        ("daemon_failed", "UNKNOWN"),
        ("duplicate", "UNKNOWN"),
    ],
)
def test_observable_service_states(deployment, case, expected):
    d = deployment
    if case == "stopped":
        for item in d.items.values():
            item["status"] = "exited"
    elif case == "absent":
        d.items.clear()
    elif case == "worker_absent":
        del d.items["emuru"]
    elif case == "unhealthy":
        d.items["litellm"]["health"] = "unhealthy"
    elif case == "not_guarded":
        d.logs["emuru-id"] = "old unrelated READY"
    elif case == "draining":
        d.logs["emuru-id"] += "EMURU Telegram checkpoint: intake STOPPED\n"
    elif case == "daemon_failed":
        d.fail = True
    else:
        d.extra.append({**d.items["emuru"], "id": "second-poller"})
    report = d.module["status"](d.home)
    assert report["state"] == expected
    assert "SENSITIVE" not in json.dumps(report)
    if case == "stopped":
        assert report["last_used"]["route"] == "primary"


@pytest.mark.parametrize("old", [True, False])
def test_process_health_is_bound_to_current_worker_start(deployment, old):
    d = deployment
    health = d.queue.path.parent / "health.json"
    write_private(health, json.dumps({"health": ["consumer_failed"]}))
    if old:
        os.utime(health, (1, 1))
    assert d.module["status"](d.home)["state"] == (
        "LOCAL_UNQUALIFIED" if old else "UNAVAILABLE"
    )


def test_status_preserves_all_files_and_redacts_receipt_content(deployment):
    d = deployment
    d.queue.db.execute(
        "INSERT INTO updates(bot_id,update_id,chat_id,user_id,message_id,message_date,received_at,state,text) VALUES(7,1,42,42,1,1,1,'started','SENSITIVE_NOTE')"
    )
    d.queue.db.commit()
    d.queue.close()

    def snapshot():
        return {
            p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in d.home.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    for _ in range(2):
        report = d.module["status"](d.home)
        assert report["queue"] == {"started": 1}
        assert report["health"] == []
        assert not any(
            secret in json.dumps(report)
            for secret in ["SENSITIVE_NOTE", str(d.home), "owner", "vault"]
        )
    assert snapshot() == before
    assert not (d.home.parent / "deployment.lock").exists()


@pytest.mark.parametrize("absent", [True, False])
def test_repeated_sleep_orders_worker_before_inference(deployment, absent):
    d = deployment
    if absent:
        d.items.clear()
    for _ in range(2):
        d.module["sleep"](d.home)
    stops = [call[call.index("stop") :] for call in d.calls if "stop" in call]
    assert (
        stops
        == [["stop", "--timeout", "660", "emuru"], ["stop", "litellm", "ollama"]] * 2
    )
    assert d.module["status"](d.home)["state"] == "STOPPED"


def test_stopped_wake_sleep_and_simulated_reboot_preserve_state(
    deployment, monkeypatch, capsys
):
    from telegram_helpers import update

    d = deployment
    monkeypatch.setitem(d.globals, "saved_deployment", lambda home: {})
    monkeypatch.setitem(d.globals, "valid_qualification", lambda route: True)
    monkeypatch.setitem(d.globals, "prepare_update", lambda command, values: {})
    monkeypatch.setitem(d.globals, "verify_release", lambda command, env: None)
    monkeypatch.setitem(d.globals, "record_release", lambda values, env: None)
    d.record["state"] = "READY"
    for item in d.items.values():
        item["status"] = "exited"
    for relative in ("profiles/emuru/history.json", "ollama/cache-identity"):
        write_private(d.home / relative, "synthetic-persistent-state")
    d.queue.clock = lambda: 100000
    d.queue.stage(7, [update(1), update(2), update(3)])
    completed = d.queue.claim(7)
    d.queue.finish(completed["sequence"], True)
    d.queue.claim(7)
    d.queue.mark_tools_started(7, 2)
    d.queue.close()
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
        for p in d.home.rglob("*")
        if p.is_file()
    }
    run = d.module["subprocess"].run
    starts = []

    def lifecycle(command, **kwargs):
        if command[0] == "systemctl":
            return NS(returncode=3, stdout="inactive\n")
        if "ps" in command and "--format" in command:
            return NS(stdout='[{"State":"exited"}]')
        if "ps" in command and "--services" in command:
            return NS(
                stdout="emuru\n" if d.items["emuru"]["status"] == "running" else ""
            )
        if "up" in command:
            services = [
                name for name in d.items if name in command[command.index("up") + 1 :]
            ]
            starts.append(services)
            for name in services:
                assert name != "emuru" or all(
                    d.items[upstream]["status"] == "running"
                    for upstream in ("ollama", "litellm")
                )
                d.items[name]["status"] = "running"
            return NS(returncode=0)
        if "run" in command:
            assert d.items["emuru"]["status"] == "exited"
            return NS(returncode=0)
        if "logs" in command and command[0:2] == ["docker", "compose"]:
            assert "--since" in command
            return NS(stdout=d.module["READY_CHECKPOINT"])
        return run(command, **kwargs)

    monkeypatch.setattr(d.module["subprocess"], "run", lifecycle)
    for cycle in range(3):
        assert d.module["status"](d.home)["state"] == "STOPPED"
        d.module["wake"](d.home)
        assert "EMURU READY" in capsys.readouterr().out
        assert d.module["status"](d.home)["state"] == "READY"
        d.module["wake"](d.home)
        assert len(starts) == 2 * (cycle + 1)
        d.module["sleep"](d.home)
        if cycle == 1:
            # Simulate host restart: new process/module, same stopped disk state.
            fresh = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
            fresh["wake"].__globals__.update(
                {
                    key: d.globals[key]
                    for key in (
                        "DEPLOYMENT_ENV",
                        "saved_deployment",
                        "valid_qualification",
                        "prepare_update",
                        "verify_release",
                        "record_release",
                    )
                }
            )
            d.module, d.globals = fresh, fresh["wake"].__globals__
    assert starts == [["litellm", "ollama"], ["emuru"]] * 3
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in before
    } == before
    queue = TelegramQueue(d.queue.path, d.queue.settings, 42, clock=lambda: 100000)
    try:
        queue.recover()
        assert queue.stage(7, [update(1), update(2), update(3)]) == [
            "completed",
            "interrupted",
            "queued",
        ]
        assert queue.claim(7)["update_id"] == 3
        assert queue.claim(7) is None
        assert "Some actions may already have completed" in queue.terminal_notice(7, 2)
    finally:
        queue.close()


def test_unconfirmed_worker_stop_keeps_inference(deployment, monkeypatch):
    d = deployment

    def compose(*args):
        d.calls.append(args)
        return ""

    monkeypatch.setitem(d.globals, "docker_compose", compose)
    monkeypatch.setitem(d.globals, "containers", lambda: d.items)
    with pytest.raises(ValueError, match="inference retained"):
        d.module["sleep"](d.home)
    assert d.calls == [("stop", "--timeout", "660", "emuru")]


def test_expired_qualification_cannot_report_local_ready(deployment):
    d = deployment
    d.record["state"] = "READY"
    assert d.module["status"](d.home)["state"] == "LOCAL_UNQUALIFIED"
    d.record["state"] = "DEGRADED_PRIMARY"
    assert d.module["status"](d.home)["state"] == "UNAVAILABLE"


def test_production_grace_covers_maximum_owner_turn():
    import inspect

    import yaml

    from emuru.telegram.native import NativeTelegram

    assert inspect.signature(NativeTelegram).parameters["turn_timeout"].default == 600
    config = yaml.safe_load((ROOT / "infra/docker/compose.yaml").read_text())
    assert config["services"]["emuru"]["stop_grace_period"] == "660s"
    assert all(service["restart"] == "no" for service in config["services"].values())


@pytest.mark.parametrize(
    "state,expected",
    [("READY", "LOCAL_UNQUALIFIED"), ("DEGRADED_PRIMARY", "UNAVAILABLE")],
)
def test_local_outage_does_not_hide_usable_primary(
    deployment, monkeypatch, state, expected
):
    d = deployment
    d.record["state"] = state
    monkeypatch.setitem(d.globals, "valid_qualification", lambda route: True)
    d.items["ollama"]["status"] = "exited"
    assert d.module["status"](d.home)["state"] == expected


def test_local_recreation_invalidates_previous_fallback_success(
    deployment, monkeypatch
):
    d = deployment
    d.record["state"] = "DEGRADED_PRIMARY"
    d.record["used"] = "local"
    d.items["ollama"]["started"] = datetime.now(UTC).isoformat()
    monkeypatch.setitem(d.globals, "valid_qualification", lambda route: True)
    report = d.module["status"](d.home)
    assert report["state"] == "UNKNOWN"
    assert report["last_used"]["route"] == "local"


def test_route_corruption_is_a_known_blocking_failure(deployment):
    d = deployment
    write_private(d.home / "route.json", "{}")
    assert d.module["status"](d.home)["state"] == "UNAVAILABLE"


def test_snapshot_changed_during_status_is_unknown(deployment, monkeypatch):
    d = deployment
    snapshots = iter([d.items, {}])
    monkeypatch.setitem(d.globals, "containers", lambda: next(snapshots))
    assert d.module["status"](d.home)["state"] == "UNKNOWN"


def test_observations_redact_route_and_ignore_log_write_failure(capsys, monkeypatch):
    route = {"model": "SENSITIVE_MODEL", "vault": "/secret/vault"}
    observations.emit(route, "UNAVAILABLE")
    output = capsys.readouterr().out
    assert "SENSITIVE_MODEL" not in output and "/secret/vault" not in output
    assert (
        json.loads(output.removeprefix(observations.PREFIX))["state"] == "UNAVAILABLE"
    )

    def broken_log(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", broken_log)
    observations.emit(route, "READY", "primary")
