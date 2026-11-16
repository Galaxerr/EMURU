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
        _fields(
            task,
            ("id", "description", "acceptance", "depends_on")
            if isinstance(task, dict) and "depends_on" in task
            else ("id", "description", "acceptance"),
        )
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
    validate_dependencies(spec["tasks"])
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
        if task.get("depends_on"):
            lines.append("  - Depends on: " + ", ".join(sorted(task["depends_on"])))
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


def validate_dependencies(tasks):
    """Validate only explicit, bounded same-spec dependency labels."""
    graph = {task["id"]: task.get("depends_on", []) for task in tasks}
    for identity, dependencies in graph.items():
        _items(dependencies, "depends_on")
        for dependency in dependencies:
            _text(dependency, "depends_on", identifier=True)
        if (
            len(set(dependencies)) != len(dependencies)
            or identity in dependencies
            or any(dependency not in graph for dependency in dependencies)
        ):
            raise ValueError("Invalid task dependencies")
    visited, active = set(), set()

    def visit(identity):
        if identity in active:
            raise ValueError("Cyclic task dependencies")
        if identity in visited:
            return
        active.add(identity)
        for dependency in graph[identity]:
            visit(dependency)
        active.remove(identity)
        visited.add(identity)

    for identity in graph:
        visit(identity)


def task_contracts(content, *, logical_keys=None):
    """Project exact approved M1 work fields; unrelated note prose is not scope."""
    from emuru.projects.board import spec_tasks
    from emuru.projects.scout import digest

    content = content.replace("\r\n", "\n")
    tasks = spec_tasks(content)
    spec_id = re.findall(r"(?m)^- Spec: ([a-z][a-z0-9-]{0,63})\r?$", content)[0]

    def section(name):
        matches = re.findall(r"(?ms)^## " + name + r"\r?\n(.*?)(?=^## |\Z)", content)
        if len(matches) != 1:
            raise ValueError("Contract needs one " + name + " section")
        return matches[0]

    goals = re.findall(
        r"(?m)^- (.{1,1000}) \^goal-" + re.escape(spec_id) + r"\r?$",
        section("Goals"),
    )
    if len(goals) != 1:
        raise ValueError("Contract needs one exact approved goal")
    decisions = section("Decisions")
    repositories = re.findall(r"(?m)^- Target repository: (.{1,1000})\r?$", decisions)
    if len(repositories) != 1:
        raise ValueError("Contract needs one exact approved repository")
    shared = {
        "goal": goals[0],
        "repository": None if repositories[0] == "unresolved" else repositories[0],
    }
    for field, label in (
        ("constraints", "Constraint"),
        ("security_restrictions", "Security restriction"),
        ("non_goals", "Non-goal"),
        ("unresolved_decisions", "Unresolved decision"),
    ):
        values = re.findall(r"(?m)^- " + label + r": (.{1,1000})\r?$", decisions)
        for value in _items(values, field, required=field == "security_restrictions"):
            _text(value, field)
        shared[field] = values
    if shared.pop("unresolved_decisions"):
        raise ValueError("Unresolved decisions block continuation binding")
    keys = {task["id"]: "task-" + spec_id + "-" + task["id"] for task in tasks}
    if logical_keys is not None:
        if (
            set(logical_keys) != set(keys)
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,200}", value)
                for value in logical_keys.values()
            )
            or len(set(logical_keys.values())) != len(keys)
        ):
            raise ValueError("Invalid confirmed logical task keys")
        keys = logical_keys
    by_id = {task["id"]: task for task in tasks}
    result = {}

    def contract(identity):
        if identity in result:
            return result[identity]
        task = by_id[identity]
        dependencies = sorted(
            [keys[parent], contract(parent)["contract_digest"]]
            for parent in task.get("depends_on", [])
        )
        value = {
            **shared,
            "logical_key": keys[identity],
            "description": task["description"],
            "acceptance": task["acceptance"],
            "dependencies": dependencies,
        }
        result[identity] = {
            "logical_key": keys[identity],
            "contract": value,
            "contract_digest": digest(value),
        }
        return result[identity]

    for task in tasks:
        contract(task["id"])
    return result
