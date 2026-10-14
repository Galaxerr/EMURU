"""Synthetic write-boundary and private recovery metadata checks."""

import hashlib
import json
import multiprocessing
import os
import stat

import pytest
from vault_helpers import create_vault

from emuru.vault.indexer import index_vault, vault_write
from emuru.vault.mutations import (
    cancel_proposal,
    journal,
    load_policy,
    mutation_parent,
    store_proposal,
    writer_lock,
)


def policy_home(tmp_path, roots=None):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    if roots is not None:
        record = home / "mutation-policy.json"
        record.write_text(json.dumps({"enabled": False, "roots": roots}))
        record.chmod(0o600)
    return home


def test_default_deny_and_disabled_broader_writes(tmp_path):
    home = policy_home(tmp_path)
    vault = create_vault(tmp_path)
    assert load_policy(home) == {"enabled": False, "roots": []}
    with pytest.raises(ValueError):
        store_proposal(home, vault, "10_Projects/new.md", "content")
    record = home / "mutation-policy.json"
    record.write_text('{"enabled":true,"roots":["10_Projects"]}')
    record.chmod(0o600)
    with pytest.raises(ValueError, match="disabled"):
        load_policy(home)


@pytest.mark.parametrize(
    "roots",
    [
        ["99_Private"],
        ["40_Journal"],
        ["90_Archive"],
        ["_index"],
        ["unknown"],
        ["10_Projects/../99_Private"],
        ["/10_Projects"],
        ["10_Projects/.git"],
        [7],
    ],
)
def test_invalid_policy_roots_fail_closed(tmp_path, roots):
    home = policy_home(tmp_path, roots)
    with pytest.raises(ValueError):
        load_policy(home)


@pytest.mark.parametrize(
    "data",
    [
        "{",
        "{}",
        "[]",
        '{"enabled":false,"roots":"10_Projects"}',
        '{"enabled":false,"roots":[],"extra":1}',
    ],
)
def test_malformed_policy(tmp_path, data):
    home = policy_home(tmp_path)
    record = home / "mutation-policy.json"
    record.write_text(data)
    record.chmod(0o600)
    with pytest.raises(ValueError):
        load_policy(home)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/00_Inbox/a.md",
        "00_Inbox/../a.md",
        "00_Inbox/./a.md",
        "00_Inbox//a.md",
        "00_Inbox\\a.md",
        "00_Inbox/a.txt",
        "99_Private/a.md",
        "10_Projects/a.md",
    ],
)
def test_capture_escape_denied(tmp_path, path):
    vault = create_vault(tmp_path)
    with pytest.raises((ValueError, OSError)):
        vault_write(vault, path, "content")
    assert not list((vault / "00_Inbox").rglob("*.md"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_mutation_destination_refused(tmp_path, kind):
    vault = create_vault(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("secret")
    target = vault / "00_Inbox/a.md"
    if kind == "symlink":
        target.symlink_to(secret)
    elif kind == "hardlink":
        os.link(secret, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises((ValueError, OSError)):
        vault_write(vault, "00_Inbox/a.md", "overwrite")
    assert secret.read_text() == "secret"


def test_parent_and_vault_root_symlinks_refused(tmp_path):
    vault = create_vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "00_Inbox/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        vault_write(vault, "00_Inbox/link/a.md", "secret")
    linked = tmp_path / "linked-vault"
    linked.symlink_to(vault, target_is_directory=True)
    with pytest.raises(OSError):
        index_vault(linked)
    assert not list(outside.iterdir())


def test_descriptor_stays_on_open_parent(tmp_path):
    vault = create_vault(tmp_path)
    parent = vault / "00_Inbox/sub"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with mutation_parent(vault, "00_Inbox/sub/a.md", capture=True) as (fd, name):
        parent.rename(vault / "00_Inbox/old")
        parent.symlink_to(outside, target_is_directory=True)
        note = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        os.write(note, b"safe")
        os.close(note)
    assert not list(outside.iterdir())
    assert (vault / "00_Inbox/old/a.md").read_bytes() == b"safe"


def _capture(vault, entered, finished, operation):
    entered.set()
    if operation == "capture":
        vault_write(vault, "00_Inbox/child.md", "child")
    else:
        index_vault(vault)
    finished.set()


@pytest.mark.parametrize("operation", ["capture", "index"])
def test_common_lock_excludes_other_process(tmp_path, operation):
    vault = create_vault(tmp_path)
    context = multiprocessing.get_context("spawn")
    entered, finished = context.Event(), context.Event()
    with writer_lock(vault):
        child = context.Process(
            target=_capture, args=(vault, entered, finished, operation)
        )
        child.start()
        assert entered.wait(10)
        assert not finished.wait(0.2)
        assert not (vault / "00_Inbox/child.md").exists()
    child.join(10)
    if child.is_alive():
        child.terminate()
        child.join()
        pytest.fail("writer did not release lock")
    assert child.exitcode == 0 and finished.is_set()


def test_proposal_receipt_private_and_durable(tmp_path):
    home = policy_home(tmp_path, ["10_Projects"])
    vault = create_vault(tmp_path)
    target = vault / "10_Projects/a.md"
    target.write_bytes(b"human\r\n")
    identity = store_proposal(home, vault, "10_Projects/a.md", "proposal")
    assert target.read_bytes() == b"human\r\n"
    cancel_proposal(home, identity)
    cancel_proposal(home, identity)
    with journal(home) as db:
        row = db.execute(
            "SELECT digest, vault, target, before_hash, after_hash, outcome FROM proposals WHERE id=?",
            (identity,),
        ).fetchone()
        assert row == (
            hashlib.sha256(b"proposal").hexdigest(),
            str(vault),
            "10_Projects/a.md",
            hashlib.sha256(b"human\r\n").hexdigest(),
            hashlib.sha256(b"proposal").hexdigest(),
            "cancelled",
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert stat.S_IMODE((home / "emuru-mutations").stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((home / "emuru-mutations/mutations.sqlite").stat().st_mode)
        == 0o600
    )


@pytest.mark.parametrize(
    "file",
    ["mutation-policy.json", "emuru-mutations/mutations.sqlite", ".emuru-writer.lock"],
)
def test_private_files_reject_hardlinks(tmp_path, file):
    home = policy_home(tmp_path)
    vault = create_vault(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("secret")
    secret.chmod(0o600)
    base = vault if file == ".emuru-writer.lock" else home
    target = base / file
    target.parent.mkdir(mode=0o700, exist_ok=True)
    os.link(secret, target)
    with pytest.raises(ValueError):
        if file == "mutation-policy.json":
            load_policy(home)
        elif file == ".emuru-writer.lock":
            with writer_lock(vault):
                pass
        else:
            with journal(home):
                pass
    assert secret.read_text() == "secret"


def test_selected_policy_location_excludes_siblings(tmp_path):
    home = policy_home(tmp_path, ["10_Projects/approved"])
    vault = create_vault(tmp_path)
    (vault / "10_Projects/approved").mkdir()
    identity = store_proposal(home, vault, "10_Projects/approved/new.md", "new")
    with journal(home) as db:
        assert db.execute(
            "SELECT outcome FROM proposals WHERE id=?", (identity,)
        ).fetchone() == ("proposed",)
    assert not (vault / "10_Projects/approved/new.md").exists()
    with pytest.raises(ValueError):
        store_proposal(home, vault, "10_Projects/approved-other/new.md", "new")


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_journal_sidecars_refuse_symlinks(tmp_path, suffix):
    home = policy_home(tmp_path)
    with journal(home):
        pass
    secret = tmp_path / "secret"
    secret.write_text("secret")
    (home / "emuru-mutations" / ("mutations.sqlite" + suffix)).symlink_to(secret)
    with pytest.raises(OSError), journal(home):
        pass
    assert secret.read_text() == "secret"


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_policy_must_be_private(tmp_path, mode):
    home = policy_home(tmp_path, ["10_Projects"])
    (home / "mutation-policy.json").chmod(mode)
    with pytest.raises(ValueError, match="permissions"):
        load_policy(home)


def test_failed_index_does_not_delete_replaced_note(tmp_path, monkeypatch):
    import emuru.vault.indexer as index

    vault = create_vault(tmp_path)
    target = vault / "00_Inbox/new.md"

    def replace_then_fail(root):
        target.rename(target.with_suffix(".old"))
        target.write_text("human replacement")
        raise OSError("index failed")

    monkeypatch.setattr(index, "index_vault", replace_then_fail)
    result = vault_write(vault, "00_Inbox/new.md", "capture")
    assert result == {
        "created": True,
        "path": "00_Inbox/new.md",
        "outcome": "index_pending",
        "message": "saved; indexing pending",
    }
    assert target.read_text() == "human replacement"
