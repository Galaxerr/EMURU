"""Gemini-specific Hermes routing; credentials are managed by Hermes."""


def provider_settings(model: str) -> dict:
    if not model.startswith("gemini-"):
        raise ValueError("Model ID does not belong to the selected Gemini provider")
    return {
        "model.provider": "gemini",
        "model.default": model,
        "model.base_url": "",
        "model.api_mode": "chat_completions",
        "model.ollama_num_ctx": 0,
        "agent.reasoning_effort": "medium",
        "providers.gemini.request_timeout_seconds": 60,
        "providers.gemini.stale_timeout_seconds": 60,
    }
