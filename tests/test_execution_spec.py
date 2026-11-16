"""M1 projections through real synthetic vault tools; no model or code execution."""

import copy
from pathlib import Path

import pytest
from mcp import Client
from vault_helpers import create_vault

from emuru.projects.board import spec_tasks
from emuru.projects.spec import spec_markdown, validate_spec
from emuru.vault import indexer, mutations
from emuru.vault.mcp import create_mcp


@pytest.fixture
def spec():
    return {
        "spec_id": "sum-prices",
        "goal": "Sum integer prices in a small Python tool",
        "repository": None,
        "non_goals": ["No payments or deployment"],
        "constraints": ["Python standard library only"],
        "tasks": [
            {
                "id": "sum",
                "description": "Implement sum_prices with unit tests",
                "acceptance": [
                    {
                        "command": "python -m unittest",
                        "expected_result": "Zero exit; empty and negative-price cases pass",
                    }
                ],
            }
        ],
        "security_restrictions": ["No secrets or network access"],
        "unresolved_decisions": ["Owner must select repository before execution"],
    }


def test_projection_is_bounded_planning_text(spec):
    assert validate_spec(spec) == spec
    markdown = spec_markdown(spec)
    assert "python -m unittest" in markdown
    assert "Execution: not authorized" in markdown
    assert "no code or checks executed" in markdown
    assert "^task-sum-prices-sum" in markdown
    for heading in ("Goals", "Tasks", "Decisions", "Results", "Next actions"):
        assert f"## {heading}\n" in markdown


@pytest.mark.parametrize("field", ["command", "expected_result"])
@pytest.mark.parametrize("validate", [validate_spec, spec_markdown])
def test_projection_rejects_separator_in_acceptance_fields(spec, field, validate):
    check = spec["tasks"][0]["acceptance"][0]
    check["command"] = "run-check"
    check[field] = "prints ; expected: OK"
    with pytest.raises(ValueError, match=field):
        validate(spec)


def test_model_guidance_uses_the_existing_renderer_format(spec):
    """Check published guidance and its example, not arbitrary model output."""
    guidance = (
        (Path(__file__).resolve().parents[1] / "agents/emuru/SOUL.md")
        .read_text()
        .split("### Execution specifications (planning only)", 1)[1]
    )
    assert "emuru.projects.spec.spec_markdown" in guidance
    assert "exactly one `## Tasks` section" in guidance
    example = guidance.split("```markdown\n", 1)[1].split("```", 1)[0]
    task = spec["tasks"][0]
    for placeholder, value in {
        "<spec-id>": spec["spec_id"],
        "<task-id>": task["id"],
        "<description>": task["description"],
        "<command>": task["acceptance"][0]["command"],
        "<expected-result>": task["acceptance"][0]["expected_result"],
    }.items():
        example = example.replace(placeholder, value)
    assert example in spec_markdown(spec)
    assert spec_tasks(example) == spec["tasks"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("spec_id", "../escape"),
        ("spec_id", "a" * 65),
        ("goal", " "),
        ("goal", "x" * 1001),
        ("goal", "bad\nheading"),
        ("repository", True),
        ("tasks", []),
        ("tasks", "task"),
        ("constraints", ["x"] * 21),
        ("security_restrictions", []),
        ("non_goals", [None]),
        ("execution_authorized", True),
        ("applied", True),
        ("scope", "execute"),
    ],
)
def test_projection_rejects_invalid_fields_and_permission_claims(spec, field, value):
    spec[field] = value
    with pytest.raises(ValueError):
        spec_markdown(spec)


def test_projection_rejects_missing_fields_duplicate_ids_and_empty_checks(spec):
    for field in spec:
        bad = copy.deepcopy(spec)
        del bad[field]
        with pytest.raises(ValueError):
            validate_spec(bad)
    spec["tasks"].append(copy.deepcopy(spec["tasks"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_spec(spec)
    spec["tasks"].pop()
    spec["tasks"][0]["acceptance"] = []
    with pytest.raises(ValueError, match="acceptance"):
        validate_spec(spec)


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
async def test_spec_creation_update_replay_and_readback(tmp_path, spec):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    policy = home / "mutation-policy.json"
    policy.write_text('{"enabled":false,"roots":["10_Projects"]}')
    policy.chmod(0o600)
    vault = create_vault(tmp_path)
    path = "10_Projects/Sum-prices.md"
    content = (
        "---\ntitle: Sum prices\ntype: project\nproject: sum-prices\n"
        "tags: [project]\nupdated: null\nsummary: Plan a Python tool.\n---\n"
        + spec_markdown(spec)
    )
    async with Client(create_mcp(vault, proposal_home=home)) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == {
            "vault_map",
            "vault_search",
            "vault_open",
            "vault_neighbors",
            "vault_write",
            "vault_propose",
            "vault_proposal_status",
        }
        found = await client.call_tool("vault_search", {"query": "Sum prices"})
        assert found.structured_content["results"] == []
        arguments = {"path": path, "content": content, "request_id": "owner:spec:1"}
        result = await client.call_tool("vault_propose", arguments)
        assert not result.is_error
        preview = result.structured_content
        assert preview["outcome"] == "proposed" and preview["content"] == content
        assert not (vault / path).exists()
        assert (
            await client.call_tool("vault_propose", arguments)
        ).structured_content == preview
        # Apply only through the existing explicit synthetic operator API.
        assert (
            mutations.apply_proposal(
                home,
                vault,
                preview["proposal_id"],
                preview["approval_digest"],
                writers_paused=True,
            )["outcome"]
            == "applied"
        )
        assert indexer.vault_open(vault, path, 50000)["content"] == content
        assert (await client.call_tool("vault_propose", arguments)).structured_content[
            "outcome"
        ] == "applied"
        assert len(list((vault / "10_Projects").glob("*.md"))) == 1

        # Simulate a pre-existing human note, then update one exact task anchor.
        target = vault / path
        target.write_bytes(
            target.read_bytes() + b"\nHuman section\r\nKeep exactly.\r\n"
        )
        opened = (
            await client.call_tool("vault_open", {"path": path})
        ).structured_content
        before = target.read_bytes()
        anchor = "- [ ] Implement sum_prices with unit tests — status: todo ^task-sum-prices-sum"
        update = {
            "path": path,
            "expected_revision": opened["revision"],
            "anchor": anchor,
            "replacement": anchor.replace(
                "with unit tests", "with empty-input unit tests"
            ),
            "request_id": "owner:spec:2",
        }
        result = await client.call_tool("vault_propose", update)
        assert not result.is_error
        preview = result.structured_content
        assert target.read_bytes() == before
        mutations.apply_proposal(
            home,
            vault,
            preview["proposal_id"],
            preview["approval_digest"],
            writers_paused=True,
        )
        assert target.read_bytes() == before.replace(
            anchor.encode(), update["replacement"].encode()
        )
        assert target.read_text().count("^task-sum-prices-sum") == 1
        for fields in (
            {"path": "99_Private/escape.md", "content": content},
            {**update, "request_id": "stale"},
            {
                **update,
                "expected_revision": indexer.vault_open(vault, path, 50000)["revision"],
                "anchor": "missing anchor",
                "request_id": "missing",
            },
        ):
            assert (await client.call_tool("vault_propose", fields)).is_error


@pytest.mark.anyio
@pytest.mark.usefixtures("mcp_environment")
async def test_missing_policy_exposes_no_proposal_or_execution(tmp_path, spec):
    vault = create_vault(tmp_path)
    async with Client(create_mcp(vault)) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == {
            "vault_map",
            "vault_search",
            "vault_open",
            "vault_neighbors",
            "vault_write",
        }
        assert (
            await client.call_tool(
                "vault_propose",
                {
                    "path": "10_Projects/Sum-prices.md",
                    "content": spec_markdown(spec),
                },
            )
        ).is_error
    assert not list((vault / "10_Projects").iterdir())


def test_continuation_binding_dependency_roundtrip_and_contracts(spec):
    from emuru.projects.spec import task_contracts

    spec["unresolved_decisions"] = []
    spec["tasks"].append(
        {
            "id": "finish",
            "description": "Finish integration",
            "acceptance": [{"command": "check", "expected_result": "Pass"}],
            "depends_on": ["sum"],
        }
    )
    content = spec_markdown(spec)
    assert "  - Depends on: sum\n" in content
    assert spec_tasks(content) == spec["tasks"]
    contracts = task_contracts(content)
    assert contracts["finish"]["contract"]["dependencies"] == [
        ["task-sum-prices-sum", contracts["sum"]["contract_digest"]]
    ]
    assert task_contracts(content + "\nUnrelated human prose.\n") == contracts
    spec["tasks"][0]["acceptance"][0]["expected_result"] = "Changed requirement"
    changed = task_contracts(spec_markdown(spec))
    assert all(
        changed[key]["contract_digest"] != contracts[key]["contract_digest"]
        for key in contracts
    )


@pytest.mark.parametrize(
    "dependencies", [["missing"], ["sum"], ["finish", "finish"], True, "finish"]
)
def test_continuation_binding_invalid_dependencies(spec, dependencies):
    spec["tasks"].append(
        {
            "id": "finish",
            "description": "Finish integration",
            "acceptance": [{"command": "check", "expected_result": "Pass"}],
        }
    )
    spec["tasks"][0]["depends_on"] = dependencies
    with pytest.raises(ValueError):
        spec_markdown(spec)


def test_continuation_binding_cycle_and_dependency_grammar(spec):
    spec["tasks"].append(
        {
            "id": "finish",
            "description": "Finish integration",
            "acceptance": [{"command": "check", "expected_result": "Pass"}],
            "depends_on": ["sum"],
        }
    )
    content = spec_markdown(spec)
    for malformed in (
        content.replace("  - Depends on: sum", "  - Depends on: sum, sum"),
        content.replace("  - Depends on: sum", "  - Depends on: missing"),
        content.replace(
            "  - Depends on: sum", "  - Depends on: sum\n  - Depends on: sum"
        ),
        content.replace(
            "  - Depends on: sum",
            "  - Depends on: sum\n  - Planned check: late; expected: Pass",
        ),
    ):
        with pytest.raises(ValueError):
            spec_tasks(malformed)
    spec["tasks"][0]["depends_on"] = ["finish"]
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_continuation_binding_contract_scope_and_ambiguity(spec):
    from emuru.projects.spec import task_contracts

    spec["unresolved_decisions"] = []
    content = spec_markdown(spec)
    contract = task_contracts(content)["sum"]["contract"]
    assert contract == {
        "logical_key": "task-sum-prices-sum",
        "description": spec["tasks"][0]["description"],
        "acceptance": spec["tasks"][0]["acceptance"],
        "goal": spec["goal"],
        "repository": None,
        "constraints": ["Python standard library only"],
        "security_restrictions": ["No secrets or network access"],
        "non_goals": ["No payments or deployment"],
        "dependencies": [],
    }
    assert task_contracts(content.replace("\n", "\r\n")) == task_contracts(content)
    for malformed in (
        content.replace("## Goals\n", "## Goals\n## Goals\n"),
        content.replace(
            "- Target repository: unresolved",
            "- Target repository: unresolved\n- Target repository: other",
        ),
        content.replace(
            "## Results", "- Unresolved decision: Needs owner\n\n## Results"
        ),
    ):
        with pytest.raises(ValueError):
            task_contracts(malformed)


def test_continuation_binding_dependency_render_order(spec):
    spec["tasks"].extend(
        [
            {
                "id": name,
                "description": name,
                "acceptance": [{"command": "check", "expected_result": "Pass"}],
            }
            for name in ("second", "third")
        ]
    )
    spec["tasks"][2]["depends_on"] = ["sum", "second"]
    content = spec_markdown(spec)
    assert "  - Depends on: second, sum\n" in content
    assert spec_tasks(content)[2]["depends_on"] == ["second", "sum"]
