"""Probe the selected primary and authenticated gateway before Telegram intake."""

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import litellm

from emuru.models.gateway import (
    LOCAL_FALLBACK_ENABLED,
    read_route,
    upstream,
    valid_qualification,
)
from emuru.models.gateway_guard import guard, qualified, valid_response

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "connection_probe",
            "description": "Confirm tool support",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    }
]
MESSAGES = [{"role": "user", "content": "Call connection_probe now."}]
PHASE = "route"


def check():
    global PHASE

    route = read_route(os.environ["EMURU_CONTAINER_ROUTE"])
    PHASE = "primary"
    key = {
        "ollama": "OLLAMA_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openai-api": "OPENAI_API_KEY",
    }[route["primary"]["provider"]]
    settings = upstream(route["primary"])
    settings["api_key"] = os.environ[key]
    if os.environ.get("EMURU_GATEWAY_TEST_FIXTURE"):
        settings["api_base"] = os.environ["EMURU_TEST_PRIMARY_BASE"]
    if not settings["api_key"]:
        raise RuntimeError("Selected primary credential missing")
    settings["timeout"] = 60
    router = litellm.Router(
        model_list=[{"model_name": "primary", "litellm_params": settings}],
        num_retries=0,
        max_fallbacks=0,
        fallbacks=[],
        context_window_fallbacks=[],
        content_policy_fallbacks=[],
        disable_cooldowns=True,
    )
    response = asyncio.run(
        guard.collect(
            router,
            "primary",
            MESSAGES,
            {"tools": TOOLS, "tool_choice": "required", "max_tokens": 64},
        )
    )
    valid_response(response, TOOLS)
    if not response.choices[0].message.tool_calls:
        raise RuntimeError("Selected primary lacks required tool calls")

    request = Request(
        "http://litellm:4000/v1/chat/completions",
        data=json.dumps(
            {
                "model": "emuru",
                "messages": MESSAGES,
                "tools": TOOLS,
                "tool_choice": "required",
                "max_tokens": 64,
            }
        ).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["EMURU_GATEWAY_KEY"],
            "Content-Type": "application/json",
        },
    )
    PHASE = "gateway"
    with build_opener(ProxyHandler({})).open(request, timeout=75) as result:
        body = json.load(result)
    choices = body.get("choices", [])
    if (
        len(choices) != 1
        or not choices[0].get("message", {}).get("tool_calls")
        or choices[0].get("finish_reason") not in {"stop", "tool_calls"}
    ):
        raise RuntimeError("Authenticated gateway did not return a tool call")
    PHASE = "fallback"
    local = (
        LOCAL_FALLBACK_ENABLED
        and route["primary"]["provider"] == "ollama"
        and valid_qualification(route)
        and asyncio.run(
            qualified(
                route["primary"],
                route["fallback"],
                MESSAGES,
                {"tools": TOOLS, "max_tokens": 64},
                "http://ollama:11434",
            )
        )
    )
    print("Selected primary and authenticated gateway: READY", flush=True)
    print(
        "Qualified local fallback: "
        + (
            "DISABLED"
            if not LOCAL_FALLBACK_ENABLED
            else "READY"
            if local
            else "LOCAL_UNQUALIFIED"
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        check()
    except Exception as error:  # noqa: BLE001 -- provider errors may contain credentials
        if os.environ.get("EMURU_GATEWAY_TEST_FIXTURE"):
            frames = traceback.extract_tb(error.__traceback__)
            print(
                "Startup fixture: "
                + type(error).__name__
                + " at "
                + ", ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}"
                    for frame in frames[-3:]
                ),
                file=sys.stderr,
            )
        raise SystemExit(f"EMURU startup: {PHASE} readiness failed") from None
