"""Launcher lock, private working directory, safe status, and runtime ownership."""

import importlib.util
import json
import os
import selectors
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from telegram_helpers import update

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def diagnostic():
    spec = importlib.util.spec_from_file_location(
        "telegram_diagnostic", ROOT / "scripts/hermes-telegram.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_bootstrap_keeps_its_interpreter_and_external_bridge(
    diagnostic, monkeypatch, tmp_path
):
    commands, executed = [], []
    native = tmp_path / "native"
    (native / ".hermes/bin").mkdir(parents=True)
    executable = native / ".hermes/bin/hermes"
    executable.write_text("synthetic")
    executable.chmod(0o700)
    monkeypatch.setattr(diagnostic.profile, "public_contract", lambda root: None)
    monkeypatch.setattr(diagnostic.profile, "installed_runtime", lambda root: native)
    monkeypatch.setenv("EMURU_HERMES_PROFILE_HOME", str(tmp_path / "profiles/emuru"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    entry = "import hermes_bootstrap; runpy.run_module('runpy', run_name='__main__', alter_sys=True)"

    def run(command, **kwargs):
        commands.append(command)
        return NS(
            stdout=json.dumps(
                [
                    "/native/python",
                    "-I",
                    "-c",
                    entry,
                    str(ROOT / "infra/hermes/telegram-runtime.py"),
                    "--check",
                ]
            )
        )

    def execute(binary, command):
        executed.append((binary, command))

    monkeypatch.setattr(diagnostic.subprocess, "run", run)
    monkeypatch.setattr(diagnostic.os, "execv", execute)
    diagnostic.launch("--check")
    assert commands[0][:2] == [str(executable), "--print-runtime-command"]
    assert diagnostic.os.environ["UV_OFFLINE"] == "1"
    assert diagnostic.os.environ["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    binary, command = executed[0]
    assert binary == "/native/python" and command[1:3] == ["-I", "-c"]
    assert "import hermes_bootstrap" in command[3] and "runpy.run_path" in command[3]
    assert command[4:] == [
        str(ROOT / "infra/hermes/telegram-runtime.py"),
        "--check",
    ]


def test_offline_never_resolves_hermes_or_private_home(diagnostic, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline check crossed its boundary")

    monkeypatch.setattr(diagnostic.profile, "installed_runtime", forbidden)
    monkeypatch.setattr(diagnostic.profile, "profile_home", forbidden)
    monkeypatch.setattr(diagnostic.subprocess, "run", forbidden)
    monkeypatch.setattr(diagnostic.subprocess, "check_output", forbidden)
    diagnostic.offline()
    assert "PASS (offline)" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["check", "runtime-check", "launch"])
def test_diagnostic_mode_routes_without_changing_cli(diagnostic, monkeypatch, mode):
    calls = []
    monkeypatch.setattr(diagnostic, "launch", calls.append)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--" + mode])
    diagnostic.main()
    assert calls == ["--" + mode]


def test_readiness_fails_closed_and_hard_exit_closes_queue(
    runtime, monkeypatch, capsys
):
    import asyncio

    calls = []

    class Runner:
        async def start(self):
            calls.append("native_start")
            return True

    run = NS(
        start_gateway=None,
        GatewayRunner=Runner,
        _exit_after_graceful_shutdown=lambda code: calls.append(("exit", code)),
    )
    queue = NS(close=lambda: calls.append("close"))

    async def consumer():
        calls.append("consumer")

    runtime.install_launch_guards(
        run,
        NS(_attach_to_host_gateway_or_guard=None),
        queue,
        NS(),
        NS(require_consumer=consumer),
    )
    monkeypatch.setattr(runtime, "vault_tools_ready", lambda: False)
    with pytest.raises(RuntimeError, match="vault_mcp_not_ready"):
        asyncio.run(Runner().start())
    assert calls == [] and capsys.readouterr().out == ""
    monkeypatch.setattr(runtime, "vault_tools_ready", lambda: True)
    assert asyncio.run(Runner().start()) is True
    assert "five MCP tools READY" in capsys.readouterr().out
    run._exit_after_graceful_shutdown(0)
    assert calls == ["native_start", "consumer", "close", ("exit", 0)]


def test_shell_lock_survives_exec_and_neutral_directory_is_private(tmp_path):
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/hermes-telegram.sh", root / "scripts/hermes-telegram.sh"
    )
    (root / ".venv/bin/python").symlink_to(sys.executable)
    (root / "scripts/hermes-telegram.py").write_text("""import fcntl,json,os,sys
if sys.argv[1:] == ["--preflight"]: sys.exit(0)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
print(json.dumps({"cwd":os.getcwd(), "mask":os.umask(0o077)}), flush=True)
sys.stdin.readline()
""")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    launcher = ["bash", str(root / "scripts/hermes-telegram.sh")]
    proc = subprocess.Popen(
        launcher,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "Launcher did not start"
        result = json.loads(proc.stdout.readline())
        assert result["cwd"] == str(tmp_path / "state/emuru/workspace")
        assert result["mask"] == 0o077
        duplicate = subprocess.run(
            launcher, env=env, capture_output=True, text=True, timeout=5, check=False
        )
        assert duplicate.returncode == 1 and "already running" in duplicate.stderr
        lock = tmp_path / "home/.hermes/profiles/emuru/emuru-telegram/instance.lock"
        assert lock.stat().st_mode & 0o777 == 0o600
        proc.communicate("\n", timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_status_only_prints_counts_not_queued_identity_or_text(
    tmp_path, policy, diagnostic, capsys, monkeypatch
):
    from emuru.telegram_queue import TelegramQueue

    q = TelegramQueue(
        tmp_path / "emuru-telegram/queue.sqlite3", policy, 424242, clock=lambda: 100000
    )
    q.stage(7, [update(uid, "SENSITIVE_NOTE", owner_id=424242) for uid in (1, 2, 3)])
    # Include a started claim and a receipt old enough for pruning.
    first = q.claim(7)
    q.finish(first["sequence"], True)
    q.db.execute("UPDATE updates SET finished_at=-1000000 WHERE state='completed'")
    q.claim(7)
    q.close()
    before = q.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Status invoked live queue mutation")

    for name in ("__init__", "recover", "_prune"):
        monkeypatch.setattr(TelegramQueue, name, forbidden)
    assert diagnostic.status(tmp_path) == 0
    assert q.path.read_bytes() == before
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "queue": {"completed": 1, "started": 1, "queued": 1},
        "health": [],
    }
    assert "SENSITIVE_NOTE" not in output and "424242" not in output
    health = tmp_path / "emuru-telegram/health.json"
    health.write_text(json.dumps({"health": ["consumer_exited", "SENSITIVE_NOTE"]}))
    health.chmod(0o600)
    assert diagnostic.status(tmp_path) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["health"] == ["consumer_exited", "consumer_failed"]
    assert "SENSITIVE_NOTE" not in output
    assert q.path.read_bytes() == before


def test_wrong_or_modified_hermes_source_is_rejected(runtime, monkeypatch, tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    monkeypatch.setenv("EMURU_HERMES_ROOT", str(native))
    profile = runtime.profile
    monkeypatch.setitem(
        sys.modules, "gateway", NS(__file__="/native/gateway/__init__.py")
    )
    monkeypatch.setattr(
        profile.subprocess, "check_output", lambda *args, **kwargs: "0" * 40
    )
    with pytest.raises(RuntimeError, match="commit_mismatch"):
        runtime.runtime_identity()
    lock = json.loads((ROOT / "infra/hermes/runtime-lock.json").read_text())
    monkeypatch.setattr(
        profile.subprocess, "check_output", lambda *args, **kwargs: lock["commit"]
    )

    def dirty(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(profile.subprocess, "run", dirty)
    with pytest.raises(RuntimeError, match="source_unverified"):
        runtime.runtime_identity()


def test_native_main_keeps_its_lifecycle_with_guards_and_private_config(
    runtime, tmp_path, monkeypatch
):
    calls = []
    config = NS()

    async def start(**kwargs):
        calls.append(kwargs)
        return True

    def native_main():
        import asyncio

        assert calls == ["guards"]
        assert sys.argv == ["hermes", "-p", "emuru", "gateway", "run"]
        asyncio.run(run.start_gateway(config=None))

    class Runner:
        def _create_adapter(self, platform, config):
            pass

        async def start(self):
            return True

    run = NS(
        start_gateway=start,
        GatewayRunner=Runner,
        _exit_after_graceful_shutdown=lambda code: None,
    )
    gateway_cli = NS(_attach_to_host_gateway_or_guard=lambda **kwargs: None)
    monkeypatch.setitem(
        sys.modules, "hermes_cli", NS(main=NS(main=native_main), gateway=gateway_cli)
    )
    admission = NS(TelegramApplication=object)
    for name, module in {
        "agent": NS(system_prompt=NS(STEER_CHANNEL_NOTE="unused steering")),
        "gateway": NS(run=run),
        "gateway.platforms.event": NS(MessageType=object),
        "plugins.platforms.telegram": NS(update_admission=admission),
        "plugins.platforms.telegram.adapter": NS(TelegramAdapter=object),
        "telegram": NS(Bot=object, Update=object),
        "telegram.ext": NS(Updater=object),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(runtime, "runtime_identity", lambda: None)
    monkeypatch.setattr(runtime, "private_config", lambda: (config, 42))
    monkeypatch.setattr(runtime, "audit_live_profile", lambda: None)
    monkeypatch.setattr(runtime, "verify_functions", lambda *args: None)
    monkeypatch.setattr(runtime, "verify_native_contract", lambda *args: None)
    monkeypatch.setattr(
        runtime, "install_guards", lambda *args, **kwargs: calls.append("guards")
    )
    monkeypatch.setattr(sys, "argv", ["bridge"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "emuru-telegram/instance.lock"
    path.parent.mkdir()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setenv("EMURU_TELEGRAM_LOCK_FD", str(fd))
    old_mask = os.umask(0o077)
    try:
        runtime.main()
    finally:
        os.umask(old_mask)
        os.close(fd)
    assert calls == ["guards", {"config": config}]


@pytest.mark.parametrize(
    "code",
    ["queue_schema_unsupported", "queue_permissions_invalid", "queue_unavailable"],
)
def test_status_adapter_formats_diagnostic_failure(
    diagnostic, tmp_path, monkeypatch, capsys, code
):
    calls = []

    def fail(path, *, health_file):
        calls.append((path, health_file))
        raise diagnostic.queue_module.QueueDiagnosticsError(code)

    monkeypatch.setattr(diagnostic.queue_module, "diagnostics", fail)
    assert diagnostic.status(tmp_path) == 1
    assert calls == [
        (
            tmp_path / "emuru-telegram/queue.sqlite3",
            tmp_path / "emuru-telegram/health.json",
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"queue": {}, "health": [code]}
