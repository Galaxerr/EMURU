"""OpenAI-specific Hermes routing; credentials are managed by Hermes."""


def provider_settings(model: str) -> dict:
    if not model.startswith("gpt-"):
        raise ValueError("Model ID does not belong to the selected OpenAI provider")
    return {
        "model.provider": "openai-api",
        "model.default": model,
        "model.base_url": "https://api.openai.com/v1",
        "model.api_mode": "codex_responses",
        "model.ollama_num_ctx": 0,
        "agent.reasoning_effort": "medium",
        "providers.openai-api.request_timeout_seconds": 60,
        "providers.openai-api.stale_timeout_seconds": 60,
    }
