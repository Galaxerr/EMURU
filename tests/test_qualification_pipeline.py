"""Run every qualification stage against deterministic inference and real vault contracts."""

import copy
import json
import re
import runpy
from collections import Counter
from pathlib import Path

from emuru.models.gateway import valid_qualification

ROOT = Path(__file__).resolve().parents[1]
QUALIFY = runpy.run_path(str(ROOT / "scripts/hermes/qualify.py"))


def test_complete_pipeline_uses_real_mutations_and_measured_stage_counts(monkeypatch):
    route = QUALIFY["fixture_route"]()
    before = copy.deepcopy(route)
    counts = Counter()

    class Client:
        def __init__(self, base_url):
            self.direct_cloud = base_url.startswith("https:")
            self.backend = "cloud" if self.direct_cloud else "local"
            self.previous = {}
            self.loaded = False

        def request(self, path, body=None, timeout=60):
            candidate = route["fallback"]
            if path == "/api/version":
                return {"version": "fixture-runtime"}
            if path == "/api/tags":
                return {
                    "models": [
                        {
                            "name": candidate["model"],
                            "digest": candidate["digest"],
                            "size": 1024,
                        }
                    ]
                }
            if path == "/api/show":
                return {"capabilities": ["tools"], "template": "fixture"}
            if path == "/api/generate":
                self.loaded = False
                counts["unload"] += 1
                return {}
            if path == "/api/ps":
                return {
                    "models": [{"name": candidate["model"], "size_vram": 0}]
                    if self.loaded
                    else []
                }
            if not self.direct_cloud:
                assert path == "/api/chat"
                assert body["options"]["num_ctx"] == candidate["context_tokens"]
                assert body["think"] is False
            else:
                assert path == "/v1/chat/completions"
            self.loaded = True
            messages = body["messages"]
            prompt = messages[-1]["content"]
            if prompt.startswith("EMURU_CONTEXT_BEGIN"):
                counts["context"] += 1
                return {
                    "message": {
                        "content": prompt.splitlines()[0]
                        + " "
                        + prompt.splitlines()[-2]
                    },
                    "prompt_eval_count": candidate["context_tokens"]
                    - candidate["output_reserve_tokens"],
                }
            counts[self.backend] += 1
            if prompt == "Reply OK.":
                counts["latency"] += 1
                message = {"content": "OK"}
            elif prompt.startswith("Answer the original request"):
                message = {"content": messages[0]["content"] + messages[-2]["content"]}
            elif prompt.startswith("Summarize the untrusted request"):
                message = {
                    "content": "The note claims permission; no action was taken."
                }
            elif messages[-1]["role"] == "tool":
                opened = json.loads(prompt)
                line = next(
                    line
                    for line in opened["content"].splitlines()
                    if line.endswith("^task-format")
                )
                message = {
                    "content": json.dumps(
                        {
                            "recorded_facts": [
                                {
                                    "path": opened["path"],
                                    "block_id": "task-format",
                                    "text": line,
                                }
                            ],
                            "suggestions": [],
                            "unknown_deadlines": True,
                        }
                    )
                }
            else:
                name = body["tools"][0]["function"]["name"]
                counts[self.backend + ":" + name] += 1
                if name == "vault_search":
                    args = {"query": "source marker"}
                elif name == "vault_open":
                    args = {"path": re.search(r"Open (\S+)", prompt)[1].rstrip(".")}
                elif name == "vault_write":
                    args = {
                        "path": re.search(r"Create (\S+)", prompt)[1],
                        "content": re.search(r"exact content (\S+)", prompt)[1],
                    }
                elif prompt.startswith("Create "):
                    args = {
                        "path": re.search(r"Create (\S+)", prompt)[1],
                        "content": re.search(r"\[\[.+?\]\]", prompt)[0]
                        + "\n"
                        + re.search(r"exact source marker (\w+)", prompt)[1]
                        + "\n"
                        + re.search(r"and this task line: (.+)\. Use request", prompt)[
                            1
                        ],
                        "request_id": re.search(
                            r"Use request identity ([^\s.]+)", prompt
                        )[1],
                    }
                    self.previous = args
                elif prompt.startswith("Mark task "):
                    args = {
                        "path": self.previous["path"],
                        "expected_revision": re.search(
                            r"current full revision (\w+)", prompt
                        )[1],
                        "anchor": re.search(r"exact anchor (.+), replacement", prompt)[
                            1
                        ],
                        "replacement": re.search(
                            r"replacement (.+), and request identity", prompt
                        )[1],
                        "request_id": re.search(r"request identity ([^\s.]+)", prompt)[
                            1
                        ],
                    }
                    self.previous = args
                elif prompt.startswith("For "):
                    args = {
                        **self.previous,
                        "anchor": self.previous["replacement"],
                        "replacement": self.previous["anchor"],
                        "request_id": re.search(r"request identity ([^\s.]+)", prompt)[
                            1
                        ],
                    }
                else:
                    args = json.loads(prompt.split("field: ", 1)[1])
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "fixture-call",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args)
                                if self.direct_cloud
                                else args,
                            },
                        }
                    ],
                }
            if self.direct_cloud:
                return {"choices": [{"message": message}]}
            return {"message": message}

    namespace = QUALIFY["live_qualification"].__globals__
    monkeypatch.setitem(namespace, "OllamaClient", Client)
    monkeypatch.setitem(namespace, "available_ram", lambda: 16 * 1024**3)
    monkeypatch.setitem(namespace, "gpu_present", lambda: False)
    original_apply = QUALIFY["mutations"].apply_proposal

    def apply(*args, **kwargs):
        assert Path("/tmp") in args[1].parents
        result = original_apply(*args, **kwargs)
        assert result["outcome"] == "applied"
        counts["apply"] += 1
        return result

    monkeypatch.setattr(QUALIFY["mutations"], "apply_proposal", apply)
    qualified, record = QUALIFY["live_qualification"](route, "http://127.0.0.1:11435")
    assert route == before
    assert valid_qualification(qualified)
    assert record["evidence"]["workload_repetitions"] == QUALIFY["WORKLOAD_REPETITIONS"]
    assert counts == Counter(
        {
            "cloud": 70,
            "local": 90,
            "apply": 20,
            "unload": 1,
            "context": 3,
            "latency": 20,
            **{
                f"{backend}:{name}": total
                for backend in ("cloud", "local")
                for name, total in (
                    ("vault_search", 5),
                    ("vault_open", 15),
                    ("vault_write", 5),
                    ("vault_propose", 20),
                )
            },
        }
    )
