import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

from emuru.environment import read_env
from emuru.models import gateway

ROOT = Path(__file__).resolve().parents[1]


def test_container_initializer_creates_private_deployment(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    for name in (
        "OLLAMA_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "EMURU_GATEWAY_KEY",
        "EMURU_TELEGRAM_OWNER_ID",
        "EMURU_CONTAINER_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    route_source = tmp_path / "source-route.json"
    gateway.write_private(
        route_source,
        json.dumps(
            {
                "schema_version": 1,
                "primary": {"provider": "ollama", "model": "cloud"},
                "fallback": {
                    "provider": "ollama",
                    "model": "local",
                    "digest": "a" * 64,
                    "context_tokens": 65536,
                    "output_reserve_tokens": 4096,
                    "qualification": None,
                },
            }
        ),
    )
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    assert module["deployment_home"]() == Path.home() / ".local/state/emuru/container"
    docker_dir = tmp_path / "infra/docker"
    docker_dir.mkdir(parents=True, mode=0o755)
    docker_dir.chmod(0o755)
    monkeypatch.setitem(
        module["initialize"].__globals__,
        "DEPLOYMENT_ENV",
        docker_dir / "deployment.env",
    )
    home = tmp_path / "deployment"
    monkeypatch.setitem(module["initialize"].__globals__, "DEFAULT_HOME", home)
    env_path = module["initialize"](route_source=route_source, telegram_owner_id=42)
    assert env_path.name == "deployment.env"
    assert env_path.parent == docker_dir
    assert docker_dir.stat().st_mode & 0o777 == 0o755
    assert not (tmp_path / "deployment/deployment.env").exists()
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert (home / "profiles/emuru/config.yaml").read_text() == (
        "model: {}\nmcp_servers: {}\n"
    )
    assert (home / "gateway.key").read_text().startswith("sk-")
    assert (home / "ollama-api.key").read_text().strip() == ""
    assert (home / "gemini-api.key").read_text().strip() == ""
    assert (home / "openai-api.key").read_text().strip() == ""
    assert gateway.read_route(home / "route.json")["primary"]["model"] == "cloud"
    assert "EMURU_UID=" in env_path.read_text()
    assert "EMURU_TELEGRAM_OWNER_ID=42" in env_path.read_text()
    globals_ = module["initialize"].__globals__
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["container.py", "up"])
    commands = []

    def run(command, check):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module["subprocess"], "run", run)
    module["main"]()
    assert commands[0][-2:] == ["build", "emuru"]
    assert commands[1][-3:] == ["up", "-d", "--build"]
    assert all(
        command[command.index("--env-file") + 1] == str(env_path)
        for command in commands
    )
    assert not (home / "deployment.env").exists()
    assert "EMURU_GATEWAY_KEY_FILE=" in env_path.read_text()
    assert "EMURU_OLLAMA_API_KEY_FILE=" in env_path.read_text()
    assert "EMURU_GEMINI_API_KEY_FILE=" in env_path.read_text()
    assert "EMURU_OPENAI_API_KEY_FILE=" in env_path.read_text()
    assert (home / "litellm.yaml").stat().st_mode & 0o777 == 0o600
    history = home / "profiles/emuru/history.txt"
    history.write_text("saved conversation")
    gateway_key = (home / "gateway.key").read_text()
    assert module["initialize"](home) == env_path
    assert history.read_text() == "saved conversation"
    assert (home / "gateway.key").read_text() == gateway_key
    assert "EMURU_TELEGRAM_OWNER_ID=42" in env_path.read_text()


def test_container_initializer_copies_main_env_keys_to_private_runtime(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o700)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OLLAMA_API_KEY=ollama-secret\n"
        "GEMINI_API_KEY=gemini-secret\n"
        "OPENAI_API_KEY=openai-secret\n"
        "TELEGRAM_BOT_TOKEN=123:telegram-secret\n"
    )
    env_file.chmod(0o600)
    for name in (
        "OLLAMA_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "EMURU_GATEWAY_KEY",
        "EMURU_TELEGRAM_OWNER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    route_source = tmp_path / "source-route.json"
    gateway.write_private(
        route_source,
        json.dumps(
            {
                "schema_version": 1,
                "primary": {"provider": "ollama", "model": "cloud"},
                "fallback": {
                    "provider": "ollama",
                    "model": "local",
                    "digest": "a" * 64,
                    "context_tokens": 65536,
                    "output_reserve_tokens": 4096,
                    "qualification": None,
                },
            }
        ),
    )
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    docker_dir = tmp_path / "infra/docker"
    docker_dir.mkdir(parents=True)
    monkeypatch.setitem(
        module["initialize"].__globals__,
        "DEPLOYMENT_ENV",
        docker_dir / "deployment.env",
    )
    module["initialize"](
        tmp_path / "deployment",
        route_source,
        telegram_owner_id=42,
        workspace_env=env_file,
    )

    home = tmp_path / "deployment"
    assert (home / "ollama-api.key").read_text().strip() == "ollama-secret"
    assert (home / "gemini-api.key").read_text().strip() == ""
    assert (home / "openai-api.key").read_text().strip() == ""
    assert (home / "profiles/emuru/.env").read_text().strip() == (
        "TELEGRAM_BOT_TOKEN=123:telegram-secret"
    )
    assert (home / "gateway.key").read_text().strip() == read_env(env_file)[
        "EMURU_GATEWAY_KEY"
    ]
