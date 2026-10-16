"""The reported direct creation must store a proposal without needing an Inbox source."""

import asyncio
import json
from pathlib import Path

import pytest
from proposal_creation_case import check

from emuru.vault.mutations import applied_diff_parts

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("draft_only", [False, True])
def test_direct_owner_creation_requires_a_stored_preview(draft_only):
    count = 0

    def chat(messages, tools):
        nonlocal count
        count += 1
        if draft_only or count == 3:
            return {"role": "assistant", "content": "No update was applied."}
        if count == 1:
            name, args = "vault_search", {"query": "Release Validation"}
        else:
            name, args = (
                "vault_propose",
                {
                    "path": "10_Projects/Release-validation.md",
                    "content": "# Release Validation\n## Goals\n- Verify EMURU v0.5.0 manual acceptance ^goal-release\n## Tasks\n- [ ] Validate project creation ^task-release\n",
                },
            )
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": str(count),
                    "type": "function",
                    "function": {
                        "name": "mcp__vault__" + name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        }

    if draft_only:
        with pytest.raises(AssertionError, match="unstored draft"):
            asyncio.run(check(chat, ROOT))
    else:
        assert asyncio.run(check(chat, ROOT)) == ["vault_search", "vault_propose"]


def test_diff_reports_only_approved_bytes_and_bounds_messages():
    preview = {
        "proposal_id": "p",
        "path": "10_Projects/a.md",
        "outcome": "proposed",
        "content": "# Other content",
        "changes": {"anchor": "- [ ] Task", "replacement": "- [x] Task"},
    }
    receipt = {"after_revision": "a" * 64, "outcome": "applied"}
    text = "\n".join(applied_diff_parts(preview, receipt))
    assert "-- [ ] Task\n" in text and "+- [x] Task\n" in text
    assert "No newline at end of file" in text and "changed fragment" in text
    assert "# Other content" not in text
    preview.pop("changes")
    preview["content"] = "😀" * 5000
    receipt["outcome"] = "index_pending"
    parts = applied_diff_parts(preview, receipt)
    assert len(parts) > 1 and all(
        len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts
    )
    assert "saved; indexing pending" in parts[0] and "/dev/null" in parts[0]
