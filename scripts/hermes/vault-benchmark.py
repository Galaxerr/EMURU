"""Opt-in synthetic freshness timings over real MCP stdio; no live vault access."""

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("unchanged", "create", "edit", "delete", "rename", "retry")
TOOLS = ("vault_search", "vault_map", "vault_neighbors")
TARGET = "10_Projects/note-00001.md"
ANCHOR = "10_Projects/note-00000.md"


def content(number, token="basetoken"):
    header = (
        f"---\ntitle: Note {number:05d}\ntype: project\nproject: synthetic\n"
        f"tags: [benchmark, deterministic]\nsummary: {token}\n---\n"
        f"{token} [[10_Projects/note-00000]] [[10_Projects/note-00001]]\n"
    ).encode()
    filler = b"Deterministic synthetic retrieval text with metadata and links. "
    return header + (filler * 200)[: 10 * 1024 - len(header)]


def server():
    # Inject one external edit after staging, without adding a production test hook.
    from emuru.vault import indexer
    from emuru.vault.server import main

    original = indexer.write_human_index
    vault = Path(os.environ["EMURU_VAULT_PATH"])

    def write(root, notes):
        result = original(root, notes)
        flag = vault / ".benchmark-retry"
        if flag.exists():
            flag.unlink()
            with (vault / TARGET).open("ab") as stream:
                stream.write(b"\nretrycompleted\n")
        return result

    indexer.write_human_index = write
    main()


async def measure(count):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="emuru-freshness-") as temporary:
        vault = Path(temporary)
        (vault / "10_Projects").mkdir()
        for number in range(count):
            (vault / f"10_Projects/note-{number:05d}.md").write_bytes(content(number))
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve()), "--server"],
            cwd=str(ROOT),
            env={"PATH": os.environ["PATH"], "EMURU_VAULT_PATH": str(vault)},
        )
        initialization = time.perf_counter()
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            async with asyncio.timeout(15):
                await session.initialize()
            initialization = time.perf_counter() - initialization

            async def call(tool, token="basetoken"):
                arguments = {
                    "vault_search": {"query": token, "limit": 20},
                    "vault_map": {},
                    "vault_neighbors": {"path": ANCHOR},
                }[tool]
                started = time.perf_counter()
                async with asyncio.timeout(15):
                    result = await session.call_tool(tool, arguments)
                elapsed = time.perf_counter() - started
                assert not result.is_error, result.model_dump_json()
                return elapsed, result.structured_content

            timings = {}
            for tool in TOOLS:
                for scenario in SCENARIOS:
                    samples = []
                    for sample in range(5):
                        target = vault / TARGET
                        renamed = vault / "10_Projects/renamed.md"
                        created = vault / "10_Projects/created.md"
                        renamed.unlink(missing_ok=True)
                        created.unlink(missing_ok=True)
                        target.write_bytes(content(1, f"before{sample}token"))
                        # Establish the same completed baseline before each sample.
                        await call("vault_search")
                        token = f"fresh{sample}token"
                        expected_path = TARGET
                        if scenario == "unchanged":
                            token = "basetoken"
                        elif scenario == "create":
                            created.write_bytes(content(count, token))
                            expected_path = "10_Projects/created.md"
                        elif scenario in {"edit", "retry"}:
                            target.write_bytes(content(1, token))
                            if scenario == "retry":
                                (vault / ".benchmark-retry").touch()
                        elif scenario == "delete":
                            token = f"before{sample}token"
                            target.unlink()
                        elif scenario == "rename":
                            target.rename(renamed)
                            token = f"before{sample}token"
                            expected_path = "10_Projects/renamed.md"
                        elapsed, result = await call(tool, token)
                        assert elapsed < 15
                        if tool == "vault_search":
                            if scenario == "delete":
                                assert result["count"] == 0
                            else:
                                assert result["count"] > 0
                                if scenario != "unchanged":
                                    assert result["results"][0]["path"] == expected_path
                        elif tool == "vault_map":
                            assert result["total_notes"] == count + (
                                scenario == "create"
                            ) - (scenario == "delete")
                            paths = [
                                item["path"] for item in result["roots"]["10_Projects"]
                            ]
                            if scenario in {"delete", "rename"}:
                                assert TARGET not in paths
                            elif scenario in {"edit", "retry"}:
                                assert (
                                    next(
                                        item
                                        for item in result["roots"]["10_Projects"]
                                        if item["path"] == TARGET
                                    )["summary"]
                                    == token
                                )
                        else:
                            edge = next(
                                edge
                                for edge in result["outgoing"]
                                if edge["raw_target"] == TARGET[:-3]
                            )
                            assert edge["status"] == (
                                "unresolved"
                                if scenario in {"delete", "rename"}
                                else "resolved"
                            )
                            if scenario == "create":
                                assert any(
                                    edge["source"] == expected_path
                                    for edge in result["incoming"]
                                )
                        if scenario == "retry":
                            assert not (vault / ".benchmark-retry").exists()
                            assert target.read_bytes().endswith(b"retrycompleted\n")
                        samples.append(elapsed)
                    timings[f"{tool}/{scenario}"] = {
                        "median_seconds": round(statistics.median(samples), 4),
                        "max_seconds": round(max(samples), 4),
                        "samples_seconds": samples,
                    }
            return {
                "notes": count,
                "bytes_per_note": 10240,
                "initialization_seconds": round(initialization, 4),
                "timings": timings,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--notes", nargs="+", type=int, default=[100, 1000, 5000])
    parser.add_argument("--output", type=Path, required=False)
    args = parser.parse_args()
    if args.server:
        server()
        return
    report = {
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "platform": platform.platform(),
        "cpu": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            platform.processor(),
        ),
        "logical_cpus": os.cpu_count(),
        "python": sys.version,
        "results": [],
        "failures": [],
    }
    for count in args.notes:
        try:
            result = asyncio.run(measure(count))
            report["results"].append(result)
            print(
                f"{count} notes: PASS; initialization {result['initialization_seconds']}s; worst call {max(row['max_seconds'] for row in result['timings'].values())}s",
                flush=True,
            )
        except (Exception, BaseExceptionGroup) as error:  # noqa: BLE001 -- retain scaling failures in the report
            report["failures"].append({"notes": count, "error": repr(error)})
            print(f"{count} notes: FAIL: {error!r}", flush=True)
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
