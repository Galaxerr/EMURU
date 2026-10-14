"""Exact manual update intents, with genuine MCP and optional model inference."""

import json
import os

import pytest
from project_update_case import CONVERSATION, update_case
from vault_helpers import create_vault

from emuru.models.ollama import OllamaClient
from emuru.vault import indexer

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("mcp_environment")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def project(tmp_path, *, structured=False):
    vault = create_vault(tmp_path)
    path = (
        "10_Projects/Expense-tracker.md"
        if structured
        else CONVERSATION["original_path"]
    )
    text = (
        "---\ntitle: Personal Expense Tracker\ntype: project\n"
        "project: Expense Tracker\n---\n# Personal Expense Tracker\n"
        "## Decisions\n- Database: PostgreSQL"
        + (" ^decision-database" if structured else "")
        + "\n- Frontend: Next.js and TypeScript\n"
        "## Tasks\n- [ ] Design database schema — status: todo"
        + (" ^task-schema" if structured else "")
        + "\n## Results\n- Initial plan recorded"
        + (" ^result-plan" if structured else "")
        + "\nHuman content remains unchanged.\n"
    )
    (vault / path).write_text(text)
    return vault, path


class ScriptedInference:
    """Tests the acceptance checker, not a claim of model behavior."""

    def __init__(
        self,
        path,
        *,
        substitute=None,
        invalid=None,
        propose=False,
        compound=False,
        create_path=None,
    ):
        self.create_path = create_path
        self.compound = compound
        self.path, self.substitute, self.invalid, self.propose = (
            path,
            substitute,
            invalid,
            propose,
        )

    def request(self, url, body, timeout):
        messages = body["messages"]
        results = [
            json.loads(m["content"]) for m in messages if m.get("role") == "tool"
        ]
        if self.substitute:
            name, args = "vault_write", {"path": self.substitute, "content": "SQLite"}
        elif not results:
            name, args = "vault_search", {"query": "expense tracker"}
        elif len(results) == 1:
            name, args = "vault_open", {"path": self.path}
        else:
            opened = results[1]
            if self.create_path:
                draft = {
                    "path": self.create_path,
                    "expected_revision": None,
                    "anchor": None,
                    "replacement": (
                        "---\ntitle: Expense Tracker\ntype: project\nproject: expense-tracker\n"
                        "tags: [project]\nupdated: null\nsummary: Track expenses.\n---\n"
                        f"[[{self.path.removesuffix('.md')}]]\n"
                        "## Goals\n- Track expenses ^goal-expenses\n"
                        "## Tasks\n- [ ] Build tracker — status: todo ^task-build\n"
                        "## Decisions\n- Database: SQLite ^decision-database\n"
                        "## Results\n- Idea captured ^result-idea\n"
                        "## Next actions\n- Review draft ^next-review\n"
                    ),
                    "references": [],
                    "applied": False,
                    "proposal_id": None,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(draft)
                                + "\nNo update was applied.",
                            }
                        }
                    ]
                }
            anchor = next(
                line for line in opened["content"].splitlines() if "Database:" in line
            )
            replacement = anchor.replace("PostgreSQL", "SQLite")
            refs = []
            if "^" in anchor:
                refs = [
                    {"path": self.path, "block_id": "decision-database", "text": anchor}
                ]
            draft = {
                "path": self.path,
                "expected_revision": opened["revision"],
                "anchor": anchor,
                "replacement": replacement,
                "references": refs,
                "applied": False,
                "proposal_id": results[-1].get("proposal_id"),
            }
            if self.propose and len(results) == 2:
                name = "vault_propose"
                args = {
                    k: draft[k]
                    for k in ("path", "expected_revision", "anchor", "replacement")
                }
            else:
                if self.invalid == "applied":
                    draft["applied"] = True
                elif self.invalid == "block":
                    draft["references"][0]["block_id"] = "invented"
                elif self.invalid == "target":
                    draft["path"] = "10_Projects/Other.md"
                content = json.dumps(draft)
                if self.compound:
                    task = next(
                        line
                        for line in opened["content"].splitlines()
                        if "Design database schema" in line
                    )
                    task_draft = {
                        **draft,
                        "anchor": task,
                        "replacement": task.replace("[ ]", "[x]").replace(
                            "todo", "done"
                        ),
                        "references": [],
                    }
                    content = (
                        "```json\n"
                        + content
                        + "\n```\n```json\n"
                        + json.dumps(task_draft)
                        + "\n```"
                    )
                content += "\nNo update was applied."
                return {
                    "choices": [{"message": {"role": "assistant", "content": content}}]
                }
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "case",
                    "type": "function",
                    "function": {
                        "name": "mcp__vault__" + name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }
        return {"choices": [{"message": message}]}


@pytest.mark.parametrize(
    ("prompt", "substitute"),
    list(
        zip(CONVERSATION["requests"], CONVERSATION["observed_substitutes"], strict=True)
    ),
)
async def test_manual_capture_substitution_fails_acceptance_without_effects(
    tmp_path, prompt, substitute
):
    vault, path = project(tmp_path)
    with pytest.RaisesGroup(
        pytest.RaisesGroup(
            pytest.RaisesExc(AssertionError, match="substituted an Inbox capture")
        )
    ):
        await update_case(
            ScriptedInference(path, substitute=substitute),
            "synthetic",
            vault,
            path,
            prompt,
        )
    assert not (vault / substitute).exists()


@pytest.mark.parametrize("structured", [False, True])
async def test_existing_project_update_drafts_without_applying(tmp_path, structured):
    vault, path = project(tmp_path, structured=structured)
    home = None
    if structured:
        home = tmp_path / "profile"
        home.mkdir(mode=0o700)
        policy = home / "mutation-policy.json"
        policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
        policy.chmod(0o600)
    drafts, trace = await update_case(
        ScriptedInference(path, propose=structured),
        "synthetic",
        vault,
        path,
        CONVERSATION["requests"][1],
        home=home,
    )
    assert all(d["applied"] is False for d in drafts)
    assert ("vault_propose" in trace) is structured
    assert "PostgreSQL" in indexer.vault_open(vault, path, 50000)["content"]


async def test_compound_request_checks_both_task_and_decision_drafts(tmp_path):
    vault, path = project(tmp_path)
    drafts, _ = await update_case(
        ScriptedInference(path, compound=True),
        "synthetic",
        vault,
        path,
        CONVERSATION["requests"][0],
    )
    assert len(drafts) == 2
    with pytest.raises(AssertionError):
        await update_case(
            ScriptedInference(path),
            "synthetic",
            vault,
            path,
            CONVERSATION["requests"][0],
        )


def ordinary_note(tmp_path):
    vault = create_vault(tmp_path)
    path = "30_Resources/Expense-tracker-database.md"
    (vault / path).write_text(
        "# Expense Tracker database reference\nDatabase: PostgreSQL\nHuman content remains unchanged.\n"
    )
    return vault, path


async def test_ordinary_note_edit_drafts_without_applying(tmp_path):
    vault, path = ordinary_note(tmp_path)
    drafts, _ = await update_case(
        ScriptedInference(path),
        "synthetic",
        vault,
        path,
        f"Edit {path}: change the database to SQLite.",
    )
    assert "SQLite" in drafts[0]["replacement"]


def project_idea(tmp_path):
    vault = create_vault(tmp_path)
    source = "00_Inbox/Expense-tracker-idea.md"
    (vault / source).write_text(
        "---\ntitle: Expense Tracker idea\ntype: idea\n---\n"
        "# Expense Tracker idea\nTrack expenses with a simple application.\n"
    )
    return vault, source


async def test_new_project_without_proposal_tool_returns_unstored_draft(tmp_path):
    vault, source = project_idea(tmp_path)
    path = "10_Projects/Expense-tracker.md"
    drafts, trace = await update_case(
        ScriptedInference(source, create_path=path),
        "synthetic",
        vault,
        path,
        f"Create the Expense Tracker project at {path} from {source}.",
    )
    assert drafts[0]["proposal_id"] is None and "vault_propose" not in trace


@pytest.mark.parametrize("invalid", ["applied", "block", "target"])
async def test_update_acceptance_rejects_false_application_and_ungrounded_drafts(
    tmp_path, invalid
):
    vault, path = project(tmp_path, structured=True)
    with pytest.raises(AssertionError):
        await update_case(
            ScriptedInference(path, invalid=invalid),
            "synthetic",
            vault,
            path,
            CONVERSATION["requests"][1],
        )


@pytest.mark.skipif(
    not os.environ.get("EMURU_TEST_PROJECT_UPDATE_MODEL"),
    reason="Set EMURU_TEST_PROJECT_UPDATE_MODEL for isolated Ollama conversation acceptance",
)
@pytest.mark.parametrize("prompt", CONVERSATION["requests"])
async def test_model_manual_project_update(tmp_path, prompt):
    vault, path = project(tmp_path)
    await update_case(
        OllamaClient(
            os.environ.get("EMURU_TEST_PROJECT_UPDATE_URL", "http://127.0.0.1:11434")
        ),
        os.environ["EMURU_TEST_PROJECT_UPDATE_MODEL"],
        vault,
        path,
        prompt,
    )


@pytest.mark.skipif(
    not os.environ.get("EMURU_TEST_PROJECT_UPDATE_MODEL"),
    reason="Set EMURU_TEST_PROJECT_UPDATE_MODEL for isolated Ollama conversation acceptance",
)
@pytest.mark.parametrize("case", ["note", "create", "proposal"])
async def test_model_note_edit_creation_and_stored_proposal(tmp_path, case):
    home = None
    if case == "note":
        vault, path = ordinary_note(tmp_path)
        prompt = f"Edit {path}: change the database to SQLite."
    elif case == "create":
        vault, source = project_idea(tmp_path)
        path = "10_Projects/Expense-tracker.md"
        prompt = f"Create the Expense Tracker project at {path} from {source}."
    else:
        vault, path = project(tmp_path, structured=True)
        prompt = (
            "Prepare and store an owner-reviewable proposal changing the Expense Tracker "
            "database decision to SQLite."
        )
        home = tmp_path / "profile"
        home.mkdir(mode=0o700)
        policy = home / "mutation-policy.json"
        policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
        policy.chmod(0o600)
    drafts, trace = await update_case(
        OllamaClient(
            os.environ.get("EMURU_TEST_PROJECT_UPDATE_URL", "http://127.0.0.1:11434")
        ),
        os.environ["EMURU_TEST_PROJECT_UPDATE_MODEL"],
        vault,
        path,
        prompt,
        home=home,
    )
    assert ("vault_propose" in trace) is (case == "proposal")
    assert all(d["proposal_id"] is None for d in drafts) is (case != "proposal")
