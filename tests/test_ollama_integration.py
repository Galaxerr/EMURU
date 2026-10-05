"""Opt-in Hermes vault retrieval with the selected cloud or downloaded Ollama model."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(
    os.environ.get("EMURU_TEST_HERMES_OLLAMA") != "1",
    reason="Set EMURU_TEST_HERMES_OLLAMA=1 for live cloud/local Ollama retrieval",
)
def test_live_hermes_ollama_vault_retrieval(tmp_path):
    root = Path(__file__).resolve().parents[1]
    selection = json.loads((root / "infra/hermes/model-selection.json").read_text())
    assert selection["provider"] == "ollama"
    spec = importlib.util.spec_from_file_location(
        "live_profile", root / "scripts/hermes-vault-profile.py"
    )
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    config = {}
    # Use only the reviewed local route and vault tools, with no real credentials.
    for key, value in profile.expected_settings().items():
        cursor = config
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    config["mcp_servers"] = {"vault": profile.expected_server()}
    home = tmp_path / "hermes" / "profiles" / "emuru"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    env = {**os.environ, "HERMES_HOME": str(home)}
    result = subprocess.run(
        [
            str(root / "scripts/hermes-emuru.sh"),
            "chat",
            "--oneshot",
            "-Q",
            "--max-turns",
            "4",
            "--run-budget",
            "180",
            "--format",
            "stream-json",
            "-q",
            "Use vault_open to read 10_Projects/EMURU.md and report its synthetic source marker.",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    opened = [
        event
        for event in events
        if event["type"] == "tool_use" and event.get("name") == "mcp__vault__vault_open"
    ]
    assert opened, result.stdout
    assert any(
        event.get("input", {}).get("path") == "10_Projects/EMURU.md" for event in opened
    ), result.stdout
    completed = [
        event
        for event in events
        if event["type"] == "tool_result"
        and event.get("name") == "mcp__vault__vault_open"
    ]
    assert any(
        not event.get("is_error")
        and "PHASE2_PROJECT_SOURCE_742" in event.get("output", "")
        for event in completed
    ), result.stdout
    assert any(
        event["type"] == "result"
        and "PHASE2_PROJECT_SOURCE_742" in event.get("text", "")
        for event in events
    ), result.stdout
