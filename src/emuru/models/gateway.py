"""Private container routing. Local candidates are metadata, never fallback routes."""

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from emuru.models.ollama import OllamaConnection, model_id
from emuru.models.providers import provider_settings

ENDPOINT = "http://litellm:4000/v1"


def validate(route):
    if not isinstance(route, dict) or set(route) != {
        "schema_version",
        "primary",
        "fallback",
    }:
        raise ValueError("Invalid route shape")
    if type(route["schema_version"]) is not int or route["schema_version"] != 1:
        raise ValueError("Invalid route version")
    primary, candidate = route["primary"], route["fallback"]
    if not isinstance(primary, dict) or set(primary) != {"provider", "model"}:
        raise ValueError("Invalid primary shape")
    provider_settings(primary)
    model_id(primary["model"])
    if not isinstance(candidate, dict) or set(candidate) != {
        "provider",
        "model",
        "digest",
        "context_tokens",
        "output_reserve_tokens",
        "qualification",
    }:
        raise ValueError("Invalid local candidate shape")
    model_id(candidate["model"])
    if candidate["provider"] != "ollama" or candidate["qualification"] is not None:
        raise ValueError("Local candidate must remain unqualified Ollama metadata")
    if not isinstance(candidate["digest"], str) or not re.fullmatch(
        r"(?:sha256:)?[0-9a-f]{64}", candidate["digest"]
    ):
        raise ValueError("Invalid local digest")
    context, reserve = candidate["context_tokens"], candidate["output_reserve_tokens"]
    if (
        type(context) is not int
        or type(reserve) is not int
        or not 0 < reserve < context
    ):
        raise ValueError("Invalid context/output relationship")
    return route


def private_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Private route path must be absolute")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Private route symlinks refused")
    return path


def read_route(path):
    path = private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_size > 4096
        ):
            raise ValueError(
                "Route must be owner-owned, mode 0600, and at most 4096 bytes"
            )
        return validate(json.load(stream))


def write_private(path, text):
    path = private_path(path)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError("Private output directory must be owner-owned and mode 0700")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def select_pair(
    primary, local, base_url, provider="ollama", context=65536, reserve=4096
):
    if base_url.rstrip("/") in {"https://ollama.com", "https://ollama.com/v1"}:
        raise ValueError("Pair selection requires the daemon inventory")
    selection = {"provider": provider, "model": primary}
    provider_settings(selection)
    if provider == "ollama":
        connection = OllamaConnection(primary, base_url)
        info = connection.check()
        if not connection.cloud:
            raise ValueError("Primary must be a cloud model")
        selection["model"] = model_id(
            connection.registered.get("remote_model")
            or info.get("remote_model")
            or primary
        )
    connection = OllamaConnection(local, base_url)
    info = connection.check()
    catalog = connection.registered
    if connection.cloud:
        raise ValueError("Local candidate has a remote route")
    if (
        type(catalog.get("size")) is not int
        or catalog["size"] <= 0
        or not isinstance(info.get("model_info"), dict)
        or not info["model_info"]
    ):
        raise ValueError("Local candidate weights unavailable")
    return validate(
        {
            "schema_version": 1,
            "primary": selection,
            "fallback": {
                "provider": "ollama",
                "model": local,
                "digest": catalog.get("digest"),
                "context_tokens": context,
                "output_reserve_tokens": reserve,
                "qualification": None,
            },
        }
    )


def settings(route):
    validate(route)
    return {
        "model.provider": "custom:emuru",
        "model.default": "emuru",
        "model.base_url": ENDPOINT,
        "model.api_mode": "chat_completions",
        "model.ollama_num_ctx": 0,
        "agent.reasoning_effort": False,
        "providers.emuru": {
            "base_url": ENDPOINT,
            "api_mode": "chat_completions",
            "key_env": "EMURU_GATEWAY_KEY",
            "request_timeout_seconds": 180,
            "stale_timeout_seconds": 180,
        },
    }


def render(route=None):
    # Imported only by the operator renderer; profile composition stays stdlib-only.
    import yaml

    primary = (
        validate(route)["primary"]
        if route is not None
        else {"provider": "ollama", "model": "unselected"}
    )
    provider = primary["provider"]
    params = {
        "model": {"ollama": "ollama_chat", "gemini": "gemini", "openai-api": "openai"}[
            provider
        ]
        + "/"
        + primary["model"],
        "num_retries": 0,
        "max_retries": 0,
    }
    if provider == "ollama":
        params["api_base"] = "https://ollama.com"
        params["api_key"] = "os.environ/OLLAMA_API_KEY"
    else:
        params["api_key"] = (
            "os.environ/"
            + {"gemini": "GEMINI_API_KEY", "openai-api": "OPENAI_API_KEY"}[provider]
        )
    return yaml.safe_dump(
        {
            "model_list": [{"model_name": "emuru", "litellm_params": params}]
            if route is not None
            else [],
            "general_settings": {
                "master_key": "os.environ/EMURU_GATEWAY_KEY",
                "disable_spend_logs": True,
            },
            "litellm_settings": {
                "num_retries": 0,
                "fallbacks": [],
                "success_callback": [],
                "failure_callback": [],
                "set_verbose": False,
            },
            "router_settings": {
                "num_retries": 0,
                "max_fallbacks": 0,
                "fallbacks": [],
                "context_window_fallbacks": [],
                "content_policy_fallbacks": [],
            },
        },
        sort_keys=True,
    )
