"""Stable proposal revisions and durable, content-shedding expiry."""

import json

import pytest
from vault_helpers import create_vault

from emuru.vault import mutations as m


def setup_proposal(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text(json.dumps({"enabled": False, "roots": ["10_Projects"]}))
    policy.chmod(0o600)
    vault = create_vault(tmp_path)
    request, _ = m.begin_request(home, vault, 42, 7, 1, "create project")
    preview = m.propose_note(
        home,
        vault,
        "10_Projects/new.md",
        content="# Original",
        authenticated_id=request,
    )
    m.record_preview(
        home, preview["proposal_id"], preview["approval_digest"], complete=True
    )
    return home, vault, preview


def test_revision_retains_id_invalidates_approval_and_preserves_failed_draft(tmp_path):
    home, vault, original = setup_proposal(tmp_path)
    identity = original["proposal_id"]
    request, _ = m.begin_request(home, vault, 42, 7, 2, "edit goal")
    with pytest.raises(ValueError, match="owner"):
        m.begin_revision(home, vault, request, original, 99, 7)
    m.begin_revision(home, vault, request, original, 42, 7)
    with pytest.raises(ValueError, match="empty"):
        m.propose_note(
            home, vault, original["path"], content="", authenticated_id=request
        )
    assert m.owner_preview(home, vault, identity, 42, 7) == original
    revised = m.propose_note(
        home, vault, original["path"], content="# Revised", authenticated_id=request
    )
    assert revised["proposal_id"] == identity
    assert (
        revised["content"] == "# Revised"
        and revised["approval_digest"] != original["approval_digest"]
    )
    assert m.request_previews(home, request) == [revised]
    with m.journal(home) as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
        assert db.execute("SELECT * FROM preview_deliveries").fetchall() == []
    with pytest.raises(ValueError):
        m.apply_proposal(
            home,
            vault,
            identity,
            original["approval_digest"],
            writers_paused=True,
            owner=42,
            bot=7,
        )
    with pytest.raises(ValueError, match="changed"):
        m.cancel_proposal(home, identity, approval_digest=original["approval_digest"])
    request, _ = m.begin_request(home, vault, 42, 7, 3, "restore first draft")
    m.begin_revision(home, vault, request, revised, 42, 7)
    restored = m.propose_note(
        home,
        vault,
        original["path"],
        content=original["content"],
        authenticated_id=request,
    )
    assert restored["approval_digest"] not in {
        original["approval_digest"],
        revised["approval_digest"],
    }
    m.record_preview(home, identity, restored["approval_digest"], complete=True)
    assert (
        m.apply_proposal(
            home,
            vault,
            identity,
            restored["approval_digest"],
            writers_paused=True,
            owner=42,
            bot=7,
            application_request=request,
        )["outcome"]
        == "applied"
    )
    assert (vault / original["path"]).read_text() == original["content"]


def test_expiry_boundary_tombstone_and_replay_after_reopen(tmp_path):
    home, vault, preview = setup_proposal(tmp_path)
    identity = preview["proposal_id"]
    with m.journal(home) as db:
        created = db.execute("SELECT created_at FROM proposal_lifecycle").fetchone()[0]
    assert m.expire_proposals(home, vault, now=created + 86399) == []
    assert m.expire_proposals(home, vault, now=created + 86400) == [identity]
    expired = m.owner_preview(home, vault, identity, 42, 7)
    assert expired["outcome"] == "expired" and expired["content"] == ""
    assert m.expire_proposals(home, vault, now=created + 172800) == []
    request, repeated = m.begin_request(home, vault, 42, 7, 2, "create project")
    assert repeated and m.request_previews(home, request) == [expired]
    with pytest.raises(ValueError):
        m.apply_proposal(
            home,
            vault,
            identity,
            preview["approval_digest"],
            writers_paused=True,
            owner=42,
            bot=7,
        )
    assert not (vault / preview["path"]).exists()


def test_followup_stops_expiry_but_foreign_or_original_update_does_not(tmp_path):
    home, vault, preview = setup_proposal(tmp_path)
    with m.journal(home) as db:
        created = db.execute("SELECT created_at FROM proposal_lifecycle").fetchone()[0]
    m.note_owner_activity(home, vault, 99, 7, 2)
    m.note_owner_activity(home, vault, 42, 8, 2)
    m.note_owner_activity(home, vault, 42, 7, 1)
    with m.journal(home) as db:
        assert (
            db.execute("SELECT interacted FROM proposal_lifecycle").fetchone()[0] == 0
        )
    m.note_owner_activity(home, vault, 42, 7, 2)
    assert m.expire_proposals(home, vault, now=created + 86400) == []
    assert (
        m.owner_preview(home, vault, preview["proposal_id"], 42, 7)["outcome"]
        == "proposed"
    )


@pytest.mark.parametrize(
    "outcome", ["prepared", "index_pending", "applied", "cancelled"]
)
def test_expiry_preserves_recovery_and_terminal_records(tmp_path, outcome):
    home, vault, preview = setup_proposal(tmp_path)
    with m.journal(home) as db:
        created = db.execute("SELECT created_at FROM proposal_lifecycle").fetchone()[0]
        db.execute(
            "INSERT INTO receipts VALUES(?,?,?,?)",
            (
                preview["proposal_id"],
                outcome,
                preview["before_revision"],
                preview["after_revision"],
            ),
        )
        db.commit()
    assert m.expire_proposals(home, vault, now=created + 172800) == []
    assert (
        m.owner_preview(home, vault, preview["proposal_id"], 42, 7)["content"]
        == preview["content"]
    )


def test_late_activity_and_direct_apply_cannot_revive_expired_draft(tmp_path):
    import time

    home, vault, preview = setup_proposal(tmp_path)
    with m.journal(home) as db:
        db.execute("UPDATE proposal_lifecycle SET created_at=?", (time.time() - 86401,))
        db.commit()
    m.note_owner_activity(home, vault, 42, 7, 2)
    with pytest.raises(ValueError):
        m.apply_proposal(
            home,
            vault,
            preview["proposal_id"],
            preview["approval_digest"],
            writers_paused=True,
            owner=42,
            bot=7,
        )
    assert (
        m.owner_preview(home, vault, preview["proposal_id"], 42, 7)["outcome"]
        == "expired"
    )
    assert not (vault / preview["path"]).exists()
