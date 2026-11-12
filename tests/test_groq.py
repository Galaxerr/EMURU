"""Groq selection and RAM-only gateway credential boundary; synthetic keys only."""

import stat
from pathlib import Path

import pytest

from emuru.models.groq import gateway_secret
from emuru.models.providers import provider_settings


def test_gateway_secret_never_copies_credentials_to_disk(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GROQ_API_KEY=synthetic-groq\nTELEGRAM_BOT_TOKEN=unrelated\n")
    path.chmod(0o600)
    before = path.read_bytes()
    with gateway_secret(path) as source:
        secret = Path(source)
        assert secret.parent == Path("/dev/shm")
        assert secret.read_text() == "synthetic-groq\n"
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
        assert list(tmp_path.iterdir()) == [path]
    assert not secret.exists()
    assert path.read_bytes() == before


def test_missing_key_refuses_gateway_secret(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=unrelated\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Groq credential"), gateway_secret(path):
        pytest.fail("Missing credential admitted")


@pytest.mark.parametrize(
    "model", ["", "groq/openai/gpt-oss-120b", "../escape", "model name"]
)
def test_invalid_model_is_rejected(model):
    with pytest.raises(ValueError):
        provider_settings({"provider": "groq", "model": model})


def test_provider_cannot_override_destination():
    with pytest.raises(ValueError, match="only configurable for Ollama"):
        provider_settings(
            {
                "provider": "groq",
                "model": "openai/gpt-oss-120b",
                "base_url": "https://untrusted.invalid",
            }
        )
