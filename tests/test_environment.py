import stat

import pytest

from emuru.environment import load_env, read_env, write_env


def test_loads_private_workspace_keys_and_keeps_process_overrides(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# local secrets\nOLLAMA_API_KEY='value=with spaces'\nOPENAI_API_KEY=file\n"
    )
    path.chmod(0o600)
    environ = {"OPENAI_API_KEY": "process"}

    load_env(path, environ)

    assert environ == {
        "OLLAMA_API_KEY": "value=with spaces",
        "OPENAI_API_KEY": "process",
    }


def test_rejects_unprotected_env_and_unknown_entries(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OLLAMA_API_KEY=secret\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_env(path)

    path.chmod(0o600)
    path.write_text("PATH=/tmp\n")
    with pytest.raises(ValueError, match="line 1"):
        read_env(path)


def test_write_env_adds_gateway_key_without_relaxing_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=existing\n")
    path.chmod(0o600)

    write_env(path, {"EMURU_GATEWAY_KEY": "internal gateway key"})

    assert read_env(path) == {
        "EMURU_GATEWAY_KEY": "internal gateway key",
        "OPENAI_API_KEY": "existing",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
