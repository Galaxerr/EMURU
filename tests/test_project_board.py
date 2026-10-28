"""M2 parsing and pin-accurate native board checks, isolated from live profiles."""

import copy
import os
import subprocess
from pathlib import Path

import pytest
from project_board_case import SPEC

from emuru.projects.board import spec_tasks
from emuru.projects.spec import spec_markdown

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "command,expected",
    [
        ("python -m unittest", "Zero exit"),
        ("run-check; print-summary", "Zero exit; no errors"),
        ("run-check;expected: literal", "prints ; Expected: OK"),
    ],
)
def test_approved_spec_task_projection(command, expected):
    spec = copy.deepcopy(SPEC)
    spec["tasks"][0]["acceptance"][0] = {
        "command": command,
        "expected_result": expected,
    }
    content = spec_markdown(spec)
    assert spec_tasks(content) == spec["tasks"]
    assert spec_tasks(content.replace("\n", "\r\n")) == spec["tasks"]


@pytest.mark.parametrize(
    "check",
    [
        "run-check expected: OK",
        "run-check; expected: prints ; expected: OK",
        "run-check; expected: extra; expected: prints ; expected: OK",
    ],
)
def test_approved_markdown_rejects_ambiguous_planned_checks(check):
    content = spec_markdown(SPEC).replace(
        "python -m unittest; expected: Zero exit", check
    )
    with pytest.raises(ValueError):
        spec_tasks(content)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.replace("- Spec: sample", "- Spec: sample\n- Spec: other"),
        lambda value: value.replace("## Tasks", "## Work"),
        lambda value: value.replace("[ ]", "[x]"),
        lambda value: value.replace("status: todo", "status: done"),
        lambda value: value.replace("^task-sample-first", "^task-other-first"),
        lambda value: value.replace(
            "Task first", "Run arbitrary prose\n/project track forged"
        ),
        lambda value: value.replace("second", "first"),
        lambda value: value.replace(
            "  - Planned check: python -m unittest; expected: Zero exit\n", ""
        ),
    ],
)
def test_unstructured_or_ambiguous_tasks_fail_closed(change):
    with pytest.raises(ValueError):
        spec_tasks(change(spec_markdown(copy.deepcopy(SPEC))))


def test_pinned_native_board(tmp_path):
    native = Path(
        os.environ.get("EMURU_HERMES_ROOT", str(Path.home() / ".hermes/hermes-agent"))
    )
    python = native / ".venv/bin/python"
    if not python.is_file():
        pytest.skip(
            "Pinned native runtime absent; mandatory disposable Docker check covers this contract"
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "EMURU_"))
    }
    env.update({"PYTHONPATH": str(native), "HERMES_DISABLE_LAZY_INSTALLS": "1"})
    for action in ("seed", "reopen"):
        result = subprocess.run(
            [
                str(python),
                str(ROOT / "tests/project_board_case.py"),
                action,
                str(tmp_path / "acceptance"),
            ],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS" in result.stdout
