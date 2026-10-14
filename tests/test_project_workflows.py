"""Durable workflow identities and evidence-only synthetic daily review."""

import copy
import hashlib
import json
import multiprocessing
import runpy
from pathlib import Path

import pytest
from vault_helpers import create_vault

from emuru.vault import indexer, mutations

ROOT = Path(__file__).resolve().parents[1]
QUALIFY = runpy.run_path(str(ROOT / "scripts/hermes/qualify.py"))


def setup(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    return home, create_vault(tmp_path)


def test_pending_exact_path_and_owner_identity_reuse_one_proposal(tmp_path):
    home, vault = setup(tmp_path)
    fields = {"path": "10_Projects/new.md", "content": "# New\n"}
    first = mutations.propose_note(home, vault, **fields)
    assert mutations.propose_note(home, vault, **fields) == first
    assert (
        mutations.propose_note(home, vault, **fields, request_id="owner:create:1")
        == first
    )
    assert (
        mutations.propose_note(home, vault, **fields, request_id="owner:create:1")
        == first
    )
    with mutations.journal(home) as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    assert not (vault / fields["path"]).exists()


def test_repeated_task_and_result_requests_never_append_twice(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/project.md"
    before = "- [ ] Task — status: todo ^task-id\n\n## Results\n"
    target.write_text(before)
    fields = {
        "path": "10_Projects/project.md",
        "expected_revision": hashlib.sha256(before.encode()).hexdigest(),
        "anchor": "- [ ] Task — status: todo ^task-id",
        "replacement": "- [x] Task — status: done ^task-id",
        "request_id": "owner:task:1",
    }
    task = mutations.propose_note(home, vault, **fields)
    mutations.apply_proposal(
        home, vault, task["proposal_id"], task["approval_digest"], writers_paused=True
    )
    current = target.read_bytes()
    assert mutations.propose_note(home, vault, **fields)["outcome"] == "applied"
    assert target.read_bytes() == current
    result_fields = {
        "path": fields["path"],
        "expected_revision": hashlib.sha256(current).hexdigest(),
        "anchor": "## Results\n",
        "replacement": "## Results\n- Draft recorded. ^result-id\n",
        "request_id": "owner:result:1",
    }
    result = mutations.propose_note(home, vault, **result_fields)
    mutations.apply_proposal(
        home,
        vault,
        result["proposal_id"],
        result["approval_digest"],
        writers_paused=True,
    )
    modified = target.stat().st_mtime_ns
    assert mutations.propose_note(home, vault, **result_fields)["outcome"] == "applied"
    assert target.stat().st_mtime_ns == modified
    assert target.read_text().count("Draft recorded.") == 1
    assert target.read_text().count("^task-id") == 1
    with pytest.raises(ValueError, match="different proposal inputs"):
        mutations.propose_note(home, vault, **{**fields, "replacement": "different"})


@pytest.mark.parametrize("identity", ["", " ", 7, "x" * 257, "bad\0id"])
def test_invalid_identity_fails_closed(tmp_path, identity):
    home, vault = setup(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        mutations.propose_note(
            home, vault, "10_Projects/new.md", content="new", request_id=identity
        )
    assert not (vault / "10_Projects/new.md").exists()


def test_identity_scope_and_policy_are_rechecked(tmp_path):
    home, vault = setup(tmp_path)
    fields = {"path": "10_Projects/new.md", "content": "new", "request_id": "owner:1"}
    first = mutations.propose_note(home, vault, **fields)
    other = create_vault(tmp_path / "other")
    assert (
        mutations.propose_note(home, other, **fields)["proposal_id"]
        != first["proposal_id"]
    )
    (home / "mutation-policy.json").write_text('{"enabled":false,"roots":[]}')
    with pytest.raises(ValueError, match="restricted"):
        mutations.propose_note(home, vault, **fields)


def test_existing_path_returns_human_note_without_creating_proposal(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/project.md"
    original = b"Human note\r\n"
    target.write_bytes(original)
    fields = {
        "path": "10_Projects/project.md",
        "content": "different proposed note",
        "request_id": "owner:existing",
    }
    existing = mutations.propose_note(home, vault, **fields)
    assert existing["outcome"] == "exists" and existing["content"].encode() == original
    assert mutations.propose_note(home, vault, **fields) == existing
    assert target.read_bytes() == original
    with mutations.journal(home) as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0


def _request(home, vault, fields, gate, results):
    gate.wait(10)
    results.put(mutations.propose_note(home, vault, **fields))


@pytest.mark.parametrize("published", [False, True])
def test_interrupted_promotion_retains_source_and_request_without_replay(
    tmp_path, monkeypatch, published
):
    home, vault = setup(tmp_path)
    source = vault / "00_Inbox/idea.md"
    source.write_bytes(b"Human idea\r\n")
    fields = {
        "path": "10_Projects/new.md",
        "content": "Source: [[00_Inbox/idea]]\n",
        "request_id": "owner:promotion:1",
    }
    proposal = mutations.propose_note(home, vault, **fields)
    publish = mutations.publish_note

    def interrupt(*args, **kwargs):
        if published:
            publish(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(mutations, "publish_note", interrupt)
        with pytest.raises(KeyboardInterrupt):
            mutations.apply_proposal(
                home,
                vault,
                proposal["proposal_id"],
                proposal["approval_digest"],
                writers_paused=True,
            )
    repeated = mutations.propose_note(home, vault, **fields)
    assert repeated["proposal_id"] == proposal["proposal_id"]
    assert repeated["outcome"] == "prepared"
    expected = "applied" if published else "not_applied"
    assert mutations.recover_proposals(home, vault)[0]["outcome"] == expected
    assert mutations.propose_note(home, vault, **fields)["outcome"] == expected
    assert source.read_bytes() == b"Human idea\r\n"
    target = vault / fields["path"]
    assert target.exists() is published
    if published:
        assert target.read_text() == fields["content"]


def test_repeated_preview_cannot_overwrite_later_human_edit(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/project.md"
    target.write_bytes(b"Human task\r\n")
    fields = {
        "path": "10_Projects/project.md",
        "expected_revision": hashlib.sha256(target.read_bytes()).hexdigest(),
        "anchor": "Human task",
        "replacement": "Completed task",
        "request_id": "owner:task:1",
    }
    proposal = mutations.propose_note(home, vault, **fields)
    target.write_bytes(b"Human revised task\r\n")
    assert mutations.propose_note(home, vault, **fields) == proposal
    with pytest.raises(ValueError, match="Stale full-file revision"):
        mutations.apply_proposal(
            home,
            vault,
            proposal["proposal_id"],
            proposal["approval_digest"],
            writers_paused=True,
        )
    assert target.read_bytes() == b"Human revised task\r\n"


def test_existing_request_never_recreates_deleted_human_note(tmp_path):
    home, vault = setup(tmp_path)
    target = vault / "10_Projects/project.md"
    target.write_text("Human project")
    fields = {
        "path": "10_Projects/project.md",
        "content": "Proposed project",
        "request_id": "owner:existing:1",
    }
    assert mutations.propose_note(home, vault, **fields)["outcome"] == "exists"
    target.unlink()
    with pytest.raises(ValueError, match="no longer present"):
        mutations.propose_note(home, vault, **fields)
    assert not target.exists()


def test_competing_owner_requests_store_once(tmp_path):
    home, vault = setup(tmp_path)
    fields = {"path": "10_Projects/new.md", "content": "new", "request_id": "owner:1"}
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    children = [
        context.Process(target=_request, args=(home, vault, fields, gate, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    gate.set()
    previews = [results.get(timeout=15) for _ in children]
    for child in children:
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join()
            pytest.fail("request worker hung")
        assert child.exitcode == 0
    assert previews[0] == previews[1]
    with mutations.journal(home) as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1


class ReviewClient:
    def __init__(self, answer=None, tool_call=None):
        self.answer = answer
        self.tool_call = tool_call
        self.bodies = []

    def request(self, path, body, timeout=180):
        self.bodies.append(body)
        if len(self.bodies) == 1:
            message = {
                "tool_calls": [
                    {
                        "id": "open-review",
                        "function": {
                            "name": "vault_open",
                            "arguments": json.dumps(
                                {"path": "10_Projects/Daily-briefing.md"}
                            ),
                        },
                    }
                ]
            }
        elif self.tool_call:
            message = {
                "tool_calls": [
                    {
                        "id": "mutation",
                        "function": {"name": self.tool_call, "arguments": "{}"},
                    }
                ]
            }
        else:
            message = {"content": json.dumps(self.answer)}
        return {"choices": [{"message": message}], "usage": {}}


def grounded_review():
    return {
        "recorded_facts": [
            {
                "path": "10_Projects/Daily-briefing.md",
                "block_id": "task-format",
                "text": "- [ ] Draft briefing format — status: todo ^task-format",
            }
        ],
        "suggestions": ["Review the existing outline with the owner before drafting."],
        "unknown_deadlines": True,
    }


def test_daily_review_uses_opened_paths_and_separates_suggestions(tmp_path):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    client = ReviewClient(grounded_review())
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    review, _ = QUALIFY["workflow_review"](client, "synthetic", vault)
    assert review == grounded_review()
    assert (
        client.bodies[0]["messages"][0]["content"]
        == (ROOT / "agents/emuru/SOUL.md").read_text()
    )
    assert client.bodies[-1]["tools"] == [QUALIFY["WRITE"]]
    assert {path: path.read_bytes() for path in before} == before
    cited = indexer.vault_open(vault, review["recorded_facts"][0]["path"], 50000)
    assert review["recorded_facts"][0]["text"] in cited["content"]


@pytest.mark.parametrize(
    "change", ["path", "block", "fact", "deadline", "separation", "invalid_json"]
)
def test_daily_review_refuses_unread_facts_or_inferred_deadlines(tmp_path, change):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    review = copy.deepcopy(grounded_review())
    if change == "path":
        review["recorded_facts"][0]["path"] = "10_Projects/not-opened.md"
    elif change == "block":
        review["recorded_facts"][0]["block_id"] = "invented-block"
    elif change == "fact":
        review["recorded_facts"][0]["text"] = "- Deadline tomorrow. ^task-format"
    elif change == "deadline":
        review["suggestions"] = ["Finish by 2026-10-15."]
    elif change == "separation":
        review["unknown_deadlines"] = False
    else:
        review = None
    with pytest.raises(RuntimeError, match="workflow review"):
        QUALIFY["workflow_review"](ReviewClient(review), "synthetic", vault)


@pytest.mark.parametrize(
    "tool", ["vault_write", "vault_propose", "vault_apply", "terminal"]
)
def test_read_only_daily_review_refuses_mutation_requests(tmp_path, tool):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    with pytest.raises(RuntimeError, match="workflow review"):
        QUALIFY["workflow_review"](ReviewClient(tool_call=tool), "synthetic", vault)


@pytest.mark.parametrize(
    "suggestion",
    [
        "Finish by tomorrow.",
        "Complete next week.",
        "Do it by Friday.",
        "Finish before lunch.",
        "Complete within 2 days.",
        "Finish by 10/15.",
    ],
)
def test_daily_review_rejects_relative_or_short_deadlines(tmp_path, suggestion):
    vault = QUALIFY["synthetic_vault"](tmp_path)
    review = grounded_review()
    review["suggestions"] = [suggestion]
    with pytest.raises(RuntimeError, match="workflow review"):
        QUALIFY["workflow_review"](ReviewClient(review), "synthetic", vault)
