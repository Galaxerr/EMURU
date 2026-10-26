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


def check_state(home, *, allow_proposal_upgrade=False):
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
                if not allow_proposal_upgrade or actual != legacy:
                    raise ValueError(
                        "Mutation state migration requires explicit approval"
                    )
                upgrade = True
    return upgrade


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
        with open_journal(home):
            pass
        check_state(home)
        return {"state_changed": True, "backup": backup}


def _sync_profile(root, home, *, apply=False):
    """Only reviewed stock guidance and compatible managed registration may change."""
    import yaml

    check_state(home)
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
