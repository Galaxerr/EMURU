"""Exercise real wake in disposable Docker state, with synthetic inference and no Telegram HTTP."""

import hashlib
import json
import os
import runpy
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

from emuru.deployment import READY_CHECKPOINT, source_identity
from emuru.models.gateway import render, write_private

ROOT = Path(__file__).resolve().parents[2]


def build_rollback_fixture(source, base_image):
    """Derive the changed source image without reinstalling either runtime."""
    identity = source_identity(source)
    with tempfile.TemporaryDirectory(dir=source.parent) as temporary:
        context = Path(temporary)
        shutil.copyfile(source / "src/emuru/__init__.py", context / "__init__.py")
        subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--tag",
                f"emuru:local-{identity}",
                "--label",
                f"io.emuru.source={identity}",
                "--file",
                "-",
                str(context),
            ],
            input=(
                f"FROM {base_image}\n"
                "COPY --chmod=644 __init__.py /opt/emuru/src/emuru/__init__.py\n"
            ),
            text=True,
            check=True,
        )


def verify(*, native_checked=False):
    checks = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))
    daemon_root = checks["docker_workspace"](ROOT)

    def daemon_path(path):
        return str(daemon_root / Path(path).relative_to(ROOT))

    runtime = ROOT / ".runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    original_run = subprocess.run
    with tempfile.TemporaryDirectory(prefix="emuru-wake-", dir=runtime) as temporary:
        home = Path(temporary)
        module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
        scope = module["wake"].__globals__
        # Never resolve production metadata, workspace credentials, or the owner's vault.
        env_file, compose_file = home / "deployment.env", home / "compose.yaml"
        scope.update(
            DEPLOYMENT_ENV=env_file, COMPOSE_FILE=compose_file, PROJECT_NAME=home.name
        )
        vault = home / "vault"
        (vault / "00_Inbox").mkdir(parents=True, mode=0o700)
        (vault / ".git").mkdir(mode=0o700)
        values = module["prepare_deployment"](home, vault, 42)
        # Docker otherwise creates root-owned parents for the nested profile bind.
        (Path(values["EMURU_STATE"]) / "profiles/emuru").mkdir(parents=True, mode=0o700)
        for key in (
            "EMURU_GATEWAY_KEY_FILE",
            "EMURU_OLLAMA_API_KEY_FILE",
            "EMURU_GEMINI_API_KEY_FILE",
            "EMURU_OPENAI_API_KEY_FILE",
        ):
            write_private(
                values[key],
                "synthetic-ollama-key"
                if key == "EMURU_OLLAMA_API_KEY_FILE"
                else "synthetic-gateway-key",
            )
        route = {
            "schema_version": 1,
            "primary": {"provider": "ollama", "model": "synthetic-cloud"},
            "fallback": {
                "provider": "ollama",
                "model": "synthetic-local",
                "digest": "a" * 64,
                "context_tokens": 65536,
                "output_reserve_tokens": 4096,
                "qualification": None,
            },
        }
        write_private(values["EMURU_ROUTE"], json.dumps(route))
        write_private(values["EMURU_GATEWAY_CONFIG"], render(route))
        write_private(
            env_file,
            "".join(
                f"{key}={shlex.quote(str(value))}\n" for key, value in values.items()
            ),
        )
        identity = module["source_identity"](ROOT)
        # Reuse the existing acceptance image for seed/HTTP fixtures. Wake itself
        # must build/select the content-addressed release, rather than this runner.
        image = original_run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                os.environ.get("EMURU_TEST_IMAGE", "emuru:latest"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        config = yaml.safe_load((ROOT / "infra/docker/compose.yaml").read_text())
        config["name"] = home.name
        checks["fixture_secrets"](config)
        config["services"]["litellm"]["healthcheck"].update(interval="1s", retries=120)
        # Resolve relative source binds without changing any production file.
        for service in config["services"].values():
            for volume in service.get("volumes", []):
                if volume["source"].startswith("${EMURU_SOURCE_ROOT"):
                    volume["source"] = volume["source"].replace(
                        "../..", str(daemon_root)
                    )
        config["services"]["emuru"]["build"]["context"] = str(ROOT)
        config["services"]["emuru"]["image"] = "${EMURU_IMAGE:-" + image + "}"
        mount = {
            "type": "bind",
            "source": daemon_path(ROOT / "scripts/hermes/container-check.py"),
            "target": "/check.py",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
        config["services"]["emuru"]["volumes"].append(mount)
        # Native acceptance runs once; lifecycle cycles exercise a synthetic poller.
        worker = "exec /opt/emuru/.venv/bin/python -u -c " + shlex.quote(
            f"import signal,time,sys; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); print({READY_CHECKPOINT!r}, flush=True); time.sleep(3600)"
        )
        config["services"]["emuru"]["command"] = ["bash", "-c", worker]
        config["services"]["litellm"]["environment"].update(
            EMURU_GATEWAY_TEST_FIXTURE="1",
            EMURU_TEST_PRIMARY_BASE="http://ollama:11434",
        )
        config["services"]["ollama"] = {
            "image": image,
            "restart": "no",
            "entrypoint": ["python", "/check.py", "fixture"],
            "volumes": [mount],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://localhost:11434/stats')",
                ],
                "interval": "1s",
                "timeout": "5s",
                "retries": 30,
            },
        }
        write_private(compose_file, yaml.safe_dump(config))
        command = [
            "docker",
            "compose",
            "--project-name",
            home.name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
        ]

        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["systemctl", "--user", "is-active"]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            if args[-2:] == ["scripts/hermes/telegram.sh", "--check"]:
                # Check each target profile without repeating native tests or calling Telegram.
                args = [
                    *args[:-2],
                    "/opt/emuru/.venv/bin/python",
                    "-m",
                    "emuru.deployment",
                    "profile-check",
                ]
            if args[:2] == ["docker", "compose"]:
                env = {**os.environ, **kwargs.get("env", {})}
                env.update(
                    (key, daemon_path(value))
                    for key, value in values.items()
                    if isinstance(value, Path)
                )
                if "EMURU_SOURCE_ROOT" in env:
                    env["EMURU_SOURCE_ROOT"] = daemon_path(env["EMURU_SOURCE_ROOT"])
                kwargs["env"] = env
            return original_run(args, **kwargs)

        def compose(*args):
            return run(
                [*command, *args], check=True, capture_output=True, text=True
            ).stdout.strip()

        # Missing runtime and profile are created only in this disposable home.
        try:
            compose(
                "run",
                "--rm",
                "--no-deps",
                "emuru",
                "/opt/emuru/.venv/bin/python",
                "scripts/hermes/vault.py",
                "prepare",
            )
            compose(
                "run",
                "--rm",
                "--no-deps",
                "emuru",
                "/opt/emuru/.venv/bin/python",
                "/check.py",
                "seed",
            )
            if not native_checked:
                compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "emuru",
                    "scripts/hermes/telegram.sh",
                    "--runtime-check",
                )
            profile = Path(values["EMURU_PROFILE"])
            cfg = yaml.safe_load((profile / "config.yaml").read_text())
            cfg["mcp_servers"]["vault"]["tools"]["include"].remove("vault_propose")
            write_private(profile / "config.yaml", yaml.safe_dump(cfg))
            preserved = {
                p: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in profile.rglob("*")
                if p.is_file() and p.name not in {"config.yaml", "SOUL.md"}
            }
            subprocess.run = run
            module["wake"](home)
            print("First disposable wake: PASS", flush=True)
            first = json.loads((home / "release.json").read_text())
            release_image = original_run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    f"emuru:local-{identity}",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert first["source"] == identity and first["image"] == release_image
            assert (profile / "SOUL.md").read_bytes() == (
                ROOT / "agents/emuru/SOUL.md"
            ).read_bytes()
            cfg = yaml.safe_load((profile / "config.yaml").read_text())
            assert (
                cfg["mcp_servers"]["vault"]["tools"]["include"][-1] == "vault_propose"
            )
            assert all(
                hashlib.sha256(p.read_bytes()).hexdigest() == digest
                for p, digest in preserved.items()
            )
            previous = len(calls)
            module["wake"](home)
            assert not any(
                "build" in c or "up" in c or "run" in c for c in calls[previous:]
            )
            module["sleep"](home)
            before = {
                p: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in profile.rglob("*")
                if p.is_file()
            }
            previous = len(calls)
            module["wake"](home)
            assert not any(c[:2] == ["docker", "build"] for c in calls[previous:])
            assert all(
                (p.read_bytes(), p.stat().st_mtime_ns) == data
                for p, data in before.items()
            )
            assert json.loads((home / "release.json").read_text()) == first
            module["sleep"](home)
            # Reuse the verified runtime; only the rollback fixture's source bytes differ.
            newer = home / "newer-source"
            shutil.copytree(home / "sources" / identity, newer)
            with (newer / "src/emuru/__init__.py").open("a") as stream:
                stream.write("\n# Disposable rollback compatibility fixture.\n")
            build_rollback_fixture(newer, f"emuru:local-{identity}")
            scope["ROOT"] = newer
            previous = len(calls)
            module["wake"](home)
            assert not any(c[:2] == ["docker", "build"] for c in calls[previous:])
            assert json.loads((home / "release.json").read_text())["source"] != identity
            module["sleep"](home)
            effect = """
from pathlib import Path
from emuru.hermes import profile
from emuru.vault import mutations
home, vault = profile.profile_home(), Path('/state/vault')
request, repeated = mutations.begin_request(home, vault, 42, 7, 100, 'newest rollback effect')
assert not repeated
proposal = mutations.propose_note(home, vault, '10_Projects/newest.md', content='# Newest effect' + chr(10), authenticated_id=request)
mutations.record_preview(home, proposal['proposal_id'], proposal['approval_digest'], complete=True)
assert mutations.apply_proposal(home, vault, proposal['proposal_id'], proposal['approval_digest'], writers_paused=True, owner=42, bot=7, application_request=request)['outcome'] == 'applied'
mutations.finish_request(home, request)
"""
            newer_release = json.loads((home / "release.json").read_text())
            assert newer_release["image"] != release_image
            run(
                [
                    *command,
                    "run",
                    "--rm",
                    "--no-deps",
                    "emuru",
                    "/opt/emuru/.venv/bin/python",
                    "-c",
                    effect,
                ],
                check=True,
                env={
                    **os.environ,
                    "EMURU_IMAGE": newer_release["image"],
                    "EMURU_SOURCE_ROOT": str(
                        home / "sources" / newer_release["source"]
                    ),
                },
            )
            write_private(
                profile / "synthetic-history.json", '{"newest_history": true}\n'
            )
            newest = {
                p: p.read_bytes()
                for directory in (
                    profile,
                    vault,
                    Path(values["EMURU_STATE"]),
                    Path(values["EMURU_RUNTIME"]),
                )
                for p in directory.rglob("*")
                if p.is_file()
            }
            previous = len(calls)
            module["wake"](home, rollback_source=identity)
            assert not any(c[:2] == ["docker", "build"] for c in calls[previous:])
            assert json.loads((home / "release.json").read_text())["source"] == identity
            assert all(p.read_bytes() == data for p, data in newest.items())
            assert (vault / "10_Projects/newest.md").read_text() == "# Newest effect\n"
            assert not any(c[:2] == ["systemctl", "start"] for c in calls)
            print(
                "Docker-to-Docker rollback: distinct verified images, newest applied effect/history/queue/journal preserved PASS",
                flush=True,
            )
            print(
                "Disposable wake: update, native owner previews/approval/cancel/readback, running no-op, stopped cache reuse, state preservation PASS",
                flush=True,
            )
        finally:
            subprocess.run = original_run
            compose("down", "--timeout", "10", "--remove-orphans")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        verify()
    except subprocess.CalledProcessError as error:
        print((error.stderr or error.stdout or "")[-4000:])
        raise
