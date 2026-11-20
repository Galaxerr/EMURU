"""Disposable pinned-native M2 acceptance; no provider, Telegram polling or real vault."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emuru.projects import board
from emuru.projects.spec import spec_markdown
from emuru.telegram.queue import TelegramQueue, load_telegram_settings
from emuru.vault import mutations

SPEC = {
    "spec_id": "sample",
    "goal": "Validate project tracking",
    "repository": None,
    "non_goals": ["No execution"],
    "constraints": [],
    "tasks": [
        {
            "id": name,
            "description": "Task " + name,
            "acceptance": [
                {"command": "python -m unittest", "expected_result": "Zero exit"}
            ],
        }
        for name in ("first", "second")
    ],
    "security_restrictions": ["No network"],
    "unresolved_decisions": [],
}


def refused(call, match):
    try:
        call()
    except (ValueError, OSError) as error:
        assert match in str(error), str(error)
    else:
        raise AssertionError("Expected refusal: " + match)


def run(folder, reopen=False):
    from hermes_cli import kanban_db_dispatch as dispatcher
    from tools import kanban_tools

    # Production also applies through EMURU's interpreter, not Hermes'.
    python = Path("/opt/emuru/.venv/bin/python")
    if not python.is_file():
        python = ROOT / ".venv/bin/python"
    home, vault = folder / "profile", folder / "vault"
    if not reopen:
        home.mkdir(mode=0o700, parents=True)
        (vault / "10_Projects").mkdir(parents=True)
        (vault / "99_System").mkdir()
        (vault / "30_Resources").mkdir()
        (vault / "30_Resources/broken.md").write_text("---\ntitle: [broken\n---\n")
        (home / "vault-target.json").write_text(
            json.dumps({"mode": "real", "vault_path": str(vault)})
        )
        (home / "mutation-policy.json").write_text(
            json.dumps(
                {"enabled": True, "roots": ["10_Projects"], "vault_path": str(vault)}
            )
        )
        for name in ("vault-target.json", "mutation-policy.json"):
            (home / name).chmod(0o600)
        (home / "config.yaml").write_text(
            json.dumps(
                {
                    "kanban": {"dispatch_profiles": []},
                    "platform_toolsets": {
                        "telegram": ["mcp-vault"],
                        "cli": ["mcp-vault"],
                    },
                    "agent": {"disabled_toolsets": ["kanban", "terminal", "file"]},
                }
            )
        )
        (home / "config.yaml").chmod(0o600)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    os.environ.pop("HERMES_KANBAN_TASK", None)
    assert not kanban_tools._check_kanban_mode()
    assert not kanban_tools._check_kanban_orchestrator_mode()
    assert dispatcher._dispatch_profile_allowlist(lambda name: name) == frozenset()
    policy = load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json")
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3", policy, 42, clock=lambda: 100000
    )
    uid = 100 if reopen else 0

    def action(text):
        nonlocal uid
        uid += 1
        queue.stage(
            7,
            [
                {
                    "update_id": uid,
                    "message": {
                        "message_id": uid,
                        "date": 100000,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        claim = queue.claim(7, "emuru", session_key=lambda payload: "owner")
        assert claim and claim["update_id"] == uid
        request, _ = mutations.begin_request(home, vault, 42, 7, uid, text, fresh=True)
        mutations.activate_request(home, request, queue.path, 7, uid)
        return request, claim

    def command(text, owner=42, bot=7):
        request, claim = action(text)
        try:
            queue.mark_tools_started(7, uid)
            return board.project_command(home, vault, ROOT, text, owner, bot)
        finally:
            mutations.finish_request(home, request)
            queue.finish(claim["sequence"], True)

    def proposal(content, *, replacement=None):
        request, claim = action("Prepare spec")
        try:
            kwargs = (
                {"content": content}
                if replacement is None
                else {
                    "expected_revision": mutations.hashlib.sha256(
                        (vault / "10_Projects/Sample.md").read_bytes()
                    ).hexdigest(),
                    "anchor": content,
                    "replacement": replacement,
                }
            )
            return mutations.propose_note(
                home, vault, "10_Projects/Sample.md", authenticated_id=request, **kwargs
            )
        finally:
            mutations.finish_request(home, request)
            queue.finish(claim["sequence"], True)

    def apply(preview):
        request, claim = action("/apply " + preview["proposal_id"] + " writers-paused")
        try:
            mutations.record_preview(
                home, preview["proposal_id"], preview["approval_digest"], complete=True
            )
            applied = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from emuru.vault.mutations import apply_proposal; print(json.dumps(apply_proposal(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5],writers_paused=True,owner=42,bot=7,application_request=sys.argv[6])))",
                    str(ROOT / "src"),
                    str(home),
                    str(vault),
                    preview["proposal_id"],
                    preview["approval_digest"],
                    request,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            result = json.loads(applied.stdout)
            assert result["outcome"] in {"applied", "index_pending"}
        finally:
            mutations.finish_request(home, request)
            queue.finish(claim["sequence"], True)

    saved = folder / "acceptance.json"
    if reopen:
        state = json.loads(saved.read_text())
        result = command("/project track " + state["proposal"])
        assert result == state["status"], result
        queue.close()
        print("Pinned project board reopen: PASS")
        return
    content = (
        "---\ntitle: Sample\ntype: project\nsummary: Tracking test\n---\n"
        + spec_markdown(SPEC)
    )
    malformed_content = "run-check; expected: prints ; expected: OK".join(
        content.rsplit("python -m unittest; expected: Zero exit", 1)
    )
    preview = proposal(malformed_content)
    refused(
        lambda: command("/project track " + preview["proposal_id"]), "applied approved"
    )
    apply(preview)
    refused(
        lambda: command("/project track " + preview["proposal_id"]),
        "planned-check format",
    )
    with board.project_board(home, ROOT) as (kb, db):
        assert kb.list_tasks(db, include_archived=True) == []
    preview = proposal(malformed_content, replacement=content)
    identity = preview["proposal_id"]
    refused(lambda: command("/project track " + identity), "applied approved")
    apply(preview)
    refused(
        lambda: command("/project track " + identity, owner=43), "authenticated owner"
    )
    retrieved = subprocess.run(
        [
            str(python),
            "-c",
            "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from emuru.vault.indexer import vault_search; print(json.dumps(vault_search(Path(sys.argv[2]),'tracking',8)))",
            str(ROOT / "src"),
            str(vault),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    retrieval = json.loads(retrieved.stdout)
    assert retrieval["count"] == 1 and retrieval["incomplete"] is True
    assert retrieval["failed_paths"] == ["30_Resources/broken.md"]
    refused(lambda: command("/project track " + identity, bot=8), "authenticated owner")
    kb = board.native_api(ROOT)
    create = kb.create_task
    attempts = 0

    def interrupted_create(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("Synthetic stop after one committed card")
        return create(*args, **kwargs)

    kb.create_task = interrupted_create
    try:
        try:
            command("/project track " + identity)
        except OSError:
            pass
        else:
            raise AssertionError("Expected interrupted task set")
    finally:
        kb.create_task = create
    assert "Partial task set" in command("/project status " + identity)
    status = command("/project track " + identity)
    assert status.count("native blocked") == 2
    assert command("/project track " + identity) == status
    assert command("/project status " + identity) == status
    before = (vault / "10_Projects/Sample.md").read_bytes()
    with board.project_board(home, ROOT) as (kb, db):
        cards = kb.list_tasks(db, include_archived=True)
        assert len(cards) == 2 and all(card.assignee is None for card in cards)
        assert all(card.current_run_id is None for card in cards)
        assert kb.get_task(db, cards[0].id).id == cards[0].id
        attachment = kb.store_attachment_bytes(
            db,
            cards[0].id,
            "check.txt",
            b"Synthetic attachment",
            content_type="text/plain",
        )
        assert kb.get_attachment(db, attachment).size == len(b"Synthetic attachment")
        assert len(kb.list_attachments(db, cards[0].id)) == 1
        kb.link_tasks(db, cards[0].id, cards[1].id)
        assert kb.parent_ids(db, cards[1].id) == [cards[0].id]
        refused(lambda: kb.link_tasks(db, cards[1].id, cards[0].id), "cycle")
        spawned = []
        dispatcher.dispatch_once(
            db,
            spawn_fn=lambda *a, **kw: spawned.append(a),
            default_assignee="default",
            reconcile_orphans=False,
        )
        assert not spawned
        assert all(card.status == "blocked" for card in kb.list_tasks(db))
        # Even an accidentally ready, assigned card must fail the empty allowlist.
        extra = kb.create_task(
            db,
            title="Dispatch negative",
            assignee="default",
            workspace_kind="scratch",
            project_id="",
        )
        assert kb.get_task(db, extra).status == "ready"
        dispatcher.dispatch_once(
            db, spawn_fn=lambda *a, **kw: spawned.append(a), reconcile_orphans=False
        )
        assert not spawned and kb.get_task(db, extra).status != "running"
        kb.archive_task(db, extra)
        kb.archive_task(db, cards[0].id)
    archived = command("/project track " + identity)
    assert "native archived" in archived
    assert "parents: " + cards[0].id in archived
    from emuru.projects.scout import digest
    from emuru.projects.scout_board import claim as scout_claim
    from emuru.projects.scout_board import finish as scout_finish

    report = {"repository": "synthetic", "baseline": "e" * 40, "files": []}
    grant = {
        "repository_id": "a" * 64,
        "source_task": cards[1].id,
        "repository": "synthetic",
        "baseline": "e" * 40,
        "files": ["app.py"],
        "timeout_seconds": 10,
        "report_sha256": digest(report),
    }
    scouting = scout_claim(home, ROOT, grant)
    scout_finish(
        home, ROOT, {"task": scouting["task"], "run": scouting["run"], "report": report}
    )
    visible = command("/project status " + identity)
    assert "read-only scout done" in visible and digest(report) in visible
    assert "baseline: " + "e" * 40 in visible
    assert command("/project track " + identity) == visible
    (vault / "10_Projects/Sample.md").write_bytes(before + b"\nHuman edit\n")
    refused(lambda: command("/project track " + identity), "stale")
    assert "Note changed" in command("/project status " + identity)
    (vault / "10_Projects/Sample.md").write_bytes(before)
    changed = proposal("Task second", replacement="Task revised")
    apply(changed)
    refused(
        lambda: command("/project track " + changed["proposal_id"]), "reconciliation"
    )
    assert "another spec revision" in command(
        "/project status " + changed["proposal_id"]
    )
    # Restore synthetic bytes only, so the prior receipt remains usable for reopen.
    (vault / "10_Projects/Sample.md").write_bytes(before)
    with board.project_board(home, ROOT) as (kb, db):
        existing_cards = kb.list_tasks(db, include_archived=True)
    malformed = proposal(content, replacement=malformed_content)
    apply(malformed)
    refused(
        lambda: command("/project track " + malformed["proposal_id"]),
        "planned-check format",
    )
    with board.project_board(home, ROOT) as (kb, db):
        assert kb.list_tasks(db, include_archived=True) == existing_cards
    (vault / "10_Projects/Sample.md").write_bytes(before)
    target = home / "vault-target.json"
    target.write_text('{"mode":"synthetic"}')
    refused(lambda: command("/project track " + identity), "selected vault")
    target.write_text(json.dumps({"mode": "real", "vault_path": str(vault)}))
    target.chmod(0o600)
    sentinel = folder / "outside.txt"
    sentinel.write_text("unchanged")
    for name in ("kanban.db-journal", "kanban.db.init.lock"):
        link = home / "emuru-projects" / name
        if link.exists():
            link.unlink()
        link.symlink_to(sentinel)
        refused(lambda: command("/project status " + identity), "")
        assert sentinel.read_text() == "unchanged"
        link.unlink()
    assert (vault / "10_Projects/Sample.md").read_bytes() == before
    saved.write_text(
        json.dumps(
            {"proposal": identity, "status": command("/project track " + identity)}
        )
    )
    queue.close()
    print(
        "Pinned project board tracking, authorization, replay, stale revision, archive and dispatch negatives: PASS"
    )


if __name__ == "__main__":
    run(Path(sys.argv[2]).absolute(), reopen=sys.argv[1] == "reopen")
