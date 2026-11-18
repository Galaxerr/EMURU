"""Explicit owner-confirmed continuation binding; never launches work.

Lock order: dispatcher, native binder, supervisor. No vault writer lock is taken.
Inspection opens snapshots only; retries are explicit authenticated actions.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from emuru.projects import board, supervisor
from emuru.projects.scout import digest, encoded
from emuru.vault import mutations

KINDS = {"binding-intent", "task-binding", "dependency-binding", "binding-complete"}


def validate_record(value, records):
    common = {"schema", "kind", "generation", "previous", "project"}
    fields = {
        "binding-intent": {
            "request",
            "text_hash",
            "proposal",
            "approval",
            "revision",
            "token",
            "manifest",
        },
        "task-binding": {
            "intent",
            "logical_key",
            "contract_digest",
            "task",
            "key",
            "proposal",
            "anchor_digest",
            "manifest",
            "supersedes",
        },
        "dependency-binding": {
            "intent",
            "predecessor",
            "successor",
            "predecessor_contract",
            "successor_contract",
        },
        "binding-complete": {"intent", "binding_digest", "response_digest", "manifest"},
    }
    kind = value.get("kind")
    if (
        value.get("schema") != 2
        or kind not in fields
        or set(value) != common | fields[kind]
    ):
        raise ValueError("Invalid continuation record; preserve newest state.")
    if kind == "binding-intent":
        if any(
            not isinstance(value[k], str) or not re.fullmatch(p, value[k])
            for k, p in (
                ("request", "[0-9a-f]{32}"),
                ("proposal", "[0-9a-f]{32}"),
                *(
                    (k, "[0-9a-f]{64}")
                    for k in ("text_hash", "approval", "revision", "token", "manifest")
                ),
            )
        ):
            raise ValueError("Invalid binding intent identity.")
        if any(r["kind"] == kind and r["request"] == value["request"] for r in records):
            raise ValueError("Duplicate binding request.")
    else:
        if type(value["intent"]) is not int or not any(
            r["kind"] == "binding-intent"
            and r["generation"] == value["intent"]
            and r["project"] == value["project"]
            for r in records
        ):
            raise ValueError("Binding record lacks its exact intent.")
        for key in fields[kind] - {"intent", "supersedes"}:
            if (
                not isinstance(value[key], str)
                or not value[key]
                or len(value[key].encode()) > 4096
            ):
                raise ValueError("Invalid binding record field.")
    digest_fields = {
        "binding-intent": ("text_hash", "approval", "revision", "token", "manifest"),
        "task-binding": ("contract_digest", "anchor_digest", "manifest"),
        "dependency-binding": ("predecessor_contract", "successor_contract"),
        "binding-complete": ("binding_digest", "response_digest", "manifest"),
    }[kind]
    if any(not re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in digest_fields):
        raise ValueError("Invalid continuation digest.")
    if kind == "task-binding" and (
        not re.fullmatch(r"[A-Za-z0-9-]{1,200}", value["logical_key"])
        or not re.fullmatch(r"emuru:[0-9a-f]{64}", value["key"])
        or not re.fullmatch(r"t_[0-9a-f]+", value["task"])
        or not isinstance(value["supersedes"], list)
        or any(
            not isinstance(v, str) or not re.fullmatch(r"t_[0-9a-f]+", v)
            for v in value["supersedes"]
        )
        or len(set(value["supersedes"])) != len(value["supersedes"])
    ):
        raise ValueError("Invalid continuation task binding.")
    if kind == "dependency-binding" and (
        any(
            not re.fullmatch(r"t_[0-9a-f]+", value[k])
            for k in ("predecessor", "successor")
        )
        or value["predecessor"] == value["successor"]
    ):
        raise ValueError("Invalid continuation dependency binding.")
    if kind != "binding-intent":
        identity_fields = {
            "task-binding": ("logical_key",),
            "dependency-binding": ("predecessor", "successor"),
            "binding-complete": (),
        }[kind]
        if any(
            r["kind"] == kind
            and r["intent"] == value["intent"]
            and all(r[k] == value[k] for k in identity_fields)
            for r in records
        ):
            raise ValueError("Duplicate continuation effect receipt.")


def _records(home):
    if not supervisor.enabled(home):
        return []
    with supervisor._locked(home) as (parent, size, marker):
        return supervisor._records(parent, size, marker)[0]


def _note(home, vault, preview):
    policy = mutations.require_authority(home, vault)
    with mutations.mutation_parent(vault, preview["path"], roots=policy["roots"]) as (
        parent,
        name,
    ):
        raw = mutations.read_note(parent, name)
    if raw is None:
        raise ValueError("Canonical project note missing.")
    return raw.decode(), hashlib.sha256(raw).hexdigest()


def _anchor(note, logical_key, description):
    stable = [
        line
        for line in note.splitlines()
        if re.search(r"\^" + re.escape(logical_key) + r"$", line)
    ]
    if stable:
        if len(stable) != 1 or not re.fullmatch(r"- \[[ xX]\] .+", stable[0]):
            raise ValueError("Ambiguous stable task anchor.")
        anchor = stable[0]
        if not re.fullmatch(
            r"- \[[ xX]\] "
            + re.escape(description)
            + r" — status: [a-z-]+ \^"
            + re.escape(logical_key),
            anchor,
        ):
            raise ValueError("Current task scope changed; approve a new specification.")
        legacy = False
    else:
        candidates = [
            line
            for line in note.splitlines()
            if re.fullmatch(
                r"- \[[ xX]\] " + re.escape(description) + r"(?: — status: [a-z-]+)?",
                line,
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                "Legacy task anchor missing or ambiguous; approve an exact mapping first."
            )
        anchor, legacy = candidates[0], True
    if len(anchor.encode()) > 4096 or note.splitlines().count(anchor) != 1:
        raise ValueError("Task anchor must be unique and at most 4 KiB.")
    return anchor, legacy


def _snapshot(home, vault, root, preview, owner, bot, selection, kb, db, records):
    from emuru.projects.spec import task_contracts

    project = board._digest(
        [str(owner), str(bot), os.path.abspath(vault), preview["path"]]
    )
    tasks = board.spec_tasks(preview["content"])
    selected = selection or [t["id"] for t in tasks]
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(t not in {v["id"] for v in tasks} for t in selected)
    ):
        raise ValueError("Select unique approved task labels explicitly.")
    note, revision = _note(home, vault, preview)
    contracts = task_contracts(preview["content"])
    anchors = {}
    keys = {}
    for task in tasks:
        identity = task["id"]
        anchor, legacy = _anchor(
            note, contracts[identity]["logical_key"], task["description"]
        )
        anchors[identity] = {"anchor": anchor, "legacy": legacy}
        if legacy:
            prior = {
                r["logical_key"]
                for r in records
                if r["kind"] == "task-binding"
                and r["project"] == project
                and r["anchor_digest"] == digest(anchor)
            }
            if len(prior) > 1:
                raise ValueError("Conflicting legacy binding identities.")
            keys[identity] = next(iter(prior), "legacy-" + digest([project, anchor]))
    if keys:
        contracts = task_contracts(
            preview["content"],
            logical_keys={
                k: keys.get(k, v["logical_key"]) for k, v in contracts.items()
            },
        )
    normalized = note
    for task in tasks:
        normalized = normalized.replace(
            anchors[task["id"]]["anchor"],
            f"- [ ] {task['description']} — status: todo ^task-{re.findall(r'(?m)^- Spec: ([a-z][a-z0-9-]*)\r?$', preview['content'])[0]}-{task['id']}",
        )

    def planning_lines(match):
        return (
            "## Tasks\n"
            + "\n".join(
                line
                for line in match[1].splitlines()
                if not line.strip()
                or line.startswith(("- [", "  - Planned check:", "  - Depends on:"))
            )
            + "\n"
        )

    normalized = re.sub(
        r"(?ms)^## Tasks\r?\n(.*?)(?=^## |\Z)", planning_lines, normalized
    )
    if (
        task_contracts(
            normalized, logical_keys={k: v["logical_key"] for k, v in contracts.items()}
        )
        != contracts
    ):
        raise ValueError("Current approved scope changed; approve a new specification.")
    cards = (
        [] if kb is None else kb.list_tasks(db, tenant=project, include_archived=True)
    )
    by_id = {c.id: c for c in cards}
    mappings = {}
    for task in tasks:
        identity = task["id"]
        contract = contracts[identity]
        matches = set()
        historical = []
        for record in records:
            if (
                record["kind"] == "task-binding"
                and record["project"] == project
                and record["logical_key"] == contract["logical_key"]
            ):
                historical.append(record["task"])
                if record["contract_digest"] == contract["contract_digest"]:
                    matches.add(record["task"])
        for card in cards:
            binding = json.loads(card.body)
            if binding.get("schema") != 1:
                raise ValueError("Unknown native card binding.")
            if "scout" in binding:
                continue
            if card.idempotency_key == "emuru:" + digest(
                [project, contract["logical_key"], contract["contract_digest"]]
            ):
                matches.add(card.id)
            elif binding.get("task", {}).get("id") == identity and not any(
                r["kind"] == "task-binding" and r["task"] == card.id for r in records
            ):
                old = mutations.approved_project(
                    home, vault, binding["proposal"], owner, bot, readonly=True
                )
                old_contracts = task_contracts(old["content"])
                if keys:
                    old_contracts = task_contracts(
                        old["content"],
                        logical_keys={
                            k: keys.get(k, v["logical_key"])
                            for k, v in old_contracts.items()
                        },
                    )
                prior = old_contracts.get(identity)
                if (
                    prior
                    and prior["logical_key"] == contract["logical_key"]
                    and prior["contract_digest"] == contract["contract_digest"]
                ):
                    matches.add(card.id)
                else:
                    historical.append(card.id)
        if len(matches) > 1 or any(c not in by_id for c in matches):
            raise ValueError("Ambiguous or missing historical native card.")
        card_id = next(iter(matches), None)
        if card_id:
            native_binding = json.loads(by_id[card_id].body)
            original = mutations.approved_project(
                home, vault, native_binding["proposal"], owner, bot, readonly=True
            )
            if (
                native_binding.get("task") not in board.spec_tasks(original["content"])
                or native_binding.get("revision") != original["after_revision"]
                or native_binding.get("approval") != original["approval_digest"]
                or by_id[card_id].created_by != "emuru-owner"
            ):
                raise ValueError(
                    "Historical native binding no longer matches its approved receipt."
                )
        key = (
            by_id[card_id].idempotency_key
            if card_id
            else "emuru:"
            + digest([project, contract["logical_key"], contract["contract_digest"]])
        )
        mappings[identity] = {
            **contract,
            **anchors[identity],
            "task": task,
            "native_task": card_id,
            "key": key,
            "supersedes": sorted(set(historical) - matches),
            "body_digest": digest(json.loads(by_id[card_id].body))
            if card_id
            else digest(
                {
                    "schema": 1,
                    "revision": preview["after_revision"],
                    "proposal": preview["proposal_id"],
                    "approval": preview["approval_digest"],
                    "task": task,
                }
            ),
        }
    edges = []
    for identity in selected:
        for dep in mappings[identity]["task"].get("depends_on", []):
            if dep not in selected and mappings[dep]["native_task"] is None:
                raise ValueError(
                    "Unselected predecessor is not bound; select it explicitly."
                )
            edges.append([dep, identity])
    # Validate the whole native graph, not just the selected subgraph.
    graph = {c.id: set(kb.parent_ids(db, c.id)) for c in cards} if kb else {}
    for identity in selected:
        entry = mappings[identity]
        node = entry["native_task"] or entry["key"]
        intended = {
            mappings[d]["native_task"] or mappings[d]["key"]
            for d in entry["task"].get("depends_on", [])
        }
        if graph.get(node, set()) - intended:
            raise ValueError(
                "Existing native parents conflict with approved dependencies."
            )
        if (
            entry["native_task"]
            and by_id[entry["native_task"]].status == "running"
            and intended - graph.get(node, set())
        ):
            raise ValueError("Cannot add dependencies to a running task.")
        graph.setdefault(node, set()).update(intended)
    visiting, done = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("Continuation dependency cycle.")
        if node not in graph:
            raise ValueError("Cross-project or missing dependency.")
        if node in done:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        done.add(node)

    for node in graph:
        visit(node)
    from emuru.deployment import source_identity

    execution_identity = {
        "source": source_identity(root),
        "pin": json.loads((Path(root) / "infra/hermes/runtime-lock.json").read_text())[
            "commit"
        ],
        "native": digest(
            json.loads(
                (Path(root) / "infra/hermes/kanban-native-contract.json").read_text()
            )
        ),
    }
    plan = {
        "execution_identity": execution_identity,
        "project": project,
        "proposal": preview["proposal_id"],
        "approval": preview["approval_digest"],
        "revision": revision,
        "selection": selected,
        "mappings": {k: mappings[k] for k in selected},
        "edges": edges,
        "external_bodies": {
            k: mappings[k]["body_digest"]
            for a, _ in edges
            for k in [a]
            if k not in selected
        },
        "external_contracts": {
            k: mappings[k]["contract_digest"]
            for a, _ in edges
            for k in [a]
            if k not in selected
        },
        "external": {
            k: mappings[k]["native_task"]
            for a, _ in edges
            for k in [a]
            if k not in selected
        },
        "board": {k: sorted(v) for k, v in sorted(graph.items())},
        "generation": max(
            (
                r["generation"]
                for r in records
                if r.get("project") == project and r["kind"] in KINDS
            ),
            default=0,
        ),
    }
    return plan


def _render(plan, *, complete=False):
    lines = [f"Project: {plan['project']}", f"Current revision: {plan['revision']}"]
    for label, item in plan["mappings"].items():
        lines += [
            f"Task {label}: {item['native_task'] or 'new blocked card'}; contract: {item['contract_digest']}; logical key: {item['logical_key']}",
            f"Exact anchor ({'legacy confirmation required' if item['legacy'] else 'stable'}): {item['anchor']}",
            "Approved contract: "
            + json.dumps(item["contract"], sort_keys=True, ensure_ascii=False),
            f"Dependencies: {', '.join(item['task'].get('depends_on', [])) or 'none'}; supersedes: {', '.join(item['supersedes']) or 'none'}",
        ]
    lines.append(
        "Binding complete. No execution authorized."
        if complete
        else f"Confirm: /project bind {plan['proposal']} {','.join(plan['selection'])} {digest(plan)}"
    )
    lines.append(
        "Execution allowance unchanged; operator authorization required. Capture remains separate; project completion not asserted."
    )
    return "\n".join(lines)


def _manifest(home, value=None, identity=None):
    from emuru.projects.coder_board import _write_new
    from emuru.projects.coder_runtime import _read_private

    folder = Path(home) / "emuru-projects/supervisor"
    # Immutable payloads share existing private grant storage, outside bounded ledger records.
    storage = Path(home) / "emuru-projects/continuation-manifests"
    if value is not None:
        data = encoded(value)
        if len(data) > 32768:
            raise ValueError("Binding manifest exceeds 32 KiB; select fewer tasks.")
        identity = digest(value)
        with mutations.directory(folder.parent) as parent:
            try:
                os.mkdir(storage.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        try:
            _write_new(storage, identity + ".json", data)
        except FileExistsError:
            if _read_private(storage / (identity + ".json"), 32768) != data:
                raise ValueError("Conflicting immutable binding manifest.")
        return identity
    value = json.loads(_read_private(storage / (identity + ".json"), 32768))
    if digest(value) != identity:
        raise ValueError("Binding manifest digest changed.")
    return value


def command(home, vault, root, text, owner, bot):
    fields = text.split()
    if (
        len(fields) not in {3, 4, 5}
        or fields[0].split("@")[0] != "/project"
        or fields[1] not in {"inspect", "bind"}
        or not re.fullmatch("[0-9a-f]{32}", fields[2])
        or (
            fields[1] == "bind"
            and (len(fields) != 5 or not re.fullmatch("[0-9a-f]{64}", fields[4]))
        )
        or (fields[1] == "inspect" and len(fields) > 4)
    ):
        raise ValueError(
            "Use /project inspect <proposal> [task-id,...] or /project bind <proposal> <task-id,...> <selection-token>."
        )
    request = mutations.authenticated_request(home, vault, read_only=True)
    with mutations._read_database(
        Path(home) / "emuru-mutations/mutations.sqlite"
    ) as db:
        binding = db.execute(
            "SELECT owner,bot,vault,text_hash FROM owner_requests WHERE id=?",
            (request,),
        ).fetchone()
    if binding != (
        str(owner),
        str(bot),
        os.path.abspath(vault),
        hashlib.sha256(text.encode()).hexdigest(),
    ):
        raise ValueError("Exact authenticated owner command required.")
    selection = fields[3].split(",") if len(fields) > 3 else None
    preview = mutations.approved_project(
        home, vault, fields[2], owner, bot, readonly=True
    )
    if fields[1] == "inspect":
        records = _records(home)
        if (Path(home) / "emuru-projects/kanban.db").exists():
            with board.project_board_readonly(home, root) as (kb, db):
                plan = _snapshot(
                    home, vault, root, preview, owner, bot, selection, kb, db, records
                )
        else:
            board.native_api(root)
            plan = _snapshot(
                home, vault, root, preview, owner, bot, selection, None, None, records
            )
        return _render(plan) + "\n" + _facts(home, vault, root, preview, plan)
    return _bind(
        home, vault, root, preview, owner, bot, selection, fields[4], request, text
    )


def _bind(home, vault, root, preview, owner, bot, selection, token, request, text):
    from emuru.projects.dispatcher import dispatcher_lock
    from emuru.projects.supervisor_controls import emergency_size
    from emuru.projects.supervisor_scout import metadata_pending

    with supervisor._locked(home) as (_, _, current_marker):
        if current_marker["schema"] != 2:
            raise ValueError(
                "Stopped-profile continuation-upgrade required before binding."
            )
    with (
        dispatcher_lock(home),
        board.project_board(home, root) as (kb, db),
        supervisor._locked(home, write=True) as (parent, size, marker),
    ):
        if marker["schema"] != 2:
            raise ValueError(
                "Stopped-profile continuation-upgrade required before binding."
            )
        records, size = _binding_records(home, parent, size, marker, request, text)
        intent = next(
            (
                r
                for r in records
                if r["kind"] == "binding-intent" and r["request"] == request
            ),
            None,
        )
        if intent:
            if intent["text_hash"] != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError("Conflicting binding request bytes.")
            complete = next(
                (
                    r
                    for r in records
                    if r["kind"] == "binding-complete"
                    and r["intent"] == intent["generation"]
                ),
                None,
            )
            if complete:
                response = _manifest(home, identity=complete["manifest"])["response"]
                if digest(response) != complete["response_digest"]:
                    raise ValueError("Binding response digest changed.")
                return response
            plan = _manifest(home, identity=intent["manifest"])
            if (
                _note(home, vault, preview)[1] != plan["revision"]
                or preview["approval_digest"] != intent["approval"]
            ):
                raise ValueError(
                    "Incomplete binding note or approval changed; inspect explicitly."
                )
        else:
            plan = _snapshot(
                home, vault, root, preview, owner, bot, selection, kb, db, records
            )
            if digest(plan) != token:
                raise ValueError("Stale continuation selection; inspect again.")
        if intent is not None:
            fresh = _snapshot(
                home, vault, root, preview, owner, bot, selection, kb, db, records
            )
            if any(
                plan[key] != fresh[key]
                for key in (
                    "external",
                    "external_contracts",
                    "external_bodies",
                    "execution_identity",
                )
            ):
                raise ValueError("External predecessor binding changed.")
            for label, item in plan["mappings"].items():
                for key in (
                    "logical_key",
                    "contract_digest",
                    "anchor",
                    "key",
                    "body_digest",
                ):
                    if item[key] != fresh["mappings"][label][key]:
                        raise ValueError(
                            "Incomplete binding mapping changed; no repair authorized."
                        )
        folder = Path(home) / "emuru-projects/supervisor"
        # Reserve every remaining record at its maximum before native side effects.
        remaining = 2 + len(plan["mappings"]) + len(plan["edges"])
        if any(
            len(encoded(item["supersedes"])) + len(item["logical_key"]) + 1500
            > supervisor.RECEIPT_BYTES
            for item in plan["mappings"].values()
        ):
            raise ValueError("Binding receipt exceeds 4 KiB; preserve history.")
        manifest_reserve = (
            len(encoded(plan))
            + len(encoded({"response": _render(plan, complete=True)}))
            + 2048
        )
        if (
            len(encoded(plan)) > 32768
            or len(encoded({"response": _render(plan, complete=True)})) + 2048 > 32768
            or manifest_reserve > 65536
        ):
            raise ValueError("Binding manifests exceed bounds; select fewer tasks.")
        if size + manifest_reserve + metadata_pending(
            records
        ) + remaining * supervisor.RECEIPT_BYTES > supervisor.METADATA_BYTES - max(
            supervisor.EMERGENCY_BYTES, emergency_size(records)
        ):
            raise ValueError(
                "Binding metadata capacity exhausted; restriction reserve retained."
            )
        if intent is None:
            before_manifests = manifest_bytes(home)
            manifest = _manifest(home, plan)
            size += manifest_bytes(home) - before_manifests
            intent, size = supervisor._append(
                folder,
                records,
                size,
                {
                    "kind": "binding-intent",
                    "project": plan["project"],
                    "request": request,
                    "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "proposal": plan["proposal"],
                    "approval": plan["approval"],
                    "revision": plan["revision"],
                    "token": token,
                    "manifest": manifest,
                },
            )
        ids = dict(plan["external"])
        for label, item in plan["mappings"].items():
            matches = [
                c
                for c in kb.list_tasks(
                    db, tenant=plan["project"], include_archived=True
                )
                if c.idempotency_key == item["key"]
            ]
            if len(matches) > 1:
                raise ValueError("Duplicate native binding key.")
            card = matches[0] if matches else None
            if item["native_task"] and (card is None or card.id != item["native_task"]):
                raise ValueError("Confirmed historical card changed.")
            if card is None:
                identity = kb.create_task(
                    db,
                    title=item["task"]["description"],
                    body=json.dumps(
                        {
                            "schema": 1,
                            "revision": preview["after_revision"],
                            "proposal": plan["proposal"],
                            "approval": plan["approval"],
                            "task": item["task"],
                        },
                        sort_keys=True,
                    ),
                    tenant=plan["project"],
                    idempotency_key=item["key"],
                    created_by="emuru-owner",
                    initial_status="blocked",
                    workspace_kind="scratch",
                    project_id="",
                    max_retries=0,
                )
                card = kb.get_task(db, identity)
            if (
                card.tenant != plan["project"]
                or digest(json.loads(card.body)) != item["body_digest"]
            ):
                raise ValueError(
                    "Native binding body changed; preserve incomplete intent."
                )
            ids[label] = card.id
            prior = [
                r
                for r in records
                if r["kind"] == "task-binding"
                and r["intent"] == intent["generation"]
                and r["logical_key"] == item["logical_key"]
            ]
            if not prior:
                _, size = supervisor._append(
                    folder,
                    records,
                    size,
                    {
                        "kind": "task-binding",
                        "project": plan["project"],
                        "intent": intent["generation"],
                        "logical_key": item["logical_key"],
                        "contract_digest": item["contract_digest"],
                        "task": card.id,
                        "key": item["key"],
                        "proposal": plan["proposal"],
                        "anchor_digest": digest(item["anchor"]),
                        "manifest": intent["manifest"],
                        "supersedes": item["supersedes"],
                    },
                )
        for predecessor, successor in plan["edges"]:
            parents = set(kb.parent_ids(db, ids[successor]))
            if ids[predecessor] not in parents:
                kb.link_tasks(db, ids[predecessor], ids[successor])
            if not any(
                r["kind"] == "dependency-binding"
                and r["intent"] == intent["generation"]
                and r["predecessor"] == ids[predecessor]
                and r["successor"] == ids[successor]
                for r in records
            ):
                # External predecessor identity comes from its durable task binding.
                pred = plan["mappings"].get(predecessor)
                pred_digest = (
                    pred["contract_digest"]
                    if pred
                    else plan["external_contracts"][predecessor]
                )
                _, size = supervisor._append(
                    folder,
                    records,
                    size,
                    {
                        "kind": "dependency-binding",
                        "project": plan["project"],
                        "intent": intent["generation"],
                        "predecessor": ids[predecessor],
                        "successor": ids[successor],
                        "predecessor_contract": pred_digest,
                        "successor_contract": plan["mappings"][successor][
                            "contract_digest"
                        ],
                    },
                )
        for label, item in plan["mappings"].items():
            if set(kb.parent_ids(db, ids[label])) != {
                ids[d] for d in item["task"].get("depends_on", [])
            }:
                raise ValueError("Native parent readback mismatch; binding incomplete.")
            item["native_task"] = ids[label]
        response = _render(plan, complete=True)
        before_manifests = manifest_bytes(home)
        manifest = _manifest(home, {"response": response})
        size += manifest_bytes(home) - before_manifests
        _, size = supervisor._append(
            folder,
            records,
            size,
            {
                "kind": "binding-complete",
                "project": plan["project"],
                "intent": intent["generation"],
                "binding_digest": digest(plan),
                "response_digest": digest(response),
                "manifest": manifest,
            },
        )
        return response


def _facts(home, vault, root, preview, plan):
    """Observational status stays outside task identity and selection tokens."""
    from emuru.projects.continuation_batch import (
        control_state,
        memberships,
        project_limits,
        projection,
        verified_completion,
    )
    from emuru.projects.supervisor_scout import reserved

    records = _records(home)
    consumed = reserved(records, plan["project"])
    lines = [
        "Cumulative reserved: " + json.dumps(consumed, sort_keys=True),
        "Remaining project ceiling: "
        + json.dumps(
            {
                k: project_limits(records, plan["project"])[k] - consumed[k]
                for k in consumed
            },
            sort_keys=True,
        ),
    ]
    batch = projection(home, records, plan["project"])
    if batch is not None:
        lines.append("Batch: " + json.dumps(batch, sort_keys=True))
    for intent in (
        r
        for r in records
        if r["kind"] == "binding-intent" and r["project"] == plan["project"]
    ):
        complete = any(
            r["kind"] == "binding-complete" and r["intent"] == intent["generation"]
            for r in records
        )
        lines.append(
            f"Binding intent {intent['generation']}: {'complete' if complete else 'incomplete; original authenticated bind retry required; restart never repairs'}"
        )
    if not (Path(home) / "emuru-projects/kanban.db").exists():
        return "\n".join(
            lines
            + [
                "Native tasks: none. Verified execution: none. Allowance: none. Capture: none."
            ]
            + [
                f"Task {label}: owner must confirm exact binding."
                for label in plan["selection"]
            ]
        )
    with board.project_board_readonly(home, root) as (kb, db):
        cards = {
            card.id: card
            for card in kb.list_tasks(db, tenant=plan["project"], include_archived=True)
        }
        relevant = {
            identity
            for item in plan["mappings"].values()
            for identity in (
                item["native_task"],
                *plan["board"].get(item["native_task"] or item["key"], []),
            )
        }
        verified = {
            identity
            for identity in relevant & cards.keys()
            if verified_completion(home, vault, root, kb, db, cards[identity])
        }
        for label, item in plan["mappings"].items():
            identity = item["native_task"]
            parents = plan["board"].get(identity or item["key"], [])
            blockers = sorted(set(parents) - verified)
            membership = memberships(records, identity)
            restriction = control_state(records, identity)
            if identity is None:
                action = "owner must confirm exact binding"
            elif identity in verified:
                action = "independent review verified; capture is separate"
            elif blockers:
                action = "await verified predecessors " + ", ".join(blockers)
            elif restriction in {"held", "revoked"}:
                action = "execution " + restriction
            elif cards[identity].status == "running":
                action = "await current native run"
            elif cards[identity].status != "blocked":
                action = "inspect native execution and review facts"
            elif not membership:
                action = "operator must enroll an exact batch"
            elif batch is None or (
                records[membership[-1]["allowance"] - 1]["manifest"] != batch["digest"]
            ):
                action = "operator must check current batch membership"
            elif batch["expired"]:
                action = "batch expired"
            elif (
                sum(
                    r["kind"] == "coder-stage"
                    and r.get("phase") == "admitted"
                    and r["task"] == identity
                    for r in records
                )
                >= membership[-1]["attempts"]
            ):
                action = "coder attempt cap exhausted"
            else:
                action = "execution candidate; operator must authorize the exact launch"
            lines.append(f"Task {label}: {action}.")
        for card in cards.values():
            binding = json.loads(card.body)
            lines.append(
                f"Historical native card: {card.id}; status: {card.status}; parents: {', '.join(kb.parent_ids(db, card.id)) or 'none'}"
            )
            supervised = supervisor.projection(
                home, root, plan["project"], card, preview=preview
            )
            if supervised:
                lines.append(supervised)
            if "scout" in binding:
                continue
            from emuru.projects.coder_board import _find_attempt, status

            if _find_attempt(home, card.id) is not None:
                report = status(kb, db, card, home, root)
                lines.append("Execution facts: " + json.dumps(report, sort_keys=True))
    path = Path(home) / "emuru-mutations/mutations.sqlite"
    with mutations._read_database(path) as journal:
        if journal.execute(
            "SELECT 1 FROM sqlite_master WHERE name='project_result_captures'"
        ).fetchone():
            for proposal, binding in journal.execute(
                "SELECT proposal_id,binding FROM project_result_captures"
            ):
                value = json.loads(binding)
                if value.get("project") == plan["project"]:
                    receipt = journal.execute(
                        "SELECT outcome FROM proposals WHERE id=?", (proposal,)
                    ).fetchone()
                    lines.append(
                        f"Capture proposal: {proposal}; state: {receipt[0] if receipt else 'missing'}; "
                        f"status: /project capture-status {value['spec']} {value['task']} {value['review_run']}; "
                        f"preview: /project capture {value['spec']} {value['task']} {value['review_run']}"
                    )
    return "\n".join(lines)


def manifest_bytes(home):
    from emuru.projects.coder_runtime import _read_private

    storage = Path(home) / "emuru-projects/continuation-manifests"
    if not storage.exists():
        return 0
    total = 0
    with mutations.directory(storage) as parent:
        mutations.private_directory(parent)
        for name in os.listdir(parent):
            if not re.fullmatch(r"[0-9a-f]{64}\.json", name):
                raise ValueError(
                    "Unknown continuation manifest; preserve newest state."
                )
            raw = _read_private(storage / name, 32768)
            if hashlib.sha256(raw).hexdigest() != name[:-5]:
                raise ValueError("Continuation manifest bytes changed.")
            total += len(raw)
            if total > supervisor.METADATA_BYTES:
                raise ValueError("Continuation manifests exceed metadata capacity.")
    return total


def _binding_records(home, parent, size, marker, request, text):
    """Only explicit same-request retry may finish one interrupted ledger publication."""
    from emuru.models.gateway import write_private

    try:
        return supervisor._records(parent, size, marker)
    except ValueError:
        names = sorted(
            n for n in os.listdir(parent) if re.fullmatch(r"[0-9]{8}\.json", n)
        )
        if len(names) != marker["generation"] + 1:
            raise
        newest, _ = supervisor._read(parent, names[-1])
        if newest.get("schema") != 2 or newest.get("kind") not in KINDS:
            raise
        candidate = {
            **marker,
            "generation": newest.get("generation"),
            "head": digest(newest),
        }
        records, size = supervisor._records(parent, size, candidate)
        intent = (
            newest
            if newest["kind"] == "binding-intent"
            else next(
                (
                    r
                    for r in records
                    if r["generation"] == newest["intent"]
                    and r["kind"] == "binding-intent"
                ),
                None,
            )
        )
        if (
            intent is None
            or intent["request"] != request
            or intent["text_hash"] != hashlib.sha256(text.encode()).hexdigest()
        ):
            raise ValueError(
                "Interrupted binding publication requires its original authenticated request."
            ) from None
        write_private(
            Path(home) / "emuru-projects/supervisor-enabled.json",
            encoded(candidate).decode(),
        )
        with mutations.directory(Path(home) / "emuru-projects") as directory:
            os.fsync(directory)
        return records, size
