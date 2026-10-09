"""Actual SDK dispatch guards: switches, native repairs and late results."""

import sys
from contextvars import copy_context
from types import SimpleNamespace as NS

import pytest

from emuru.hermes.inference import install_guard
from emuru.hermes.profile import HealthError
from emuru.models.gateway import settings


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("EMURU_GATEWAY_KEY", "synthetic-key")
    endpoint = settings(None)["model.base_url"]
    calls = []
    tool_executor = NS(
        _dispatch_authorized_once=lambda agent, state, ref, execute, **kwargs: execute(
            {}
        )
    )

    class Compressor:
        def _generate_summary(self, body):
            return body()

    compressor_module = NS(ContextCompressor=Compressor, call_llm=None)
    monkeypatch.setitem(
        sys.modules,
        "agent",
        NS(tool_executor=tool_executor, context_compressor=compressor_module),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.interrupt_compat",
        NS(request_hard_interrupt=lambda agent: None),
    )

    class Agent:
        provider = "custom:emuru"
        model = "emuru"
        base_url = endpoint
        api_mode = "chat_completions"
        _api_max_retries = 1
        _auto_recovery_cycles = 0
        _current_api_request_id = "turn:api:1"

        def _create_openai_client(self, kwargs, **kw):
            if getattr(self, "reused_client", None) is not None:
                return self.reused_client

            def dispatch(**payload):
                calls.append(payload)
                if getattr(self, "sdk_error", False):
                    raise RuntimeError("gateway exhausted")
                if getattr(self, "on_dispatch", None):
                    self.on_dispatch()
                return "result"

            return NS(
                max_retries=kwargs.get("max_retries", 0),
                base_url=kwargs["base_url"],
                chat=NS(completions=NS(create=dispatch)),
            )

        def _interruptible_api_call(self, payload):
            if getattr(self, "defer_dispatch", False):
                context = copy_context()
                self.delayed_dispatch = lambda: context.run(
                    self.client.chat.completions.create, **payload
                )
                return "pending"
            return self.client.chat.completions.create(**payload)

        _interruptible_streaming_api_call = _interruptible_api_call

        def run_conversation(self, body):
            return body(self)

        def _invoke_tool(self, *args, **kwargs):
            return "tool"

    active = [True]
    install_guard(Agent, permitted=lambda agent: active[0])
    agent = Agent()
    kwargs = {"base_url": endpoint, "api_key": "synthetic-key", "max_retries": 0}
    agent.client = agent._create_openai_client(kwargs)
    agent.context_compressor = Compressor()
    return agent, calls, kwargs, active


def test_only_one_dispatch_per_inference_even_after_failure(guarded):
    agent, calls, _, _ = guarded
    assert agent._interruptible_api_call({"model": "emuru"}) == "result"
    with pytest.raises(HealthError, match="repeat_dispatch"):
        agent._interruptible_streaming_api_call({"model": "emuru"})
    assert len(calls) == 1
    agent._current_api_request_id = "turn:api:2"
    assert agent._interruptible_api_call({"model": "emuru"}) == "result"
    assert len(calls) == 2
    assert agent._try_activate_fallback() is False
    assert agent._try_recover_primary_transport() is False


def summary(agent, body):
    return agent.run_conversation(
        lambda agent: agent.context_compressor._generate_summary(body)
    )


def compression_call(**kwargs):
    return sys.modules["agent"].context_compressor.call_llm(
        task="compression", messages=[{"role": "user", "content": "history"}], **kwargs
    )


def test_compression_uses_guarded_client_and_independent_dispatch_budget(guarded):
    agent, calls, _, _ = guarded
    agent._interruptible_api_call({"model": "emuru"})
    route = {}
    assert summary(agent, lambda: compression_call(route_info=route)) == "result"
    assert route == {"provider": "custom:emuru", "model": "emuru"}
    assert calls[-1] == {
        "model": "emuru",
        "messages": [{"role": "user", "content": "history"}],
    }
    assert summary(agent, compression_call) == "result"
    assert len(calls) == 3


def test_compression_failure_cannot_retry_through_recursive_fallback(guarded):
    agent, calls, _, _ = guarded

    agent.sdk_error = True

    def attempt():
        with pytest.raises(RuntimeError, match="gateway exhausted"):
            compression_call()
        with pytest.raises(HealthError, match="repeat_dispatch"):
            agent.context_compressor._generate_summary(compression_call)

    summary(agent, attempt)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "gemini"},
        {"model": "other"},
        {"base_url": "https://other/v1"},
        {"api_key": "other"},
    ],
)
def test_compression_route_override_rejected(guarded, override):
    agent, calls, _, _ = guarded
    with pytest.raises(HealthError, match="gateway_policy"):
        summary(agent, lambda: compression_call(**override))
    assert calls == []


def test_compression_cancellation_suppresses_late_result_and_next_dispatch(guarded):
    agent, calls, _, active = guarded
    agent.on_dispatch = lambda: active.__setitem__(0, False)
    with pytest.raises(HealthError, match="cancelled"):
        summary(agent, compression_call)
    with pytest.raises(HealthError, match="cancelled"):
        summary(agent, compression_call)
    assert len(calls) == 1


def test_compression_old_owner_context_cannot_dispatch(guarded):
    agent, calls, _, _ = guarded
    contexts = []
    summary(agent, lambda: contexts.append(copy_context()))

    def next_turn(agent):
        with pytest.raises(HealthError, match="stale_owner"):
            contexts[0].run(compression_call)
        with pytest.raises(HealthError, match="stale_owner"):
            contexts[0].run(
                agent.context_compressor._generate_summary, compression_call
            )

    agent.run_conversation(next_turn)
    assert calls == []


def test_closed_compression_context_cannot_dispatch_in_same_owner_turn(guarded):
    agent, calls, _, _ = guarded
    contexts = []

    def turn(agent):
        agent.context_compressor._generate_summary(
            lambda: contexts.append(copy_context())
        )
        with pytest.raises(HealthError, match="stale_dispatch"):
            contexts[0].run(compression_call)

    agent.run_conversation(turn)
    assert calls == []


@pytest.mark.parametrize(
    "field,value", [("_interrupt_requested", True), ("_emuru_owner_deadline", 0)]
)
def test_compression_interrupt_and_deadline_block_dispatch(guarded, field, value):
    agent, calls, _, _ = guarded

    def attempt():
        setattr(agent, field, value)
        compression_call()

    with pytest.raises(HealthError, match="cancelled|owner_turn_deadline"):
        summary(agent, attempt)
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "ollama"),
        ("base_url", "https://ollama.com/v1"),
        ("model", "other"),
        ("_api_max_retries", 2),
        ("_auto_recovery_cycles", 1),
    ],
)
def test_model_switch_or_retry_override_fails_before_dispatch(guarded, field, value):
    agent, calls, _, _ = guarded
    setattr(agent, field, value)
    with pytest.raises(HealthError, match="gateway_policy"):
        agent._interruptible_api_call({"model": "emuru"})
    assert calls == []


@pytest.mark.parametrize(
    "override",
    [{"base_url": "https://ollama.com/v1"}, {"api_key": "other"}, {"max_retries": 2}],
)
def test_client_bypass_rejected(guarded, override):
    agent, calls, kwargs, _ = guarded
    with pytest.raises(HealthError, match="gateway_client"):
        agent._create_openai_client({**kwargs, **override})
    assert calls == []


def test_cancel_prevents_dispatch_and_suppresses_late_result(guarded):
    agent, calls, _, active = guarded
    active[0] = False
    with pytest.raises(HealthError, match="cancelled"):
        agent._interruptible_api_call({"model": "emuru"})
    assert calls == []
    active[0] = True
    agent.client.chat.completions.create = lambda **payload: (
        active.__setitem__(0, False) or "late"
    )
    with pytest.raises(HealthError, match="cancelled"):
        agent._interruptible_api_call({"model": "emuru"})


def test_reused_sdk_client_has_only_one_wrapper(guarded):
    agent, calls, kwargs, _ = guarded
    client = agent.client
    # Native construction can return the same SDK object.
    agent.reused_client = client
    assert agent._create_openai_client(kwargs) is client
    assert agent._create_openai_client(kwargs) is client
    assert agent._interruptible_api_call({"model": "emuru"}) == "result"
    agent._current_api_request_id = "turn:api:2"
    assert agent._interruptible_api_call({"model": "emuru"}) == "result"
    assert client.chat.completions._emuru_guard_agent is agent
    assert len(calls) == 2


@pytest.mark.parametrize("field", ["_emuru_owner_open", "_interrupt_requested"])
def test_closed_turn_or_native_interrupt_blocks_sdk_dispatch(guarded, field):
    agent, calls, _, _ = guarded
    setattr(agent, field, field == "_interrupt_requested")
    with pytest.raises(HealthError, match="cancelled"):
        agent._interruptible_api_call({"model": "emuru"})
    assert calls == []


def test_extra_tool_schema_rejected_before_sdk_dispatch(guarded):
    agent, calls, _, _ = guarded
    with pytest.raises(HealthError, match="tool_schema_refused"):
        agent._interruptible_api_call(
            {
                "model": "emuru",
                "tools": [{"type": "function", "function": {"name": "terminal"}}],
            }
        )
    assert calls == []


def test_old_worker_cannot_dispatch_during_next_owner_turn(guarded):
    agent, calls, _, _ = guarded
    agent._emuru_owner_open = True
    agent.defer_dispatch = True
    assert agent._interruptible_api_call({"model": "emuru"}) == "pending"
    agent._emuru_owner_open = False
    # A fresh owner turn reopens this reused agent while the old worker survives.
    agent._emuru_owner_open = True
    agent._current_api_request_id = "turn:api:2"
    with pytest.raises(HealthError, match="stale_dispatch"):
        agent.delayed_dispatch()
    assert calls == []
    agent.defer_dispatch = False
    assert agent._interruptible_api_call({"model": "emuru"}) == "result"
    assert len(calls) == 1


def test_copied_old_owner_context_cannot_dispatch_tool_or_sdk(guarded):
    agent, calls, _, _ = guarded
    old = []
    agent.run_conversation(lambda agent: old.append(copy_context()))
    tools = sys.modules["agent"].tool_executor

    def next_turn(agent):
        agent._current_api_request_id = "turn:api:2"
        with pytest.raises(HealthError, match="stale_owner"):
            old[0].run(agent._interruptible_api_call, {"model": "emuru"})
        with pytest.raises(HealthError, match="stale_owner"):
            old[0].run(
                tools._dispatch_authorized_once,
                agent,
                NS(),
                NS(name="mcp__vault__vault_open"),
                execute=lambda args: calls.append("bad"),
            )
        assert calls == []
        assert agent._interruptible_api_call({"model": "emuru"}) == "result"

    agent.run_conversation(next_turn)
    assert len(calls) == 1
