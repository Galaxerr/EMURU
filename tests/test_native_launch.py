"""Both transport adapters execute the shared isolated bootstrap contract."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from emuru import environment
from emuru.hermes import launch, profile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native(tmp_path, monkeypatch):
    native = tmp_path / "native"
    native.mkdir()
    (native / "hermes_bootstrap.py").write_text(
        "import os; os.environ['SYNTHETIC_BOOTSTRAP'] = 'loaded'\n"
    )
    monkeypatch.setattr(profile, "installed_runtime", lambda root: native)
    monkeypatch.setattr(profile, "public_contract", lambda root: None)
    monkeypatch.setattr(profile, "profile_home", lambda: tmp_path / "profiles/emuru")
    monkeypatch.setattr(environment, "load_env", lambda path: None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return native


def run_adapter(adapter, monkeypatch):
    if adapter == "cli":
        monkeypatch.setattr(
            sys, "argv", ["env_exec.py", "hermes", "-p", "emuru", "chat", "-q", "probe"]
        )
        runpy.run_path(str(ROOT / "scripts/hermes/env_exec.py"))
    else:
        module = runpy.run_path(str(ROOT / "scripts/hermes/telegram.py"))
        module["launch"]("--check")


@pytest.mark.parametrize("adapter", ["cli", "telegram"])
@pytest.mark.parametrize("runtime", ["checkout", "package-manager"])
def test_adapters_execute_isolated_bootstrap(
    native, tmp_path, monkeypatch, adapter, runtime
):
    if runtime == "checkout":
        python = native / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
    else:
        hermes = native / ".hermes/bin/hermes"
        hermes.parent.mkdir(parents=True)
        entry = f"import sys,runpy; sys.path.insert(0,{str(native)!r}); import hermes_bootstrap; runpy.run_module('runpy', run_name='__main__', alter_sys=True)"
        hermes.write_text(
            f"#!{sys.executable}\nimport json,sys\nassert sys.argv[1:5] == ['--print-runtime-command','--module','runpy','--']\nprint(json.dumps([{sys.executable!r},'-I','-c',{entry!r},*sys.argv[5:]]))\n"
        )
        hermes.chmod(0o700)
    probe = tmp_path / "bridge.py"
    probe.write_text(
        "import json,os,sys\nprint(json.dumps({'arguments':sys.argv[1:], 'bootstrap':os.environ.get('SYNTHETIC_BOOTSTRAP'), 'offline':os.environ['UV_OFFLINE'], 'lazy':os.environ['HERMES_DISABLE_LAZY_INSTALLS'], 'lock':os.fstat(int(os.environ['SYNTHETIC_LOCK'])).st_ino}))\n"
    )
    lock = os.open(tmp_path / "instance.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(lock, True)
    monkeypatch.setenv("SYNTHETIC_LOCK", str(lock))
    outputs = []
    run = subprocess.run

    class Executed(BaseException):
        pass

    def execute(binary, command):
        assert binary == command[0] and command[1:3] == ["-I", "-c"]
        expected = str(
            ROOT
            / "infra/hermes"
            / ("cli-runtime.py" if adapter == "cli" else "telegram-runtime.py")
        )
        assert expected in command
        command = list(command)
        command[command.index(expected)] = str(probe)
        outputs.append(
            json.loads(
                run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    pass_fds=(lock,),
                ).stdout
            )
        )
        raise Executed

    monkeypatch.setattr(launch.os, "execv", execute)
    monkeypatch.delenv("SYNTHETIC_BOOTSTRAP", raising=False)
    try:
        with pytest.raises(Executed):
            run_adapter(adapter, monkeypatch)
        assert outputs == [
            {
                "arguments": ["chat", "-q", "probe"]
                if adapter == "cli"
                else ["--check"],
                "bootstrap": "loaded" if runtime == "package-manager" else None,
                "offline": "1",
                "lazy": "1",
                "lock": os.fstat(lock).st_ino,
            }
        ]
    finally:
        os.close(lock)


@pytest.mark.parametrize("adapter", ["cli", "telegram"])
@pytest.mark.parametrize(
    "bad", ["json", "shape", "relative", "isolation", "bootstrap", "arguments"]
)
def test_adapters_reject_malformed_package_manager_contract(
    native, monkeypatch, adapter, bad
):
    hermes = native / ".hermes/bin/hermes"
    hermes.parent.mkdir(parents=True)
    hermes.write_text("synthetic")
    hermes.chmod(0o700)

    def response(command, **kwargs):
        result = [
            sys.executable,
            "-I",
            "-c",
            "import runpy; runpy.run_module('runpy', run_name='__main__', alter_sys=True)",
            *command[5:],
        ]
        if bad == "json":
            return type("Response", (), {"stdout": "invalid"})()
        if bad == "shape":
            result = {"command": result}
        elif bad == "relative":
            result[0] = "python"
        elif bad == "isolation":
            result[1] = "-s"
        elif bad == "bootstrap":
            result[3] = "import runpy"
        elif bad == "arguments":
            result[-1] = "changed"
        return type("Response", (), {"stdout": json.dumps(result)})()

    monkeypatch.setattr(launch.subprocess, "run", response)
    monkeypatch.setattr(
        launch.os, "execv", lambda *args: pytest.fail("Unsafe contract executed")
    )
    with pytest.raises(
        (SystemExit, profile.HealthError), match="runtime_launcher_contract_changed"
    ):
        run_adapter(adapter, monkeypatch)
