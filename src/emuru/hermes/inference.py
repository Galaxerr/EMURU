"""Pinned native inference guards, shared by CLI and guarded Telegram."""

import os
import threading
import time
from contextvars import ContextVar
from functools import wraps

from emuru.hermes.profile import TOOLS, HealthError
from emuru.models.gateway import settings


def install_guard(agent_class, permitted=None):
    """Guard actual SDK construction and each call, including interactive switches."""
    if agent_class.__dict__.get("_emuru_inference_guard"):
        return
    expected = settings(None)
    endpoint = expected["model.base_url"]
    owner_scope = ContextVar("emuru_inference_owner", default=None)
    request_scope = ContextVar("emuru_inference_request", default=None)
    owner_agent = ContextVar("emuru_inference_agent", default=None)
    compression_scope = ContextVar("emuru_compression_request", default=None)
    allowed_tools = {"mcp__vault__" + name for name in TOOLS}

    def check(agent):
        if getattr(agent, "_emuru_owner_token", None) is not owner_scope.get():
            raise HealthError("inference_stale_owner_refused")
        if getattr(agent, "_emuru_owner_open", True) is False or getattr(
            agent, "_interrupt_requested", False
        ):
            raise HealthError("inference_cancelled")
        if (
            agent.provider not in {"custom", "custom:emuru"}
            or getattr(agent, "requested_provider", "custom:emuru") != "custom:emuru"
            or agent.model != "emuru"
            or agent.base_url.rstrip("/") != endpoint
            or agent.api_mode != "chat_completions"
            or agent._api_max_retries != 1  # Native counts attempts, not retries.
            or agent._auto_recovery_cycles != 0
        ):
            raise HealthError("inference_gateway_policy_mismatch")
        if time.monotonic() >= getattr(agent, "_emuru_owner_deadline", float("inf")):
            raise HealthError("owner_turn_deadline")
        if permitted is not None and not permitted(agent):
            raise HealthError("inference_cancelled")

    original_client = agent_class._create_openai_client

    @wraps(original_client)
    def create_client(agent, kwargs, *args, **kw):
        key = os.environ.get("EMURU_GATEWAY_KEY")
        if (
            not key
            or kwargs.get("api_key") != key
            or str(kwargs.get("base_url", "")).rstrip("/") != endpoint
            or kwargs.get("max_retries", 0) != 0
        ):
            raise HealthError("inference_gateway_client_mismatch")
        result = original_client(agent, kwargs, *args, **kw)
        if result.max_retries != 0 or str(result.base_url).rstrip("/") != endpoint:
            raise HealthError("inference_gateway_sdk_mismatch")
        resource = result.chat.completions
        if getattr(resource, "_emuru_guard_agent", None) is agent:
            return result
        if getattr(resource, "_emuru_guard_agent", None) is not None:
            raise HealthError("inference_shared_client_refused")
        original_create = resource.create

        @wraps(original_create)
        def create(*args, **kwargs):
            check(agent)
            compression = compression_scope.get()
            if compression is not None:
                if compression["agent"] is not agent or not compression["open"]:
                    raise HealthError("inference_stale_dispatch_refused")
                if compression["dispatched"]:
                    raise HealthError("inference_repeat_dispatch_refused")
                compression["dispatched"] = True
                if kwargs.get("model") != "emuru" or kwargs.get("tools"):
                    raise HealthError("inference_gateway_model_mismatch")
                return original_create(*args, **kwargs)
            captured = request_scope.get()
            request_id = getattr(agent, "_current_api_request_id", None)
            if captured != (getattr(agent, "_emuru_owner_token", None), request_id):
                raise HealthError("inference_stale_dispatch_refused")
            if (
                not request_id
                or getattr(agent, "_emuru_dispatched_request", None) == request_id
            ):
                raise HealthError("inference_repeat_dispatch_refused")
            agent._emuru_dispatched_request = request_id
            if kwargs.get("model") != "emuru":
                raise HealthError("inference_gateway_model_mismatch")
            if any(
                tool.get("type") != "function"
                or tool.get("function", {}).get("name") not in allowed_tools
                for tool in (kwargs.get("tools") or [])
            ):
                raise HealthError("inference_tool_schema_refused")
            return original_create(*args, **kwargs)

        resource.create = create
        resource._emuru_guard_agent = agent
        return result

    agent_class._create_openai_client = create_client

    def guarded_call(original):
        @wraps(original)
        def call(agent, *args, **kwargs):
            check(agent)
            scope = (
                getattr(agent, "_emuru_owner_token", None),
                getattr(agent, "_current_api_request_id", None),
            )
            token = request_scope.set(scope)
            try:
                result = original(agent, *args, **kwargs)
                check(agent)
                if scope != (
                    getattr(agent, "_emuru_owner_token", None),
                    getattr(agent, "_current_api_request_id", None),
                ):
                    raise HealthError("inference_stale_result_refused")
                return result
            finally:
                request_scope.reset(token)

        return call

    for name in ("_interruptible_api_call", "_interruptible_streaming_api_call"):
        setattr(agent_class, name, guarded_call(getattr(agent_class, name)))
    # A recovered transport would issue another inference after gateway exhaustion.
    agent_class._try_recover_primary_transport = lambda *args, **kwargs: False
    agent_class._try_activate_fallback = lambda *args, **kwargs: False
    if hasattr(agent_class, "run_conversation"):
        from agent import context_compressor

        original_summary = context_compressor.ContextCompressor._generate_summary

        @wraps(original_summary)
        def generate_summary(compressor, *args, **kwargs):
            agent = owner_agent.get()
            if agent is None:
                raise HealthError("inference_stale_owner_refused")
            check(agent)
            # Recursive native fallback shares the original dispatch budget.
            state = compression_scope.get()
            outermost = state is None
            if outermost:
                state = {"agent": agent, "dispatched": False, "open": True}
            token = compression_scope.set(state)
            try:
                result = original_summary(compressor, *args, **kwargs)
                check(agent)
                return result
            finally:
                if outermost:
                    state["open"] = False
                compression_scope.reset(token)

        def compression_call(*, task, messages, route_info=None, **kwargs):
            agent = owner_agent.get()
            if agent is None or compression_scope.get() is None:
                raise HealthError("inference_stale_owner_refused")
            check(agent)
            if (
                task != "compression"
                or kwargs.get("model", "emuru") != "emuru"
                or kwargs.get("provider", "custom:emuru") != "custom:emuru"
                or str(kwargs.get("base_url", endpoint)).rstrip("/") != endpoint
                or kwargs.get("api_key", os.environ.get("EMURU_GATEWAY_KEY"))
                != os.environ.get("EMURU_GATEWAY_KEY")
            ):
                raise HealthError("inference_gateway_policy_mismatch")
            if route_info is not None:
                route_info.update(provider="custom:emuru", model="emuru")
            # Bypass auxiliary retry/fallback and interrupt shielding, retaining native summary validation.
            result = agent.client.chat.completions.create(
                model="emuru", messages=messages
            )
            check(agent)
            return result

        context_compressor.ContextCompressor._generate_summary = generate_summary
        context_compressor.call_llm = compression_call
        original_turn = agent_class.run_conversation

        @wraps(original_turn)
        def run_turn(agent, *args, **kwargs):
            from agent.interrupt_compat import request_hard_interrupt

            agent._emuru_owner_token = object()
            owner_context = owner_scope.set(agent._emuru_owner_token)
            agent_context = owner_agent.set(agent)
            agent._emuru_owner_deadline = time.monotonic() + 600
            agent._emuru_owner_open = True
            timer = threading.Timer(600, lambda: request_hard_interrupt(agent))
            timer.daemon = True
            timer.start()
            try:
                result = original_turn(agent, *args, **kwargs)
                check(agent)
                return result
            finally:
                agent._emuru_owner_open = False
                timer.cancel()
                owner_scope.reset(owner_context)
                owner_agent.reset(agent_context)

        agent_class.run_conversation = run_turn
        original_tool = agent_class._invoke_tool

        @wraps(original_tool)
        def invoke_tool(agent, *args, **kwargs):
            check(agent)
            return original_tool(agent, *args, **kwargs)

        agent_class._invoke_tool = invoke_tool
        from agent import tool_executor

        original_dispatch = tool_executor._dispatch_authorized_once

        @wraps(original_dispatch)
        def dispatch(agent, state, ref, *, execute, **kwargs):
            def checked(args):
                if not getattr(agent, "_emuru_owner_open", False):
                    raise HealthError("owner_turn_closed")
                check(agent)
                if ref.name not in allowed_tools:
                    raise HealthError("inference_tool_dispatch_refused")
                return execute(args)

            return original_dispatch(agent, state, ref, execute=checked, **kwargs)

        tool_executor._dispatch_authorized_once = dispatch
    agent_class._emuru_inference_guard = True
