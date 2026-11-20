"""Receipt-backed model tool contracts; synthetic state only, no model acceptance."""

import json
import sqlite3
from pathlib import Path

import pytest
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram_helpers import update
from vault_helpers import create_vault

from emuru.hermes import profile
from emuru.telegram.queue import TelegramQueue, load_telegram_settings
from emuru.vault import mutations as m
from emuru.vault.mcp import create_mcp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scope(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    vault = create_vault(tmp_path)
    for name, data in {
        "vault-target.json": {"mode": "real", "vault_path": str(vault)},
        "mutation-policy.json": {
            "enabled": True,
            "roots": ["10_Projects"],
            "vault_path": str(vault),
        },
    }.items():
        path = home / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        42,
        clock=lambda: 100000,
    )
    queue.stage(7, [update(1, "create")])
    queue.claim(7)
    request, _ = m.begin_request(home, vault, 42, 7, 1, "create")
    m.activate_request(home, request, queue.path, 7, 1)
    yield home, vault, queue, request
    queue.close()


def proposal(scope, name="new", *, owner=42, bot=7, vault=None):
    home, selected, _, _ = scope
    vault = selected if vault is None else vault
    request, _ = m.begin_request(home, vault, owner, bot, name, name)
    return m.propose_note(
        home,
        selected,
        f"10_Projects/{name}.md",
        content=f"# {name}",
        authenticated_id=request,
    )


def apply(scope, preview):
    home, vault, _, request = scope
    m.record_preview(
        home, preview["proposal_id"], preview["approval_digest"], complete=True
    )
    return m.apply_proposal(
        home,
        vault,
        preview["proposal_id"],
        preview["approval_digest"],
        writers_paused=True,
        owner=42,
        bot=7,
        application_request=request,
    )


def snapshot(*roots):
    return {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
@pytest.mark.parametrize("terminal", ["applied", "cancelled"])
async def test_same_conversation_and_restart_override_historical_creation(
    scope, terminal
):
    home, vault, _, _ = scope
    server = create_mcp(vault, proposal_home=home)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        tool = tools["vault_proposal_status"]
        assert tool.annotations.read_only_hint is True
        assert set(tool.input_schema["properties"]) == {"proposal_id", "latest"}
        assert "historical vault_propose results cannot override" in tool.description
        assert "vault_apply" not in tools and "vault_cancel" not in tools
        assert profile.vault_tools(home) == [
            *profile.TOOLS,
            "vault_proposal_status",
            "vault_propose",
        ]
        created = await client.call_tool(
            "vault_propose",
            {"path": "10_Projects/conversation.md", "content": "# Project"},
        )
        assert not created.is_error
        historical = created.structured_content
        identity = historical["proposal_id"]
        pending = await client.call_tool("vault_proposal_status", {})
        assert pending.structured_content["proposals"][0]["state"] == "pending"
        if terminal == "applied":
            assert apply(scope, historical)["outcome"] == terminal
        else:
            assert m.cancel_proposal(home, identity)["outcome"] == terminal
        before = snapshot(home, vault)
        for args in ({"proposal_id": identity}, {"latest": True}):
            fresh = await client.call_tool("vault_proposal_status", args)
            assert not fresh.is_error
            record = fresh.structured_content["proposals"][0]
            assert record == {
                "proposal_id": identity,
                "path": historical["path"],
                "state": terminal,
                "outcome": terminal,
                "receipt_confirmed": True,
            }
            assert terminal in fresh.content[0].text
        assert (
            await client.call_tool("vault_proposal_status", {})
        ).structured_content == {"proposals": []}
        assert snapshot(home, vault) == before
        assert historical["outcome"] == "proposed"
    # A new server/client reads durable receipts, not the previous tool history.
    async with Client(create_mcp(vault, proposal_home=home)) as restarted:
        fresh = await restarted.call_tool(
            "vault_proposal_status", {"proposal_id": identity}
        )
        assert fresh.structured_content["proposals"][0]["state"] == terminal
    with m.journal(home) as db:
        row = db.execute(
            "SELECT outcome FROM proposals WHERE id=?", (identity,)
        ).fetchone()[0]
        assert row == ("proposed" if terminal == "applied" else "cancelled")


@pytest.mark.parametrize(
    "outcome,state",
    [
        ("prepared", "prepared"),
        ("index_pending", "index_pending"),
        ("quarantined", "uncertain"),
        ("not_applied", "not_applied"),
        ("uncertain", "uncertain"),
    ],
)
def test_receipt_states_excluded_from_pending_without_recovery(scope, outcome, state):
    home, vault, _, _ = scope
    preview = proposal(scope)
    with m.journal(home) as db:
        m._record_receipt(db, m._proposal(db, preview["proposal_id"]), outcome)
    before = snapshot(home, vault)
    result = m.proposal_status(home, vault, preview["proposal_id"])["proposals"][0]
    assert result["state"] == state and result["receipt_confirmed"]
    assert m.proposal_status(home, vault) == {"proposals": []}
    assert snapshot(home, vault) == before
    assert not (vault / preview["path"]).exists()


def test_expired_receipt_and_pending_list(scope):
    home, vault, _, _ = scope
    expired = proposal(scope, "expired")
    with m.journal(home) as db:
        created = db.execute("SELECT created_at FROM proposal_lifecycle").fetchone()[0]
    assert m.expire_proposals(home, vault, now=created + 86401) == [
        expired["proposal_id"]
    ]
    pending = proposal(scope, "pending")
    before = snapshot(home, vault)
    assert (
        m.proposal_status(home, vault, expired["proposal_id"])["proposals"][0]["state"]
        == "expired"
    )
    assert [r["proposal_id"] for r in m.proposal_status(home, vault)["proposals"]] == [
        pending["proposal_id"]
    ]
    assert snapshot(home, vault) == before


@pytest.mark.parametrize("owner,bot", [(99, 7), (42, 8)])
def test_cross_owner_and_bot_isolation(scope, owner, bot):
    home, vault, _, _ = scope
    foreign = proposal(scope, "foreign", owner=owner, bot=bot)
    before = snapshot(home, vault)
    with pytest.raises(ValueError, match="binding"):
        m.proposal_status(home, vault, foreign["proposal_id"])
    assert m.proposal_status(home, vault) == {"proposals": []}
    assert m.proposal_status(home, vault, latest=True) == {"proposals": []}
    assert snapshot(home, vault) == before


def test_cross_vault_and_unknown_id_fail_closed(scope, tmp_path):
    home, vault, _, _ = scope
    preview = proposal(scope)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="another vault"):
        m.proposal_status(home, other, preview["proposal_id"])
    with pytest.raises(ValueError, match="binding"):
        m.proposal_status(home, vault, "unknown")
    # Even a mismatched persisted proposal vault cannot leak through its request binding.
    with m.journal(home) as db:
        db.execute("UPDATE proposals SET vault=?", (str(other),))
        db.commit()
    with pytest.raises(ValueError, match="binding"):
        m.proposal_status(home, vault, preview["proposal_id"])
    assert m.proposal_status(home, vault) == {"proposals": []}


def test_missing_and_finished_claims_do_not_initialize_or_mutate(scope):
    home, vault, queue, request = scope
    before = snapshot(home, vault)
    with pytest.raises(ValueError, match="Choose"):
        m.proposal_status(home, vault, "id", latest=True)
    assert snapshot(home, vault) == before
    m.finish_request(home, request)
    before = snapshot(home, vault)
    with pytest.raises(ValueError, match="active authenticated"):
        m.proposal_status(home, vault)
    assert snapshot(home, vault) == before
    m.activate_request(home, request, queue.path, 7, 1)
    queue.finish(1, True)
    with pytest.raises(ValueError, match="invalid"):
        m.proposal_status(home, vault)
    missing = home / "absent.sqlite"
    before = snapshot(home, vault)
    with pytest.raises(FileNotFoundError), m._read_database(missing):
        pass
    assert snapshot(home, vault) == before


def test_read_only_snapshot_includes_wal_and_denies_sql_writes(scope):
    home, vault, _, _ = scope
    preview = proposal(scope)
    path = home / "emuru-mutations/mutations.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO receipts VALUES(?, 'cancelled', ?, ?)",
            (
                preview["proposal_id"],
                preview["before_revision"],
                preview["after_revision"],
            ),
        )
        writer.commit()
        before = snapshot(home, vault)
        assert (
            m.proposal_status(home, vault, preview["proposal_id"])["proposals"][0][
                "state"
            ]
            == "cancelled"
        )
        with (
            m._read_database(path) as db,
            pytest.raises(sqlite3.OperationalError, match="readonly"),
        ):
            db.execute("DELETE FROM receipts")
        assert snapshot(home, vault) == before


def test_soul_requires_fresh_lookup_and_allows_receipt_confirmed_application():
    soul = (ROOT / "agents/emuru/SOUL.md").read_text()
    assert (
        "Before making claims about current proposal status, call vault_proposal_status freshly."
        in soul
    )
    assert "they cannot override fresh authoritative status" in soul
    assert "Claim application only after a fresh authoritative lifecycle lookup" in soul
    assert "Never claim that a proposal was applied." not in soul


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
@pytest.mark.parametrize("terminal", ["applied", "cancelled"])
async def test_stdio_process_restart_and_authorization_errors(scope, terminal):
    from test_vault_proposal_mcp import command

    home, vault, queue, request = scope
    startup = command(home, vault, "mutation-serve")
    params = StdioServerParameters(command=startup[0], args=startup[1:], cwd=str(ROOT))
    for restart in (False, True):
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            if not restart:
                created = await client.call_tool(
                    "vault_propose",
                    {"path": "10_Projects/restart.md", "content": "# Restart"},
                )
                assert not created.is_error
                historical = created.structured_content
                if terminal == "applied":
                    assert apply(scope, historical)["outcome"] == terminal
                else:
                    m.cancel_proposal(home, historical["proposal_id"])
            before = snapshot(home, vault)
            fresh = await client.call_tool(
                "vault_proposal_status", {"proposal_id": historical["proposal_id"]}
            )
            assert not fresh.is_error
            assert fresh.structured_content["proposals"][0]["state"] == terminal
            assert historical["outcome"] == "proposed"
            assert (
                await client.call_tool(
                    "vault_proposal_status", {"proposal_id": "unknown"}
                )
            ).is_error
            assert snapshot(home, vault) == before
            if restart:
                m.finish_request(home, request)
                queue.finish(1, True)
                before = snapshot(home, vault)
                assert (await client.call_tool("vault_proposal_status", {})).is_error
                assert snapshot(home, vault) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_snapshot_refuses_symlinks(scope, tmp_path, suffix):
    home, vault, _, _ = scope
    proposal(scope)
    source = home / "emuru-mutations/mutations.sqlite"
    target = tmp_path / "private.sqlite"
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    candidate = Path(str(source) + suffix)
    if candidate.exists():
        candidate.unlink()
    candidate.symlink_to(target)
    before = snapshot(home, vault)
    with pytest.raises(OSError), m._read_database(source):
        pass
    assert snapshot(home, vault) == before


def test_snapshot_refuses_unsafe_permissions_and_recovery(scope):
    home, vault, _, _ = scope
    proposal(scope)
    source = home / "emuru-mutations/mutations.sqlite"
    source.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"), m._read_database(source):
        pass
    source.chmod(0o600)
    journal = Path(str(source) + "-journal")
    journal.write_bytes(b"recovery needed")
    journal.chmod(0o600)
    before = snapshot(home, vault)
    with pytest.raises(ValueError, match="recovery required"), m._read_database(source):
        pass
    assert snapshot(home, vault) == before
