"""Exercise the vault server through actual stdio, without provider credentials."""

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from vault_helpers import create_vault


def test_backend_keeps_environment_isolated(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "vault_adapter", root / "scripts/hermes/vault.py"
    )
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    monkeypatch.setattr(adapter.shutil, "which", lambda _: "/mock/bin/uv")
    monkeypatch.setenv("OLLAMA_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("UV_NO_CACHE", "1")
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
    assert env["UV_NO_CACHE"] == "1"
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "EMURU_HERMES_PROFILE_HOME",
        "EMURU_VAULT_PATH",
        "UV_OFFLINE",
        "UV_NO_CACHE",
    }


def test_hermes_real_stdio_contract(mcp_environment):
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


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
@pytest.mark.parametrize("first_tool", ["vault_search", "vault_map", "vault_neighbors"])
@pytest.mark.parametrize("update_links", [False, True])
async def test_stdio_capture_external_rename(tmp_path, first_tool, update_links):
    """A filename-only Obsidian rename must refresh an already-running server."""
    vault = create_vault(tmp_path)
    old_path = "00_Inbox/test-note.md"
    new_path = "00_Inbox/no-testing.md"
    source_path = "10_Projects/source.md"
    source = vault / source_path
    source.write_text("[[00_Inbox/test-note]]")
    content = (
        "---\ntitle: Test Note\nsummary: A test note.\n---\n"
        "Rename acceptance marker. [[10_Projects/source]]"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "emuru.vault.server"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"EMURU_VAULT_PATH": str(vault)},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        async with asyncio.timeout(15):
            await session.initialize()

        async def call(name, **arguments):
            async with asyncio.timeout(15):
                result = await session.call_tool(name, arguments)
            assert not result.is_error, result.model_dump_json()
            return result.structured_content

        assert await call("vault_write", path=old_path, content=content) == {
            "created": True,
            "path": old_path,
        }
        before = await call("vault_search", query="test note")
        assert old_path in [item["path"] for item in before["results"]]
        original = (vault / old_path).read_bytes()
        (vault / old_path).rename(vault / new_path)
        if update_links:
            # Obsidian can update references when "Automatically update internal links" is on.
            source.write_text("[[00_Inbox/no-testing]]")

        arguments = {
            "vault_search": {"query": "test note"},
            "vault_map": {},
            "vault_neighbors": {"path": source_path},
        }[first_tool]
        first = await call(first_tool, **arguments)
        if first_tool == "vault_search":
            paths = [item["path"] for item in first["results"]]
            assert new_path in paths and old_path not in paths, (
                f"Stale search paths: {paths}"
            )
        elif first_tool == "vault_map":
            paths = [item["path"] for item in first["roots"]["00_Inbox"]]
            assert paths == [new_path], f"Stale map paths: {paths}"
        else:
            assert first["incoming"][0]["source"] == new_path, (
                "Stale incoming relationship"
            )

        found = await call("vault_search", query="test note")
        paths = [item["path"] for item in found["results"]]
        assert new_path in paths and old_path not in paths
        mapped = await call("vault_map")
        assert [item["path"] for item in mapped["roots"]["00_Inbox"]] == [new_path]
        opened = await call("vault_open", path=new_path)
        assert opened["content"].encode() == original
        assert opened["path"] == new_path
        neighbors = await call("vault_neighbors", path=source_path)
        assert neighbors["incoming"][0]["source"] == new_path
        outgoing = neighbors["outgoing"][0]
        assert outgoing["status"] == ("resolved" if update_links else "unresolved")
        assert outgoing["target"] == (new_path if update_links else None)
        renamed = await call("vault_neighbors", path=new_path)
        assert renamed["outgoing"][0]["target"] == source_path
        assert len(renamed["incoming"]) == int(update_links)
        assert not (vault / old_path).exists()
        assert (vault / new_path).read_bytes() == original
