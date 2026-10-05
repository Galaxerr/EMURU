"""Dispatch provider configuration while keeping vault policy provider-independent."""

from emuru import gemini, ollama, openai

PROVIDERS = {"gemini": gemini, "openai-api": openai, "ollama": ollama}


def provider_settings(selection: dict) -> dict:
    if (
        not isinstance(selection, dict)
        or not {"provider", "model"}.issubset(selection)
        or set(selection) - {"provider", "model", "base_url"}
    ):
        raise ValueError(
            "Model selection requires provider/model and optional Ollama base_url"
        )
    name, model = selection["provider"], selection["model"]
    if not isinstance(name, str) or name not in PROVIDERS:
        raise ValueError("Select gemini, openai-api or ollama explicitly")
    if not isinstance(model, str) or not model or model.strip() != model:
        raise ValueError("Model must be a nonempty, unquoted model ID")
    if any(character.isspace() or character in ('"', "'") for character in model):
        raise ValueError("Model ID contains invalid quoting or whitespace")
    if name == "ollama":
        return ollama.provider_settings(
            model,
            selection.get("base_url", ollama.DEFAULT_BASE_URL),
        )
    if set(selection) != {"provider", "model"}:
        raise ValueError("base_url is only configurable for Ollama")
    return PROVIDERS[name].provider_settings(model)


def check_provider(settings: dict) -> None:
    if settings["model.provider"] == "ollama":
        connection = ollama.OllamaConnection(
            settings["model.default"], settings["model.base_url"]
        )
        connection.check()
        settings.update(
            ollama.provider_settings(
                connection.model, connection.base_url, cloud=connection.cloud
            )
        )
