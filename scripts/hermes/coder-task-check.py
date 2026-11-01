"""Run one synthetic native task through Ticket 2 using the pinned image."""

import argparse
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _docker(arguments):
    return subprocess.run(
        ["docker", *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def _runtime(image, folder):
    container = _docker(["create", image])
    native = folder / "native-runtime"
    native.mkdir(mode=0o700)
    try:
        for source, target in (
            ("/opt/hermes/.venv/lib/python3.14/site-packages", folder / "hermes-site"),
            ("/opt/emuru/.venv/lib/python3.14/site-packages", folder / "emuru-site"),
        ):
            target.mkdir(mode=0o700, exist_ok=True)
            _docker(["cp", f"{container}:{source}", str(target)])
        archive = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "cd /opt/hermes && tar -cf - --exclude=.venv --exclude=.git --exclude=node_modules --exclude=__pycache__ .",
            ],
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(native, filter="data")
    finally:
        _docker(["rm", container])
    sys.path[:0] = [
        str(folder / "emuru-site" / "site-packages"),
        str(folder / "hermes-site" / "site-packages"),
        str(native),
    ]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def run(image):
    os.umask(0o077)
    root = Path(
        tempfile.mkdtemp(prefix="coder-task-acceptance-", dir=ROOT / ".runtime")
    )
    root.chmod(0o700)
    _runtime(image, root)
    from emuru.projects import board, coder_board
    from emuru.projects.coder import LIMITS
    from emuru.projects.scout import digest, git
    from emuru.projects.spec import spec_markdown
    from emuru.telegram.queue import TelegramQueue, load_telegram_settings
    from emuru.vault import mutations

    home = root / "profile"
    home.mkdir(mode=0o700)
    vault = root / "vault"
    vault.mkdir(mode=0o700)
    for name in ("10_Projects", "99_System", "30_Resources"):
        (vault / name).mkdir()
    (home / "vault-target.json").write_text(
        json.dumps({"mode": "real", "vault_path": str(vault)})
    )
    (home / "mutation-policy.json").write_text(
        json.dumps(
            {"enabled": True, "roots": ["10_Projects"], "vault_path": str(vault)}
        )
    )
    for name in ("vault-target.json", "mutation-policy.json"):
        (home / name).chmod(0o600)
    queue = TelegramQueue(
        home / "emuru-telegram/queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        42,
        clock=lambda: 100000,
    )
    update_id = 0

    def owner_request(text):
        nonlocal update_id
        update_id += 1
        queue.stage(
            7,
            [
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id,
                        "date": 100000,
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": text,
                    },
                }
            ],
            "emuru",
        )
        claim = queue.claim(7, "emuru", session_key=lambda _payload: "owner")
        assert claim and claim["update_id"] == update_id
        request, _ = mutations.begin_request(
            home, vault, 42, 7, update_id, text, fresh=True
        )
        mutations.activate_request(home, request, queue.path, 7, update_id)
        return request, claim

    def project_command(text, owner=42, bot=7):
        request, claim = owner_request(text)
        try:
            queue.mark_tools_started(7, update_id)
            return board.project_command(home, vault, ROOT, text, owner, bot)
        finally:
            mutations.finish_request(home, request)
            queue.finish(claim["sequence"], True)

    task = {
        "id": "sum-prices",
        "description": "Implement sum_prices as the sum of input prices",
        "acceptance": [
            {"command": "python -m unittest", "expected_result": "Zero exit"}
        ],
    }
    spec_id = "coder-acceptance"
    spec = {
        "spec_id": spec_id,
        "goal": "Qualify one bounded native coding task",
        "repository": None,
        "non_goals": ["No automatic project completion"],
        "constraints": [],
        "tasks": [task],
        "security_restrictions": ["No network"],
        "unresolved_decisions": [],
    }
    content = (
        "---\ntitle: Coder\ntype: project\nsummary: Synthetic acceptance\n---\n"
        + spec_markdown(spec)
    )
    request, claim = owner_request("Prepare approved coder spec")
    try:
        preview = mutations.propose_note(
            home,
            vault,
            "10_Projects/Coder.md",
            content=content,
            authenticated_id=request,
        )
    finally:
        mutations.finish_request(home, request)
        queue.finish(claim["sequence"], True)
    request, claim = owner_request(
        "/apply " + preview["proposal_id"] + " writers-paused"
    )
    try:
        mutations.record_preview(
            home, preview["proposal_id"], preview["approval_digest"], complete=True
        )
        queue.mark_tools_started(7, update_id)
        mutations.apply_proposal(
            home,
            vault,
            preview["proposal_id"],
            preview["approval_digest"],
            writers_paused=True,
            owner=42,
            bot=7,
            application_request=request,
        )
    finally:
        mutations.finish_request(home, request)
        queue.finish(claim["sequence"], True)
    project_command("/project track " + preview["proposal_id"])
    project = board._digest(["42", "7", os.path.abspath(vault), preview["path"]])
    with board.project_board_readonly(home, ROOT) as (kb, db):
        cards = kb.list_tasks(db, tenant=project, include_archived=True)
        assert len(cards) == 1
        task_id = cards[0].id
        original = kb.get_task(db, task_id)
        original_key, card_body = original.idempotency_key, original.body
    repo = root / "repository"
    repo.mkdir(mode=0o700)
    (repo / "sum_prices.py").write_text(
        "def sum_prices(prices):\n    return 0\n", encoding="utf-8"
    )
    (repo / "test_sum_prices.py").write_text(
        "import unittest\nfrom sum_prices import sum_prices\n\n"
        "class Prices(unittest.TestCase):\n"
        "    def test_positive(self):\n        self.assertEqual(sum_prices([1, 2, 3]), 6)\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "sum_prices.py", "test_sum_prices.py")
    _git(
        repo,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "synthetic coder baseline",
    )
    baseline = git(repo, "rev-parse", "HEAD").decode().strip()
    approved = mutations.approved_project(home, vault, preview["proposal_id"], 42, 7)
    proposal = approved["proposal_id"]
    approval = approved["approval_digest"]
    revision = approved["after_revision"]
    grant = {
        "schema": 1,
        "grant_id": uuid.uuid4().hex,
        "owner": 42,
        "bot": 7,
        "vault": digest(os.path.abspath(vault)),
        "project": project,
        "proposal": proposal,
        "approval": approval,
        "revision": revision,
        "task": task_id,
        "task_digest": digest(task),
        "repository": "synthetic-coder-acceptance",
        "path": str(repo),
        "baseline": baseline,
        "read_files": ["sum_prices.py", "test_sum_prices.py"],
        "write_files": ["sum_prices.py"],
        "create_files": [],
        "check_ids": ["python_unittest"],
        "catalogue": coder_board.catalogue_digest(),
        "network": "none",
        "limits": dict(LIMITS),
    }
    policy = root / "coder-grant.json"
    policy.write_text(json.dumps(grant, sort_keys=True), encoding="utf-8")
    policy.chmod(0o600)
    receipt = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"](
        home, vault, policy, task_id, ROOT
    )
    assert receipt["status"] == "tested local result / awaiting owner review"
    assert git(repo, "rev-parse", "HEAD").decode().strip() == baseline
    with board.project_board_readonly(home, ROOT) as (kb, db):
        card = kb.get_task(db, task_id)
        assert card.status == "done" and card.idempotency_key == original_key
        assert card.body == card_body
        report = coder_board.status(kb, db, card, home, ROOT)
        assert report["status"] == receipt["status"]
        ids = tuple(item.id for item in kb.list_attachments(db, task_id))
        identity = (card.current_run_id, ids)
        evidence_sha256 = json.loads(card.result)["evidence_sha256"]
        diff_sha256 = json.loads(card.result)["diff_sha256"]
    try:
        project_command("/project status " + proposal, owner=43)
    except ValueError:
        pass
    else:
        raise AssertionError("Unauthenticated coder status was exposed.")
    status = project_command("/project status " + proposal)
    assert f"{task_id} (sum-prices): coder {receipt['status']}" in status
    assert evidence_sha256 in status and diff_sha256 in status
    repeated = runpy.run_path(str(ROOT / "scripts/hermes/coder.py"))["launch"](
        home, vault, policy, task_id, ROOT
    )
    assert repeated == {"status": receipt["status"], "evidence": receipt}
    with board.project_board_readonly(home, ROOT) as (kb, db):
        card = kb.get_task(db, task_id)
        assert (
            card.current_run_id,
            tuple(item.id for item in kb.list_attachments(db, task_id)),
        ) == identity
    queue.close()
    (root / "receipt.json").write_text(json.dumps(receipt, sort_keys=True))
    for name in ("native-runtime", "hermes-site", "emuru-site"):
        shutil.rmtree(root / name)
    print(f"Ticket 2 native task acceptance: PASS ({root})", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="Verified current-source image digest"
    )
    run(parser.parse_args().image)
