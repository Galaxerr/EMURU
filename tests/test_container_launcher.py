import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from emuru.environment import read_env
from emuru.models import gateway

ROOT = Path(__file__).resolve().parents[1]


def test_container_initializer_creates_private_deployment(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    vault = tmp_path / "vault"
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / ".git").mkdir()
    monkeypatch.setenv("EMURU_VAULT_PATH", str(vault))
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
    assert json.loads((home / "profiles/emuru/vault-target.json").read_text()) == {
        "mode": "real",
        "vault_path": "/state/vault",
    }
    assert (home / "gateway.key").read_text().startswith("sk-")
    assert (home / "ollama-api.key").read_text().strip() == ""
    assert (home / "gemini-api.key").read_text().strip() == ""
    assert (home / "openai-api.key").read_text().strip() == ""
    assert gateway.read_route(home / "route.json")["primary"]["model"] == "cloud"
    assert "EMURU_UID=" in env_path.read_text()
    assert "EMURU_TELEGRAM_OWNER_ID=42" in env_path.read_text()
    assert f"EMURU_VAULT={vault}" in env_path.read_text()
    globals_ = module["initialize"].__globals__
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(f"EMURU_VAULT_PATH={vault}\n")
    (tmp_path / ".env").chmod(0o600)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["container.py", "up"])
    commands = []

    def run(command, check):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module["subprocess"], "run", run)
    initialized = []
    initialize = module["initialize"]

    def prepare(*args):
        initialized.append(args)
        return initialize(*args)

    monkeypatch.setitem(globals_, "initialize", prepare)
    module["main"]()
    assert len(initialized) == 1
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


@pytest.mark.parametrize(
    "command,arguments", [("down", ["down", "--remove-orphans"]), ("status", ["ps"])]
)
def test_saved_deployment_operates_without_vault_or_initialization(
    tmp_path, monkeypatch, command, arguments
):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    globals_ = module["main"].__globals__
    home = tmp_path / "custom-deployment"
    home.mkdir()
    metadata = tmp_path / "deployment.env"
    metadata.write_text(
        f"EMURU_PROFILE={home}/profiles/emuru\nEMURU_VAULT={tmp_path}/missing-vault\n"
    )
    metadata.chmod(0o600)
    secret = home / "gateway.key"
    secret.write_text("saved-key")
    secret.chmod(0o600)
    # Invalid workspace credentials must not prevent observing/stopping saved work.
    (tmp_path / ".env").write_text("invalid")
    before = {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in (metadata, secret, tmp_path / ".env")
    }
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    monkeypatch.setitem(globals_, "DEPLOYMENT_ENV", metadata)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["container.py", "--home", str(home), command])

    def forbidden(*args):
        raise AssertionError("Saved lifecycle command attempted initialization")

    monkeypatch.setitem(globals_, "initialize", forbidden)
    monkeypatch.setitem(globals_, "_vault_path", forbidden)
    executed = []
    monkeypatch.setattr(
        module["subprocess"],
        "run",
        lambda command, **kwargs: (
            executed.append(command) or SimpleNamespace(returncode=0)
        ),
    )
    module["main"]()
    assert len(executed) == 1 and executed[0][-len(arguments) :] == arguments
    assert executed[0][executed[0].index("--env-file") + 1] == str(metadata)
    assert {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in before
    } == before
    assert list(home.iterdir()) == [secret]


def test_saved_deployment_rejects_different_home_without_docker(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    metadata = tmp_path / "deployment.env"
    metadata.write_text(f"EMURU_PROFILE={tmp_path}/other/profiles/emuru\n")
    metadata.chmod(0o600)
    monkeypatch.setitem(module["compose"].__globals__, "DEPLOYMENT_ENV", metadata)
    monkeypatch.setattr(
        module["subprocess"],
        "run",
        lambda *args, **kwargs: pytest.fail("Wrong deployment reached Docker"),
    )
    with pytest.raises(ValueError, match="does not match"):
        module["compose"](tmp_path / "requested", "down")


@pytest.mark.parametrize(
    "linked",
    ["home", "parent", "profiles", "state", "gateway.key", "route-source", "metadata"],
)
def test_initialization_rejects_symlinks_before_mutation(tmp_path, monkeypatch, linked):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    home = tmp_path / "deployment"
    home.mkdir(mode=0o755)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    (target / "keep.txt").write_text("unchanged")
    route = tmp_path / "route.json"
    route.write_text("{}")
    metadata = tmp_path / "deployment.env"
    if linked == "home":
        home.rmdir()
        home.symlink_to(target, target_is_directory=True)
    elif linked == "parent":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(target, target_is_directory=True)
        home = parent / "deployment"
    elif linked == "route-source":
        route.unlink()
        route.symlink_to(target / "keep.txt")
    elif linked == "metadata":
        metadata.symlink_to(target / "keep.txt")
    else:
        (home / linked).symlink_to(target, target_is_directory=True)
    monkeypatch.setitem(module["initialize"].__globals__, "DEPLOYMENT_ENV", metadata)

    def snapshot():
        return {
            str(path.relative_to(tmp_path)): (
                path.lstat().st_mode,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
                path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            )
            for path in (tmp_path, *tmp_path.rglob("*"))
        }

    before = snapshot()
    with pytest.raises(ValueError, match="symlinks"):
        module["initialize"](home, route, telegram_owner_id=42)
    assert snapshot() == before


def test_container_initializer_copies_main_env_keys_to_private_runtime(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o700)
    vault = tmp_path / "vault"
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / ".git").mkdir()
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
    monkeypatch.setenv("EMURU_VAULT_PATH", str(vault))
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
