"""Serve the owner's selected vault; keep prepare/inspect/check synthetic."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from emuru.vault.target import TargetError, load_target, profile_home

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/hermes-vault"
RUNTIME = ROOT / ".runtime/vault"
TOOLS = {"vault_map", "vault_search", "vault_open", "vault_neighbors", "vault_write"}
CONTAINER_PROFILE = Path("/state/profiles/emuru")


def selected_profile_home():
    if os.environ.get("EMURU_HERMES_PROFILE_HOME") or os.environ.get("HERMES_HOME"):
        return profile_home()
    if (CONTAINER_PROFILE / "vault-target.json").is_file():
        return CONTAINER_PROFILE
    return profile_home()


def backend(vault: Path, *arguments: str):
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not on PATH")
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "EMURU_HERMES_PROFILE_HOME",
        )
        if key in os.environ
    }
    env["EMURU_VAULT_PATH"] = str(vault.resolve())
    env["UV_OFFLINE"] = "1"
    command = [
        uv,
        "--directory",
        str(ROOT),
        "run",
        "--frozen",
        "--no-sync",
        *arguments,
    ]
    return command, env


def prepare(vault: Path):
    if vault.resolve() != vault.absolute():
        raise SystemExit("Synthetic vault path must not contain symlinks")
    if not vault.exists():
        shutil.copytree(FIXTURE, vault)
    command, env = backend(vault, "emuru-index", "--vault", str(vault.resolve()))
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=60)


async def inspect_or_check(vault: Path, inspect: bool):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import MCPError

    command, env = backend(vault, "emuru-vault-mcp")
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

        async def call(name, **arguments):
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
    parser.add_argument(
        "mode", choices=("prepare", "serve", "inspect", "check", "target")
    )
    parser.add_argument("--require-real", action="store_true")
    args = parser.parse_args()
    if args.require_real and args.mode not in {"serve", "target"}:
        parser.error("--require-real applies only to serve or target")
    if args.mode == "prepare":
        prepare(RUNTIME)
        print("Synthetic runtime vault prepared")
    elif args.mode in {"serve", "target"}:
        try:
            kind, vault = load_target(ROOT, selected_profile_home())
        except TargetError as error:
            raise SystemExit(f"EMURU vault target: {error}") from None
        if args.require_real and kind != "real":
            raise SystemExit("EMURU vault target: real_vault_not_selected")
        if args.mode == "target":
            print(json.dumps({"mode": kind}))
            return
        if not vault.is_dir():
            raise SystemExit("Prepare the synthetic vault or repair the private target")
        command, env = backend(vault, "emuru-vault-mcp")
        os.chdir(ROOT)
        os.execvpe(command[0], command, env)
    else:
        with tempfile.TemporaryDirectory(prefix="emuru-hermes-") as directory:
            vault = Path(directory) / "vault"
            prepare(vault)
            asyncio.run(inspect_or_check(vault, args.mode == "inspect"))


if __name__ == "__main__":
    main()
