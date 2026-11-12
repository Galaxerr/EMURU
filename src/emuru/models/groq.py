"""Groq model validation; application inference still uses the private gateway."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from emuru.environment import read_env
from emuru.models.ollama import model_id


@contextmanager
def gateway_secret(workspace_env):
    """Expose only Groq's key as a RAM-only Docker secret for the gateway."""
    value = read_env(workspace_env).get("GROQ_API_KEY", "")
    if not value or any(character.isspace() for character in value):
        raise ValueError("Selected Groq credential required")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    if not any(
        line.split()[4] == "/dev/shm" and line.split(" - ", 1)[1].split()[0] == "tmpfs"
        for line in mounts
    ):
        raise ValueError("Groq gateway secret requires /dev/shm tmpfs")
    with tempfile.NamedTemporaryFile(
        mode="w", dir="/dev/shm", prefix="emuru-groq-", encoding="utf-8"
    ) as secret:
        os.fchmod(secret.fileno(), 0o600)
        secret.write(value + "\n")
        secret.flush()
        yield secret.name


def provider_settings(model: str) -> dict:
    model_id(model)
    if model.startswith("groq/"):
        raise ValueError("Use the Groq catalog model ID without the groq/ prefix")
    return {
        "model.provider": "custom:groq",
        "model.default": model,
        "model.base_url": "https://api.groq.com/openai/v1",
        "model.api_mode": "chat_completions",
        "model.ollama_num_ctx": 0,
        "agent.reasoning_effort": "medium",
        "providers.groq.key_env": "GROQ_API_KEY",
    }
