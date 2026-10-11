"""Bounded policy around the pinned LiteLLM Router, loaded only by its proxy."""

import asyncio
import json
import os

import httpx
import jsonschema
import litellm
from litellm import CustomLLM, Router
from litellm.llms.custom_llm import CustomLLMError

from emuru.models.gateway import read_route, upstream, valid_qualification

CLOUD_SECONDS = 60
LOCAL_SECONDS = 180
ROUTED_SECONDS = 245


def fail(message, status=400):
    raise CustomLLMError(status_code=status, message=message)


def deadline(name, maximum):
    value = float(os.environ.get(name, maximum))
    if not 0 < value <= maximum:
        fail("Invalid gateway deadline")
    return value


def valid_response(response, tools):
    choices = response.choices
    if len(choices) != 1 or choices[0].finish_reason not in {"stop", "tool_calls"}:
        fail("Unsafe or incomplete inference response", 502)
    message = choices[0].message
    identifiers = set()
    schemas = {x["function"]["name"]: x["function"]["parameters"] for x in tools}
    for call in message.tool_calls or []:
        if not isinstance(call.id, str) or not call.id:
            fail("Invalid tool call ID", 502)
        if call.id in identifiers or call.function.name not in schemas:
            fail("Unknown or duplicate tool call", 502)
        identifiers.add(call.id)
        if call.type != "function" or not call.function.name:
            fail("Invalid tool call", 502)
        try:

            def object_pairs(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError("Duplicate argument key")
                return result

            def invalid_constant(value):
                raise ValueError("Non-JSON constant")

            arguments = json.loads(
                call.function.arguments,
                object_pairs_hook=object_pairs,
                parse_constant=invalid_constant,
            )
        except (ValueError, TypeError):
            fail("Incomplete tool arguments", 502)
        try:
            jsonschema.validate(arguments, schemas[call.function.name])
        except (jsonschema.ValidationError, jsonschema.SchemaError):
            fail("Invalid tool arguments", 502)
        if not isinstance(arguments, dict):
            fail("Tool arguments must be an object", 502)
    if not message.content and not message.tool_calls:
        fail("Empty inference response", 502)
    return response


def context_bound(messages, params):
    return len(
        json.dumps(
            {"messages": messages, "params": params}, ensure_ascii=False
        ).encode()
    ) + 256 * (len(messages) + len(params.get("tools", [])) + 1)


async def qualified(primary, candidate, messages, params, base):
    """Qualification is evidence, never synthesized by a successful chat."""
    q = candidate.get("qualification")
    if (
        isinstance(q, dict)
        and q.get("provenance") == "deterministic-fixture"
        and not os.environ.get("EMURU_GATEWAY_TEST_FIXTURE")
    ):
        return False
    if any(
        not isinstance(m, dict)
        or not isinstance(m.get("content", ""), (str, type(None)))
        for m in messages
    ):
        return False
    # Conservative byte admission includes the full request and output reserve.
    size = context_bound(messages, params)
    reserve = candidate["output_reserve_tokens"]
    requested = params.get("max_tokens", reserve)
    if (
        type(requested) is not int
        or requested <= 0
        or requested > reserve
        or size + reserve > candidate["context_tokens"]
    ):
        return False
    try:
        import hashlib

        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            version = (await client.get(base + "/api/version")).json()["version"]
            catalog = (await client.get(base + "/api/tags")).json()["models"]
            show = (
                await client.post(
                    base + "/api/show", json={"model": candidate["model"]}
                )
            ).json()
        match = next(x for x in catalog if x["name"] == candidate["model"])
        template_sha256 = hashlib.sha256(show.get("template", "").encode()).hexdigest()
        route = {
            "primary": primary,
            "fallback": candidate,
        }
        return (
            valid_qualification(
                route,
                runtime_version=version,
                template_sha256=template_sha256,
            )
            and match["digest"] == candidate["digest"]
            and not show.get("remote_model")
            and not show.get("remote_host")
            and "tools" in show.get("capabilities", [])
        )
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        StopIteration,
        TypeError,
        AttributeError,
    ):
        return False


class Guard(CustomLLM):
    async def run(self, messages, optional_params, streaming=False):
        route = read_route(
            os.environ.get("EMURU_CONTAINER_ROUTE", "/run/emuru/route.json")
        )
        params = dict(optional_params)
        params.pop("stream", None)
        options = params.pop("stream_options", None)
        if options is not None and options != {"include_usage": True}:
            fail("Unsupported stream options")
        if params.pop("max_retries", 0) != 0:
            fail("Inference retries disabled")
        # Reject knobs that could override policy or dispatch destinations.
        if set(params) - {
            "temperature",
            "top_p",
            "max_tokens",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stop",
            "seed",
            "response_format",
        }:
            fail("Unsupported inference arguments: " + ",".join(sorted(params)))
        primary = upstream(route["primary"])
        primary["timeout"] = deadline("EMURU_CLOUD_SECONDS", CLOUD_SECONDS)
        local_base = os.environ.get("EMURU_LOCAL_BASE", "http://ollama:11434")
        if local_base != "http://ollama:11434" and not os.environ.get(
            "EMURU_GATEWAY_TEST_FIXTURE"
        ):
            fail("Invalid local endpoint")
        if os.environ.get("EMURU_GATEWAY_TEST_FIXTURE"):
            primary["api_base"] = os.environ["EMURU_TEST_PRIMARY_BASE"]
        router = Router(
            model_list=[
                {"model_name": "primary", "litellm_params": primary},
                {
                    "model_name": "local",
                    "litellm_params": {
                        "model": "ollama_chat/" + route["fallback"]["model"],
                        "api_base": local_base,
                        "num_retries": 0,
                        "max_retries": 0,
                        "timeout": deadline("EMURU_LOCAL_SECONDS", LOCAL_SECONDS),
                        "num_ctx": route["fallback"]["context_tokens"],
                    },
                },
            ],
            num_retries=0,
            max_fallbacks=0,
            fallbacks=[],
            context_window_fallbacks=[],
            content_policy_fallbacks=[],
            disable_cooldowns=True,
        )
        async with asyncio.timeout(deadline("EMURU_ROUTED_SECONDS", ROUTED_SECONDS)):
            for backend, budget in (
                ("primary", primary["timeout"]),
                ("local", deadline("EMURU_LOCAL_SECONDS", LOCAL_SECONDS)),
            ):
                try:
                    async with asyncio.timeout(budget):
                        if backend == "local":
                            if route["primary"][
                                "provider"
                            ] != "ollama" or not await qualified(
                                route["primary"],
                                route["fallback"],
                                messages,
                                params,
                                local_base,
                            ):
                                fail(
                                    "LOCAL_UNQUALIFIED: local qualification or context admission unavailable",
                                    503,
                                )
                            params["max_tokens"] = params.get(
                                "max_tokens", route["fallback"]["output_reserve_tokens"]
                            )
                        # Buffer upstream deltas: malformed/partial tool arguments never reach Hermes.
                        result = await self.collect(router, backend, messages, params)
                        if backend == "local":
                            bound = context_bound(messages, params)
                            if (
                                not result.usage
                                or not 0 < result.usage.prompt_tokens <= bound
                            ):
                                fail("Upstream token count violates qualification", 502)
                        return valid_response(result, params.get("tools", []))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    eligible = (
                        isinstance(
                            error,
                            (TimeoutError, litellm.Timeout, litellm.RateLimitError),
                        )
                        or (
                            isinstance(error, litellm.APIConnectionError)
                            and not isinstance(
                                error,
                                (litellm.AuthenticationError, litellm.BadRequestError),
                            )
                        )
                        or (
                            getattr(error, "status_code", 0) == 429
                            or 500 <= getattr(error, "status_code", 0) < 600
                        )
                    )
                    # Validation failures are not availability failures.
                    if isinstance(
                        error,
                        (
                            CustomLLMError,
                            litellm.AuthenticationError,
                            litellm.BadRequestError,
                            litellm.ContentPolicyViolationError,
                        ),
                    ):
                        eligible = False
                    if backend == "local" or not eligible:
                        raise

    async def collect(self, router, backend, messages, params):
        first = deadline(
            "EMURU_CLOUD_FIRST_TOKEN_SECONDS"
            if backend == "primary"
            else "EMURU_LOCAL_FIRST_TOKEN_SECONDS",
            30 if backend == "primary" else 120,
        )
        between = deadline("EMURU_INTER_CHUNK_SECONDS", 30)
        first_end = asyncio.get_running_loop().time() + first
        stream = await asyncio.wait_for(
            router.acompletion(model=backend, messages=messages, stream=True, **params),
            first,
        )
        chunks = []
        substantive = False
        try:
            iterator = stream.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(iterator),
                        max(0, first_end - asyncio.get_running_loop().time())
                        if not substantive
                        else between,
                    )
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                substantive = substantive or any(
                    c.delta.content or c.delta.tool_calls for c in chunk.choices
                )
            if not chunks:
                fail("Empty upstream stream", 502)
            # The pinned wrapper synthesizes a stop chunk on EOF; require the
            # upstream termination marker instead.
            if not any(
                choice.finish_reason
                for chunk in getattr(stream, "chunks", [])
                for choice in chunk.choices
            ):
                fail("Incomplete upstream stream", 502)
            return litellm.stream_chunk_builder(chunks, messages=messages)
        finally:
            close = getattr(stream, "aclose", None)
            if close:
                await close()

    async def acompletion(self, messages, optional_params, **kwargs):
        return await self.run(messages, optional_params)

    async def astreaming(self, messages, optional_params, **kwargs):
        result = await self.run(messages, optional_params, streaming=True)
        message = result.choices[0].message
        if message.content:
            yield {
                "text": message.content,
                "tool_use": None,
                "is_finished": False,
                "finish_reason": "",
                "usage": None,
                "index": 0,
            }
        for index, call in enumerate(message.tool_calls or []):
            tool = call.model_dump()
            tool["index"] = index
            yield {
                "text": "",
                "tool_use": tool,
                "is_finished": False,
                "finish_reason": "",
                "usage": None,
                "index": 0,
            }
        yield {
            "text": "",
            "tool_use": None,
            "is_finished": True,
            "finish_reason": result.choices[0].finish_reason,
            "usage": result.usage.model_dump() if result.usage else None,
            "index": 0,
        }


guard = Guard()
