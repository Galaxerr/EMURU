import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mapped", [False, True])
def test_wake_mounts_use_daemon_paths(tmp_path, monkeypatch, mapped):
    import shutil

    import yaml

    module = runpy.run_path(str(ROOT / "scripts/hermes/wake-check.py"))
    monkeypatch.setitem(module["verify"].__globals__, "ROOT", tmp_path)
    (tmp_path / "infra/docker").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "infra/docker/compose.yaml", tmp_path / "infra/docker/compose.yaml"
    )
    daemon = Path("/daemon/workspace") if mapped else tmp_path
    load = runpy.run_path

    def load_script(path):
        if Path(path).name == "container-check.py":
            return {"docker_workspace": lambda root: daemon}
        result = load(ROOT / "scripts/hermes/container.py")
        result["source_identity"] = lambda root: "source"
        return result

    monkeypatch.setattr(runpy, "run_path", load_script)

    class MountsChecked(Exception):
        pass

    def run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout="sha256:fixture")
        if command[-1] == "prepare":
            import shlex

            env_file = Path(command[command.index("--env-file") + 1])
            env = dict(line.split("=", 1) for line in shlex.split(env_file.read_text()))
            env.update(kwargs.get("env", {}))
            for name in ("EMURU_GATEWAY_KEY_FILE", "EMURU_PROFILE", "EMURU_VAULT"):
                mounted = Path(env[name])
                assert mounted.is_relative_to(daemon)
                assert (tmp_path / mounted.relative_to(daemon)).exists()
            config = yaml.safe_load(Path(command[command.index("-f") + 1]).read_text())
            for service in config["services"].values():
                for volume in service.get("volumes", []):
                    if volume["source"].startswith("${"):
                        if volume["source"].startswith("${EMURU_SOURCE_ROOT"):
                            assert str(daemon) in volume["source"]
                    else:
                        assert Path(volume["source"]).is_relative_to(daemon)
            raise MountsChecked
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(module["subprocess"], "run", run)
    with pytest.raises(MountsChecked):
        module["verify"]()


@pytest.mark.parametrize("source", ["/host/workspace", "/runner/workspace"])
def test_act_workspace_maps_to_daemon_source(monkeypatch, source):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))
    monkeypatch.setenv("ACT", "true")
    monkeypatch.setenv("HOSTNAME", "host-name-inherited-by-act")
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return f"1 2 8:1 /var/lib/docker/containers/{'a' * 64}/hostname /etc/hostname rw - ext4 /dev/sda rw\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)

    def run(command, **kwargs):
        assert command[-1] == "a" * 64
        return SimpleNamespace(
            stdout=json.dumps([{"Destination": "/runner/workspace", "Source": source}])
        )

    monkeypatch.setattr(module["subprocess"], "run", run)
    assert module["docker_workspace"](Path("/runner/workspace/repo")) == (
        Path(source) / "repo"
    )


def test_disposable_mounts_live_in_shared_workspace(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))
    monkeypatch.setitem(
        module["verify"].__globals__,
        "__file__",
        str(tmp_path / "scripts/hermes/container-check.py"),
    )
    production = tmp_path / "infra/docker/deployment.env"
    production.parent.mkdir(parents=True)
    production.write_text("production metadata")
    workspace_env = tmp_path / ".env"
    workspace_env.write_text("production credentials")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (production, workspace_env)
    }
    monkeypatch.setenv("OLLAMA_API_KEY", "must-not-leak")
    load = runpy.run_path
    prepared = []

    def load_deployment(path):
        deployment = load(path)
        globals_ = deployment["prepare_deployment"].__globals__
        globals_["DEPLOYMENT_ENV"] = production
        globals_["ROOT"] = tmp_path

        def forbidden(*args, **kwargs):
            pytest.fail("Disposable preparation touched production state")

        for name in (
            "initialize",
            "load_env",
            "write_env",
            "_vault_path",
            "_saved_owner_id",
        ):
            globals_[name] = forbidden
        prepare = deployment["prepare_deployment"]

        def tracked(*args):
            prepared.append(args)
            return prepare(*args)

        deployment["prepare_deployment"] = tracked
        return deployment

    monkeypatch.setattr(runpy, "run_path", load_deployment)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        deployment = Path(command[command.index("--env-file") + 1])
        assert deployment.parent.parent == tmp_path / ".runtime"
        assert deployment.parent.stat().st_mode & 0o777 == 0o700
        values = dict(
            line.split("=", 1) for line in deployment.read_text().splitlines()
        )
        for name, value in values.items():
            if name not in {"EMURU_UID", "EMURU_GID", "EMURU_TELEGRAM_OWNER_ID"}:
                assert Path(value).exists(), name
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(module["subprocess"], "run", run)
    previous = os.umask(0o077)
    try:
        module["verify"](config_only=True)
    finally:
        os.umask(previous)
    assert len(prepared) == 1
    assert len(commands) == 1
    private = prepared[0][0]
    assert (private / "ollama-api.key").read_text() == "synthetic-ollama-key"
    assert (private / "gemini-api.key").read_text() == ""
    assert (private / "openai-api.key").read_text() == ""
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before
    } == before
    assert commands[0][-2:] == ["config", "--quiet"]


@pytest.mark.parametrize("image", [None, "emuru:0.5.0-rc-test"])
def test_disposable_checks_do_not_depend_on_shared_uv_cache(
    tmp_path, monkeypatch, image
):
    """Inject the reported rename failure if a check uses the shared cache."""
    import subprocess

    import yaml

    module = runpy.run_path(str(ROOT / "scripts/hermes/container-check.py"))
    monkeypatch.setitem(
        module["verify"].__globals__,
        "__file__",
        str(tmp_path / "scripts/hermes/container-check.py"),
    )
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / "infra/docker/compose.yaml").write_text(
        "services:\n  litellm:\n    image: synthetic-image\n"
    )
    monkeypatch.delenv("ACT", raising=False)
    if image is None:
        monkeypatch.delenv("EMURU_TEST_IMAGE", raising=False)
    else:
        monkeypatch.setenv("EMURU_TEST_IMAGE", image)
    load = runpy.run_path

    def load_deployment(path):
        deployment = load(path)
        deployment["prepare_deployment"].__globals__["ROOT"] = tmp_path
        return deployment

    monkeypatch.setattr(runpy, "run_path", load_deployment)
    commands = []

    class ChecksFinished(Exception):
        pass

    def run(command, **kwargs):
        if "/check.py" in command and command[-1] == "seed":
            override = next(
                Path(value) for value in command if value.endswith("/check.yaml")
            )
            assert yaml.safe_load(override.read_text())["services"]["emuru"][
                "image"
            ] == (image or "emuru:latest")
            raise ChecksFinished
        if "run" in command and "emuru" in command:
            commands.append(command)
            if "uv" in command and "UV_NO_CACHE=1" not in command:
                raise subprocess.CalledProcessError(
                    2,
                    command,
                    stderr="error: Failed to query Python interpreter\n"
                    "cause: failed to rename file from /state/.cache/uv/interpreter-v4/.tmpX "
                    "to /state/.cache/uv/interpreter-v4/python.msgpack: "
                    "No such file or directory (os error 2)",
                )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(module["subprocess"], "run", run)
    previous = os.umask(0o077)
    try:
        with pytest.raises(ChecksFinished):
            module["verify"]()
    finally:
        os.umask(previous)
    assert len(commands) == 6
    assert all("UV_NO_CACHE=1" in command for command in commands)
