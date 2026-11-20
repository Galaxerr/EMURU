"""Exact reviewed predecessor inputs; no dispatch or mutable shared workspace."""

import hashlib
import json
import re
from contextvars import ContextVar
from pathlib import Path

from emuru.projects.scout import digest, git

_RECORDS = ContextVar("continuation_admission_records", default=None)


def admission_records(home):
    current = _RECORDS.get()
    if current is not None and current[0] == str(home):
        return current[1]
    from emuru.projects.continuation import _records

    return _records(home)


def is_rework(grant):
    return grant.get("schema") == 3 or (
        grant.get("schema") == 4 and grant.get("rework") is not None
    )


def approval_fingerprint(grant, identity, context):
    """Allow reuse only for an initial, dependency-free, read-only task."""
    continuation = grant["continuation"]
    if (
        is_rework(grant)
        or continuation["predecessors"]
        or grant["write_files"]
        or grant["create_files"]
        or context[1] == "GROQ_API_KEY"
    ):
        # Groq reopens its credentials independently of the sampled gateway context.
        raise ValueError("Execution requires fresh explicit owner authorization.")
    scope = {
        k: v
        for k, v in grant.items()
        if k
        not in {
            "grant_id",
            "proposal",
            "approval",
            "revision",
            "continuation",
            "approval_reuse",
        }
    }
    return digest(
        {
            "policy": 1,
            "scope": scope,
            "contract": continuation["contract"],
            "input": continuation["input_sha256"],
            "execution": identity,
            "provider": context,
        }
    )


def enrollment_authorization(home, vault, root, records, grant, identity, previous):
    """Resolve an explicitly reusable owner grant from the immutable ledger."""
    from emuru.projects import supervisor
    from emuru.projects.coder_gateway import provider_context
    from emuru.projects.supervisor_controls import control_state
    from emuru.vault import mutations

    reuse = grant.get("approval_reuse")
    if reuse is None:
        return None
    fingerprint = approval_fingerprint(grant, identity, provider_context(root))
    if reuse is True:
        return {"fingerprint": fingerprint, "reused_from": None}
    if (
        previous is None
        or previous["grant_sha256"] != reuse
        or previous.get("execution_authorization")
        != {"fingerprint": fingerprint, "reused_from": None}
        or previous["proposal"] == grant["proposal"]
        or previous["boot_id"] != supervisor.boot_id()
        or supervisor.time.monotonic_ns() >= previous["deadline_ns"]
        or control_state(records, previous) != "unrestricted"
    ):
        raise ValueError(
            "Reusable owner authorization unavailable; fresh approval required."
        )
    origin = mutations.approved_project(
        home, vault, previous["proposal"], grant["owner"], grant["bot"], readonly=True
    )
    if (
        origin["approval_digest"] != previous["authority"]["approval"]
        or origin["after_revision"] != previous["authority"]["revision"]
    ):
        raise ValueError(
            "Original owner authorization changed; fresh approval required."
        )
    return {"fingerprint": fingerprint, "reused_from": previous["generation"]}


def validate(grant):
    value = grant["continuation"]
    hashes = {"batch", "contract", "input_sha256"}
    fields = {
        "task",
        "contract",
        "grant_id",
        "grant_sha256",
        "evidence_sha256",
        "receipt_sha256",
        "final_sha256",
        "coder_run",
        "reviewer_run",
    }
    if (
        not isinstance(value, dict)
        or set(value) != hashes | {"predecessors"}
        or any(
            not isinstance(value[k], str) or not re.fullmatch("[a-f0-9]{64}", value[k])
            for k in hashes
        )
        or not isinstance(value["predecessors"], list)
        or len(value["predecessors"]) > 100
    ):
        raise ValueError("Invalid continuation authority.")
    seen = set()
    for item in value["predecessors"]:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or any(
                not isinstance(item[k], str)
                or not re.fullmatch("[a-f0-9]{64}", item[k])
                for k in fields - {"task", "grant_id", "coder_run", "reviewer_run"}
            )
            or not isinstance(item["task"], str)
            or not re.fullmatch("t_[a-f0-9]+", item["task"])
            or not isinstance(item["grant_id"], str)
            or not re.fullmatch("[a-f0-9]{32}", item["grant_id"])
            or any(
                type(item[k]) is not int or item[k] <= 0
                for k in ("coder_run", "reviewer_run")
            )
            or item["coder_run"] == item["reviewer_run"]
            or item["task"] == grant["task"]
            or item["task"] in seen
        ):
            raise ValueError("Invalid continuation predecessor binding.")
        seen.add(item["task"])


def baseline(grant):
    """Read selected committed blobs only; inherited creates are composed later."""
    result = {}
    repo = Path(grant["path"])
    total = 0
    for name in grant["read_files"]:
        row = git(repo, "ls-tree", "-z", "--full-tree", grant["baseline"], "--", name)
        if not row:
            continue
        fields = row.split(b"\t", 1)[0].decode().split()
        if len(fields) != 3 or fields[:2] not in (
            ["100644", "blob"],
            ["100755", "blob"],
        ):
            raise ValueError("Continuation baseline is not a regular blob.")
        size = int(git(repo, "cat-file", "-s", fields[2]))
        total += size
        if (
            size > grant["limits"]["file_bytes"]
            or total > grant["limits"]["total_bytes"]
        ):
            raise ValueError("Continuation input size exceeded.")
        data = git(repo, "cat-file", "blob", fields[2])
        if len(data) != size:
            raise ValueError("Continuation baseline changed.")
        result[name] = (data, int(fields[0][-3:], 8))
    return result


def manifest(tree):
    return {
        name: {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": mode,
        }
        for name, (data, mode) in sorted(tree.items())
    }


def compose(grant, outputs):
    """Compose verified trees in dependency order, refusing independent conflicts."""
    tree = baseline(grant)
    writers = {}
    seen = set()
    for item, original, final, ancestors in outputs:
        task = item["task"]
        if task in seen or not set(ancestors) <= seen:
            raise ValueError("Continuation dependency order changed.")
        seen.add(task)
        if not set(original) <= set(final):
            raise ValueError("Continuation deletion unsupported.")
        for name, value in final.items():
            if value == original.get(name) or name not in grant["read_files"]:
                continue
            prior = writers.get(name, set())
            if tree.get(name) != value and prior - set(ancestors):
                raise ValueError(
                    "Conflicting independent predecessor writes; clarification required."
                )
            tree[name] = value
            writers[name] = {task} if prior <= set(ancestors) else prior | {task}
    if (
        not set(tree) <= set(grant["read_files"])
        or set(grant["read_files"]) - set(grant["create_files"]) - set(tree)
        or set(grant["create_files"]) & set(tree)
        or len(tree) > grant["limits"]["files"]
        or sum(len(v[0]) for v in tree.values()) > grant["limits"]["total_bytes"]
    ):
        raise ValueError("Continuation composed scope or size changed.")
    for data, mode in tree.values():
        if (
            len(data) > grant["limits"]["file_bytes"]
            or mode not in {0o644, 0o755}
            or b"\0" in data
        ):
            raise ValueError("Invalid continuation input file.")
        data.decode("utf-8")
    if digest(manifest(tree)) != grant["continuation"]["input_sha256"]:
        raise ValueError("Continuation input digest changed.")
    return tree


def inputs(folder, grant):
    """Reopen immutable dependency trees, recursively verifying exact provenance."""
    from emuru.projects.coder import load_grant
    from emuru.projects.coder_runtime import (
        _tree_matches,
        verify_evidence,
    )
    from emuru.projects.reviewer import OUTPUT_BYTES, _read

    outputs = []
    bindings = grant["continuation"]["predecessors"]
    available = {b["task"]: b for b in bindings}
    for item in bindings:
        previous = Path(folder).parent / item["grant_id"]
        parent = load_grant(previous / "grant.json")
        if (
            digest(parent) != item["grant_sha256"]
            or parent["task"] != item["task"]
            or any(
                parent[k] != grant[k]
                for k in (
                    "owner",
                    "bot",
                    "vault",
                    "project",
                    "repository",
                    "path",
                    "baseline",
                )
            )
        ):
            raise ValueError("Continuation predecessor grant/base identity changed.")
        run = {"task": item["task"], "run": item["coder_run"]}
        report = verify_evidence(previous, item["evidence_sha256"], task_run=run)
        receipt = _read(previous / "review/receipt.json", OUTPUT_BYTES)
        if (
            not report["success"]
            or receipt["status"] != "verified"
            or receipt["verdict"]["verdict"] != "approve"
            or receipt["task"] != item["task"]
            or receipt["coder_run"] != item["coder_run"]
            or receipt["run"] != item["reviewer_run"]
            or receipt["grant_sha256"] != digest(parent)
            or receipt["evidence_sha256"] != item["evidence_sha256"]
            or digest(receipt) != item["receipt_sha256"]
            or digest(report["final_manifest"]) != item["final_sha256"]
        ):
            raise ValueError("Continuation predecessor review/evidence changed.")
        ancestors = parent.get("continuation", {}).get("predecessors", [])
        if any(available.get(a["task"]) != a for a in ancestors):
            raise ValueError("Continuation ancestor lineage changed.")
        original_folder = previous
        original_report = report
        if is_rework(parent):
            from emuru.projects.rework import lineage

            original_folder, _, original_report, _ = lineage(previous, parent)
        original = _tree_matches(
            original_folder / "initial-source",
            original_report["initial_manifest"],
            frozen=True,
        )
        final = _tree_matches(
            previous / "final-source", report["final_manifest"], frozen=True
        )
        outputs.append(
            (
                item,
                {
                    k: (v, original_report["initial_manifest"][k]["mode"])
                    for k, v in original.items()
                },
                {k: (v, report["final_manifest"][k]["mode"]) for k, v in final.items()},
                {a["task"] for a in ancestors},
            )
        )
    return compose(grant, outputs)


def authorize(home, root, grant, contracts, bindings, allowance, records):
    """Require exact closure and independently verified native reviews before claim."""
    from emuru.projects import board, reviewer
    from emuru.projects.coder import load_grant

    value = grant["continuation"]
    contract = next(
        (
            v
            for v in contracts.values()
            if v["contract_digest"] == value["contract"]
            and bindings.get(v["logical_key"], {}).get("task") == grant["task"]
        ),
        None,
    )
    if contract is None or value["batch"] != allowance["manifest"]:
        raise ValueError("Continuation batch/contract changed.")
    expected = {}

    def visit(current):
        for key, identity in current["contract"]["dependencies"]:
            parent = next(
                (
                    v
                    for v in contracts.values()
                    if v["logical_key"] == key and v["contract_digest"] == identity
                ),
                None,
            )
            binding = bindings.get(key)
            if (
                parent is None
                or binding is None
                or binding["contract_digest"] != identity
            ):
                raise ValueError("Continuation dependency contract unavailable.")
            visit(parent)
            expected[binding["task"]] = identity

    visit(contract)
    if {p["task"]: p["contract"] for p in value["predecessors"]} != expected:
        raise ValueError("Continuation exact predecessor closure changed.")
    folder = Path(home) / "emuru-projects/coder-attempts" / grant["grant_id"]
    inputs(folder, grant)
    with board.project_board_readonly(home, root) as (kb, db):
        direct = {
            bindings[key]["task"] for key, _ in contract["contract"]["dependencies"]
        }
        if set(kb.parent_ids(db, grant["task"])) != direct:
            raise ValueError("Continuation native dependency edges changed.")
        for item in value["predecessors"]:
            previous = folder.parent / item["grant_id"]
            parent = load_grant(previous / "grant.json")
            token = _RECORDS.set((str(home), records))
            try:
                receipt = reviewer.verify(
                    home,
                    Path(allowance["authority"]["vault"]),
                    root,
                    item["task"],
                    parent,
                    previous,
                    kb,
                    db,
                    historical=True,
                )
            finally:
                _RECORDS.reset(token)
            card = kb.get_task(db, item["task"])
            native = kb.get_run(db, item["reviewer_run"])
            if (
                digest(receipt) != item["receipt_sha256"]
                or receipt["status"] != "verified"
                or card.status not in {"done", "archived"}
                or native is None
                or native.ended_at is None
                or native.outcome != "completed"
                or json.loads(card.result or "null") != receipt
            ):
                raise ValueError(
                    "Continuation native predecessor completion unverified."
                )
