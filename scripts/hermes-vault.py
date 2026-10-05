"""Connect the tested v0.2.0 commands to a synthetic vault; no model calls."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/hermes-vault"
RUNTIME = ROOT / ".runtime/vault"
TOOLS = {"vault_map", "vault_search", "vault_open", "vault_neighbors", "vault_write"}


def binding() -> dict:
    data = json.loads((ROOT / "infra/hermes/vault-binding.json").read_text())
    if "CHANGE_ME" in json.dumps(data):
        raise ValueError("Finish vault-binding.json using your actual v0.2.0 interface")
    for name in ("server_args", "indexer_args"):
        if (
            not isinstance(data.get(name), list)
            or not data[name]
            or not all(isinstance(x, str) for x in data[name])
        ):
            raise ValueError(f"{name} must be a nonempty argv list after 'uv run'")
        if data[name][0] == "uv":
            raise ValueError("Do not include 'uv run' in the bound argv lists")
    if not isinstance(data.get("env"), dict):
        raise TypeError("env must be a mapping")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data["env"].items()
    ):
        raise ValueError("env names and values must be strings")
    for kind in ("server", "indexer"):
        encoded = json.dumps([data[f"{kind}_args"], data["env"]])
        if "@VAULT_ROOT@" not in encoded:
            raise ValueError(f"{kind} invocation must be bound to @VAULT_ROOT@")
    for name in TOOLS:
        if not isinstance(data.get("tool_args", {}).get(name), dict):
            raise TypeError(f"Missing argument template for {name}")
    return data


def render(value, replacements):
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [render(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: render(item, replacements) for key, item in value.items()}
    return value


def backend(vault: Path, kind: str):
    config = binding()
    replacements = {
        "@VAULT_ROOT@": str(vault.resolve()),
        "@INDEX_DIR@": str((vault / "_index").resolve()),
        "@REPO_ROOT@": str(ROOT),
    }
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not on PATH")
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    env.update(render(config["env"], replacements))
    env["UV_OFFLINE"] = "1"
    command = [
        uv,
        "--directory",
        str(ROOT),
        "run",
        "--frozen",
        "--no-sync",
        *render(config[f"{kind}_args"], replacements),
    ]
    return command, env


def prepare(vault: Path):
    if not vault.exists():
        shutil.copytree(FIXTURE, vault)
    command, env = backend(vault, "indexer")
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=60)


async def inspect_or_check(vault: Path, inspect: bool):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import MCPError

    command, env = backend(vault, "server")
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=env, cwd=str(ROOT)
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        async with asyncio.timeout(15):
            await session.initialize()
            listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        if names != TOOLS:
            raise AssertionError(f"Unexpected MCP surface: {sorted(names)}")
        if inspect:
            print(
                json.dumps(
                    {tool.name: tool.input_schema for tool in listed.tools},
                    indent=2,
                )
            )
            return
        templates = binding()["tool_args"]

        async def call(name, **values):
            replacements = {f"@{key.upper()}@": value for key, value in values.items()}
            arguments = render(templates[name], replacements)
            async with asyncio.timeout(15):
                return await session.call_tool(name, arguments)

        def text(result):
            if result.is_error:
                raise AssertionError(f"MCP tool failed: {result.model_dump_json()}")
            return result.model_dump_json()

        mapped = text(await call("vault_map"))
        if "EMURU" not in mapped:
            raise AssertionError("Root map did not include EMURU")
        project = text(await call("vault_open", path="10_Projects/EMURU.md"))
        if not all(
            word in project
            for word in ("Telegram", "Hermes", "MCP", "Synthetic source marker")
        ):
            raise AssertionError("Project fact was not retrieved from the fixture")
        found = text(await call("vault_search", query="EMURU"))
        if "EMURU" not in found:
            raise AssertionError("Search did not return the fixture project")
        neighbors = text(await call("vault_neighbors", path="10_Projects/EMURU.md"))
        if not all(word in neighbors for word in ("Hermes", "MCP")):
            raise AssertionError("Expected outgoing relationships were not returned")
        text(await call("vault_open", path="30_Resources/Hermes.md"))

        protected = {
            path: path.read_bytes()
            for folder in ("10_Projects", "30_Resources")
            for path in (vault / folder).glob("*.md")
        }
        note_path = "00_Inbox/daily-briefing.md"
        content = (
            "---\n"
            "title: Daily briefing idea\n"
            "type: idea\n"
            "project: EMURU\n"
            "tags: [emuru, briefing]\n"
            "updated: 2026-10-05\n"
            "summary: Briefing with pending tasks, failed jobs and sync status.\n"
            "---\n\n"
            "# Daily briefing idea\n\n"
            "Add a daily briefing showing pending tasks, failed jobs and "
            "vault synchronization status.\n\n"
            "Acceptance marker: BRIEFING_742\n"
        )
        text(await call("vault_write", path=note_path, content=content))
        created = vault / note_path
        if not created.is_file() or "BRIEFING_742" not in created.read_text():
            raise AssertionError("Inbox write did not create the requested note")
        searchable = text(await call("vault_search", query="BRIEFING_742"))
        if "BRIEFING_742" not in searchable and "daily-briefing" not in searchable:
            raise AssertionError("Inbox write was not immediately searchable")

        # One integration spot-check; existing v0.2.0 policy tests stay authoritative.
        try:
            rejected = await call(
                "vault_write",
                path="10_Projects/forbidden-write.md",
                content=content,
            )
        except MCPError:
            rejected = None
        if (vault / "10_Projects/forbidden-write.md").exists():
            raise AssertionError("Forbidden write created a file")
        if rejected is not None and not rejected.is_error:
            serialized = rejected.model_dump_json().lower()
            if not any(
                term in serialized
                for term in (
                    "error",
                    "denied",
                    "forbidden",
                    "not allowed",
                    "restricted",
                    "outside",
                )
            ):
                raise AssertionError("Forbidden write was not reported as rejected")
        if any(path.read_bytes() != original for path, original in protected.items()):
            raise AssertionError("An existing protected note was modified")
        print("Stdio retrieval / relationships / Inbox write / reindex / policy: PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "serve", "inspect", "check"))
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(RUNTIME)
        print("Synthetic runtime vault prepared")
    elif args.mode == "serve":
        if not RUNTIME.is_dir():
            raise SystemExit("Run hermes-vault.py prepare before connecting Hermes")
        command, env = backend(RUNTIME, "server")
        os.chdir(ROOT)
        os.execvpe(command[0], command, env)
    else:
        with tempfile.TemporaryDirectory(prefix="emuru-hermes-") as directory:
            vault = Path(directory) / "vault"
            prepare(vault)
            asyncio.run(inspect_or_check(vault, args.mode == "inspect"))


if __name__ == "__main__":
    main()
