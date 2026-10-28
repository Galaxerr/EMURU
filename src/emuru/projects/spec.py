"""Bounded execution-spec projection and Markdown for the existing proposal flow.

Repository names and acceptance commands are inert text, never paths to open or
commands to run. Validation proves shape, not feasibility, approval or completion.
"""

import re


def _text(value, field, *, identifier=False):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > (64 if identifier else 1000)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (identifier and not re.fullmatch(r"[a-z][a-z0-9-]*", value))
    ):
        raise ValueError(f"Invalid spec field: {field}")
    return value


def _fields(value, fields):
    if not isinstance(value, dict) or value.keys() != set(fields):
        raise ValueError("Unexpected or missing spec fields")


def _items(value, field, *, required=False):
    if not isinstance(value, list) or not (int(required) <= len(value) <= 20):
        raise ValueError(f"Invalid spec list: {field}")
    return value


def validate_spec(spec):
    """Validate one planning-only input/output projection; reject authority flags.

    IDs are stable owner/model planning labels, not authenticated request IDs.
    A repository is an optional descriptive target, not an execution allowlist.
    """
    _fields(
        spec,
        (
            "spec_id",
            "goal",
            "repository",
            "non_goals",
            "constraints",
            "tasks",
            "security_restrictions",
            "unresolved_decisions",
        ),
    )
    _text(spec["spec_id"], "spec_id", identifier=True)
    _text(spec["goal"], "goal")
    if spec["repository"] is not None:
        _text(spec["repository"], "repository")
    for field in (
        "non_goals",
        "constraints",
        "security_restrictions",
        "unresolved_decisions",
    ):
        for item in _items(
            spec[field], field, required=field == "security_restrictions"
        ):
            _text(item, field)
    seen = set()
    for task in _items(spec["tasks"], "tasks", required=True):
        _fields(task, ("id", "description", "acceptance"))
        identity = _text(task["id"], "task.id", identifier=True)
        if identity in seen:
            raise ValueError("Duplicate spec task ID")
        seen.add(identity)
        _text(task["description"], "task.description")
        for check in _items(task["acceptance"], "acceptance", required=True):
            _fields(check, ("command", "expected_result"))
            for field in ("command", "expected_result"):
                if "; expected:" in _text(check[field], field):
                    raise ValueError(f"Invalid spec field: {field}")
    return spec


def spec_markdown(spec):
    """Render validated planning text; callers still resolve notes and proposals.

    Use this body for new notes. For existing notes, merge only the requested
    sections into an exact anchored proposal, preserving existing IDs and bytes.
    """
    validate_spec(spec)
    identity = spec["spec_id"]
    lines = [
        "## Goals",
        f"- {spec['goal']} ^goal-{identity}",
        "",
        "## Tasks",
    ]
    for task in spec["tasks"]:
        lines.append(
            f"- [ ] {task['description']} — status: todo ^task-{identity}-{task['id']}"
        )
        for check in task["acceptance"]:
            lines.append(
                f"  - Planned check: {check['command']}; expected: {check['expected_result']}"
            )
    lines += [
        "",
        "## Decisions",
        f"- Spec: {identity}",
        f"- Target repository: {spec['repository'] or 'unresolved'}",
        "- Execution: not authorized. Network: disabled by default.",
    ]
    for field, label in (
        ("non_goals", "Non-goal"),
        ("constraints", "Constraint"),
        ("security_restrictions", "Security restriction"),
        ("unresolved_decisions", "Unresolved decision"),
    ):
        lines.extend(f"- {label}: {item}" for item in spec[field])
    lines += [
        "",
        "## Results",
        "- Planning draft only; no code or checks executed.",
        "",
        "## Next actions",
        f"- Review spec and unresolved decisions. ^next-{identity}-review",
        "- Note approval does not authorize execution.",
    ]
    return "\n".join(lines) + "\n"
