"""Stopped update identity, managed policy, and state-preservation regressions."""

import fcntl
import hashlib
import json
import runpy
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from emuru import deployment
from emuru.hermes import profile
from emuru.models.gateway import write_private
from emuru.telegram.queue import TelegramQueue, load_telegram_settings
from emuru.vault.mutations import journal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def managed(tmp_path, monkeypatch):
    home = tmp_path / "profiles/emuru"
    home.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("EMURU_HERMES_PROFILE_HOME", str(home))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/uv")
    monkeypatch.delenv("EMURU_CONTAINER_ROUTE", raising=False)
    settings = profile.expected_settings(ROOT)
    config = {"model": {}, "mcp_servers": {"vault": profile.expected_server(ROOT)}}
    for key, value in settings.items():
        cursor = config
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    write_private(home / "config.yaml", yaml.safe_dump(config))
    write_private(home / "SOUL.md", (ROOT / "agents/emuru/SOUL.md").read_text())
    write_private(home / "history.json", "private history")
    write_private(home / ".env", "PRIVATE_CREDENTIAL=unchanged\n")
    return home, config


def files(home):
    return {
        p.relative_to(home): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in home.rglob("*")
        if p.is_file()
    }


def test_current_profile_is_noop_and_state_is_untouched(managed):
    home, _ = managed
    with journal(home):
        pass
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        42,
    )
    queue.close()
    before = files(home)
    result = deployment.sync_profile(ROOT, home, apply=True)
    assert not result["profile_changed"] and not result["guidance_changed"]
    assert files(home) == before
    assert not (home / "mutation-policy.json").exists()


def test_older_profile_gets_no_worker_dispatch_fence(managed):
    home, config = managed
    del config["kanban"]
    write_private(home / "config.yaml", yaml.safe_dump(config))
    before = files(home)
    assert deployment.sync_profile(ROOT, home)["profile_changed"]
    assert files(home) == before
    deployment.sync_profile(ROOT, home, apply=True)
    actual = yaml.safe_load((home / "config.yaml").read_text())
    assert actual["kanban"]["dispatch_profiles"] == []
    assert not deployment.sync_profile(ROOT, home, apply=True)["profile_changed"]


def test_custom_dispatch_allowlist_blocks_profile_upgrade(managed):
    home, config = managed
    config["kanban"]["dispatch_profiles"] = ["default"]
    write_private(home / "config.yaml", yaml.safe_dump(config))
    before = files(home)
    with pytest.raises(ValueError):
        deployment.sync_profile(ROOT, home, apply=True)
    assert files(home) == before


def test_reviewed_guidance_and_policy_selected_tools_upgrade(managed, monkeypatch):
    home, config = managed
    old = b"Previous reviewed stock guidance.\n"
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == ROOT / "infra/hermes/profile-updates.json":
            reviewed = json.loads(original_read(path))
            reviewed["previous_soul_sha256"] = [hashlib.sha256(old).hexdigest()]
            return json.dumps(reviewed)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    write_private(home / "SOUL.md", old.decode())
    # Policy enablement is an explicit owner choice, never a consequence of wake.
    monkeypatch.setattr(
        profile, "vault_tools", lambda: [*profile.TOOLS, "vault_propose"]
    )
    before = files(home)
    result = deployment.sync_profile(ROOT, home)
    assert result["profile_changed"] and result["guidance_changed"]
    assert files(home) == before
    deployment.sync_profile(ROOT, home, apply=True)
    assert (home / "SOUL.md").read_bytes() == (
        ROOT / "agents/emuru/SOUL.md"
    ).read_bytes()
    actual = yaml.safe_load((home / "config.yaml").read_text())
    assert actual["mcp_servers"]["vault"]["tools"]["include"][-1] == "vault_propose"
    for name in ("history.json", ".env"):
        assert files(home)[Path(name)] == before[Path(name)]
    after = files(home)
    deployment.sync_profile(ROOT, home, apply=True)
    assert files(home) == after
    assert config["mcp_servers"]["vault"]["tools"]["include"] == profile.TOOLS


@pytest.mark.parametrize(
    "custom", ["guidance", "server", "settings", "queue", "journal", "missing"]
)
def test_unknown_customization_or_schema_blocks_without_writes(managed, custom):
    home, config = managed
    if custom == "guidance":
        write_private(home / "SOUL.md", "custom policy")
    elif custom == "missing":
        (home / "SOUL.md").unlink()
    elif custom in {"server", "settings"}:
        if custom == "server":
            config["mcp_servers"]["other"] = {}
        else:
            config["agent"]["max_turns"] = 99
        write_private(home / "config.yaml", yaml.safe_dump(config))
    else:
        directory = home / (
            "emuru-telegram" if custom == "queue" else "emuru-mutations"
        )
        directory.mkdir(mode=0o700)
        path = directory / (
            "queue.sqlite3" if custom == "queue" else "mutations.sqlite"
        )
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=999")
        path.chmod(0o600)
    before = files(home)
    with pytest.raises((ValueError, FileNotFoundError)):
        deployment.sync_profile(ROOT, home, apply=True)
    assert files(home) == before


def test_same_version_queue_with_missing_schema_is_rejected_unchanged(managed):
    home, _ = managed
    directory = home / "emuru-telegram"
    directory.mkdir(mode=0o700)
    path = directory / "queue.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=1")
    path.chmod(0o600)
    before = path.read_bytes(), path.stat().st_mtime_ns

    with pytest.raises(ValueError, match="Queue migration"):
        deployment.check_state(home)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.fixture
def legacy_proposals(managed):
    home, _ = managed
    with journal(home) as db:
        payload = "# Pending owner project"
        digest = hashlib.sha256(payload.encode()).hexdigest()
        db.execute(
            "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,'proposed')",
            (
                "a" * 32,
                payload,
                digest,
                "/state/vault",
                "10_Projects/pending.md",
                None,
                digest,
            ),
        )
        db.execute("DROP TABLE revision_requests")
        db.execute("DROP TABLE proposal_lifecycle")
        db.commit()
    return home


def test_previous_proposal_schema_requires_explicit_backed_up_upgrade(legacy_proposals):
    home = legacy_proposals
    path = home / "emuru-mutations/mutations.sqlite"
    before, schema = files(home), deployment._schema(path)
    with pytest.raises(ValueError, match="Mutation state migration"):
        deployment.sync_profile(ROOT, home, apply=True)
    assert files(home) == before

    result = deployment.upgrade_proposals(home)
    assert result["state_changed"] is True
    backup = Path(result["backup"])
    assert backup.stat().st_mode & 0o777 == 0o600
    assert deployment._schema(backup) == schema
    with sqlite3.connect(backup) as old, sqlite3.connect(path) as current:
        tables = [name for kind, name, sql in schema[1] if kind == "table"]
        for table in tables:
            assert (
                old.execute(f'SELECT * FROM "{table}"').fetchall()
                == current.execute(f'SELECT * FROM "{table}"').fetchall()
            )
        assert current.execute(
            "SELECT proposal_id,interacted,version FROM proposal_lifecycle"
        ).fetchall() == [("a" * 32, 0, 0)]
        assert current.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    deployment.sync_profile(ROOT, home, apply=True)
    after = files(home)
    assert deployment.upgrade_proposals(home) == {"state_changed": False}
    assert files(home) == after


@pytest.mark.parametrize("custom", ["extra_table", "changed_table", "version", "queue"])
def test_proposal_upgrade_refuses_unknown_state_without_writes(
    legacy_proposals, custom
):
    home = legacy_proposals
    path = home / "emuru-mutations/mutations.sqlite"
    if custom == "queue":
        path = home / "emuru-telegram/queue.sqlite3"
        path.parent.mkdir(mode=0o700)
        path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        if custom == "extra_table":
            db.execute("CREATE TABLE unknown(value TEXT)")
        elif custom == "changed_table":
            db.execute("ALTER TABLE proposals ADD COLUMN unknown TEXT")
        else:
            db.execute("PRAGMA user_version=999")
    before = files(home)
    with pytest.raises(ValueError, match="migration"):
        deployment.upgrade_proposals(home)
    assert files(home) == before


def test_proposal_upgrade_refuses_active_profile(legacy_proposals):
    home = legacy_proposals
    lock = home / "emuru-telegram/instance.lock"
    lock.parent.mkdir(mode=0o700)
    write_private(lock, "")
    before = files(home)
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            deployment.upgrade_proposals(home)
    assert files(home) == before


def test_active_profile_lock_blocks_sync(managed):
    home, _ = managed
    (home / "emuru-telegram").mkdir(mode=0o700)
    lock = home / "emuru-telegram/instance.lock"
    write_private(lock, "")
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            deployment.sync_profile(ROOT, home, apply=True)


def test_source_snapshot_is_stable_and_detects_edits(tmp_path, monkeypatch):
    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir(mode=0o700)
    (source / "src").mkdir()
    (source / "src/example.py").write_text("value = 1\n")
    monkeypatch.setattr(deployment, "BUILD_FILES", ("src",))
    identity = deployment.source_identity(source)
    snapshot = deployment.snapshot_source(source, home, identity)
    assert deployment.source_identity(snapshot) == identity
    before = files(snapshot)
    assert deployment.snapshot_source(source, home, identity) == snapshot
    assert files(snapshot) == before
    (source / "src/example.py").write_text("value = 2\n")
    assert deployment.source_identity(source) != identity
    assert deployment.source_identity(snapshot) == identity
    (snapshot / "src/example.py").write_text("tampered\n")
    with pytest.raises(ValueError, match="snapshot mismatch"):
        deployment.snapshot_source(source, home, identity)


def test_source_identity_includes_executable_bit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    executable = source / "src/example.py"
    executable.write_text("value = 1\n")
    monkeypatch.setattr(deployment, "BUILD_FILES", ("src",))
    identity = deployment.source_identity(source)

    executable.chmod(executable.stat().st_mode | 0o111)

    assert deployment.source_identity(source) != identity


def test_unreviewed_current_guidance_is_rejected_unchanged(managed, monkeypatch):
    home, _ = managed
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == ROOT / "infra/hermes/profile-updates.json":
            manifest = json.loads(original_read(path, *args, **kwargs))
            manifest["soul_sha256"] = "0" * 64
            return json.dumps(manifest)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    before = files(home)

    with pytest.raises(ValueError, match="reviewed guidance manifest"):
        deployment.sync_profile(ROOT, home, apply=True)

    assert files(home) == before


@pytest.mark.parametrize("cached", [False, True])
def test_preparation_reuses_verified_image_and_never_pulls_dependencies(
    tmp_path, monkeypatch, cached
):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["prepare_update"].__globals__
    monkeypatch.setitem(scope, "source_identity", lambda root: "a" * 64)
    monkeypatch.setitem(scope, "snapshot_source", lambda *args: tmp_path)
    monkeypatch.setitem(scope, "gateway_identity", lambda *args: "b" * 64)
    calls, built = [], cached

    def run(command, **kwargs):
        nonlocal built
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return NS(
                returncode=0 if built else 1,
                stdout=json.dumps(
                    {
                        "Id": "sha256:test",
                        "Config": {"Labels": {"io.emuru.source": "a" * 64}},
                    }
                ),
            )
        if command[:2] == ["docker", "build"]:
            built = True
        return NS(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    env = module["prepare_update"](
        ["docker", "compose"], {"EMURU_PROFILE": str(tmp_path / "profiles/emuru")}
    )
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert len(builds) == (0 if cached else 1)
    assert all("--pull=false" in c and "--no-cache" not in c for c in builds)
    assert env["EMURU_IMAGE"] == "sha256:test"
    assert calls[-1][-1] == "profile-sync"
    assert sum(c[-1] == "profile-sync" for c in calls) == 1
    assert all("pull" not in c and "git" not in c for c in calls)


def test_image_verification_failure_never_syncs_profile(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    monkeypatch.setitem(
        module["prepare_update"].__globals__, "source_identity", lambda root: "a" * 64
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return NS(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "sha256:test",
                        "Config": {"Labels": {"io.emuru.source": "a" * 64}},
                    }
                ),
            )
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        module["prepare_update"](
            ["docker", "compose"], {"EMURU_PROFILE": str(tmp_path / "profiles/emuru")}
        )
    assert not any("profile-sync" in c for c in calls)


@pytest.mark.parametrize("state", ["paused", "restarting", "removing"])
def test_uncertain_worker_state_blocks_update(tmp_path, monkeypatch, state):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["wake"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", tmp_path / "deployment.env")
    monkeypatch.setitem(scope, "saved_deployment", lambda home: {})
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "systemctl":
            return NS(returncode=3, stdout="inactive\n")
        return NS(
            returncode=0,
            stdout=json.dumps([{"State": state}]) if "--format" in command else "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="not safely stopped"):
        module["wake"]()
    assert len(calls) == 3


@pytest.mark.parametrize(
    "mismatch", [None, "source", "worker_image", "gateway_image", "status"]
)
def test_running_release_checks_images_labels_and_status(monkeypatch, mismatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    pins = json.loads((ROOT / "infra/docker/deployment-lock.json").read_text())
    env = {"EMURU_IMAGE": "sha256:worker", "EMURU_SOURCE_ID": "source"}

    def run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            assert command == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                pins["litellm"],
            ]
            return NS(stdout="sha256:gateway-config\n")
        if command[:2] == ["docker", "compose"]:
            return NS(stdout=command[-1] + "-id")
        worker = command[-1] == "emuru-id"
        record = {
            "image": env["EMURU_IMAGE"] if worker else "sha256:gateway-config",
            "source": "source",
            "status": "running",
        }
        if mismatch == "source":
            record["source"] = "other"
        elif mismatch == "status":
            record["status"] = "exited"
        elif (mismatch == "worker_image" and worker) or (
            mismatch == "gateway_image" and not worker
        ):
            record["image"] = "sha256:wrong"
        return NS(stdout=json.dumps(record))

    monkeypatch.setattr(subprocess, "run", run)
    if mismatch:
        with pytest.raises(ValueError, match="mismatch"):
            module["verify_release"](["docker", "compose"], env)
    else:
        module["verify_release"](["docker", "compose"], env)


def test_systemd_starting_during_update_blocks_worker_cutover(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["wake"].__globals__
    monkeypatch.setitem(scope, "DEPLOYMENT_ENV", tmp_path / "deployment.env")
    monkeypatch.setitem(scope, "saved_deployment", lambda home: {})
    monkeypatch.setitem(scope, "prepare_update", lambda *args: {})
    calls, systemd_checks = [], 0

    def run(command, **kwargs):
        nonlocal systemd_checks
        calls.append(command)
        if command[0] == "systemctl":
            systemd_checks += 1
            return NS(
                returncode=3 if systemd_checks == 1 else 0,
                stdout="inactive\n" if systemd_checks == 1 else "active\n",
            )
        return NS(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="Systemd Telegram poller is active"):
        module["wake"]()
    assert systemd_checks == 2
    assert not any("--profile" in command for command in calls)


def test_owner_approved_private_policy_survives_normal_wake_sync(managed):
    home, _ = managed
    policy = {
        "enabled": True,
        "roots": ["10_Projects", "20_Areas", "30_Resources"],
        "vault_path": "/state/vault",
    }
    write_private(
        home / "vault-target.json",
        json.dumps({"mode": "real", "vault_path": "/state/vault"}),
    )
    write_private(home / "mutation-policy.json", json.dumps(policy))
    protected = {
        name: ((home / name).read_bytes(), (home / name).stat().st_mtime_ns)
        for name in (
            "mutation-policy.json",
            "vault-target.json",
            ".env",
            "history.json",
            "SOUL.md",
        )
    }
    for _ in range(2):
        deployment.sync_profile(ROOT, home, apply=True)
        assert profile.vault_tools(home) == [*profile.TOOLS, "vault_propose"]
        actual = yaml.safe_load((home / "config.yaml").read_text())
        assert actual["mcp_servers"]["vault"]["tools"][
            "include"
        ] == profile.vault_tools(home)
        assert "vault_apply" not in profile.vault_tools(home)
        assert all(
            ((home / name).read_bytes(), (home / name).stat().st_mtime_ns) == value
            for name, value in protected.items()
        )


def test_rollback_selects_verified_snapshot_without_sync_or_state_replacement(
    managed, tmp_path, monkeypatch
):
    home, _ = managed
    identity = deployment.source_identity(ROOT)
    snapshot = deployment.snapshot_source(ROOT, tmp_path, identity)
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    scope = module["prepare_update"].__globals__
    calls = []

    def image(root, *, build):
        assert root == snapshot and build is False
        return identity, "sha256:verified"

    monkeypatch.setitem(scope, "ensure_image", image)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    before = files(home)
    env = module["prepare_update"](
        ["docker", "compose"], {"EMURU_PROFILE": str(home)}, identity
    )
    assert env["EMURU_SOURCE_ROOT"] == str(snapshot)
    assert env["EMURU_IMAGE"] == "sha256:verified"
    assert calls[0][0][-3:] == ["rollback-check", "--expect", identity]
    assert "profile-sync" not in calls[0][0]
    assert files(home) == before


@pytest.mark.parametrize("source", ["../escape", "a" * 64])
def test_rollback_rejects_missing_or_invalid_snapshot(managed, monkeypatch, source):
    home, _ = managed
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("Must reject before Docker")
    )
    before = files(home)
    with pytest.raises((ValueError, FileNotFoundError)):
        module["prepare_update"]([], {"EMURU_PROFILE": str(home)}, source)
    assert files(home) == before


def test_rollback_never_builds_a_missing_image(monkeypatch):
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return NS(returncode=1, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="rollback never builds"):
        module["ensure_image"](ROOT, build=False)
    assert len(calls) == 1 and calls[0][:3] == ["docker", "image", "inspect"]


def test_rollback_image_requires_matching_profile_without_changing_state(
    managed, monkeypatch
):
    import sys

    home, _ = managed
    monkeypatch.setattr(
        sys,
        "argv",
        ["deployment", "rollback-check", "--expect", deployment.source_identity(ROOT)],
    )
    before = files(home)
    deployment.main()
    assert files(home) == before
    write_private(home / "SOUL.md", "Unreviewed guidance")
    before = files(home)
    with pytest.raises(ValueError, match="Customized or unknown"):
        deployment.main()
    assert files(home) == before


def test_rollback_refuses_changed_runtime_pins(managed, tmp_path, monkeypatch):
    home, _ = managed
    original = deployment.source_identity(ROOT)
    candidate = deployment.snapshot_source(ROOT, tmp_path, original)
    (candidate / "infra/hermes/runtime-lock.json").write_text("{}\n")
    identity = deployment.source_identity(candidate)
    candidate.rename(candidate.with_name(identity))
    module = runpy.run_path(str(ROOT / "scripts/hermes/container.py"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("Must reject before Docker")
    )
    before = files(home)
    with pytest.raises(ValueError, match="runtime or orchestration is incompatible"):
        module["prepare_update"]([], {"EMURU_PROFILE": str(home)}, identity)
    assert files(home) == before


def test_rollback_rejects_enabled_fallback_policy(managed, monkeypatch):
    import sys

    from emuru.models import gateway

    home, _ = managed
    monkeypatch.setattr(gateway, "LOCAL_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["deployment", "rollback-check", "--expect", deployment.source_identity(ROOT)],
    )
    before = files(home)
    with pytest.raises(SystemExit, match="cloud-primary-only"):
        deployment.main()
    assert files(home) == before
