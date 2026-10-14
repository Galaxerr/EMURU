"""Synthetic precise proposals, durable publication and crash classification."""

import errno
import hashlib
import json
import multiprocessing
import os
import signal

import pytest
from vault_helpers import create_vault

from emuru.vault import indexer, mutations


def setup(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text(json.dumps({"enabled": False, "roots": ["10_Projects"]}))
    policy.chmod(0o600)
    return home, create_vault(tmp_path)


def apply(home, vault, proposal):
    return mutations.apply_proposal(
        home,
        vault,
        proposal["proposal_id"],
        proposal["approval_digest"],
        writers_paused=True,
    )


def test_create_requires_absence_and_repeated_apply_returns_receipt(
    tmp_path, monkeypatch
):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(
        home, vault, "10_Projects/new.md", content="# New\r\n"
    )
    target = vault / proposal["path"]
    assert not target.exists()
    first = apply(home, vault, proposal)
    assert first["outcome"] == "applied"
    assert target.read_bytes() == b"# New\r\n"
    metadata = target.stat()

    def forbidden(*args, **kwargs):
        pytest.fail("repeated proposal published twice")

    monkeypatch.setattr(mutations, "publish_note", forbidden)
    assert apply(home, vault, proposal) == first
    assert target.stat().st_mtime_ns == metadata.st_mtime_ns
    existing = mutations.propose_note(home, vault, proposal["path"], content="another")
    assert existing["outcome"] == "exists"
    assert existing["content"].encode() == b"# New\r\n"


def test_exact_update_preserves_human_bytes(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/note.md"
    original = b"---\r\ntitle: Human\r\nsummary: Keep me\r\n---\r\n# Human\r\n- [ ] Task ^task-id\r\n[[Other#Block]]\r\nHuman edits before preview.\r\n"
    target.write_bytes(original)
    proposal = mutations.propose_note(
        home,
        vault,
        "10_Projects/note.md",
        expected_revision=hashlib.sha256(original).hexdigest(),
        anchor="- [ ] Task ^task-id",
        replacement="- [x] Task ^task-id",
    )
    assert target.read_bytes() == original
    assert apply(home, vault, proposal)["outcome"] == "applied"
    assert target.read_bytes() == original.replace(b"- [ ] Task", b"- [x] Task")


@pytest.mark.parametrize(
    "text,anchor",
    [("aaa", "aa"), ("same same", "same"), ("text", "missing"), ("text", "")],
)
def test_ambiguous_or_missing_anchor_refused(tmp_path, text, anchor):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/note.md"
    target.write_text(text)
    with pytest.raises(ValueError, match="anchor"):
        mutations.propose_note(
            home,
            vault,
            "10_Projects/note.md",
            expected_revision=hashlib.sha256(text.encode()).hexdigest(),
            anchor=anchor,
            replacement="new",
        )
    assert target.read_text() == text


@pytest.mark.parametrize(
    "text",
    [
        "<<<<<<< mine\na\n=======\nb\n>>>>>>> theirs",
        "=======\n",
        ">>>>>>> theirs\n",
        "<<<<<<<\r\n",
        "=======\r\n",
        ">>>>>>>\r\n",
        "<<<<<<<< ours\n",
        "========\n",
    ],
)
def test_merge_markers_refused(tmp_path, text):
    home, vault = setup(tmp_path)
    with pytest.raises(ValueError, match="merge markers"):
        mutations.propose_note(home, vault, "10_Projects/new.md", content=text)


def test_stale_revision_and_collision_refuse_without_writing(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/note.md"
    target.write_text("original")
    with pytest.raises(ValueError, match="revision"):
        mutations.propose_note(
            home,
            vault,
            "10_Projects/note.md",
            expected_revision="0" * 64,
            anchor="original",
            replacement="new",
        )
    proposal = mutations.propose_note(
        home,
        vault,
        "10_Projects/note.md",
        expected_revision=hashlib.sha256(b"original").hexdigest(),
        anchor="original",
        replacement="new",
    )
    target.write_text("human edit after preview")
    with pytest.raises(ValueError, match="revision"):
        apply(home, vault, proposal)
    assert target.read_text() == "human edit after preview"
    created = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    (vault / "10_Projects/new.md").write_text("human collision")
    with pytest.raises(ValueError, match="revision"):
        apply(home, vault, created)
    assert (vault / "10_Projects/new.md").read_text() == "human collision"
    assert not list((vault / "10_Projects").glob("new-*.md"))


def test_approval_policy_vault_and_pause_are_rechecked(tmp_path):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    with pytest.raises(ValueError, match="paused"):
        mutations.apply_proposal(
            home, vault, proposal["proposal_id"], proposal["approval_digest"]
        )
    with pytest.raises(ValueError, match="Approval"):
        apply(home, vault, {**proposal, "approval_digest": "0" * 64})
    other = create_vault(tmp_path / "other")
    with pytest.raises(ValueError, match="another vault"):
        apply(home, other, proposal)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":[]}')
    with pytest.raises(ValueError, match="restricted"):
        apply(home, vault, proposal)
    assert not (vault / proposal["path"]).exists()


def test_tampered_proposal_requires_new_approval(tmp_path):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    with mutations.journal(home) as db:
        digest = hashlib.sha256(b"changed").hexdigest()
        db.execute(
            "UPDATE proposals SET payload=?, digest=?, after_hash=? WHERE id=?",
            ("changed", digest, digest, proposal["proposal_id"]),
        )
        db.commit()
    with pytest.raises(ValueError, match="Approval"):
        apply(home, vault, proposal)
    assert not (vault / proposal["path"]).exists()


def test_legacy_proposal_cannot_apply(tmp_path):
    home, vault = setup(tmp_path)
    identity = mutations.store_proposal(home, vault, "10_Projects/new.md", "new")
    with pytest.raises(ValueError, match="validated"):
        mutations.apply_proposal(home, vault, identity, "0" * 64, writers_paused=True)


def test_real_target_refused_even_in_tmp(tmp_path):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    target_record = home / "vault-target.json"
    target_record.write_text(json.dumps({"mode": "real", "vault_path": str(vault)}))
    target_record.chmod(0o600)
    with pytest.raises(ValueError, match="real targets"):
        apply(home, vault, proposal)


def test_cancelled_proposal_never_applies(tmp_path):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    mutations.cancel_proposal(home, proposal["proposal_id"])
    assert apply(home, vault, proposal)["outcome"] == "cancelled"
    assert not (vault / proposal["path"]).exists()


def test_index_failure_keeps_note_and_retries_only_index(tmp_path, monkeypatch):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")

    def fail(*args):
        raise OSError("index unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "index_vault", fail)
        receipt = apply(home, vault, proposal)
    assert receipt["outcome"] == "index_pending"
    assert receipt["message"] == "saved; indexing pending"
    target = vault / proposal["path"]
    assert target.read_bytes() == b"new"
    modified = target.stat().st_mtime_ns
    assert apply(home, vault, proposal)["outcome"] == "applied"
    assert target.stat().st_mtime_ns == modified


def test_disk_full_does_not_publish_or_replay(tmp_path, monkeypatch):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")

    fsync = mutations.os.fsync

    def full(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith(".tmp"):
            raise OSError(errno.ENOSPC, "disk full")
        return fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(mutations.os, "fsync", full)
        with pytest.raises(OSError):
            apply(home, vault, proposal)
    assert not (vault / proposal["path"]).exists()
    assert apply(home, vault, proposal)["outcome"] == "not_applied"
    assert not (vault / proposal["path"]).exists()


@pytest.mark.parametrize("capture", [False, True])
def test_malformed_yaml_after_commit_reports_index_pending(tmp_path, capture):
    home, vault = setup(tmp_path)
    human = vault / "10_Projects/human.md"
    human.write_bytes(b"---\ntitle: [unfinished\n---\nHuman content\n")
    original = human.read_bytes()
    path = "00_Inbox/new.md" if capture else "10_Projects/new.md"
    if capture:
        result = indexer.vault_write(vault, path, "new\n")
    else:
        proposal = mutations.propose_note(home, vault, path, content="new\n")
        result = apply(home, vault, proposal)
    assert result["outcome"] == "index_pending"
    assert result["message"] == "saved; indexing pending"
    target = vault / path
    assert target.read_bytes() == b"new\n"
    assert human.read_bytes() == original
    modified = target.stat().st_mtime_ns
    if not capture:
        assert apply(home, vault, proposal)["outcome"] == "index_pending"
        human.write_text("Repaired human content\n")
        assert mutations.recover_proposals(home, vault)[0]["outcome"] == "applied"
        assert target.stat().st_mtime_ns == modified


def _crash(home, vault, proposal, point):
    from emuru.vault import mutations as child

    publish = child._publish_note
    record = child._record_receipt

    def interrupted_publish(*args, **kwargs):
        if point == "before_publication":
            os.kill(os.getpid(), signal.SIGKILL)
        publish(*args, **kwargs)
        if point == "after_publication":
            os.kill(os.getpid(), signal.SIGKILL)

    def interrupted_record(db, effect, outcome):
        if point == "before_receipt" and outcome == "applied":
            os.kill(os.getpid(), signal.SIGKILL)
        result = record(db, effect, outcome)
        if point == "after_receipt" and outcome == "applied":
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    child._publish_note = interrupted_publish
    child._record_receipt = interrupted_record
    apply(home, vault, proposal)


@pytest.mark.parametrize(
    "point",
    ["before_publication", "after_publication", "before_receipt", "after_receipt"],
)
@pytest.mark.parametrize("update", [False, True])
def test_sigkill_classifies_without_replay(tmp_path, point, update):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/note.md"
    if update:
        target.write_text("before")
        proposal = mutations.propose_note(
            home,
            vault,
            "10_Projects/note.md",
            expected_revision=hashlib.sha256(b"before").hexdigest(),
            anchor="before",
            replacement="after",
        )
    else:
        proposal = mutations.propose_note(
            home, vault, "10_Projects/note.md", content="after"
        )
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crash, args=(home, vault, proposal, point))
    child.start()
    child.join(15)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("crash probe hung")
    assert child.exitcode == -signal.SIGKILL
    before = target.read_bytes() if target.exists() else None
    mutations.recover_proposals(home, vault)
    result = apply(home, vault, proposal)
    assert result["outcome"] == (
        "not_applied" if point == "before_publication" else "applied"
    )
    assert (target.read_bytes() if target.exists() else None) == before
    assert not list(target.parent.glob(".emuru-*.tmp"))


def test_indeterminate_effect_quarantined(tmp_path, monkeypatch):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")

    def changed(*args, **kwargs):
        (vault / proposal["path"]).write_text("unknown human effect")
        raise OSError("publication interrupted")

    monkeypatch.setattr(mutations, "publish_note", changed)
    with pytest.raises(OSError):
        apply(home, vault, proposal)
    assert apply(home, vault, proposal)["outcome"] == "quarantined"
    assert (vault / proposal["path"]).read_text() == "unknown human effect"


def _compete(home, vault, proposal, gate, results):
    gate.wait(10)
    results.put(apply(home, vault, proposal))


def test_competing_apply_commits_once(tmp_path):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    children = [
        context.Process(target=_compete, args=(home, vault, proposal, gate, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    gate.set()
    receipts = [results.get(timeout=15) for _ in children]
    for child in children:
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join()
            pytest.fail("competing apply hung")
        assert child.exitcode == 0
    assert receipts[0] == receipts[1]
    assert receipts[0]["outcome"] == "applied"
    with mutations.journal(home) as db:
        assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 1


@pytest.mark.parametrize("creation", [False, True])
def test_human_edit_during_preparation_rejects_stale_payload(
    tmp_path, monkeypatch, creation
):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/note.md"
    if not creation:
        target.write_text("before")
    store = mutations.store_proposal

    def edit_then_store(*args, **kwargs):
        target.write_text("new human edit")
        return store(*args, **kwargs)

    monkeypatch.setattr(mutations, "store_proposal", edit_then_store)
    with pytest.raises(ValueError, match="Stale revision"):
        if creation:
            mutations.propose_note(home, vault, "10_Projects/note.md", content="new")
        else:
            mutations.propose_note(
                home,
                vault,
                "10_Projects/note.md",
                expected_revision=hashlib.sha256(b"before").hexdigest(),
                anchor="before",
                replacement="after",
            )
    assert target.read_text() == "new human edit"
    with mutations.journal(home) as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["symlink", "hardlink", "content"])
def test_staging_tampering_refused_before_publication(tmp_path, monkeypatch, change):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    secret = tmp_path / "secret"
    secret.write_text("secret")
    secret.chmod(0o600)
    read_note = mutations.read_note

    def alter_staging(parent, name):
        stages = list((vault / "10_Projects").glob(".emuru-*.tmp"))
        if stages:
            stage = stages[0]
            if change == "content":
                stage.write_text("tampered")
            else:
                stage.unlink()
                if change == "symlink":
                    stage.symlink_to(secret)
                else:
                    os.link(secret, stage)
        return read_note(parent, name)

    monkeypatch.setattr(mutations, "read_note", alter_staging)
    with pytest.raises((OSError, ValueError)):
        apply(home, vault, proposal)
    assert not (vault / proposal["path"]).exists()
    assert secret.read_text() == "secret"


def test_cancel_refuses_prepared_and_completed_effects(tmp_path, monkeypatch):
    home, vault = setup(tmp_path)
    proposal = mutations.propose_note(home, vault, "10_Projects/new.md", content="new")
    publish = mutations.publish_note

    def cancel_then_publish(*args, **kwargs):
        with pytest.raises(ValueError, match="cannot be cancelled"):
            mutations.cancel_proposal(home, proposal["proposal_id"])
        return publish(*args, **kwargs)

    monkeypatch.setattr(mutations, "publish_note", cancel_then_publish)
    assert apply(home, vault, proposal)["outcome"] == "applied"
    with pytest.raises(ValueError, match="cannot be cancelled"):
        mutations.cancel_proposal(home, proposal["proposal_id"])


@pytest.mark.parametrize(
    "field,value",
    [("content", 7), ("anchor", []), ("replacement", True), ("expected_revision", {})],
)
def test_untrusted_proposal_field_types_fail_closed(tmp_path, field, value):
    home, vault = setup(tmp_path)
    with pytest.raises(ValueError, match="fields"):
        mutations.propose_note(home, vault, "10_Projects/new.md", **{field: value})
    assert not (vault / "10_Projects/new.md").exists()
