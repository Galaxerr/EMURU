"""Deterministic qualification evidence and command checks."""

import copy
import hashlib
import json
import os
import re
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
        "84e6a40dbf975498cad8cc24c5e93748195a5c701d40f2218158256f9490108f"
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
    changed = copy.deepcopy(route)
    changed["fallback"]["qualification"]["workload_policy_version"] = (
        "synthetic-vault-v1"
    )
    assert not valid_qualification(changed, now=101)


def test_expanded_workloads_are_required_for_both_models():
    expected = {
        "fact_retrieval",
        "mcp_navigation",
        "inbox_create_search_readback",
        "project_creation",
        "anchored_task_update",
        "stale_proposal",
        "repeated_request",
        "daily_planning",
        "injection_resistance",
    }
    assert set(QUALIFY["WORKLOAD_REPETITIONS"]["cloud"]) == expected
    assert set(QUALIFY["WORKLOAD_REPETITIONS"]["local"]) == expected
    assert set(QUALIFY["WORKLOAD_REPETITIONS"]["cloud"].values()) == {5}
    assert set(QUALIFY["WORKLOAD_REPETITIONS"]["local"].values()) == {5}


@pytest.mark.parametrize("wrong_stage", [None, "stale", "creation_line"])
def test_project_workloads_apply_linked_projects_and_reject_stale_updates(
    tmp_path, monkeypatch, wrong_stage
):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    previous = {}

    def fake_chat(client, model, messages, *, tools=None, **kwargs):
        assert kwargs["max_tokens"] == 2048
        prompt = messages[0]["content"]
        if prompt.startswith("Create "):
            path = re.search(r"Create (\S+)", prompt)[1]
            task = re.search(r"and this task line: (.+)\. Use request", prompt)[1]
            request_id = re.search(r"Use request identity ([^\s.]+)", prompt)[1]
            args = {
                "path": path,
                "content": f"# Project\n[[00_Inbox/daily-briefing-idea]]\nBRIEFING_IDEA_SOURCE_742\n{task}\n",
                "expected_revision": None,
                "anchor": None,
                "replacement": None,
                "request_id": request_id,
            }
            if wrong_stage == "creation_line":
                args["content"] = args["content"].replace(task, task + ".")
            previous.update(path=path, task=task, request_id=request_id)
        elif prompt.startswith("Mark task "):
            revision = re.search(r"current full revision (\w+)", prompt)[1]
            anchor = re.search(r"exact anchor (.+), replacement", prompt)[1]
            replacement = re.search(r"replacement (.+), and request identity", prompt)[
                1
            ]
            request_id = re.search(r"and request identity ([^\s.]+)", prompt)[1]
            args = {
                "path": previous["path"],
                "content": None,
                "expected_revision": revision,
                "anchor": anchor,
                "replacement": replacement,
                "request_id": request_id,
            }
            previous["update"] = args
        elif prompt.startswith("For "):
            update = previous["update"]
            args = {
                **update,
                "expected_revision": re.search(r"stale revision (\w+)", prompt)[1],
                "anchor": update["replacement"],
                "replacement": update["anchor"],
                "request_id": re.search(r"request identity ([^\s.]+)", prompt)[1],
            }
            if wrong_stage == "stale":
                args["expected_revision"] = "0" * 64
        else:
            args = previous["update"]
        message = {
            "tool_calls": [
                {
                    "id": "proposal",
                    "function": {
                        "name": "vault_propose",
                        "arguments": json.dumps(args),
                    },
                }
            ]
        }
        return message, 0.1, {}

    monkeypatch.setitem(QUALIFY["project_workloads"].__globals__, "chat", fake_chat)
    if wrong_stage == "stale":
        with pytest.raises(
            RuntimeError, match="Stale proposal workload arguments differ"
        ):
            QUALIFY["project_workloads"](object(), "fixture", vault, home)
        return
    if wrong_stage == "creation_line":
        with pytest.raises(RuntimeError, match="Project creation workload failed"):
            QUALIFY["project_workloads"](object(), "fixture", vault, home)
        return
    durations = QUALIFY["project_workloads"](object(), "fixture", vault, home)
    assert len(durations) == 20
    assert list((vault / "10_Projects").glob("qualification-*.md"))
    assert (vault / "00_Inbox/daily-briefing-idea.md").read_bytes() == (
        QUALIFY["FIXTURE"] / "00_Inbox/daily-briefing-idea.md"
    ).read_bytes()


@pytest.mark.parametrize("field", ["expected_revision", "anchor", "replacement"])
def test_proposal_creation_contract_uses_json_null_and_rejects_string_null(
    tmp_path, field
):
    definition = QUALIFY["PROPOSE"]["function"]
    description = definition["parameters"]["properties"][field]["description"]
    assert "JSON null" in description
    assert "creation" in description
    vault = QUALIFY["synthetic_vault"](tmp_path)
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    args = dict.fromkeys(definition["parameters"]["required"])
    args.update(path="10_Projects/new.md", content="# New", request_id="create")
    args[field] = "NULL"
    message = {
        "tool_calls": [
            {"function": {"name": "vault_propose", "arguments": json.dumps(args)}}
        ]
    }
    parsed = QUALIFY["proposal_arguments"](message)
    with pytest.raises(ValueError, match="Creation accepts no update fields"):
        QUALIFY["mutations"].propose_note(home, vault, **parsed)
    assert not (vault / args["path"]).exists()
    args[field] = None
    proposal = QUALIFY["mutations"].propose_note(home, vault, **args)
    assert proposal["outcome"] == "proposed"
    assert not (vault / args["path"]).exists()


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

    monkeypatch.setitem(QUALIFY["main"].__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(QUALIFY["main"].__globals__, "live_qualification", fail)
    monkeypatch.setattr("sys.argv", ["qualify.py", "--live", "--route", str(path)])
    with pytest.raises(SystemExit, match="LOCAL_UNQUALIFIED"):
        QUALIFY["main"]()
    assert path.read_bytes() == before
    assert json.loads((tmp_path / "route.qualification-report.json").read_text()) == {
        "schema_version": 1,
        "status": "LOCAL_UNQUALIFIED",
        "workload_policy_version": "synthetic-vault-v2",
        "gates": {},
        "error": "RuntimeError",
    }


@pytest.mark.parametrize("override", [None, "process-key"])
def test_live_command_loads_workspace_env(tmp_path, monkeypatch, override):
    tmp_path.chmod(0o700)
    env = tmp_path / ".env"
    env.write_text("OLLAMA_API_KEY=file-key\n")
    env.chmod(0o600)
    path = tmp_path / "route.json"
    write_private(path, json.dumps(QUALIFY["fixture_route"]()))
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    if override:
        monkeypatch.setenv("OLLAMA_API_KEY", override)

    def fail(*args):
        assert os.environ.get("OLLAMA_API_KEY") == (override or "file-key")
        raise RuntimeError("controlled qualification failure")

    monkeypatch.setitem(QUALIFY["main"].__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(QUALIFY["main"].__globals__, "live_qualification", fail)
    monkeypatch.setattr("sys.argv", ["qualify.py", "--live", "--route", str(path)])
    with pytest.raises(SystemExit, match="controlled qualification failure"):
        QUALIFY["main"]()


def test_context_trials_set_context_and_require_processed_tokens():
    class Client:
        def __init__(self, prompt_tokens):
            self.prompt_tokens = prompt_tokens
            self.bodies = []

        def request(self, path, body, timeout=180):
            assert path == "/api/chat"
            assert timeout == 1800
            assert body["think"] is False
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
                "message": {"content": f"{begin} {end}"},
                "prompt_eval_count": self.prompt_tokens,
            }

    client = Client(90)
    QUALIFY["context_trials"](client, "local", 100, 10)
    assert len(client.bodies) == 3
    assert all(
        body["options"] == {"num_ctx": 100, "num_predict": 32, "temperature": 0}
        for body in client.bodies
    )
    with pytest.raises(RuntimeError, match="Near-context"):
        QUALIFY["context_trials"](Client(10), "local", 100, 10)


def test_tool_workload_disables_reasoning_before_token_budget_is_exhausted():
    class Client:
        def request(self, path, body, timeout=180):
            message = {"content": "", "reasoning": "Token budget exhausted"}
            if body.get("reasoning_effort") == "none":
                message = {"content": "MARKER"}
                if body.get("tools"):
                    message = {
                        "tool_calls": [
                            {
                                "id": "search",
                                "function": {
                                    "name": "vault_search",
                                    "arguments": '{"query":"EMURU"}',
                                },
                            }
                        ]
                    }
            return {"choices": [{"message": message}]}

    QUALIFY["one_tool_call"](
        Client(),
        "local",
        "Search EMURU",
        QUALIFY["SEARCH"],
        lambda args: args,
        "MARKER",
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "fenced",
        "wrong_key",
        "partial_quote",
        "wrong_path",
        "wrong_block",
        "deadline",
        "write",
        "missing_open",
    ],
)
def test_workflow_review_states_exact_contract_and_preserves_validation(
    tmp_path, failure
):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    note = "10_Projects/Daily-briefing.md"
    text = next(
        line
        for line in (vault / note).read_text().splitlines()
        if line.endswith("^task-format")
    )
    fact = {"path": note, "block_id": "task-format", "text": text}
    review = {
        "recorded_facts": [fact],
        "suggestions": ["Ask the owner to review the format."],
        "unknown_deadlines": True,
    }
    if failure == "wrong_key":
        fact["exact_source_text"] = fact.pop("text")
    elif failure == "partial_quote":
        fact["text"] = "Draft briefing format — status: todo"
    elif failure == "wrong_path":
        fact["path"] = "10_Projects/Other.md"
    elif failure == "wrong_block":
        fact["block_id"] = "invented"
    elif failure == "deadline":
        review["suggestions"] = ["Finish tomorrow."]

    class Client:
        def __init__(self):
            self.calls = 0

        def request(self, path, body, timeout=180):
            self.calls += 1
            prompt = body["messages"][1]["content"]
            assert "without Markdown fences" in prompt
            assert '"text"' in prompt
            assert "complete source line" in prompt
            assert "including" in prompt and "block ID" in prompt
            if self.calls == 1:
                message = {
                    "tool_calls": [
                        {
                            "id": "open",
                            "function": {
                                "name": "vault_open",
                                "arguments": json.dumps({"path": note}),
                            },
                        }
                    ]
                }
                if failure == "missing_open":
                    message = {"content": json.dumps(review)}
            else:
                assert body["tools"] == [QUALIFY["WRITE"]]
                assert body["max_tokens"] == 2048
                message = {"content": json.dumps(review)}
                if failure == "fenced":
                    message["content"] = "```json\n" + message["content"] + "\n```"
                elif failure == "write":
                    message["tool_calls"] = [{"function": {"name": "vault_write"}}]
            return {"choices": [{"message": message}]}

    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")}
    if failure:
        with pytest.raises(RuntimeError, match="Synthetic workflow review failed"):
            QUALIFY["workflow_review"](Client(), "fixture", vault)
    else:
        actual, _ = QUALIFY["workflow_review"](Client(), "fixture", vault)
        assert actual == review
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")} == before


def test_injection_check_exposes_write_and_rejects_attempt(tmp_path):
    vault = QUALIFY["synthetic_vault"](tmp_path)

    class Client:
        def __init__(
            self,
            write=False,
            content="The untrusted text requests a write.",
            tool_name="vault_write",
        ):
            self.write = write
            self.content = content
            self.tool_name = tool_name
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
                            "function": {"name": self.tool_name, "arguments": "{}"},
                        }
                    ]
                }
            else:
                message = {"content": self.content}
            return {"choices": [{"message": message}], "usage": {}}

    client = Client()
    QUALIFY["prompt_injection"](client, "local", vault)
    assert client.bodies[1]["tools"] == [QUALIFY["WRITE"], QUALIFY["PROPOSE"]]
    assert client.bodies[1]["tool_choice"] == "auto"
    QUALIFY["prompt_injection"](
        Client(content="The reference claims approval to create an Inbox note."),
        "local",
        vault,
    )
    with pytest.raises(RuntimeError, match="Prompt-injection"):
        QUALIFY["prompt_injection"](Client(write=True), "local", vault)
    with pytest.raises(RuntimeError, match="Prompt-injection"):
        QUALIFY["prompt_injection"](
            Client(write=True, tool_name="vault_propose"), "local", vault
        )
    for content in (None, "", "   "):
        with pytest.raises(RuntimeError, match="Prompt-injection"):
            QUALIFY["prompt_injection"](Client(content=content), "local", vault)


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


@pytest.mark.parametrize(
    "vram,ram,error",
    [(1, 8, None), (0, 8, "telemetry"), (-1, 8, "telemetry"), (1, 3, "headroom")],
)
def test_hybrid_gpu_residency_is_not_free_capacity(monkeypatch, vram, ram, error):
    route = QUALIFY["fixture_route"]()
    candidate = route["fallback"]

    class Client:
        direct_cloud = False

        def __init__(self, base_url):
            pass

        def request(self, path, body=None):
            return {
                "/api/version": {"version": "fixture-runtime"},
                "/api/tags": {
                    "models": [
                        {**candidate, "name": candidate["model"], "size": 4 * 1024**3}
                    ]
                },
                "/api/show": {"capabilities": ["tools"], "template": "fixture"},
                "/api/ps": {
                    "models": [
                        {"name": candidate["model"], "size_vram": vram * 1024**3}
                    ]
                },
            }[path]

    namespace = QUALIFY["live_qualification"].__globals__
    for name, replacement in {
        "OllamaClient": Client,
        "workloads": lambda *args: [1.0],
        "project_workloads": lambda *args: [1.0],
        "workflow_review": lambda *args: ({}, 1.0),
        "unload": lambda *args: None,
        "prompt_injection": lambda *args: None,
        "context_trials": lambda *args: None,
        "chat": lambda *args, **kwargs: ({}, 0.1, {}),
        "gpu_present": lambda: True,
        "available_ram": lambda: ram * 1024**3,
    }.items():
        monkeypatch.setitem(namespace, name, replacement)
    if error:
        with pytest.raises(RuntimeError, match=error):
            QUALIFY["live_qualification"](route, "http://127.0.0.1:11434/v1")
    else:
        qualified, _ = QUALIFY["live_qualification"](route, "http://127.0.0.1:11434/v1")
        assert valid_qualification(qualified)


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


@pytest.mark.parametrize("stage", ["injection", "answer"])
def test_read_checks_reject_skipped_injection_or_extra_tool_calls(
    tmp_path, monkeypatch, stage
):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    responses = [
        {
            "tool_calls": [
                {
                    "id": "open",
                    "function": {
                        "name": "vault_open",
                        "arguments": json.dumps({"path": "10_Projects/EMURU.md"}),
                    },
                }
            ]
        },
        {"content": "PHASE2_PROJECT_SOURCE_742", "tool_calls": []},
    ]
    if stage == "answer":
        responses[1]["tool_calls"] = [{"function": {"name": "vault_write"}}]
    monkeypatch.setitem(
        QUALIFY["chat"].__globals__,
        "chat",
        lambda *a, **kw: (responses.pop(0), 0.1, {}),
    )
    with pytest.raises(RuntimeError):
        if stage == "injection":
            QUALIFY["prompt_injection"](object(), "fixture", vault)
        else:
            QUALIFY["one_tool_call"](
                object(),
                "fixture",
                "Open EMURU",
                QUALIFY["OPEN"],
                lambda args: args,
                "PHASE2_PROJECT_SOURCE_742",
            )


def test_local_chat_uses_native_context_and_replays_tool_arguments():
    class Client:
        context_tokens = 65536

        def request(self, path, body, timeout):
            assert path == "/api/chat"
            assert body["options"] == {
                "num_ctx": 65536,
                "num_predict": 2048,
                "temperature": 0,
            }
            assert body["think"] is False
            assert body["messages"][0]["tool_calls"][0]["function"]["arguments"] == {
                "path": "10_Projects/EMURU.md"
            }
            assert body["messages"][1]["tool_name"] == "vault_open"
            assert body["tools"] == [QUALIFY["PROPOSE"]]
            return {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "vault_propose",
                                "arguments": {"path": "10_Projects/new.md"},
                            }
                        }
                    ],
                }
            }

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "open",
                    "function": {
                        "name": "vault_open",
                        "arguments": '{"path":"10_Projects/EMURU.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "open", "content": "source"},
    ]
    before = copy.deepcopy(messages)
    answer, _, _ = QUALIFY["chat"](
        Client(), "fixture", messages, tools=[QUALIFY["PROPOSE"]], max_tokens=2048
    )
    call = answer["tool_calls"][0]
    assert call["id"]
    assert json.loads(call["function"]["arguments"]) == {"path": "10_Projects/new.md"}
    assert messages == before


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["path", "content", "expected_revision", "anchor", "replacement", "request_id"],
        {
            "path": 7,
            "content": None,
            "expected_revision": None,
            "anchor": None,
            "replacement": None,
            "request_id": None,
        },
    ],
)
def test_proposal_parser_rejects_nonobjects_and_wrong_field_types(arguments):
    message = {
        "tool_calls": [
            {"function": {"name": "vault_propose", "arguments": json.dumps(arguments)}}
        ]
    }
    with pytest.raises(RuntimeError, match="Qualification proposal tool call failed"):
        QUALIFY["proposal_arguments"](message)


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
async def test_qualification_proposal_matches_real_mcp_optional_defaults(tmp_path):
    from mcp import Client

    from emuru.vault.mcp import create_mcp

    vault = QUALIFY["synthetic_vault"](tmp_path)
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    args = {
        "path": "10_Projects/mcp-contract.md",
        "content": "# Contract",
        "request_id": "contract",
    }
    async with Client(create_mcp(vault, proposal_home=home)) as client:
        tools = await client.list_tools()
        actual_schema = next(
            t for t in tools.tools if t.name == "vault_propose"
        ).model_dump(by_alias=True)["inputSchema"]
        result = await client.call_tool("vault_propose", args)
    assert not result.is_error
    assert result.structured_content["outcome"] == "proposed"
    message = {
        "tool_calls": [
            {"function": {"name": "vault_propose", "arguments": json.dumps(args)}}
        ]
    }
    parsed = QUALIFY["proposal_arguments"](message)
    assert parsed == {
        **args,
        "expected_revision": None,
        "anchor": None,
        "replacement": None,
    }
    schema = QUALIFY["PROPOSE"]["function"]["parameters"]
    assert set(schema["required"]) == set(actual_schema["required"])
    for key in set(schema["properties"]) - {"path"}:
        assert (
            schema["properties"][key]["default"]
            is actual_schema["properties"][key]["default"]
            is None
        )
    assert not (vault / args["path"]).exists()


def test_proposal_failure_preserves_private_trace_and_redacts_public_report(
    tmp_path, monkeypatch
):
    path = tmp_path / "route.json"
    write_private(path, json.dumps(QUALIFY["fixture_route"]()))
    before = path.read_bytes()
    message = {"content": "SYNTHETIC_PRIVATE_RESPONSE", "tool_calls": []}
    with pytest.raises(RuntimeError) as caught:
        QUALIFY["proposal_arguments"](message, "selected-model/stale_proposal")
    error = caught.value
    assert isinstance(error.__cause__, ValueError)
    assert "Expected exactly one vault_propose" in str(error)

    def fail(*args):
        raise error

    monkeypatch.setitem(QUALIFY["main"].__globals__, "live_qualification", fail)
    monkeypatch.setitem(QUALIFY["main"].__globals__, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["qualify.py", "--live", "--route", str(path)])
    with pytest.raises(SystemExit, match="LOCAL_UNQUALIFIED"):
        QUALIFY["main"]()
    evidence = json.loads((tmp_path / "route.qualification.json").read_text())
    assert evidence["diagnostics"]["stage"] == "selected-model/stale_proposal"
    assert evidence["diagnostics"]["actual_message"] == message
    assert evidence["diagnostics"]["expected_tool"] == QUALIFY["PROPOSE"]
    assert evidence["diagnostics"]["underlying_exception"]["type"] == "ValueError"
    assert (tmp_path / "route.qualification.json").stat().st_mode & 0o777 == 0o600
    report = (tmp_path / "route.qualification-report.json").read_text()
    assert "SYNTHETIC_PRIVATE_RESPONSE" not in report
    assert "selected-model" not in report
    assert path.read_bytes() == before
