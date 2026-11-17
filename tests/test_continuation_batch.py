"""Approved operator/command seams for bounded continuation authority."""

# ruff: noqa: F811 -- shared pytest fixture injection.

import pytest

from emuru.projects import continuation_batch as batch
from emuru.projects.coder import LIMITS


@pytest.mark.parametrize(
    "tasks,scout,seconds,runs,artifacts",
    [
        (1, 0, 1800, 2, 17 * 1024**2),
        (1, 1, 1920, 2, 17 * 1024**2 + 65536),
        (2, 0, 3600, 4, 34 * 1024**2),
        (3, 0, 5400, 6, 51 * 1024**2),
        (2, 1, 3720, 4, 34 * 1024**2 + 65536),
        (3, 1, 5520, 6, 51 * 1024**2 + 65536),
    ],
)
def test_continuation_batch_spec_arithmetic(tasks, scout, seconds, runs, artifacts):
    value = batch.additional([{"attempts": 2, "limits": LIMITS}] * tasks, scout)
    assert value == {
        "coder_runs": runs,
        "reviewer_runs": runs,
        "scout_runs": scout,
        "active_seconds": seconds,
        "coder_iterations": 12 * runs,
        "reviewer_iterations": 4 * runs,
        "checks": 4 * runs,
        "output_bytes": 320 * 1024 * runs,
        "artifact_bytes": artifacts,
    }


# Reuse the existing pinned-native command fixture and disposable repository.
import json

from test_continuation_binding import (  # noqa: F401
    ROOT,
    continuation_binding,
    snapshot,
)

from emuru.projects import board, supervisor
from emuru.projects.coder_runtime import qualification_fixture
from emuru.projects.scout import digest


@pytest.fixture
def batch_case(continuation_binding, tmp_path, monkeypatch):
    case = continuation_binding
    case.command(case.inspect())
    _, policy = qualification_fixture(tmp_path / "repo-fixture")
    with supervisor._locked(case.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    complete = next(r for r in records if r["kind"] == "binding-complete")
    tasks = []
    with board.project_board_readonly(case.home, ROOT) as (kb, db):
        cards = sorted(
            kb.list_tasks(db, include_archived=True),
            key=lambda c: json.loads(c.body)["task"]["id"],
        )
    for card in cards:
        bound = next(
            r for r in records if r["kind"] == "task-binding" and r["task"] == card.id
        )
        tasks.append(
            {
                **{k: policy[k] for k in batch.SCOPE},
                "logical_key": bound["logical_key"],
                "contract_digest": bound["contract_digest"],
                "native_task": card.id,
                "attempts": 2,
            }
        )
    policy.update(
        schema=2,
        review=__import__(
            "emuru.projects.reviewer", fromlist=["authority"]
        ).authority(),
        project=complete["project"],
        proposal=case.preview["proposal_id"],
        revision=case.preview["after_revision"],
        approval=case.preview["approval_digest"],
        vault=digest(str(case.vault)),
        task=cards[0].id,
        task_digest=digest(json.loads(cards[0].body)["task"]),
    )
    manifest = {
        "schema": 1,
        "project": policy["project"],
        "proposal": policy["proposal"],
        "binding_digest": complete["binding_digest"],
        "tasks": tasks,
        "scout": 0,
        "previous_allowance": None,
        "ceilings": batch.additional(tasks),
    }
    case.policy, case.manifest, case.cards = policy, manifest, cards
    case.image = "sha256:" + "a" * 64
    monkeypatch.setattr(supervisor, "boot_id", lambda: "boot-one")
    monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: 1000000000)

    def preview():
        return batch.preview(
            case.home, case.vault, ROOT, case.manifest, case.policy, case.image
        )

    case.batch_preview = preview

    def enroll():
        report = preview()
        return batch.enroll(
            case.home,
            case.vault,
            ROOT,
            case.manifest,
            case.policy,
            case.image,
            report["digest"],
        )

    case.batch_enroll = enroll
    return case


def test_continuation_batch_preview_readonly_enroll_replay(batch_case):
    c = batch_case
    before = snapshot(c.home), snapshot(c.vault)
    report = c.batch_preview()
    assert (snapshot(c.home), snapshot(c.vault)) == before
    assert report["ceilings"]["active_seconds"] == 3600
    assert report["dependency_order"] == ["first", "second"]
    assert report["consumed"]["coder_runs"] == report["unspent"]["coder_runs"] == 0
    allowance = batch.enroll(
        c.home, c.vault, ROOT, c.manifest, c.policy, c.image, report["digest"]
    )
    before = snapshot(c.home), snapshot(c.vault)
    assert (
        batch.enroll(
            c.home, c.vault, ROOT, c.manifest, c.policy, c.image, report["digest"]
        )
        == allowance
    )
    assert (snapshot(c.home), snapshot(c.vault)) == before
    with pytest.raises(ValueError, match="previous allowance"):
        c.batch_preview()


@pytest.mark.parametrize(
    "mutation", ["attempts", "extra", "scope", "ceiling", "token", "binding"]
)
def test_continuation_batch_refuses_stale_or_expanded_authority(batch_case, mutation):
    import copy

    c = batch_case
    report = c.batch_preview()
    manifest = copy.deepcopy(c.manifest)
    expect = report["digest"]
    if mutation == "attempts":
        manifest["tasks"][0]["attempts"] = True
    elif mutation == "extra":
        manifest["execute"] = True
    elif mutation == "scope":
        manifest["tasks"][0]["read_files"].append("secret.txt")
    elif mutation == "ceiling":
        manifest["ceilings"]["active_seconds"] += 1
    elif mutation == "binding":
        manifest["binding_digest"] = "0" * 64
    else:
        expect = "0" * 64
    before = snapshot(c.home), snapshot(c.vault)
    with pytest.raises(ValueError):
        batch.enroll(c.home, c.vault, ROOT, manifest, c.policy, c.image, expect)
    assert (snapshot(c.home), snapshot(c.vault)) == before


def test_continuation_batch_initial_admission_and_successor_refusal(
    batch_case, tmp_path
):
    from emuru.projects import supervisor_coder

    c = batch_case
    c.batch_enroll()
    policy_file = tmp_path / "grant.json"
    policy_file.write_text(json.dumps(c.policy))
    policy_file.chmod(0o600)
    supervisor.enroll(
        c.home, c.vault, ROOT, "coder", policy_file, c.policy["task"], c.image
    )
    admitted = supervisor_coder.reserve(c.home, c.vault, ROOT, c.policy, c.image)
    assert admitted["reserved"]["coder_runs"] == 1
    assert admitted["reserved"]["active_seconds"] == 600
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    detail = batch.projection(c.home, records, c.policy["project"])
    assert detail["consumed"]["active_seconds"] == 600
    assert detail["remaining"]["active_seconds"] == 3000
    successor = {
        **c.policy,
        "task": c.cards[1].id,
        "task_digest": digest(json.loads(c.cards[1].body)["task"]),
    }
    with pytest.raises(ValueError, match="successor"):
        batch.require_task(c.home, records, successor)


@pytest.fixture
def batch_controls(batch_case):
    from emuru.projects.supervisor_controls import owner_control
    from emuru.telegram.queue import TelegramQueue, load_telegram_settings
    from emuru.vault import mutations

    c = batch_case
    allowance = c.batch_enroll()
    queue = TelegramQueue(
        c.home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        42,
        clock=lambda: 100000,
    )

    def deliver(action, control_token, uid):
        text = f"/project batch-{action} {allowance['manifest']} {control_token}"
        queue.stage(
            7,
            [
                {
                    "update_id": uid,
                    "message": {
                        "message_id": uid,
                        "date": 100000,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        row = queue.claim(7, "emuru")
        request, _ = mutations.begin_request(
            c.home, c.vault, 42, 7, uid, text, fresh=True
        )
        result = owner_control(c.home, c.vault, ROOT, text, 42, 7, request, uid)
        queue.finish(row["sequence"], True)
        return result

    def facts():
        with supervisor._locked(c.home) as (parent, size, marker):
            records, _ = supervisor._records(parent, size, marker)
        return batch.projection(c.home, records, c.policy["project"]), records

    c.deliver, c.batch_facts = deliver, facts
    yield c
    queue.close()


def test_continuation_batch_controls_hold_continue_cancel_no_launch(batch_controls):
    c = batch_controls
    first, _ = c.batch_facts()
    assert "held" in c.deliver("hold", first["token"], 1)
    held, records = c.batch_facts()
    assert held["control"] == "held"
    with pytest.raises(ValueError, match="held"):
        batch.require_task(c.home, records, c.policy)
    assert "Stale" in c.deliver("continue", first["token"], 2)
    assert "no work launched" in c.deliver("continue", held["token"], 3)
    ready, records = c.batch_facts()
    assert ready["control"] == "unrestricted"
    assert ready["deadline_ns"] == first["deadline_ns"]
    assert ready["consumed"] == first["consumed"]
    assert "stop_requested" in c.deliver("cancel", ready["token"], 4)
    revoked, records = c.batch_facts()
    with pytest.raises(ValueError, match="revoked"):
        batch.require_task(c.home, records, c.policy)
    assert "revoked" in c.deliver("continue", revoked["token"], 5)
    assert not any(r.get("phase") == "admitted" for r in records)
    assert revoked["consumed"] == first["consumed"]


@pytest.mark.parametrize("change", ["boot", "deadline"])
def test_continuation_batch_original_expiry_never_renews(
    batch_case, monkeypatch, change
):
    c = batch_case
    c.batch_enroll()
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    before = snapshot(c.home)
    if change == "boot":
        monkeypatch.setattr(supervisor, "boot_id", lambda: "boot-two")
    else:
        monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: 86401000000001)
    with pytest.raises(ValueError, match="expired"):
        batch.require_task(c.home, records, c.policy)
    assert snapshot(c.home) == before


def test_continuation_batch_cumulative_slots_and_single_scout(batch_case):
    c = batch_case
    for task in c.manifest["tasks"]:
        task["attempts"] = 1
    c.manifest["scout"] = 1
    c.manifest["ceilings"] = batch.additional(c.manifest["tasks"], 1)
    first = c.batch_enroll()
    assert first["ceilings"]["active_seconds"] == 1920
    c.manifest["previous_allowance"] = first["manifest"]
    with pytest.raises(ValueError, match="second Scout"):
        c.batch_preview()
    c.manifest["scout"] = 0
    for task in c.manifest["tasks"]:
        task["attempts"] = 2
    c.manifest["ceilings"] = batch.additional(c.manifest["tasks"], 1)
    report = c.batch_preview()
    assert report["unspent"]["active_seconds"] == 1920
    assert report["new"]["active_seconds"] == 1800
    assert report["ceilings"]["active_seconds"] == 3720
    second = c.batch_enroll()
    assert second["deadline_ns"] == first["deadline_ns"]
    c.manifest["previous_allowance"] = second["manifest"]
    for task in c.manifest["tasks"]:
        task["attempts"] = 3
    with pytest.raises(ValueError):
        c.batch_preview()


@pytest.mark.parametrize("checkpoint", ["batch-allowance", "batch-task", "marker"])
def test_continuation_batch_interrupted_enrollment_requires_exact_retry(
    batch_case, monkeypatch, checkpoint
):
    from emuru.models import gateway

    c = batch_case
    report = c.batch_preview()
    original = supervisor._append
    original_marker = gateway.write_private
    crashed = False

    def append(folder, records, size, value, **kwargs):
        nonlocal crashed
        result = original(folder, records, size, value, **kwargs)
        if not crashed and value["kind"] == checkpoint:
            crashed = True
            raise OSError("injected lost acknowledgement")
        return result

    def marker(path, text):
        nonlocal crashed
        if (
            not crashed
            and checkpoint == "marker"
            and path.name == "supervisor-enabled.json"
        ):
            crashed = True
            raise OSError("injected marker interruption")
        return original_marker(path, text)

    monkeypatch.setattr(supervisor, "_append", append)
    monkeypatch.setattr(gateway, "write_private", marker)
    with pytest.raises(OSError):
        batch.enroll(
            c.home, c.vault, ROOT, c.manifest, c.policy, c.image, report["digest"]
        )
    before = snapshot(c.home)
    with pytest.raises(ValueError):
        batch.enroll(c.home, c.vault, ROOT, c.manifest, c.policy, c.image, "0" * 64)
    assert snapshot(c.home) == before
    allowance = batch.enroll(
        c.home, c.vault, ROOT, c.manifest, c.policy, c.image, report["digest"]
    )
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    assert sum(r["kind"] == "batch-allowance" for r in records) == 1
    assert sum(r["kind"] == "batch-task" for r in records) == 2
    assert allowance["ceilings"]["coder_runs"] == 4


def test_continuation_batch_admitted_stage_drains_hold_then_observes_cancel(
    batch_controls, tmp_path
):
    from emuru.projects import supervisor_coder

    c = batch_controls
    policy_file = tmp_path / "grant.json"
    policy_file.write_text(json.dumps(c.policy))
    policy_file.chmod(0o600)
    supervisor.enroll(
        c.home, c.vault, ROOT, "coder", policy_file, c.policy["task"], c.image
    )
    supervisor_coder.reserve(c.home, c.vault, ROOT, c.policy, c.image)
    stage = supervisor_coder.CoderStage(c.home, c.vault, ROOT, c.policy)
    first, _ = c.batch_facts()
    c.deliver("hold", first["token"], 1)
    stage.check()
    held, _ = c.batch_facts()
    c.deliver("cancel", held["token"], 2)
    with pytest.raises(InterruptedError, match="stop requested"):
        stage.check()
    revoked, records = c.batch_facts()
    assert revoked["consumed"]["coder_runs"] == 1
    assert revoked["consumed"]["active_seconds"] == 600
    assert sum(r.get("phase") == "admitted" for r in records) == 1


def test_continuation_batch_metadata_capacity_preserves_hold_cancel(
    batch_controls, monkeypatch, tmp_path
):
    from emuru.projects import supervisor_coder

    c = batch_controls
    policy = tmp_path / "grant.json"
    policy.write_text(json.dumps(c.policy))
    policy.chmod(0o600)
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, c.policy["task"], c.image)
    with supervisor._locked(c.home) as (parent, size, marker):
        _records, used = supervisor._records(parent, size, marker)
    monkeypatch.setattr(supervisor, "METADATA_BYTES", used + supervisor.EMERGENCY_BYTES)
    before = snapshot(c.home)
    with pytest.raises(ValueError):
        supervisor_coder.reserve(c.home, c.vault, ROOT, c.policy, c.image)
    assert snapshot(c.home) == before
    first, _ = c.batch_facts()
    assert "held" in c.deliver("hold", first["token"], 1)
    held, _ = c.batch_facts()
    assert "stop_requested" in c.deliver("cancel", held["token"], 2)
    revoked, _ = c.batch_facts()
    assert revoked["control"] == "revoked"
    assert revoked["consumed"]["coder_runs"] == 0


def test_continuation_batch_preserves_prior_m6_scout_slot(batch_case, tmp_path):
    from emuru.projects import supervisor_scout

    c = batch_case
    scout = {
        "schema": 1,
        "repository": c.policy["repository"],
        "path": c.policy["path"],
        "baseline": c.policy["baseline"],
        "files": c.policy["read_files"],
        "tasks": [c.policy["task"]],
        "timeout_seconds": 120,
    }
    path = tmp_path / "scout.json"
    path.write_text(json.dumps(scout))
    path.chmod(0o600)
    supervisor.enroll(
        c.home, c.vault, ROOT, "scout", path, c.policy["task"], c.image, owner=42, bot=7
    )
    c.manifest["ceilings"] = batch.additional(c.manifest["tasks"], 1)
    report = c.batch_preview()
    assert report["unspent"]["scout_runs"] == 1
    assert report["new"]["scout_runs"] == 0
    c.batch_enroll()
    assert (
        supervisor_scout.preflight(
            c.home, c.vault, ROOT, scout, c.policy["task"], c.image
        )
        is None
    )
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    assert (
        batch.projection(c.home, records, c.policy["project"])["remaining"][
            "scout_runs"
        ]
        == 1
    )


def test_continuation_batch_dependency_tampering_refuses_before_claim(batch_case):
    c = batch_case
    c.batch_enroll()
    with board.project_board(c.home, ROOT) as (kb, db):
        extra = kb.create_task(
            db,
            title="Unapproved parent",
            tenant=c.policy["project"],
            created_by="emuru-owner",
            initial_status="blocked",
        )
        kb.link_tasks(db, extra, c.policy["task"])
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    before = snapshot(c.home)
    with pytest.raises(ValueError, match="dependencies"):
        batch.require_task(c.home, records, c.policy)
    assert snapshot(c.home) == before


@pytest.mark.parametrize("action", ["hold", "cancel"])
@pytest.mark.parametrize(
    "ordering", ["restriction-first", "admission-first", "concurrent"]
)
@pytest.mark.parametrize("grant_schema", [2, 4])
def test_continuation_execution_and_batch_restriction_admission_race(
    batch_controls, tmp_path, action, ordering, grant_schema
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from emuru.projects import supervisor_coder

    c = batch_controls
    if grant_schema == 4:
        from emuru.projects import continuation_execution as execution

        entry = next(
            e for e in c.manifest["tasks"] if e["native_task"] == c.policy["task"]
        )
        c.policy.update(
            schema=4,
            rework=None,
            continuation={
                "batch": digest(c.manifest),
                "contract": entry["contract_digest"],
                "input_sha256": digest(
                    execution.manifest(execution.baseline(c.policy))
                ),
                "predecessors": [],
            },
        )
    policy = tmp_path / "grant.json"
    policy.write_text(json.dumps(c.policy))
    policy.chmod(0o600)
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, c.policy["task"], c.image)
    first, _ = c.batch_facts()
    start = Barrier(2)

    def reserve():
        start.wait(timeout=10)
        try:
            return supervisor_coder.reserve(c.home, c.vault, ROOT, c.policy, c.image)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        if ordering == "restriction-first":
            c.deliver(action, first["token"], 1)
        future = pool.submit(reserve)
        start.wait(timeout=10)
        if ordering == "admission-first":
            admitted = future.result(timeout=20)
            assert admitted is not None
        if ordering != "restriction-first":
            c.deliver(action, first["token"], 1)
        admitted = future.result(timeout=20)
    detail, records = c.batch_facts()
    restriction = next(r for r in records if r["kind"] == "batch-control")
    assert all(
        r["generation"] < restriction["generation"]
        for r in records
        if r.get("phase") == "admitted"
    )
    assert detail["consumed"]["coder_runs"] == (0 if admitted is None else 1)
    if ordering == "restriction-first":
        assert admitted is None
    with board.project_board_readonly(c.home, ROOT) as (kb, db):
        assert kb.list_runs(db, c.policy["task"]) == []
    assert not list(c.home.rglob("run.json"))


@pytest.mark.parametrize("continuation_binding", ["three"], indirect=True)
def test_continuation_execution_native_three_task_chain(batch_case, monkeypatch):
    import copy
    import hashlib
    import multiprocessing
    import os
    import uuid
    from pathlib import Path

    from test_coder_supervisor_native import launch_child

    from emuru.projects import (
        coder_board,
        coder_runtime,
    )
    from emuru.projects import (
        continuation_execution as execution,
    )

    image = os.environ.get("EMURU_CODER_TEST_IMAGE")
    if image is None:
        pytest.skip("Matching immutable native/container image required")
    c = batch_case
    # Restore real native stage clocks; the ordinary batch tests use fixed clocks.
    monkeypatch.setattr(supervisor.time, "monotonic_ns", _REAL_MONOTONIC_NS)
    monkeypatch.setattr(
        supervisor,
        "boot_id",
        lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    c.image = image
    target = c.home / "vault-target.json"
    target.write_text(json.dumps({"mode": "real", "vault_path": str(c.vault)}))
    target.chmod(0o600)
    c.batch_enroll()
    entries = {e["native_task"]: e for e in c.manifest["tasks"]}
    # Card IDs sort independently of dependency order.
    cards = {json.loads(card.body)["task"]["id"]: card for card in c.cards}
    predecessors = []
    previous_data = b"def sum_prices(prices):\n    return 0\n"
    proof = []
    for index, label in enumerate(["first", "second", "third"]):
        card = cards[label]
        grant = copy.deepcopy(c.policy)
        grant.update(
            schema=4,
            rework=None,
            grant_id=uuid.uuid4().hex,
            task=card.id,
            task_digest=digest(json.loads(card.body)["task"]),
        )
        tree = execution.baseline(grant)
        if index:
            tree["sum_prices.py"] = (previous_data, 0o644)
        grant["continuation"] = {
            "batch": digest(c.manifest),
            "contract": entries[card.id]["contract_digest"],
            "input_sha256": digest(execution.manifest(tree)),
            "predecessors": copy.deepcopy(predecessors),
        }
        policy = c.home.parent / f"grant-{index}.json"
        policy.write_text(json.dumps(grant))
        policy.chmod(0o600)
        source = (
            f"def sum_prices(prices):\n    return sum(prices) + {index} - {index}\n"
        )
        fixture = {
            "root": c.home.parent,
            "home": c.home,
            "vault": c.vault,
            "policy": policy,
            "grant": grant,
            "task_id": card.id,
            "repo": Path(grant["path"]),
            "coder_source": source,
            "input_source_sha256": hashlib.sha256(previous_data).hexdigest(),
        }
        if index:
            import runpy

            launch = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"]
            parent_folder = (
                c.home / "emuru-projects/coder-attempts" / predecessors[-1]["grant_id"]
            )
            for artifact in [
                "evidence.json",
                "review/receipt.json",
                "final-source/sum_prices.py",
            ]:
                path = parent_folder / artifact
                original = path.read_bytes()
                mode = path.stat().st_mode & 0o777
                path.chmod(0o600)
                path.write_bytes(original + b" altered")
                try:
                    with pytest.raises((ValueError, OSError)):
                        launch(c.home, c.vault, policy, card.id, ROOT)
                    with board.project_board_readonly(c.home, ROOT) as (kb, db):
                        assert kb.list_runs(db, card.id) == []
                finally:
                    path.write_bytes(original)
                    path.chmod(mode)
            proof[-1]["zero_claim_tampering_cases"] = 3
        supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, card.id, image)
        parent, child = multiprocessing.get_context("fork").Pipe()
        process = multiprocessing.get_context("fork").Process(
            target=launch_child, args=(fixture, "review", child)
        )
        process.start()
        try:
            process.join(90)
            assert not process.is_alive(), "Continuation native launch stalled"
            assert process.exitcode == 0, (
                fixture["root"] / "child-result.json"
            ).read_text()
        finally:
            if process.is_alive():
                process.kill()
                process.join(10)
            parent.close()
            child.close()
        receipt = json.loads((fixture["root"] / "child-result.json").read_text())
        assert receipt["status"] == "verified"
        folder = coder_board.attempt_path(c.home, grant)
        report = coder_runtime.verify_evidence(
            folder,
            receipt["evidence_sha256"],
            task_run={"task": card.id, "run": receipt["coder_run"]},
        )
        assert (
            digest(report["initial_manifest"]) == grant["continuation"]["input_sha256"]
        )
        assert (folder / "final-source/sum_prices.py").read_bytes() == source.encode()
        with board.project_board_readonly(c.home, ROOT) as (kb, db):
            runs = kb.list_runs(db, card.id)
            assert len(runs) == 2 and all(r.ended_at is not None for r in runs)
            assert runs[0].id != runs[1].id
        predecessors.append(
            {
                "task": card.id,
                "contract": entries[card.id]["contract_digest"],
                "grant_id": grant["grant_id"],
                "grant_sha256": digest(grant),
                "evidence_sha256": receipt["evidence_sha256"],
                "receipt_sha256": digest(receipt),
                "final_sha256": digest(report["final_manifest"]),
                "coder_run": receipt["coder_run"],
                "reviewer_run": receipt["run"],
            }
        )
        proof.append({"grant": grant, "receipt": receipt, "report": report})
        previous_data = source.encode()
    assert len({p["grant"]["grant_id"] for p in proof}) == 3
    destination = os.environ.get("EMURU_CODER_QUALIFICATION")
    if destination:
        Path(destination).mkdir(mode=0o700, parents=True, exist_ok=True)
        (Path(destination) / "continuation-three-task.json").write_text(
            json.dumps(proof, indent=2)
        )


# Save before fixed-clock fixtures alter the shared time module.
import time

_REAL_MONOTONIC_NS = time.monotonic_ns


@pytest.mark.parametrize(
    "attack", ["none", "input", "contract", "batch", "missing-predecessor"]
)
def test_continuation_execution_admission_refuses_before_native_claim(
    batch_case, attack
):
    import copy

    from emuru.projects import continuation_execution as execution

    c = batch_case
    c.batch_enroll()
    grant = copy.deepcopy(c.policy)
    if attack == "missing-predecessor":
        card = next(
            card for card in c.cards if json.loads(card.body)["task"]["id"] == "second"
        )
        grant.update(task=card.id, task_digest=digest(json.loads(card.body)["task"]))
    entry = next(e for e in c.manifest["tasks"] if e["native_task"] == grant["task"])
    grant.update(schema=4, rework=None)
    grant["continuation"] = {
        "batch": digest(c.manifest),
        "contract": entry["contract_digest"],
        "input_sha256": digest(execution.manifest(execution.baseline(grant))),
        "predecessors": [],
    }
    if attack in {"input", "contract", "batch"}:
        grant["continuation"][{"input": "input_sha256"}.get(attack, attack)] = "0" * 64
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    before = snapshot(c.home)
    if attack == "none":
        assert batch.require_task(c.home, records, grant)["manifest"] == digest(
            c.manifest
        )
    else:
        with pytest.raises(ValueError):
            batch.require_task(c.home, records, grant)
    assert snapshot(c.home) == before
    with board.project_board_readonly(c.home, ROOT) as (kb, db):
        assert all(kb.list_runs(db, card.id) == [] for card in c.cards)


@pytest.mark.parametrize("verdict", ["changes_requested", "blocked"])
def test_continuation_execution_native_rework_authority(
    batch_case, monkeypatch, verdict
):
    import copy
    import os
    import runpy
    from pathlib import Path

    from test_reviewer_supervisor_native import invoke, join, stop

    from emuru.projects import coder_board, reviewer
    from emuru.projects import continuation_execution as execution

    image = os.environ.get("EMURU_CODER_TEST_IMAGE")
    if image is None:
        pytest.skip("Matching immutable native/container image required")
    c = batch_case
    monkeypatch.setattr(supervisor.time, "monotonic_ns", _REAL_MONOTONIC_NS)
    monkeypatch.setattr(
        supervisor,
        "boot_id",
        lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    c.image = image
    target = c.home / "vault-target.json"
    target.write_text(json.dumps({"mode": "real", "vault_path": str(c.vault)}))
    target.chmod(0o600)
    c.batch_enroll()
    grant = copy.deepcopy(c.policy)
    entry = next(e for e in c.manifest["tasks"] if e["native_task"] == grant["task"])
    grant.update(schema=4, rework=None)
    grant["continuation"] = {
        "batch": digest(c.manifest),
        "contract": entry["contract_digest"],
        "input_sha256": digest(execution.manifest(execution.baseline(grant))),
        "predecessors": [],
    }
    policy = c.home.parent / "grant.json"
    policy.write_text(json.dumps(grant))
    policy.chmod(0o600)
    fixture = {
        "root": c.home.parent,
        "home": c.home,
        "vault": c.vault,
        "repo": Path(grant["path"]),
        "grant": grant,
        "policy": policy,
        "task_id": grant["task"],
        "review_verdict": {
            "verdict": verdict,
            "summary": "Synthetic review rejection",
            "blockers": [
                {
                    "path": "sum_prices.py",
                    "line": 1,
                    "reason": "Requires revision",
                    "requested_change": "Revise implementation",
                }
            ],
            "suggestions": [],
        },
    }
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, grant["task"], image)
    process, parent, child = invoke(fixture)
    try:
        result = join(fixture, process)
        assert result["status"] == verdict
    finally:
        stop(process, parent, child)
    second = runpy.run_path(str(ROOT / "scripts/hermes/rework-check.py"))["new_grant"](
        fixture
    )
    assert second["schema"] == 4 and second["continuation"] == grant["continuation"]
    policy.write_text(json.dumps(second))
    if verdict == "blocked":
        before = snapshot(c.home)
        with pytest.raises(ValueError):
            supervisor.enroll(
                c.home, c.vault, ROOT, "coder", policy, second["task"], image
            )
        assert snapshot(c.home) == before
        return
    fixture.update(
        grant=second,
        coder_source="def sum_prices(prices):\n    return sum(prices) + 0\n",
        input_source_sha256=__import__("hashlib")
        .sha256(b"def sum_prices(prices):\n    return sum(prices)\n")
        .hexdigest(),
        review_verdict={
            "verdict": "approve",
            "summary": "Synthetic revision approved",
            "blockers": [],
            "suggestions": [],
        },
    )
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, second["task"], image)
    process, parent, child = invoke(fixture)
    try:
        result = join(fixture, process)
        assert result["status"] == "verified"
    finally:
        stop(process, parent, child)
    folder = coder_board.attempt_path(c.home, second)
    assert reviewer._read(folder / "loop.json")["coder_runs"] == 2
    third = runpy.run_path(str(ROOT / "scripts/hermes/rework-check.py"))["new_grant"](
        fixture
    )
    policy.write_text(json.dumps(third))
    before = snapshot(c.home)
    with pytest.raises(ValueError):
        supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, third["task"], image)
    assert snapshot(c.home) == before
    output = os.environ.get("EMURU_CODER_QUALIFICATION")
    if output:
        Path(output).mkdir(mode=0o700, parents=True, exist_ok=True)
        (Path(output) / "continuation-rework.json").write_text(
            json.dumps({"parent": grant, "child": second, "receipt": result}, indent=2)
        )


@pytest.mark.parametrize("boundary", ["reservation", "claim"])
def test_continuation_execution_process_death_retains_charge_without_replay(
    batch_case, tmp_path, boundary, monkeypatch
):
    import copy
    import multiprocessing
    import os

    from emuru.projects import coder_board, supervisor_coder
    from emuru.projects import continuation_execution as execution
    from emuru.projects.dispatcher import dispatcher_lock

    c = batch_case
    from pathlib import Path

    monkeypatch.setattr(supervisor.time, "monotonic_ns", _REAL_MONOTONIC_NS)
    monkeypatch.setattr(
        supervisor,
        "boot_id",
        lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    c.batch_enroll()
    grant = copy.deepcopy(c.policy)
    entry = next(e for e in c.manifest["tasks"] if e["native_task"] == grant["task"])
    grant.update(
        schema=4,
        rework=None,
        continuation={
            "batch": digest(c.manifest),
            "contract": entry["contract_digest"],
            "input_sha256": digest(execution.manifest(execution.baseline(grant))),
            "predecessors": [],
        },
    )
    policy = tmp_path / "grant.json"
    policy.write_text(json.dumps(grant))
    policy.chmod(0o600)
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, grant["task"], c.image)

    def die():
        with dispatcher_lock(c.home):
            supervisor_coder.reserve(c.home, c.vault, ROOT, grant, c.image)
            if boundary == "reservation":
                os._exit(17)
            stage = supervisor_coder.CoderStage(c.home, c.vault, ROOT, grant)
            folder = coder_board.create_attempt(c.home, grant, supervision=stage)
            coder_board.record_launcher(folder)
            coder_board.claim(
                c.home, c.vault, ROOT, grant["task"], grant, folder, supervision=stage
            )
            os._exit(17)

    process = multiprocessing.get_context("fork").Process(target=die)
    process.start()
    process.join(20)
    try:
        assert not process.is_alive() and process.exitcode == 17
        with supervisor._locked(c.home) as (parent, size, marker):
            records, _ = supervisor._records(parent, size, marker)
        assert (
            sum(
                r["kind"] == "coder-stage" and r.get("phase") == "admitted"
                for r in records
            )
            == 1
        )
        assert (
            batch.projection(c.home, records, grant["project"])["consumed"][
                "coder_runs"
            ]
            == 1
        )
        with board.project_board_readonly(c.home, ROOT) as (kb, db):
            assert len(kb.list_runs(db, grant["task"])) == (
                1 if boundary == "claim" else 0
            )
        with pytest.raises(ValueError):
            supervisor_coder.reserve(c.home, c.vault, ROOT, grant, c.image)
        before = snapshot(c.home)
        with pytest.raises(ValueError):
            supervisor.enroll(
                c.home, c.vault, ROOT, "coder", policy, grant["task"], c.image
            )
        assert snapshot(c.home) == before
        with dispatcher_lock(c.home):
            recovered = coder_board.reconcile(
                c.home, c.vault, ROOT, grant["task"], grant
            )
        assert recovered["status"] == "uncertain" and recovered["stop"] == "proven"
        with board.project_board_readonly(c.home, ROOT) as (kb, db):
            assert len(kb.list_runs(db, grant["task"])) == (
                1 if boundary == "claim" else 0
            )
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)


def test_continuation_execution_prepaid_cleanup_survives_full_control_reserve(
    batch_controls, tmp_path, monkeypatch
):
    import sys

    from emuru.projects import supervisor_coder
    from emuru.projects.supervisor_scout import metadata_pending

    c = batch_controls
    policy = tmp_path / "grant.json"
    policy.write_text(json.dumps(c.policy))
    policy.chmod(0o600)
    supervisor.enroll(c.home, c.vault, ROOT, "coder", policy, c.policy["task"], c.image)
    supervisor_coder.reserve(c.home, c.vault, ROOT, c.policy, c.image)
    stage = supervisor_coder.CoderStage(c.home, c.vault, ROOT, c.policy)
    with supervisor._locked(c.home) as (parent, size, marker):
        records, used = supervisor._records(parent, size, marker)
    monkeypatch.setattr(
        supervisor,
        "METADATA_BYTES",
        used + metadata_pending(records) + supervisor.EMERGENCY_BYTES,
    )
    first, _ = c.batch_facts()
    c.deliver("cancel", first["token"], 1)
    with pytest.raises(InterruptedError):
        stage.check()
    assert (
        stage.command([sys.executable, "-c", "pass"], maintenance=True)["exit_code"]
        == 0
    )
    with supervisor._locked(c.home) as (parent, size, marker):
        records, _ = supervisor._records(parent, size, marker)
    assert any(r.get("phase") == "client-stopped" for r in records)
