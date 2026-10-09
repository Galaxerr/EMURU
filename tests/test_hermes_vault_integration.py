"""Exercise the vault server through actual stdio, without provider credentials."""

import importlib.util
import subprocess
import sys
from pathlib import Path


def test_backend_keeps_environment_isolated(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "vault_adapter", root / "scripts/hermes/vault.py"
    )
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    monkeypatch.setattr(adapter.shutil, "which", lambda _: "/mock/bin/uv")
    monkeypatch.setenv("OLLAMA_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("EMURU_VAULT_PATH", "/unrelated/vault")
    monkeypatch.setenv("EMURU_HERMES_PROFILE_HOME", "/state/profiles/emuru")
    command, env = adapter.backend(tmp_path, "emuru-vault-mcp")
    assert command == [
        "/mock/bin/uv",
        "--directory",
        str(root),
        "run",
        "--frozen",
        "--no-sync",
        "emuru-vault-mcp",
    ]
    assert env["EMURU_VAULT_PATH"] == str(tmp_path.resolve())
    assert env["UV_OFFLINE"] == "1"
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "EMURU_HERMES_PROFILE_HOME",
        "EMURU_VAULT_PATH",
        "UV_OFFLINE",
    }


def test_hermes_real_stdio_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/hermes/vault.py"), "check"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Stdio retrieval" in result.stdout


def test_container_profile_fallback_requires_explicit_target(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "vault_adapter", root / "scripts/hermes/vault.py"
    )
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    monkeypatch.delenv("EMURU_HERMES_PROFILE_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(adapter, "CONTAINER_PROFILE", tmp_path / "profiles/emuru")
    adapter.CONTAINER_PROFILE.mkdir(parents=True)
    assert adapter.selected_profile_home() == adapter.profile_home()
    (adapter.CONTAINER_PROFILE / "vault-target.json").write_text("{}")
    assert adapter.selected_profile_home() == adapter.CONTAINER_PROFILE
