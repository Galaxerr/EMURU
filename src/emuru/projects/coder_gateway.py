"""Disposable cloud-only model gateway for the native coder runtime."""

import contextlib
import json
import os
import secrets
import time
from pathlib import Path

from emuru.environment import read_env
from emuru.models.gateway import read_route, render, write_private
from emuru.projects.coder_sandbox import bounded_process

ROOT = Path(__file__).resolve().parents[3]


def _docker(arguments, seconds=30):
    result = bounded_process(["docker", *arguments], b"", seconds, 65536)
    if result["exit_code"]:
        raise RuntimeError("Disposable coder gateway operation failed.")
    return result["stdout"].strip()


@contextlib.contextmanager
def cloud_gateway(folder, daemon_root=None, process_event=None):
    folder = Path(folder)
    daemon_root = Path(daemon_root) if daemon_root else ROOT
    route = read_route(Path.home() / ".local/state/emuru/container/route.json")
    values = read_env(ROOT / ".env")
    selected = {
        "ollama": "OLLAMA_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openai-api": "OPENAI_API_KEY",
    }[route["primary"]["provider"]]
    upstream_key = values.get(selected) or (
        values.get("GOOGLE_API_KEY") if selected == "GEMINI_API_KEY" else None
    )
    if not upstream_key:
        raise ValueError("Selected cloud route credential is unavailable.")
    folder.mkdir(mode=0o700)
    gateway_key = "sk-" + secrets.token_hex(32)
    write_private(folder / "gateway.key", gateway_key + "\n")
    write_private(folder / "provider.key", upstream_key + "\n")
    write_private(folder / "route.json", json.dumps(route))
    write_private(folder / "litellm.yaml", render(route))
    suffix = secrets.token_hex(8)
    model_network, egress_network = (
        "emuru-coder-model-" + suffix,
        "emuru-coder-egress-" + suffix,
    )
    name = "emuru-coder-gateway-" + suffix

    def mapped(path):
        return str(daemon_root / Path(path).relative_to(ROOT))

    created_networks = []
    container_owned = False
    try:
        existing = bounded_process(
            ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"], b"", 5, 4096
        )
        if existing["exit_code"] or existing["stdout"].strip():
            raise RuntimeError("Coder gateway container identity is unavailable.")
        for network in (model_network, egress_network):
            found = bounded_process(
                ["docker", "network", "ls", "-q", "--filter", f"name=^{network}$"],
                b"",
                5,
                4096,
            )
            if found["exit_code"] or found["stdout"].strip():
                raise RuntimeError("Coder gateway network identity is unavailable.")
        if process_event:
            process_event(name, "starting")
        container_owned = True
        created_networks.extend((model_network, egress_network))
        _docker(["network", "create", "--internal", model_network])
        _docker(["network", "create", egress_network])
        argv = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            egress_network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONPATH=/opt/emuru/src",
            "--env",
            "EMURU_CONTAINER_ROUTE=/run/emuru/route.json",
            "--env",
            "LITELLM_LOG=ERROR",
        ]
        mounts = [
            (ROOT / "src", "/opt/emuru/src"),
            (ROOT / "infra/docker/secret-exec.py", "/opt/emuru/secret-exec.py"),
            (folder / "gateway.key", "/run/secrets/emuru_gateway_key"),
            (folder / "provider.key", "/run/secrets/" + selected.lower()),
            (folder / "route.json", "/run/emuru/route.json"),
            (folder / "litellm.yaml", "/run/emuru/litellm.yaml"),
        ]
        for source, target in mounts:
            argv += [
                "--mount",
                f"type=bind,source={mapped(source)},target={target},readonly",
            ]
        pins = json.loads((ROOT / "infra/docker/deployment-lock.json").read_text())
        argv += [
            "--entrypoint",
            "/usr/bin/timeout",
            pins["litellm"],
            "-s",
            "KILL",
            "600",
            "python",
            "/opt/emuru/secret-exec.py",
            "litellm",
            "--config",
            "/run/emuru/litellm.yaml",
            "--port",
            "4000",
        ]
        _docker(argv)
        _docker(["network", "connect", "--alias", "litellm", model_network, name])
        deadline = time.monotonic() + 120
        while True:
            health = bounded_process(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness',timeout=2)",
                ],
                b"",
                5,
                4096,
            )
            if health["exit_code"] == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Disposable model gateway did not become ready.")
            time.sleep(1)
        yield model_network, gateway_key, route["primary"]
    finally:
        cleanup_error = None
        try:
            if container_owned:
                found = bounded_process(
                    ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
                    b"",
                    5,
                    4096,
                )
                if found["exit_code"]:
                    raise RuntimeError("Coder gateway inspection is unavailable.")
                if found["stdout"].strip():
                    _docker(["rm", "--force", name], 15)
                remaining = bounded_process(
                    ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
                    b"",
                    5,
                    4096,
                )
                if remaining["exit_code"] or remaining["stdout"].strip():
                    raise RuntimeError("Coder gateway termination is uncertain.")
                if process_event:
                    process_event(name, "stopped")
        except BaseException as error:  # noqa: BLE001 -- finish cleanup after interruption
            cleanup_error = error
        finally:
            for secret in ("gateway.key", "provider.key"):
                (folder / secret).unlink(missing_ok=True)
        for network in reversed(created_networks):
            try:
                found = bounded_process(
                    ["docker", "network", "ls", "-q", "--filter", f"name=^{network}$"],
                    b"",
                    5,
                    4096,
                )
                if found["exit_code"]:
                    raise RuntimeError(
                        "Coder gateway network inspection is unavailable."
                    )
                if found["stdout"].strip():
                    _docker(["network", "rm", network], 15)
                remaining = bounded_process(
                    ["docker", "network", "ls", "-q", "--filter", f"name=^{network}$"],
                    b"",
                    5,
                    4096,
                )
                if remaining["exit_code"] or remaining["stdout"].strip():
                    raise RuntimeError("Coder gateway network cleanup is uncertain.")
            except BaseException as error:  # noqa: BLE001 -- remove every network after interruption
                cleanup_error = cleanup_error or error
        if cleanup_error:
            if container_owned and process_event:
                process_event(name, "uncertain")
            raise RuntimeError("Coder gateway cleanup is uncertain.") from cleanup_error
