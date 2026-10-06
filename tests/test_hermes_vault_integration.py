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
