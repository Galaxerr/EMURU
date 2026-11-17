"""Owner command seam with pin-verified native board; admission/approval fixtures are synthetic."""

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from project_board_case import SPEC

from emuru.projects import board, continuation
from emuru.projects.scout import encoded
from emuru.projects.spec import spec_markdown
from emuru.vault import mutations

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def continuation_binding(tmp_path, monkeypatch, request):
    native = Path(
        os.environ.get(
            "EMURU_NATIVE_RUNTIME",
            ROOT
            / ".runtime/ticket5-corrected-qualification-20261115/fixture-final/native-runtime",
        )
    )
    site = Path(
        os.environ.get(
            "EMURU_NATIVE_SITE",
            ROOT / ".runtime/coder-task-acceptance-n90rgtfq/hermes-site/site-packages",
        )
    )
    if not native.is_dir() or not site.is_dir():
        pytest.skip("Pinned native runtime/site required for continuation integration")
    monkeypatch.syspath_prepend(str(native))
    monkeypatch.syspath_prepend(str(site))
    kb = board.native_api(ROOT)
    home, vault = tmp_path / "profile", tmp_path / "vault"
    home.mkdir(mode=0o700)
    (vault / "10_Projects").mkdir(parents=True)
    folder = home / "emuru-projects"
    folder.mkdir(mode=0o700)
    supervisor = folder / "supervisor"
    supervisor.mkdir(mode=0o700)
    for path, data in (
        (supervisor / "control.lock", b""),
        (
            folder / "supervisor-enabled.json",
            encoded(
                {
                    "schema": 2,
                    "generation": 0,
                    "head": None,
                    "legacy_generation": 0,
                    "legacy_head": None,
                }
            ),
        ),
    ):
        path.write_bytes(data)
        path.chmod(0o600)
    spec = copy.deepcopy(SPEC)
    spec["tasks"][1]["depends_on"] = ["first"]
    if getattr(request, "param", None) == "three":
        spec["tasks"].append(
            {
                **copy.deepcopy(spec["tasks"][1]),
                "id": "third",
                "description": "Third continuation task",
                "depends_on": ["second"],
            }
        )
    content = spec_markdown(spec)
    note = vault / "10_Projects/Sample.md"
    note.write_text(content)
    preview = {
        "path": "10_Projects/Sample.md",
        "proposal_id": "a" * 32,
        "approval_digest": "b" * 64,
        "after_revision": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
        "current": True,
        "outcome": "applied",
    }
    active = {"request": "c" * 32}
    monkeypatch.setattr(
        mutations, "authenticated_request", lambda *a, **kw: active["request"]
    )
    approved = {preview["proposal_id"]: copy.deepcopy(preview)}

    def approved_project(*args, **kwargs):
        return preview if args[2] == preview["proposal_id"] else approved[args[2]]

    monkeypatch.setattr(mutations, "approved_project", approved_project)
    monkeypatch.setattr(
        mutations,
        "require_authority",
        lambda *a, **kw: {"enabled": True, "roots": ["10_Projects"]},
    )

    def admit(text, request=None):
        approved.setdefault(preview["proposal_id"], copy.deepcopy(preview))
        active["request"] = (
            request or hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
        )
        with mutations.journal(home) as db:
            db.execute(
                "INSERT OR REPLACE INTO owner_requests (id,owner,bot,vault,text_hash) VALUES (?,?,?,?,?)",
                (
                    active["request"],
                    "42",
                    "7",
                    str(vault),
                    hashlib.sha256(text.encode()).hexdigest(),
                ),
            )
            db.commit()
        return active["request"]

    def command(text, *, owner=42, request=None):
        admit(text, request)
        return board.project_command(home, vault, ROOT, text, owner, 7)

    def inspect():
        text = command("/project inspect " + preview["proposal_id"])
        return re.search(r"^Confirm: (.+)$", text, re.MULTILINE)[1]

    return SimpleNamespace(
        home=home,
        vault=vault,
        note=note,
        preview=preview,
        kb=kb,
        command=command,
        inspect=inspect,
        admit=admit,
    )


def snapshot(folder):
    return {
        str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in folder.rglob("*")
        if p.is_file()
    }


def test_continuation_binding_readonly_and_idempotent_native(continuation_binding):
    case = continuation_binding
    text = "/project inspect " + case.preview["proposal_id"]
    case.admit(text)
    before = snapshot(case.home), snapshot(case.vault)
    inspected = board.project_command(case.home, case.vault, ROOT, text, 42, 7)
    assert (snapshot(case.home), snapshot(case.vault)) == before
    bind = re.search(r"^Confirm: (.+)$", inspected, re.MULTILINE)[1]
    original = case.note.read_bytes()
    result = case.command(bind)
    assert "Binding complete" in result
    assert case.command(bind) == result
    assert case.note.read_bytes() == original
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        cards = kb.list_tasks(db, include_archived=True)
        assert len(cards) == 2
        assert {c.status for c in cards} == {"blocked"}
        by_label = {json.loads(c.body)["task"]["id"]: c for c in cards}
        assert kb.parent_ids(db, by_label["second"].id) == [by_label["first"].id]
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )
    assert not list(case.home.rglob("grant.json"))


@pytest.mark.parametrize("checkpoint", ["create", "link"])
def test_continuation_binding_interruption_retry(
    continuation_binding, monkeypatch, checkpoint
):
    case = continuation_binding
    bind = case.inspect()
    name = "create_task" if checkpoint == "create" else "link_tasks"
    original = getattr(case.kb, name)
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("Interrupted after native commit")
        return result

    monkeypatch.setattr(case.kb, name, interrupted)
    with pytest.raises(RuntimeError, match="Interrupted"):
        case.command(bind)
    monkeypatch.setattr(case.kb, name, original)
    assert "Binding complete" in case.command(bind)
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert len(kb.list_tasks(db, include_archived=True)) == 2
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )


def test_continuation_binding_stale_wrong_owner_and_track_refusal(continuation_binding):
    case = continuation_binding
    bind = case.inspect()
    with pytest.raises(ValueError, match="owner"):
        case.command(bind, owner=43)
    with pytest.raises(ValueError, match="explicit /project bind"):
        case.command("/project track " + case.preview["proposal_id"])
    assert not (case.home / "emuru-projects/kanban.db").exists()
    case.note.write_text(case.note.read_text() + "\nHuman edit\n")
    with pytest.raises(ValueError, match="Stale"):
        case.command(bind)
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert kb.list_tasks(db, include_archived=True) == []


@pytest.mark.parametrize(
    "kind", ["binding-intent", "task-binding", "dependency-binding", "binding-complete"]
)
def test_continuation_binding_receipt_interruption(
    continuation_binding, monkeypatch, kind
):
    case = continuation_binding
    bind = case.inspect()
    append = continuation.supervisor._append
    interrupted = False

    def checkpoint(*args, **kwargs):
        nonlocal interrupted
        result = append(*args, **kwargs)
        if args[3]["kind"] == kind and not interrupted:
            interrupted = True
            raise RuntimeError("Interrupted after receipt commit")
        return result

    monkeypatch.setattr(continuation.supervisor, "_append", checkpoint)
    with pytest.raises(RuntimeError, match="receipt commit"):
        case.command(bind)
    monkeypatch.setattr(continuation.supervisor, "_append", append)
    assert "Binding complete" in case.command(bind)
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert len(kb.list_tasks(db, include_archived=True)) == 2
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )


def test_continuation_binding_revision_alias_and_rescope(continuation_binding):
    case = continuation_binding
    case.command(case.inspect())
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        original = {c.id: c.body for c in kb.list_tasks(db, include_archived=True)}
    case.note.write_text(case.note.read_text() + "\nUnrelated human detail\n")
    case.preview["proposal_id"] = "d" * 32
    case.command(case.inspect())
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert {
            c.id: c.body for c in kb.list_tasks(db, include_archived=True)
        } == original
    case.preview["content"] = case.preview["content"].replace(
        "Task first", "Changed first requirement"
    )
    case.preview["proposal_id"] = "e" * 32
    case.preview["after_revision"] = hashlib.sha256(
        case.preview["content"].encode()
    ).hexdigest()
    case.note.write_text(case.preview["content"])
    case.command(case.inspect())
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        current = {c.id: c.body for c in kb.list_tasks(db, include_archived=True)}
        assert len(current) == 4
        assert all(current[key] == body for key, body in original.items())
    records = continuation._records(case.home)
    assert (
        len([r for r in records if r["kind"] == "task-binding" and r["supersedes"]])
        == 2
    )


def test_continuation_binding_legacy_confirmation_preserves_note(continuation_binding):
    case = continuation_binding
    content = re.sub(r" \^task-sample-[a-z]+", "", case.note.read_text())
    case.note.write_text(content)
    original = case.note.read_bytes()
    bind = case.inspect()
    assert "Binding complete" in case.command(bind)
    assert case.note.read_bytes() == original
    assert all(
        r["logical_key"].startswith("legacy-")
        for r in continuation._records(case.home)
        if r["kind"] == "task-binding"
    )


def test_continuation_binding_ambiguous_anchor_no_mutation(continuation_binding):
    case = continuation_binding
    case.note.write_text(
        case.note.read_text() + "\n- [ ] Duplicate ^task-sample-first\n"
    )
    text = "/project inspect " + case.preview["proposal_id"]
    case.admit(text)
    before = snapshot(case.home), snapshot(case.vault)
    with pytest.raises(ValueError, match="Ambiguous"):
        board.project_command(case.home, case.vault, ROOT, text, 42, 7)
    assert (snapshot(case.home), snapshot(case.vault)) == before


@pytest.mark.parametrize(
    "before,after",
    [
        ("expected: Zero exit", "expected: Changed acceptance"),
        ("Planned check: python -m unittest", "Planned check: different-check"),
        ("Validate project tracking", "Changed project goal"),
        ("Target repository: unresolved", "Target repository: other"),
        ("Security restriction: No network", "Security restriction: Network allowed"),
        ("Non-goal: No execution", "Non-goal: Different restriction"),
        ("  - Depends on: first", ""),
    ],
)
def test_continuation_binding_current_scope_edits_refuse_before_writes(
    continuation_binding, before, after
):
    case = continuation_binding
    assert before in case.note.read_text()
    case.note.write_text(case.note.read_text().replace(before, after))
    text = "/project inspect " + case.preview["proposal_id"]
    case.admit(text)
    original = snapshot(case.home), snapshot(case.vault)
    with pytest.raises(ValueError, match="scope changed"):
        board.project_command(case.home, case.vault, ROOT, text, 42, 7)
    assert (snapshot(case.home), snapshot(case.vault)) == original


def test_continuation_binding_successor_only_with_m1_predecessor(continuation_binding):
    case = continuation_binding
    project = board._digest(["42", "7", str(case.vault), case.preview["path"]])
    task = board.spec_tasks(case.preview["content"])[0]
    with board.project_board(case.home, ROOT) as (kb, db):
        predecessor = kb.create_task(
            db,
            title=task["description"],
            body=json.dumps(
                {
                    "schema": 1,
                    "revision": case.preview["after_revision"],
                    "proposal": case.preview["proposal_id"],
                    "approval": case.preview["approval_digest"],
                    "task": task,
                },
                sort_keys=True,
            ),
            tenant=project,
            idempotency_key="emuru:"
            + board._digest([project, case.preview["after_revision"], task["id"]]),
            created_by="emuru-owner",
            initial_status="blocked",
            workspace_kind="scratch",
            project_id="",
            max_retries=0,
        )
    text = case.command("/project inspect " + case.preview["proposal_id"] + " second")
    bind = re.search(r"^Confirm: (.+)$", text, re.MULTILINE)[1]
    assert "Binding complete" in case.command(bind)
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        cards = kb.list_tasks(db, tenant=project, include_archived=True)
        assert len(cards) == 2
        successor = next(c for c in cards if c.id != predecessor)
        assert kb.parent_ids(db, successor.id) == [predecessor]
    assert not any(
        r["kind"] == "task-binding" and r["task"] == predecessor
        for r in continuation._records(case.home)
    )


def test_continuation_binding_orphan_manifest_capacity_preflight(continuation_binding):
    case = continuation_binding
    with board.project_board(case.home, ROOT):
        pass
    storage = case.home / "emuru-projects/continuation-manifests"
    storage.mkdir(mode=0o700)
    total = 0
    for index in range(31):
        raw = encoded({"orphan": index, "padding": "x" * 32700})
        total += len(raw)
        path = storage / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
    assert continuation.manifest_bytes(case.home) == total
    bind = case.inspect()
    case.admit(bind)
    previous = snapshot(storage)
    with pytest.raises(ValueError, match="capacity exhausted"):
        board.project_command(case.home, case.vault, ROOT, bind, 42, 7)
    assert snapshot(storage) == previous
    assert continuation._records(case.home) == []
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert kb.list_tasks(db, include_archived=True) == []


def test_continuation_binding_external_predecessor_body_tamper(continuation_binding):
    case = continuation_binding
    case.command(case.inspect())
    text = case.command("/project inspect " + case.preview["proposal_id"] + " second")
    bind = re.search(r"^Confirm: (.+)$", text, re.MULTILINE)[1]
    with board.project_board(case.home, ROOT) as (kb, db):
        predecessor = next(
            c
            for c in kb.list_tasks(db, include_archived=True)
            if json.loads(c.body)["task"]["id"] == "first"
        )
        body = json.loads(predecessor.body)
        body["task"]["description"] = "Unapproved predecessor"
        db.execute(
            "UPDATE tasks SET body=? WHERE id=?",
            (json.dumps(body, sort_keys=True), predecessor.id),
        )
        db.commit()
    case.admit(bind)
    before = snapshot(case.home / "emuru-projects/supervisor")
    with pytest.raises(ValueError, match="body|binding|receipt"):
        board.project_command(case.home, case.vault, ROOT, bind, 42, 7)
    assert snapshot(case.home / "emuru-projects/supervisor") == before
    with board.project_board_readonly(case.home, ROOT) as (_kb, db):
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("supersedes", "t_deadbeef"),
        ("supersedes", ["invalid"]),
        ("contract_digest", "bad"),
        ("anchor_digest", "f" * 63),
    ],
)
def test_continuation_binding_malformed_record_refuses_write(
    continuation_binding, field, value
):
    case = continuation_binding
    case.command(case.inspect())
    records = continuation._records(case.home)
    record = copy.deepcopy(next(r for r in records if r["kind"] == "task-binding"))
    record[field] = value
    for common in ("schema", "generation", "previous"):
        del record[common]
    before = snapshot(case.home / "emuru-projects")
    with continuation.supervisor._locked(case.home, write=True) as (
        parent,
        size,
        marker,
    ):
        current, size = continuation.supervisor._records(parent, size, marker)
        with pytest.raises(ValueError, match="digest|binding"):
            continuation.supervisor._append(
                case.home / "emuru-projects/supervisor", current, size, record
            )
    assert snapshot(case.home / "emuru-projects") == before


@pytest.mark.parametrize("checkpoint", [1, 2, 4, 5])
def test_continuation_binding_marker_interruption_explicit_retry(
    continuation_binding, monkeypatch, checkpoint
):
    from emuru.models import gateway

    case = continuation_binding
    bind = case.inspect()
    write = gateway.write_private
    count = 0

    def interrupted(path, *args, **kwargs):
        nonlocal count
        if Path(path).name == "supervisor-enabled.json":
            count += 1
            if count == checkpoint:
                raise OSError("Interrupted marker publication")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(gateway, "write_private", interrupted)
    with pytest.raises(OSError, match="marker publication"):
        case.command(bind)
    monkeypatch.setattr(gateway, "write_private", write)
    assert "Binding complete" in case.command(bind)
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        assert len(kb.list_tasks(db, include_archived=True)) == 2
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )
    assert (
        len(
            [
                r
                for r in continuation._records(case.home)
                if r["kind"] == "binding-complete"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("changed", ["bot", "vault"])
def test_continuation_binding_wrong_bot_or_vault(continuation_binding, changed):
    case = continuation_binding
    text = "/project inspect " + case.preview["proposal_id"]
    case.admit(text)
    original = snapshot(case.home), snapshot(case.vault)
    vault = case.vault.with_name("wrong-vault") if changed == "vault" else case.vault
    bot = 8 if changed == "bot" else 7
    with pytest.raises(ValueError, match="owner"):
        board.project_command(case.home, vault, ROOT, text, 42, bot)
    assert (snapshot(case.home), snapshot(case.vault)) == original


def test_continuation_binding_concurrent_identical_request(continuation_binding):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    case = continuation_binding
    bind = case.inspect()
    case.admit(bind)
    barrier = Barrier(2)

    def execute():
        barrier.wait(timeout=10)
        try:
            return board.project_command(case.home, case.vault, ROOT, bind, 42, 7)
        except BlockingIOError:
            # Dispatcher intentionally refuses overlap; an explicit retry may reopen its receipt.
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: execute(), range(2)))
    completed = [result for result in results if result is not None]
    assert completed and all(result == completed[0] for result in completed)
    assert (
        board.project_command(case.home, case.vault, ROOT, bind, 42, 7) == completed[0]
    )
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        cards = kb.list_tasks(db, include_archived=True)
        assert len(cards) == len({card.idempotency_key for card in cards}) == 2
        assert (
            db.execute(
                "SELECT count(*) FROM task_events WHERE kind='linked'"
            ).fetchone()[0]
            == 1
        )
    assert (
        len(
            [
                r
                for r in continuation._records(case.home)
                if r["kind"] == "binding-complete"
            ]
        )
        == 1
    )
