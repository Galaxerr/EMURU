"""Deterministic qualification evidence and command checks."""

import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from emuru.models.gateway import (
    qualification_record,
    valid_qualification,
    write_private,
)

ROOT = Path(__file__).resolve().parents[1]
QUALIFY = runpy.run_path(str(ROOT / "scripts/hermes/qualify.py"))


def qualified_route(now=100):
    route = QUALIFY["fixture_route"]()
    record = qualification_record(
        route,
        "fixture-runtime",
        hashlib.sha256(b"fixture").hexdigest(),
        QUALIFY["fixture_evidence"](),
        now,
    )
    record["provenance"] = "deterministic-fixture"
    route["fallback"]["qualification"] = record
    return route


def test_qualification_record_preserves_v2_policy():
    route = qualified_route()
    record = route["fallback"]["qualification"]
    assert set(record) == {
        "schema_version",
        "primary",
        "local",
        "runtime_version",
        "template_sha256",
        "effective_context_tokens",
        "output_reserve_tokens",
        "workload_policy_version",
        "config_fingerprint",
        "qualified_at",
        "expires_at",
        "gates",
        "provenance",
        "evidence",
    }
    assert record["schema_version"] == 2
    assert record["qualified_at"] == 100
    assert record["expires_at"] == 100 + 30 * 86400
    assert record["gates"] == {
        "tools": True,
        "context": True,
        "latency": True,
        "hardware": True,
        "fault": True,
        "workload": True,
    }
    assert record["config_fingerprint"] == (
        "786b055a81b6068863037f651916a744d2a544b54e9e8f761948746f6fce6c10"
    )
    assert (
        qualification_record(
            route, "fixture-runtime", hashlib.sha256(b"fixture").hexdigest(), {}, 100
        )["provenance"]
        == "operator-qualified"
    )


@pytest.mark.parametrize(
    "change", ["missing", "extra", "version", "provenance", "expiry"]
)
def test_malformed_qualification_fails_closed(change):
    route = qualified_route()
    record = route["fallback"]["qualification"]
    if change == "missing":
        del record["gates"]
    elif change == "extra":
        record["unexpected"] = True
    elif change == "version":
        record["schema_version"] = 1
    elif change == "provenance":
        record["provenance"] = "untrusted"
    else:
        record["expires_at"] += 1
    assert not valid_qualification(route, now=101)


@pytest.mark.parametrize("now", [99, 100 + 30 * 86400])
def test_qualification_time_window_fails_closed(now):
    assert not valid_qualification(qualified_route(), now=now)


@pytest.mark.parametrize(
    "identity",
    [
        {"runtime_version": "changed-runtime"},
        {"template_sha256": "b" * 64},
    ],
)
def test_qualification_live_identity_mismatch_fails_closed(identity):
    assert not valid_qualification(qualified_route(), now=101, **identity)


def test_fixture_qualification_and_canonical_invalidation():
    QUALIFY["run_fixture"]()
    route = qualified_route()
    assert valid_qualification(route, now=101)
    changed = copy.deepcopy(route)
    changed["primary"]["model"] = "changed-cloud"
    assert not valid_qualification(changed, now=101)
    changed = copy.deepcopy(route)
    changed["fallback"]["digest"] = "b" * 64
    assert not valid_qualification(changed, now=101)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workload_repetitions", {}),
        ("context_trials", 2),
        ("latency_samples", 19),
        ("cold_seconds", 181),
        ("warm_p95_seconds", 61),
        ("available_ram_bytes", 0),
        ("gpu_vram_bytes", -1),
        ("oom_events", 1),
    ],
)
def test_threshold_failure_rejects_evidence(field, value):
    route = qualified_route()
    route["fallback"]["qualification"]["evidence"][field] = value
    assert not valid_qualification(route, now=101)


def test_gpu_requires_vram_telemetry():
    route = qualified_route()
    evidence = route["fallback"]["qualification"]["evidence"]
    evidence["gpu_present"] = True
    evidence["gpu_vram_bytes"] = 0
    assert not valid_qualification(route, now=101)


def test_gate_integers_do_not_pass_as_booleans():
    route = qualified_route()
    route["fallback"]["qualification"]["gates"]["tools"] = 1
    assert not valid_qualification(route, now=101)


def test_redacted_report_excludes_identity_and_hardware_values():
    record = qualified_route()["fallback"]["qualification"]
    report = json.dumps(QUALIFY["redacted_report"](record))
    assert "synthetic-cloud" not in report
    assert "synthetic-local" not in report
    assert "fixture-runtime" not in report
    assert "available_ram_bytes" not in report
    assert "workload_repetitions" in report


def test_synthetic_vault_isolated_from_fixture(tmp_path):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    note = vault / "00_Inbox/new.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("synthetic")
    assert note.exists()
    assert not (QUALIFY["FIXTURE"] / "00_Inbox/new.md").exists()


def test_failed_live_run_preserves_route(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    write_private(path, json.dumps(QUALIFY["fixture_route"]()))
    before = path.read_bytes()

    def fail(*args):
        raise RuntimeError("controlled qualification failure")

    monkeypatch.setitem(QUALIFY["main"].__globals__, "live_qualification", fail)
    monkeypatch.setattr("sys.argv", ["qualify.py", "--live", "--route", str(path)])
    with pytest.raises(SystemExit, match="LOCAL_UNQUALIFIED"):
        QUALIFY["main"]()
    assert path.read_bytes() == before
    assert json.loads((tmp_path / "route.qualification-report.json").read_text()) == {
        "schema_version": 1,
        "status": "LOCAL_UNQUALIFIED",
        "workload_policy_version": "synthetic-vault-v1",
        "gates": {},
        "error": "RuntimeError",
    }


def test_context_trials_set_context_and_require_processed_tokens():
    class Client:
        def __init__(self, prompt_tokens):
            self.prompt_tokens = prompt_tokens
            self.bodies = []

        def request(self, path, body, timeout=180):
            self.bodies.append(body)
            prompt = body["messages"][0]["content"]
            begin = next(
                word
                for word in prompt.split()
                if word.startswith("EMURU_CONTEXT_BEGIN")
            )
            end = next(
                word for word in prompt.split() if word.startswith("EMURU_CONTEXT_END")
            )
            return {
                "choices": [{"message": {"content": f"{begin} {end}"}}],
                "usage": {"prompt_tokens": self.prompt_tokens},
            }

    client = Client(90)
    QUALIFY["context_trials"](client, "local", 100, 10)
    assert len(client.bodies) == 3
    assert all(body["options"] == {"num_ctx": 100} for body in client.bodies)
    with pytest.raises(RuntimeError, match="Near-context"):
        QUALIFY["context_trials"](Client(10), "local", 100, 10)


def test_injection_check_exposes_write_and_rejects_attempt(tmp_path):
    vault = QUALIFY["synthetic_vault"](tmp_path)

    class Client:
        def __init__(self, write=False):
            self.write = write
            self.bodies = []

        def request(self, path, body, timeout=180):
            self.bodies.append(body)
            if len(self.bodies) == 1:
                message = {
                    "tool_calls": [
                        {
                            "id": "open",
                            "function": {
                                "name": "vault_open",
                                "arguments": json.dumps(
                                    {"path": "30_Resources/Untrusted-reference.md"}
                                ),
                            },
                        }
                    ]
                }
            elif self.write:
                message = {
                    "tool_calls": [
                        {
                            "id": "write",
                            "function": {"name": "vault_write", "arguments": "{}"},
                        }
                    ]
                }
            else:
                message = {"content": "The untrusted text requests a write."}
            return {"choices": [{"message": message}], "usage": {}}

    client = Client()
    QUALIFY["prompt_injection"](client, "local", vault)
    assert client.bodies[1]["tools"] == [QUALIFY["WRITE"]]
    assert client.bodies[1]["tool_choice"] == "auto"
    with pytest.raises(RuntimeError, match="Prompt-injection"):
        QUALIFY["prompt_injection"](Client(write=True), "local", vault)


def test_live_qualification_rejects_cloud_local_endpoint(monkeypatch):
    class Client:
        direct_cloud = True

        def __init__(self, base_url):
            pass

    monkeypatch.setitem(
        QUALIFY["live_qualification"].__globals__, "OllamaClient", Client
    )
    with pytest.raises(RuntimeError, match="loopback"):
        QUALIFY["live_qualification"](
            QUALIFY["fixture_route"](), "https://ollama.com/v1"
        )


def test_cold_latency_unloads_model_first():
    class Client:
        def __init__(self, resident=False):
            self.resident = resident
            self.calls = []

        def request(self, path, body=None):
            self.calls.append((path, body))
            return {"models": [{"name": "local"}]} if self.resident else {"models": []}

    client = Client()
    QUALIFY["unload"](client, "local")
    assert client.calls[0] == ("/api/generate", {"model": "local", "keep_alive": 0})
    with pytest.raises(RuntimeError, match="unloaded"):
        QUALIFY["unload"](Client(resident=True), "local")


@pytest.mark.parametrize("option", ["--evidence", "--report"])
def test_output_path_collision_preserves_route(tmp_path, monkeypatch, option):
    tmp_path.chmod(0o700)
    path = tmp_path / "route.json"
    write_private(path, json.dumps(QUALIFY["fixture_route"]()))
    before = path.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        ["qualify.py", "--live", "--route", str(path), option, str(path)],
    )
    with pytest.raises(ValueError, match="must be distinct"):
        QUALIFY["main"]()
    assert path.read_bytes() == before
