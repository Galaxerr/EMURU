"""Ollama cloud and local connection checks with no model-weight downloads."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"


def provider_settings(
    model: str, base_url: str = DEFAULT_BASE_URL, *, cloud: bool | None = None
) -> dict:
    connection = OllamaConnection(model, base_url)
    if cloud is not None:
        connection.cloud = cloud
    return {
        "model.provider": "ollama",
        "model.default": connection.model,
        "model.base_url": connection.base_url,
        "model.api_mode": "chat_completions",
        "providers.ollama.base_url": connection.base_url,
        "providers.ollama.api_mode": "chat_completions",
        "providers.ollama.key_env": "OLLAMA_API_KEY" if connection.direct_cloud else "",
        "providers.ollama.request_timeout_seconds": 60 if connection.cloud else 180,
        "providers.ollama.stale_timeout_seconds": 60 if connection.cloud else 180,
        "model.ollama_num_ctx": 0 if connection.cloud else 65536,
        "agent.reasoning_effort": False,
    }


def ollama_base_url(value: str = DEFAULT_BASE_URL) -> str:
    """Accept loopback Ollama or its official HTTPS cloud endpoint."""
    if not isinstance(value, str):
        raise ValueError("Ollama base_url must be a local HTTP URL")  # noqa: TRY004
    parsed = urlsplit(value)
    if value == "http://ollama:11434/v1" and os.environ.get("EMURU_CONTAINER_ROUTE"):
        return value
    if value.rstrip("/") in {"https://ollama.com", "https://ollama.com/v1"}:
        return "https://ollama.com/v1"
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid Ollama port") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
        or port == 0
        or value.strip() != value
    ):
        raise ValueError("Use a loopback Ollama HTTP URL or https://ollama.com/v1")
    return f"{parsed.scheme}://{parsed.netloc}/v1"


def model_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value)
        or ".." in value
    ):
        raise ValueError("Select a valid model ID from your Ollama model list")
    return value


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = ollama_base_url(base_url)
        self.root = self.base_url.removesuffix("/v1")
        self.direct_cloud = self.root == "https://ollama.com"

    def request(self, path: str, body: dict | None = None, timeout: int = 60) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.direct_cloud:
            key = os.environ.get("OLLAMA_API_KEY", "").strip()
            if not key:
                raise RuntimeError("Set OLLAMA_API_KEY for direct Ollama cloud access")
            headers["Authorization"] = f"Bearer {key}"
        request = Request(
            self.root + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            if error.code == 401:
                raise RuntimeError(
                    "Ollama authentication required: run 'ollama signin' or check OLLAMA_API_KEY for direct cloud"
                ) from error
            if error.code in {402, 429}:
                raise RuntimeError(
                    "Ollama cloud quota/rate limit reached; check your Ollama usage"
                ) from error
            raise RuntimeError(f"Ollama {path} returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.root}; check the connection"
            ) from error
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("Ollama returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("Ollama returned an invalid response")  # noqa: TRY004
        if result.get("error"):
            raise RuntimeError(f"Ollama {path} reported an error")
        return result

    def list_models(self) -> list[dict]:
        """Return the daemon's complete model catalog, as used by ollama list."""
        models = self.request("/api/tags").get("models")
        if not isinstance(models, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in models
        ):
            raise RuntimeError("Ollama returned an invalid model catalog")
        for item in models:
            model_id(item["name"])
        return models


class OllamaConnection(OllamaClient):
    def __init__(self, model: str, base_url: str = DEFAULT_BASE_URL):
        super().__init__(base_url)
        self.model = model_id(model)
        self.cloud = self.direct_cloud or model.endswith(("-cloud", ":cloud"))

    def check(self) -> dict:
        """Check availability and discover the selected model's actual route."""
        names = (
            {self.model} if self.direct_cloud else {self.model, f"{self.model}:latest"}
        )
        registered = next(
            (item for item in self.list_models() if item["name"] in names),
            None,
        )
        if registered is None:
            if self.direct_cloud:
                raise RuntimeError("Selected model is not in Ollama's cloud catalog")
            raise RuntimeError(
                f"Ollama model {self.model} is not installed; "
                f"run 'ollama pull {self.model}'"
            )
        self.registered = registered
        if self.direct_cloud:
            # Catalog discovery does not advertise tools; smoke verifies them live.
            return {}
        info = self.request("/api/show", {"model": self.model})
        # Remote metadata also identifies user-created cloud aliases without a suffix.
        for key in ("remote_host", "remote_model"):
            values = [
                record.get(key)
                for record in (registered, info)
                if record.get(key) not in (None, "")
            ]
            if (
                any(not isinstance(value, str) for value in values)
                or len(set(values)) > 1
            ):
                raise RuntimeError("Inconsistent Ollama remote metadata")
        self.cloud = any(
            record.get(key)
            for record in (registered, info)
            for key in ("remote_host", "remote_model")
        )
        if (
            not isinstance(info.get("capabilities"), list)
            or "tools" not in info["capabilities"]
        ):
            raise RuntimeError(
                "Selected Ollama model lacks tool calling; agent vault chat requires a tools model"
            )
        return info

    def smoke(self) -> str:
        """Exercise chat and a complete tool-call round trip without a paid API."""
        info = self.check()
        messages = [
            {"role": "user", "content": "Call connection_probe with message ping."}
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "connection_probe",
                    "description": "Return the connection test marker.",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                },
            }
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "temperature": 0,
            "max_tokens": 1024,
        }
        if not self.cloud:
            body["options"] = {"num_ctx": 65536}
        if "thinking" in info.get("capabilities", []):
            body["reasoning_effort"] = "none"
        first = self.request("/v1/chat/completions", body, timeout=180)
        try:
            message = first["choices"][0]["message"]
            calls = message["tool_calls"]
            if len(calls) != 1 or calls[0]["function"]["name"] != "connection_probe":
                raise ValueError("Unexpected tool call")
            if json.loads(calls[0]["function"]["arguments"]) != {"message": "ping"}:
                raise ValueError("Unexpected tool arguments")
            messages.extend(
                [
                    message,
                    {
                        "role": "tool",
                        "tool_call_id": calls[0]["id"],
                        "content": "EMURU_OLLAMA_OK",
                    },
                    {
                        "role": "user",
                        "content": "Repeat the connection test marker from the tool result.",
                    },
                ]
            )
            second = self.request("/v1/chat/completions", body, timeout=180)
            answer = second["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or "EMURU_OLLAMA_OK" not in answer:
                raise ValueError("Missing tool result marker")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise RuntimeError("Ollama chat/tool round trip failed") from error
        return answer
