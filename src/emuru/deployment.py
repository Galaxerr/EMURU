"""Content identity and conservative synchronization for stopped deployments."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from emuru.hermes import profile
from emuru.models.gateway import private_path, write_private
from emuru.telegram.queue import TelegramQueue, load_telegram_settings

READY_CHECKPOINT = (
    "EMURU Telegram checkpoint: required guards and selected MCP tools READY"
)
BUILD_FILES = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    "scripts/hermes",
    "infra/hermes",
    "agents/emuru",
    "infra/docker/secret-exec.py",
    "infra/docker/compose.yaml",
    "infra/docker/scout.compose.yaml",
    "infra/docker/coder.compose.yaml",
    "infra/docker/deployment-lock.json",
    "tests/fixtures/hermes-vault",
    "tests/telegram/native_session_case.py",
    "tests/native_inference_case.py",
    "tests/project_board_case.py",
    "tests/scout_board_case.py",
)


def source_files(root):
    for name in BUILD_FILES:
        path = root / name
        if path.is_symlink():
            raise ValueError("Build input symlinks refused")
        paths = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in paths:
            if "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            if item.is_symlink():
                raise ValueError("Build input symlinks refused")
            if item.is_file():
                yield item.relative_to(root)
            elif not item.is_dir():
                raise ValueError(f"Missing build input: {name}")


def source_identity(root):
    digest = hashlib.sha256()
    for relative in source_files(root):
        digest.update(str(relative).encode() + b"\0")
        digest.update(str((root / relative).stat().st_mode & 0o111).encode() + b"\0")
        digest.update(hashlib.sha256((root / relative).read_bytes()).digest())
    return digest.hexdigest()


def gateway_identity(root, launcher=None):
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        if "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update((launcher or root / "infra/docker/secret-exec.py").read_bytes())
    return digest.hexdigest()


def snapshot_source(root, home, identity):
    """Freeze gateway imports; an old gateway must never import a changed checkout."""
    destination = private_path(home / "sources" / identity)
    if not destination.exists():
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            stage = Path(temporary) / "source"
            stage.mkdir(mode=0o700)
            for relative in source_files(root):
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / relative, target)
            if source_identity(stage) != identity:
                raise ValueError("Source changed during snapshot; retry wake")
            stage.rename(destination)
    if source_identity(destination) != identity:
        raise ValueError("Deployment source snapshot mismatch")
    return destination


def _private_file(path):
    private_path(path)
    info = path.stat()
    if (
        not path.is_file()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o600
    ):
        raise ValueError("Profile file must be owner-owned and mode 0600")
    return path.read_bytes()


@contextmanager
def _database_snapshot(path):
    # Inspect a copy, including WAL, without creating sidecars in private state.
    with tempfile.TemporaryDirectory() as temporary:
        copy = Path(temporary) / path.name
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(path) + suffix)
            if source.exists():
                _private_file(source)
                shutil.copyfile(source, Path(str(copy) + suffix))
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as db:
            yield db


def _schema(path):
    with _database_snapshot(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0], db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()


def check_state(home, *, allow_proposal_upgrade=False, target_root=None):
    check_review_state(home, target_root or Path(__file__).resolve().parents[2])
    upgrade = False
    queue = home / "emuru-telegram/queue.sqlite3"
    if queue.exists():
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "queue.sqlite3"
            settings = load_telegram_settings(
                Path(__file__).resolve().parents[2]
                / "infra/hermes/telegram-settings.json"
            )
            baseline = TelegramQueue(reference, settings, 1)
            baseline.close()
            if _schema(queue) != _schema(reference):
                raise ValueError("Queue migration requires explicit approval")
    journal = home / "emuru-mutations/mutations.sqlite"
    if journal.exists():
        from emuru.vault.mutations import journal as open_journal

        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary)
            with open_journal(reference):
                pass
            actual = _schema(journal)
            expected = _schema(reference / "emuru-mutations/mutations.sqlite")
            if actual != expected:
                legacy = (
                    expected[0],
                    [
                        row
                        for row in expected[1]
                        if row[1] not in {"proposal_lifecycle", "revision_requests"}
                    ],
                )
                capture_names = {
                    "project_result_captures",
                    "project_result_capture_active",
                }
                previous_capture = (
                    expected[0],
                    [row for row in expected[1] if row[1] not in capture_names],
                )
                previous_legacy = (
                    legacy[0],
                    [row for row in legacy[1] if row[1] not in capture_names],
                )
                if not allow_proposal_upgrade or actual not in (
                    legacy,
                    previous_capture,
                    previous_legacy,
                ):
                    raise ValueError(
                        "Mutation state migration requires explicit approval"
                    )
                upgrade = True
    return upgrade


def check_review_state(home, target_root):
    """Never roll newest review facts into an executor unaware of their schema."""
    from emuru.projects.supervisor_lifecycle import check_compatible

    check_compatible(home, target_root)
    from emuru.projects.coder_runtime import _read_private
    from emuru.projects.reviewer import _read, unique_fields
    from emuru.vault.mutations import directory, private_directory

    attempts = Path(home) / "emuru-projects/coder-attempts"
    if not attempts.exists():
        return
    with directory(attempts) as parent:
        private_directory(parent)
        names = os.listdir(parent)
    for name in names:
        folder = attempts / name
        grant = json.loads(
            _read_private(folder / "grant.json", 32768), object_pairs_hook=unique_fields
        )
        if (
            not isinstance(grant, dict)
            or type(grant.get("schema")) is not int
            or grant["schema"] not in {1, 2, 3}
        ):
            raise ValueError("Unknown coder state schema; preserve newest state.")
        if grant["schema"] == 3:
            source = Path(target_root) / "src/emuru/projects/rework.py"
            if not source.is_file() or "STATE_SCHEMA = 1\n" not in source.read_text():
                raise ValueError(
                    "Rework state is incompatible; preserve newest state and refuse rollback."
                )
        if grant["schema"] in {2, 3}:
            state = _read(folder / "loop.json")
            source = Path(target_root) / "src/emuru/projects/reviewer.py"
            if (
                state.get("schema") != 1
                or not source.is_file()
                or "STATE_SCHEMA = 1\n" not in source.read_text()
            ):
                raise ValueError(
                    "Review state is incompatible; preserve newest state and refuse rollback."
                )


@contextmanager
def stopped_profile(home):
    lock = private_path(home / "emuru-telegram/instance.lock")
    if not lock.exists():
        yield
        return
    _private_file(lock)
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def sync_profile(root, home, *, apply=False):
    with stopped_profile(home):
        return _sync_profile(root, home, apply=apply)


def upgrade_proposals(home):
    """Explicitly upgrade only the known proposal schema, preserving a stopped backup."""
    from emuru.vault.mutations import journal as open_journal

    with stopped_profile(home):
        if not check_state(home, allow_proposal_upgrade=True):
            return {"state_changed": False}
        path = home / "emuru-mutations/mutations.sqlite"
        fd, backup = tempfile.mkstemp(
            prefix="before-proposal-upgrade-", suffix=".sqlite", dir=path.parent
        )
        try:
            with (
                _database_snapshot(path) as source,
                closing(sqlite3.connect(backup)) as target,
            ):
                source.backup(target)
            os.fsync(fd)
        finally:
            os.close(fd)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        with open_journal(home, capture_upgrade=True):
            pass
        check_state(home)
        return {"state_changed": True, "backup": backup}


def upgrade_continuation(home, root=None):
    """Explicit stopped upgrade; retain byte-exact legacy backup and recovery marker."""
    from emuru.projects import supervisor
    from emuru.projects.coder_board import _write_new
    from emuru.projects.dispatcher import dispatcher_lock
    from emuru.projects.scout import encoded
    from emuru.projects.supervisor_lifecycle import inspect_idle
    from emuru.vault import mutations

    home = Path(home)
    root = root or Path(__file__).resolve().parents[2]
    folder = home / "emuru-projects"
    with stopped_profile(home):
        if not folder.exists():
            folder.mkdir(mode=0o700)
        with dispatcher_lock(home):
            if not supervisor.enabled(home):
                if set(os.listdir(folder)) - {"scout.lock", "binder.lock"}:
                    inspect_idle(home, root, [])
                supervisor._initialize(home)
            with supervisor._locked(home, write=True) as (parent, size, old):
                previous = old
                records, size = supervisor._records(parent, size, old)
                # An empty never-launched profile has no native database to inspect.
                empty = not records and set(os.listdir(folder)) <= {
                    "scout.lock",
                    "binder.lock",
                    "supervisor",
                    "supervisor-enabled.json",
                    "continuation-backup.json",
                    "continuation-upgrade.json",
                }
                if not empty:
                    inspect_idle(home, root, records)
                backup_path = folder / "continuation-backup.json"
                recovery_path = folder / "continuation-upgrade.json"
                if recovery_path.exists():
                    recovery = json.loads(_private_file(recovery_path))
                    backup_data = _private_file(backup_path)
                    if (
                        set(recovery) != {"schema", "backup_sha256", "old", "new"}
                        or recovery["schema"] != 1
                        or hashlib.sha256(backup_data).hexdigest()
                        != recovery["backup_sha256"]
                    ):
                        raise ValueError("Unknown continuation upgrade recovery state.")
                    if old["schema"] == supervisor.CONTINUATION_SCHEMA:
                        if any(
                            old[k] != recovery["new"][k]
                            for k in ("schema", "legacy_generation", "legacy_head")
                        ):
                            raise ValueError("Continuation upgrade prefix mismatch.")
                    elif old != recovery["old"]:
                        raise ValueError("Continuation upgrade old state mismatch.")
                    new = recovery["new"]
                    previous = recovery["old"]
                    if new != {
                        **previous,
                        "schema": supervisor.CONTINUATION_SCHEMA,
                        "legacy_generation": previous["generation"],
                        "legacy_head": previous["head"],
                    }:
                        raise ValueError(
                            "Unknown continuation upgrade marker transition."
                        )
                else:
                    if old["schema"] != supervisor.STATE_SCHEMA:
                        raise ValueError(
                            "Continuation upgrade recovery marker missing."
                        )
                    historical = {
                        name: _private_file(folder / "supervisor" / name).decode()
                        for name in sorted(os.listdir(parent))
                    }
                    backup_data = encoded(
                        {"schema": 1, "marker": old, "files": historical}
                    )
                    if backup_path.exists():
                        if _private_file(backup_path) != backup_data:
                            raise ValueError("Continuation upgrade backup mismatch.")
                    else:
                        _write_new(folder, backup_path.name, backup_data)
                    new = {
                        **old,
                        "schema": supervisor.CONTINUATION_SCHEMA,
                        "legacy_generation": old["generation"],
                        "legacy_head": old["head"],
                    }
                    supervisor._records(
                        parent,
                        size - sum(len(v.encode()) for v in historical.values()),
                        new,
                    )
                    _write_new(
                        folder,
                        recovery_path.name,
                        encoded(
                            {
                                "schema": 1,
                                "old": old,
                                "new": new,
                                "backup_sha256": hashlib.sha256(
                                    backup_data
                                ).hexdigest(),
                            }
                        ),
                    )
                backup = json.loads(_private_file(backup_path))
                if (
                    set(backup) != {"schema", "marker", "files"}
                    or backup["schema"] != 1
                    or backup["marker"] != previous
                    or set(backup["files"])
                    != {
                        "control.lock",
                        *(
                            f"{i:08d}.json"
                            for i in range(1, previous["generation"] + 1)
                        ),
                    }
                ):
                    raise ValueError("Continuation backup marker mismatch.")
                for name, data in backup["files"].items():
                    if _private_file(folder / "supervisor" / name).decode() != data:
                        raise ValueError("Continuation historical bytes changed.")
                if old["schema"] == supervisor.CONTINUATION_SCHEMA:
                    return {"state_changed": False, "backup": str(backup_path)}
                write_private(folder / "supervisor-enabled.json", encoded(new).decode())
                with mutations.directory(folder) as directory:
                    os.fsync(directory)
                return {
                    "state_changed": True,
                    "backup": str(backup_path),
                    "legacy_head": new["legacy_head"],
                }


def _sync_profile(root, home, *, apply=False):
    """Only reviewed stock guidance and compatible managed registration may change."""
    import yaml

    check_state(home, target_root=root)
    guidance = _private_file(home / "SOUL.md")
    desired = (root / "agents/emuru/SOUL.md").read_bytes()
    reviewed = json.loads((root / "infra/hermes/profile-updates.json").read_text())
    if hashlib.sha256(desired).hexdigest() != reviewed["soul_sha256"]:
        raise ValueError("New SOUL.md requires a reviewed guidance manifest")
    runtime = json.loads((root / "infra/hermes/runtime-lock.json").read_text())
    if runtime["commit"] != reviewed["hermes_commit"]:
        raise ValueError("Hermes runtime change requires explicit compatibility review")
    if (
        guidance != desired
        and hashlib.sha256(guidance).hexdigest() not in reviewed["previous_soul_sha256"]
    ):
        raise ValueError("Customized or unknown SOUL.md; explicit review required")
    raw = _private_file(home / "config.yaml")
    config = yaml.safe_load(raw)
    if not isinstance(config, dict):
        raise TypeError("Invalid profile configuration")

    def get(key):
        cursor = config
        for part in key.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        return cursor

    settings, server = profile.expected_settings(root), profile.expected_server(root)
    # Older profiles predate the explicit no-worker Kanban allowlist.
    kanban = config.get("kanban", {})
    if not isinstance(kanban, dict):
        raise TypeError("Customized Kanban configuration; explicit review required")
    add_dispatch_fence = "dispatch_profiles" not in kanban
    if add_dispatch_fence:
        config.setdefault("kanban", {})["dispatch_profiles"] = []
    previous = profile.expected_server(root, tools=profile.TOOLS)
    compatible = [{"vault": server}, {"vault": previous}]
    if "vault_proposal_status" in server["tools"]["include"]:
        compatible.append(
            {
                "vault": profile.expected_server(
                    root, tools=[*profile.TOOLS, "vault_propose"]
                )
            }
        )
    registered = get("mcp_servers")
    if registered not in compatible:
        raise ValueError("Customized MCP registration; explicit review required")
    profile.verify_profile(settings, registered["vault"], get)
    updated = registered != {"vault": server} or add_dispatch_fence
    if apply:
        if updated:
            config["mcp_servers"] = {"vault": server}
            write_private(home / "config.yaml", yaml.safe_dump(config, sort_keys=False))
        if guidance != desired:
            write_private(home / "SOUL.md", desired.decode())
    return {
        "profile_changed": updated,
        "guidance_changed": guidance != desired,
        "tools": server["tools"]["include"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "identity",
            "profile-check",
            "profile-sync",
            "proposal-upgrade",
            "continuation-upgrade",
            "rollback-check",
        ),
    )
    parser.add_argument("--expect")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.command == "identity":
        identity = source_identity(root)
        if args.expect and identity != args.expect:
            raise SystemExit("Image/source identity mismatch")
        print(identity)
    elif args.command == "rollback-check":
        from emuru.models.gateway import LOCAL_FALLBACK_ENABLED

        if not args.expect or source_identity(root) != args.expect:
            raise SystemExit("Rollback image/source identity mismatch")
        if LOCAL_FALLBACK_ENABLED:
            raise SystemExit("Rollback target must preserve cloud-primary-only policy")
        result = sync_profile(root, profile.profile_home())
        if result["profile_changed"] or result["guidance_changed"]:
            raise SystemExit("Rollback requires matching managed profile and guidance")
        print(json.dumps(result))
    elif args.command == "continuation-upgrade":
        print(json.dumps(upgrade_continuation(profile.profile_home(), root)))
    elif args.command == "proposal-upgrade":
        print(json.dumps(upgrade_proposals(profile.profile_home())))
    else:
        print(
            json.dumps(
                sync_profile(
                    root, profile.profile_home(), apply=args.command == "profile-sync"
                )
            )
        )


if __name__ == "__main__":
    main()
