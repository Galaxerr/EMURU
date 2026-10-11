"""Private gateway routing and fail-closed local qualification contract."""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from emuru.models.ollama import OllamaConnection, model_id
from emuru.models.providers import provider_settings

ENDPOINT = "http://litellm:4000/v1"
QUALIFICATION_DAYS = 30
WORKLOAD_POLICY_VERSION = "synthetic-vault-v1"
QUALIFICATION_GATES = {
    "tools": True,
    "context": True,
    "latency": True,
    "hardware": True,
    "fault": True,
    "workload": True,
}
WORKLOAD_REPETITIONS = {
    backend: {
        "fact_retrieval": 5,
        "mcp_navigation": 5,
        "inbox_create_search_readback": 5,
    }
    for backend in ("cloud", "local")
}


def qualification_fingerprint(route, runtime_version, template_sha256):
    candidate = route["fallback"]
    bound = {
        "primary": route["primary"],
        "local": {key: candidate[key] for key in ("provider", "model", "digest")},
        "runtime_version": runtime_version,
        "template_sha256": template_sha256,
        "effective_context_tokens": candidate["context_tokens"],
        "output_reserve_tokens": candidate["output_reserve_tokens"],
        "workload_policy_version": WORKLOAD_POLICY_VERSION,
    }
    canonical = json.dumps(bound, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def qualification_record(route, runtime_version, template_sha256, evidence, now=None):
    now = time.time() if now is None else now
    candidate = route["fallback"]
    return {
        "schema_version": 2,
        "primary": route["primary"],
        "local": {key: candidate[key] for key in ("provider", "model", "digest")},
        "runtime_version": runtime_version,
        "template_sha256": template_sha256,
        "effective_context_tokens": candidate["context_tokens"],
        "output_reserve_tokens": candidate["output_reserve_tokens"],
        "workload_policy_version": WORKLOAD_POLICY_VERSION,
        "config_fingerprint": qualification_fingerprint(
            route, runtime_version, template_sha256
        ),
        "qualified_at": now,
        "expires_at": now + QUALIFICATION_DAYS * 86400,
        "gates": dict(QUALIFICATION_GATES),
        "provenance": "operator-qualified",
        "evidence": evidence,
    }


def valid_qualification(route, *, runtime_version=None, template_sha256=None, now=None):
    """Validate v2 evidence without trusting it to repair or widen the route."""
    candidate = route["fallback"]
    record = candidate.get("qualification")
    required = {
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
    if not isinstance(record, dict) or set(record) != required:
        return False
    timestamp = time.time() if now is None else now
    local = {key: candidate[key] for key in ("provider", "model", "digest")}
    evidence = record["evidence"]
    evidence_keys = {
        "offline_local",
        "workload_repetitions",
        "context_trials",
        "latency_samples",
        "cold_seconds",
        "warm_p95_seconds",
        "available_ram_bytes",
        "model_size_bytes",
        "gpu_present",
        "gpu_vram_bytes",
        "oom_events",
    }
    numbers = (int, float)
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 2
        or record["primary"] != route["primary"]
        or record["local"] != local
        or not isinstance(record["runtime_version"], str)
        or not record["runtime_version"]
        or not isinstance(record["template_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["template_sha256"])
        or record["effective_context_tokens"] != candidate["context_tokens"]
        or record["output_reserve_tokens"] != candidate["output_reserve_tokens"]
        or record["workload_policy_version"] != WORKLOAD_POLICY_VERSION
        or record["config_fingerprint"]
        != qualification_fingerprint(
            route, record["runtime_version"], record["template_sha256"]
        )
        or type(record["qualified_at"]) not in numbers
        or type(record["expires_at"]) not in numbers
        or not record["qualified_at"] <= timestamp < record["expires_at"]
        or record["expires_at"] - record["qualified_at"] > QUALIFICATION_DAYS * 86400
        or not isinstance(record["gates"], dict)
        or set(record["gates"]) != set(QUALIFICATION_GATES)
        or any(value is not True for value in record["gates"].values())
        or record["provenance"] not in {"operator-qualified", "deterministic-fixture"}
        or not isinstance(evidence, dict)
        or set(evidence) != evidence_keys
    ):
        return False
    if (
        evidence["offline_local"] is not True
        or evidence["workload_repetitions"] != WORKLOAD_REPETITIONS
        or type(evidence["context_trials"]) is not int
        or evidence["context_trials"] < 3
        or type(evidence["latency_samples"]) is not int
        or evidence["latency_samples"] < 20
        or type(evidence["cold_seconds"]) not in numbers
        or not 0 <= evidence["cold_seconds"] <= 180
        or type(evidence["warm_p95_seconds"]) not in numbers
        or not 0 <= evidence["warm_p95_seconds"] <= 60
        or type(evidence["available_ram_bytes"]) is not int
        or evidence["available_ram_bytes"] <= 0
        or type(evidence["model_size_bytes"]) is not int
        or evidence["model_size_bytes"] <= 0
        or type(evidence["gpu_present"]) is not bool
        or type(evidence["gpu_vram_bytes"]) is not int
        or evidence["gpu_vram_bytes"] < 0
        or (evidence["gpu_present"] and evidence["gpu_vram_bytes"] <= 0)
        or type(evidence["oom_events"]) is not int
        or evidence["oom_events"] != 0
    ):
        return False
    if runtime_version is not None and record["runtime_version"] != runtime_version:
        return False
    return template_sha256 is None or record["template_sha256"] == template_sha256


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
    if candidate["provider"] != "ollama":
        raise ValueError("Local candidate must use Ollama")
    if candidate["qualification"] is not None and not isinstance(
        candidate["qualification"], dict
    ):
        raise ValueError("Invalid qualification")
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


def settings(route=None):
    if route is not None:
        validate(route)
    endpoint = os.environ.get("EMURU_GATEWAY_URL", ENDPOINT)
    if endpoint not in {ENDPOINT, "http://127.0.0.1:4000/v1"}:
        raise ValueError("Gateway must use the authenticated local LiteLLM endpoint")
    return {
        "model.provider": "custom:emuru",
        "model.default": "emuru",
        "model.base_url": endpoint,
        "model.api_mode": "chat_completions",
        "model.ollama_num_ctx": 0,
        "agent.reasoning_effort": False,
        "providers.emuru": {
            "base_url": endpoint,
            "api_mode": "chat_completions",
            "key_env": "EMURU_GATEWAY_KEY",
            "request_timeout_seconds": 250,
            "stale_timeout_seconds": 250,
        },
    }


def upstream(primary):
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
    return params


def render(route=None):
    # Imported only by the operator renderer; profile composition stays stdlib-only.
    import yaml

    if route is not None:
        validate(route)
    return yaml.safe_dump(
        {
            "model_list": [
                {
                    "model_name": "emuru",
                    "litellm_params": {
                        "model": "emuru_guard/emuru",
                        "num_retries": 0,
                        "max_retries": 0,
                        "timeout": 245,
                    },
                }
            ]
            if route is not None
            else [],
            "general_settings": {
                "master_key": "os.environ/EMURU_GATEWAY_KEY",
                "disable_spend_logs": True,
                "cancel_on_disconnect": True,
            },
            "litellm_settings": {
                "num_retries": 0,
                "drop_params": True,
                "custom_provider_map": [
                    {
                        "provider": "emuru_guard",
                        "custom_handler": "emuru.models.gateway_guard.guard",
                    }
                ],
                "fallbacks": [],
                "success_callback": [],
                "failure_callback": [],
                "set_verbose": False,
            },
            "router_settings": {
                "num_retries": 0,
                "max_fallbacks": 0,
                "disable_cooldowns": True,
                "fallbacks": [],
                "context_window_fallbacks": [],
                "content_policy_fallbacks": [],
            },
        },
        sort_keys=True,
    )
