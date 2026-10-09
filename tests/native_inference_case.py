import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import httpx
from openai import APIStatusError

assert not any("/opt/emuru/.venv" in path for path in sys.path)

from emuru.hermes import profile
from emuru.models.gateway import settings

root = ROOT
os.environ.pop("EMURU_CONTAINER_ROUTE", None)
temporary = tempfile.TemporaryDirectory()
home = Path(temporary.name)
os.environ["HERMES_HOME"] = str(home)
os.environ["EMURU_GATEWAY_KEY"] = "synthetic-key"
config = {}
for name, value in profile.expected_settings(root).items():
    cursor = config
    parts = name.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
config["model"]["context_length"] = 64000
(home / "config.yaml").write_text(json.dumps(config))
from run_agent import AIAgent

from emuru.hermes.inference import install_guard

clients = []
calls = []
mode = ["success"]


def respond(request):
    calls.append(request)
    if mode[0] == "error":
        return httpx.Response(503, json={"error": {"message": "synthetic"}})
    if mode[0] == "cancel":
        agent._interrupt_requested = True
    return httpx.Response(
        200,
        json={
            "id": "synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": "emuru",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "synthetic success"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


original = AIAgent._create_openai_client


def create(agent, kwargs, **kw):
    kwargs = {
        **kwargs,
        "http_client": httpx.Client(transport=httpx.MockTransport(respond)),
    }
    client = original(agent, kwargs, **kw)
    clients.append(client)
    return client


AIAgent._create_openai_client = create
original_turn = AIAgent.run_conversation


def turn(agent, message, *args, **kwargs):
    if callable(message):
        return message(agent)
    return original_turn(agent, message, *args, **kwargs)


AIAgent.run_conversation = turn
install_guard(AIAgent)
agent = AIAgent(
    model="emuru",
    provider="custom",
    requested_provider="custom:emuru",
    base_url=settings(None)["model.base_url"],
    api_key="synthetic-key",
    api_mode="chat_completions",
    max_iterations=12,
    enabled_toolsets=[],
    disabled_toolsets=list(profile.DISABLED_TOOLSETS),
    skip_memory=True,
    skip_background_review=True,
    quiet_mode=True,
)
assert clients[-1].timeout.read >= 245
print("resolved", agent.provider, agent.requested_provider)
agent._current_api_request_id = "synthetic:api:1"
response = agent._interruptible_api_call(
    {"model": "emuru", "messages": [{"role": "user", "content": "synthetic"}]}
)
assert response.choices[0].message.content == "synthetic success"
assert len(calls) == 1
try:
    agent._interruptible_api_call(
        {"model": "emuru", "messages": [{"role": "user", "content": "synthetic"}]}
    )
except profile.HealthError as error:
    assert str(error) == "inference_repeat_dispatch_refused"
else:
    raise AssertionError("native repeated SDK dispatch")
assert len(calls) == 1
agent._current_api_request_id = "synthetic:api:2"
agent._interruptible_api_call(
    {"model": "emuru", "messages": [{"role": "user", "content": "later"}]}
)
assert len(calls) == 2
# Actual SDK retries stay disabled on retryable transport status.
agent._current_api_request_id = "synthetic:api:3"
mode[0] = "error"
try:
    agent._interruptible_api_call({"model": "emuru", "messages": []})
except APIStatusError:
    pass
else:
    raise AssertionError("native retryable error was accepted")
assert len(calls) == 3
assert agent._try_activate_fallback() is False
assert agent._try_recover_primary_transport() is False

# A cancellation inside transport must suppress the completed SDK response.
mode[0] = "cancel"
agent._current_api_request_id = "synthetic:api:4"
try:
    agent._interruptible_api_call({"model": "emuru", "messages": []})
except (profile.HealthError, InterruptedError):
    pass
else:
    raise AssertionError("native late response escaped cancellation")
assert len(calls) == 4
try:
    agent._current_api_request_id = "synthetic:api:5"
    agent._interruptible_api_call({"model": "emuru", "messages": []})
except profile.HealthError:
    pass
else:
    raise AssertionError("native late dispatch escaped cancellation")
assert len(calls) == 4

# Exercise the native authorized dispatch seam for sequential tools.
from agent import tool_executor

agent._interrupt_requested = False
agent._emuru_owner_open = True
effects = []
ref = SimpleNamespace(
    name="mcp__vault__vault_open", args={}, task_id=None, call_id="probe"
)


def dispatch(execute):
    return tool_executor._dispatch_authorized_once(
        agent,
        SimpleNamespace(args={}, blocked=False),
        ref,
        execute=execute,
        scope_block=None,
        display_index=None,
        begin_execution=None,
        authorization_gate=None,
    )


def first(args):
    effects.append("first")
    agent._interrupt_requested = True
    return "first"


assert dispatch(first) == "first"
try:
    dispatch(lambda args: effects.append("late"))
except profile.HealthError:
    pass
else:
    raise AssertionError("native second tool escaped cancellation")
assert effects == ["first"]
agent._interrupt_requested = False
ref.name = "terminal"
try:
    dispatch(lambda args: effects.append("unauthorized"))
except profile.HealthError as error:
    assert str(error) == "inference_tool_dispatch_refused"
else:
    raise AssertionError("native unauthorized tool escaped whitelist")
assert effects == ["first"]
ref.name = "mcp__vault__vault_open"
mode[0] = "success"
agent._current_api_request_id = "synthetic:api:6"
agent._interruptible_api_call({"model": "emuru", "messages": []})
assert len(calls) == 5
agent._emuru_owner_open = False
try:
    dispatch(lambda args: effects.append("closed"))
except profile.HealthError:
    pass
else:
    raise AssertionError("native late tool escaped closed turn")
assert effects == ["first"]

# Actual native summary validation uses the primary guarded SDK, never auxiliary SDK/retries.
compressor = agent.context_compressor
transcript = [
    {"role": "user", "content": "synthetic request"},
    {"role": "assistant", "content": "synthetic reply"},
]
before = len(calls)
mode[0] = "success"
assert agent.run_conversation(lambda agent: compressor._generate_summary(transcript))
assert len(calls) == before + 1
assert json.loads(calls[-1].content)["model"] == "emuru"
assert str(calls[-1].url).startswith(settings(None)["model.base_url"])

mode[0] = "error"
compressor._summary_failure_cooldown_until = 0
before = len(calls)
agent.run_conversation(lambda agent: compressor._generate_summary(transcript))
assert len(calls) == before + 1

mode[0] = "cancel"
compressor._summary_failure_cooldown_until = 0
before = len(calls)
try:
    agent.run_conversation(lambda agent: compressor._generate_summary(transcript))
except profile.HealthError:
    pass
else:
    raise AssertionError("native compression result escaped cancellation")
assert len(calls) == before + 1
print(
    "Pinned native SDK, single dispatch, cancellation, tools, later inference: PASS (synthetic)"
)
