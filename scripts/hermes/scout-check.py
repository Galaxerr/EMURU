"""Disposable Docker/native scout acceptance; no live profiles, providers or vault."""

import json
import os
import runpy
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))
scout = RUNNER["snapshot"].__globals__


def verify():
    os.umask(0o077)
    daemon_root = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))[
        "docker_workspace"
    ](ROOT)
    image = os.environ.get("EMURU_TEST_IMAGE")
    if not image:
        _, image = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))[
            "ensure_image"
        ](build=False)
    with tempfile.TemporaryDirectory(
        prefix="scout-acceptance-", dir=ROOT / ".runtime"
    ) as temporary:
        folder = Path(temporary)
        home = folder / "profile"
        home.mkdir(mode=0o700)
        (home / "emuru-projects").mkdir(mode=0o700)
        (home / "vault").mkdir()
        (home / "vault/private.md").write_text("SYNTHETIC_VAULT_SENTINEL")
        (home / "telegram-token").write_text("SYNTHETIC_BOT_SENTINEL")
        staging = folder / "staging"
        staging.mkdir()
        (staging / "snapshot").mkdir()
        backend = RUNNER["DockerScout"](ROOT, image, home, staging)
        config_path = staging / "compose.json"
        config = json.loads(config_path.read_text())
        for service in config["services"].values():
            for volume in service["volumes"]:
                volume["source"] = str(
                    daemon_root / Path(volume["source"]).relative_to(ROOT)
                )
        config_path.write_text(json.dumps(config))
        original = json.loads(config_path.read_text())

        def code(service, expression):
            config["services"][service]["command"] = [expression]
            config_path.write_text(json.dumps(config))
            return backend.call(service)

        source = code(
            "ledger",
            "import runpy,sys; sys.argv=['case','source','/control']; runpy.run_path('/opt/emuru/tests/scout_board_case.py',run_name='__main__')",
        )["source"]
        config_path.write_text(json.dumps(original))
        repo = folder / "repository"
        repo.mkdir()
        (repo / "app.py").write_text("def answer():\n    return 42\n")
        for arguments in (
            ["init", "-q"],
            ["add", "app.py"],
            [
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(["git", "-C", str(repo), *arguments], check=True)
        baseline = scout["git"](repo, "rev-parse", "HEAD").decode().strip()
        policy = {
            "schema": 1,
            "repository": "synthetic",
            "path": str(repo),
            "baseline": baseline,
            "files": ["app.py"],
            "tasks": [source],
            "timeout_seconds": 10,
        }
        expected = RUNNER["snapshot"](policy, staging / "snapshot")
        result = RUNNER["execute"](backend, policy, source, expected)
        assert result["status"] == "done" and result["evidence"]["baseline"] == baseline
        assert RUNNER["execute"](backend, policy, source, expected) == result
        assert (repo / "app.py").read_text() == "def answer():\n    return 42\n"
        print("Scout native report / baseline / replay: PASS", flush=True)
        # Same service, deliberately hostile probe: assert the actual OS denies access.
        probe = """
import json,os,socket
assert os.getuid() != 0
for path in PATHS:
    try:
        open(path,'rb').read()
    except (OSError,PermissionError):
        pass
    else:
        raise AssertionError('forbidden read')
for path in ['/repo/0','/repo/new-file','/opt/emuru/changed']:
    try:
        open(path,'wb').write(b'forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('forbidden write')
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError:
    pass
else:
    raise AssertionError('external network available')
assert not any('TOKEN' in key or 'API_KEY' in key for key in os.environ)
print(json.dumps({'isolation':'PASS'}))
""".replace(
            "PATHS",
            repr(
                [
                    str(home / "vault/private.md"),
                    str(home / "telegram-token"),
                    "/state/vault/private.md",
                    "/run/secrets/emuru_gateway_key",
                    "/var/run/docker.sock",
                    "/control/emuru-projects/kanban.db",
                    "/etc/shadow",
                ]
            ),
        )
        assert code("scout", probe) == {"isolation": "PASS"}
        try:
            code(
                "scout",
                "import sys,time; sys.path.insert(0,'/opt/emuru/src'); from emuru.projects.scout import limit_runtime; limit_runtime(1); time.sleep(10)",
            )
        except subprocess.CalledProcessError as error:
            assert b"Scout stopped or exceeded its time budget." in error.stderr
        else:
            raise AssertionError("Worker failed to enforce its own deadline")
        print("Scout process deadline: PASS", flush=True)
        print(
            "Scout OS mount / credential / write / network isolation: PASS", flush=True
        )
        config_path.write_text(json.dumps(original))
        # Crash after claim, before evidence: durable ownership prevents another run.
        changed = {**policy, "timeout_seconds": 11}
        grant = {
            key: changed[key]
            for key in ("repository", "baseline", "files", "timeout_seconds")
        }
        grant.update(
            source_task=source,
            repository_id=expected["repository_id"],
            report_sha256=RUNNER["digest"](expected),
        )
        claimed = backend.call("ledger", {"action": "claim", "grant": grant})
        assert claimed["status"] == "claimed"
        assert (
            backend.call("ledger", {"action": "claim", "grant": grant})["status"]
            == "uncertain"
        )
        assert (
            backend.call(
                "ledger",
                {"action": "finish", "task": claimed["task"], "run": claimed["run"]},
            )["status"]
            == "blocked"
        )

        def archive_stopped(task):
            code(
                "ledger",
                f"import sys,json; sys.path[:0]=['/opt/emuru/src','/opt/hermes']; from pathlib import Path; from emuru.projects.board import project_board;\nwith project_board(Path('/control'),Path('/opt/emuru')) as (kb,db): kb.archive_task(db,{task!r})\nprint(json.dumps({{'reconciled':True}}))",
            )
            config_path.write_text(json.dumps(original))

        # No worker was spawned for this synthetic interrupted claim.
        archive_stopped(claimed["task"])
        # Kill an actual worker while its native run is claimed; never automatically reclaim.
        config["services"]["scout"]["command"] = [
            "import sys,time; sys.path.insert(0,'/opt/emuru/src'); from emuru.projects.scout import limit_runtime; limit_runtime(120); print('SCOUT_READY',flush=True); time.sleep(120)"
        ]
        config_path.write_text(json.dumps(config))
        for death in (signal.SIGTERM, signal.SIGKILL):
            config_path.write_text(json.dumps(original))
            death_grant = {**grant, "timeout_seconds": death.value + 20}
            claimed = backend.call("ledger", {"action": "claim", "grant": death_grant})
            assert claimed["status"] == "claimed"
            config_path.write_text(json.dumps(config))
            name = "scout-death-" + str(death.value) + "-" + str(os.getpid())
            command = [
                *backend.command,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--name",
                name,
                "scout",
            ]
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    command, stdout=output, stderr=subprocess.DEVNULL
                )
                try:
                    for _ in range(100):
                        output.seek(0)
                        if b"SCOUT_READY" in output.read():
                            break
                        time.sleep(0.1)
                    else:
                        raise AssertionError("Worker did not start")
                    subprocess.run(
                        ["docker", "kill", "--signal", death.name, name],
                        check=True,
                        stdout=subprocess.DEVNULL,
                    )
                    assert process.wait(timeout=10) != 0
                finally:
                    subprocess.run(
                        ["docker", "rm", "--force", name],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    process.wait(timeout=10)
            config_path.write_text(json.dumps(original))
            assert (
                backend.call("ledger", {"action": "claim", "grant": death_grant})[
                    "status"
                ]
                == "uncertain"
            )
            assert (
                backend.call(
                    "ledger",
                    {
                        "action": "finish",
                        "task": claimed["task"],
                        "run": claimed["run"],
                    },
                )["status"]
                == "blocked"
            )
            # Native archive follows independently observed process termination above.
            archive_stopped(claimed["task"])
            print(
                f"Scout {death.name} / uncertain native run / no replay: PASS",
                flush=True,
            )
        print(
            "Scout exact HEAD / native report / replay / OS isolation / SIGTERM / SIGKILL: PASS"
        )


if __name__ == "__main__":
    try:
        verify()
    except subprocess.CalledProcessError as error:
        # Disposable fixtures contain no real data; production runner never prints stderr.
        print((error.stderr or b"").decode(errors="replace"))
        raise
