"""Synthetic stopped-vault archive and restore drill; no runtime vault touched."""

import hashlib
import json
import os
import tarfile
from pathlib import Path

from telegram_helpers import update

from emuru.telegram.queue import TelegramQueue
from emuru.vault.mutations import cancel_proposal, journal, store_proposal


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stopped_synthetic_vault_backup_restores_uncertainty_without_replay(tmp_path):
    project = Path(__file__).resolve().parents[1]
    policy = json.loads((project / "infra/hermes/telegram-settings.json").read_text())
    source = tmp_path / "synthetic-vault"
    queue_path = source / "runtime" / "queue.sqlite3"
    queue_path.parent.mkdir(parents=True, mode=0o700)
    profile = source / "profiles" / "emuru"
    history = profile / "history"
    history.mkdir(parents=True, mode=0o700)
    (profile / "profile.md").write_text("synthetic private profile\n")
    (history / "conversation.jsonl").write_text('{"role":"user","text":"hello"}\n')
    vault = source / "vault"
    note = vault / "10_Projects" / "note.md"
    note.parent.mkdir(parents=True, mode=0o700)
    note.write_text("# Synthetic note\n")
    (profile / "mutation-policy.json").write_text(
        '{"enabled":false,"roots":["10_Projects"]}\n'
    )
    (source / "route.json").write_text('{"profile":"emuru","session":"synthetic"}\n')
    (source / "deployment-identity.json").write_text(
        '{"deployment":"synthetic-v0.4.1","image":"fixture"}\n'
    )
    for directory in (source, *[p for p in source.rglob("*") if p.is_dir()]):
        directory.chmod(0o700)
    for item in source.rglob("*"):
        if item.is_file():
            item.chmod(0o600)

    queue = TelegramQueue(queue_path, policy, 42, clock=lambda: 100000)
    queue.stage(7, [update(9)])
    historical = queue.claim(7)
    queue.finish(historical["sequence"], True)
    queue.stage(7, [update(8)])
    unknown_send = queue.claim(7)
    queue.finish(unknown_send["sequence"], False, "send_unknown")
    queue.stage(7, [update(1), update(2), update(3), update(4)])
    started = queue.claim(7, session_key="synthetic-session")
    assert started["update_id"] == 1
    queue.mark_tools_started(
        7, 1
    )  # Tool effects may have happened; outcome is unknown.
    queued = queue.claim(7, local_status=True)
    assert queued is None  # Started work blocks ordinary dispatch.
    queue.close()

    proposal_id = store_proposal(profile, vault, "10_Projects/note.md", "# Proposed\n")
    cancel_proposal(profile, proposal_id)

    files = sorted(path for path in source.rglob("*") if path.is_file())
    original = {path.relative_to(source).as_posix(): _sha256(path) for path in files}
    modes = {
        path.relative_to(source).as_posix(): path.stat().st_mode & 0o777
        for path in [source, *source.rglob("*"), *files]
        if path.is_dir() or path.is_file()
    }
    archive = tmp_path / "vault.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(source, arcname="vault")
    archive.chmod(0o600)
    checksum = tmp_path / "vault.tar.sha256"
    checksum.write_text(f"{_sha256(archive)}  vault.tar\n")
    checksum.chmod(0o600)
    assert checksum.read_text().split()[0] == _sha256(archive)

    restored = tmp_path / "restored-elsewhere"
    restored.mkdir(mode=0o700)
    with tarfile.open(archive, "r") as tar:
        tar.extractall(restored, filter="data")
    restored_source = restored / "vault"
    restored_files = sorted(
        path for path in restored_source.rglob("*") if path.is_file()
    )
    assert {
        path.relative_to(restored_source).as_posix(): _sha256(path)
        for path in restored_files
    } == original
    assert {
        path.relative_to(restored_source).as_posix(): path.stat().st_mode & 0o777
        for path in [restored_source, *restored_source.rglob("*"), *restored_files]
        if path.is_dir() or path.is_file()
    } == modes
    assert os.stat(restored_source / "runtime").st_mode & 0o777 == 0o700
    assert os.stat(restored_source / "runtime/queue.sqlite3").st_mode & 0o777 == 0o600

    restored_profile = restored_source / "profiles" / "emuru"
    with journal(restored_profile) as db:
        proposal = db.execute(
            "SELECT id, vault, target, before_hash, after_hash, outcome FROM proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        receipt = db.execute(
            "SELECT proposal_id, outcome, before_hash, after_hash FROM receipts WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    assert tuple(proposal) == (
        proposal_id,
        str(vault),
        "10_Projects/note.md",
        hashlib.sha256(b"# Synthetic note\n").hexdigest(),
        hashlib.sha256(b"# Proposed\n").hexdigest(),
        "cancelled",
    )
    assert tuple(receipt) == (
        proposal_id,
        "cancelled",
        hashlib.sha256(b"# Synthetic note\n").hexdigest(),
        hashlib.sha256(b"# Proposed\n").hexdigest(),
    )
    assert proposal[1] != str(restored_source / "vault")

    restored_queue = TelegramQueue(
        restored_source / "runtime" / "queue.sqlite3",
        policy,
        42,
        clock=lambda: 100000,
    )
    restored_queue.recover()
    assert restored_queue.status() == {
        "completed": 1,
        "failed": 1,
        "interrupted": 1,
        "queued": 3,
    }
    report = restored_queue.claim_report(7)
    assert (report["update_id"], report["error_code"]) == (8, "send_unknown")
    report = restored_queue.claim_report(7)
    assert (report["update_id"], report["error_code"]) == (1, "restart_interrupted")
    assert restored_queue.claim_report(7) is None
    assert restored_queue.claim(7)["update_id"] == 2
    assert restored_queue.stage(7, [update(1)]) == ["interrupted"]
    restored_queue.close()
