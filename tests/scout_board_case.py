"""Disposable pinned-native scout acceptance; synthetic M2 cards, no real vault."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/emuru/src")
sys.path.insert(0, "/opt/hermes")

from emuru.projects.board import project_board
from emuru.projects.scout import digest, encoded
from emuru.projects.scout_board import claim, finish, status

ROOT = Path("/opt/emuru")


def refused(call):
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError("Expected scout refusal")


def run(home, action):
    os.umask(0o077)
    saved = home / "emuru-projects/scout-acceptance.json"
    if action == "reopen":
        state = json.loads(saved.read_text())
        with project_board(home, ROOT) as (kb, db):
            report = status(kb, db, kb.get_task(db, state["completed"]), home)
            assert report["status"] == "done"
            assert report["evidence"]["sha256"] == state["digest"]
            assert kb.get_task(db, state["source"]).status == "blocked"
            assert (
                status(kb, db, kb.get_task(db, state["uncertain"]), home)["status"]
                == "uncertain"
            )
        print("Scout board persistence / interrupted no-replay: PASS")
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with project_board(home, ROOT) as (kb, db):
        source = kb.create_task(
            db,
            title="Synthetic tracked task",
            body=json.dumps(
                {
                    "schema": 1,
                    "revision": "a" * 64,
                    "approval": "b" * 64,
                    "proposal": "c" * 32,
                    "task": {
                        "id": "inspect",
                        "description": "Synthetic",
                        "acceptance": [],
                    },
                }
            ),
            tenant="d" * 64,
            created_by="emuru-owner",
            initial_status="blocked",
            project_id="",
            workspace_kind="scratch",
        )
    if action == "source":
        print(json.dumps({"source": source}))
        return
    report = {
        "schema": 1,
        "repository": "sample",
        "baseline": "e" * 40,
        "files": [{"path": "app.py", "sha256": "f" * 64}],
    }
    grant = {
        "repository_id": "a" * 64,
        "source_task": source,
        "repository": "sample",
        "baseline": "e" * 40,
        "files": ["app.py"],
        "timeout_seconds": 10,
        "report_sha256": digest(report),
    }
    refused(lambda: claim(home, ROOT, {**grant, "source_task": "t_000000000000"}))
    claimed = claim(home, ROOT, grant)
    assert claimed["status"] == "claimed" and claimed["run"]
    assert claim(home, ROOT, grant)["status"] == "uncertain"
    request = {"task": claimed["task"], "run": claimed["run"], "report": report}
    refused(lambda: finish(home, ROOT, {**request, "run": claimed["run"] + 1}))
    refused(lambda: finish(home, ROOT, {**request, "report": {"forged": True}}))
    completed = finish(home, ROOT, request)
    assert completed["status"] == "done"
    assert completed["evidence"]["sha256"] == digest(report)
    assert claim(home, ROOT, grant) == completed
    refused(lambda: finish(home, ROOT, request))
    artifact = home / "emuru-projects/scout-artifacts" / (digest(report) + ".json")
    artifact.write_bytes(b"{}")
    refused(lambda: claim(home, ROOT, grant))
    artifact.unlink()
    artifact.symlink_to(home / "outside-secret")
    refused(lambda: claim(home, ROOT, grant))
    artifact.unlink()
    artifact.write_bytes(encoded(report))
    artifact.chmod(0o600)
    with project_board(home, ROOT) as (kb, db):
        assert kb.get_task(db, source).status == "blocked"
        assert len(kb.list_attachments(db, claimed["task"])) == 1
        assert len(kb.list_runs(db, claimed["task"])) == 1
    # Failure closes only its exact run, with no verified-completion claim.
    failed = claim(home, ROOT, {**grant, "timeout_seconds": 11})
    assert failed["status"] == "claimed"
    blocked = finish(home, ROOT, {"task": failed["task"], "run": failed["run"]})
    assert blocked["status"] == "blocked"
    assert claim(home, ROOT, {**grant, "timeout_seconds": 11})["status"] == "blocked"
    assert claim(home, ROOT, {**grant, "timeout_seconds": 12})["status"] == "blocked"
    # Explicit operator reconciliation in this synthetic fixture: no worker ever spawned.
    with project_board(home, ROOT) as (kb, db):
        kb.archive_task(db, failed["task"])
    # Simulate death after durable claim: a changed grant cannot bypass ownership.
    interrupted = claim(home, ROOT, {**grant, "timeout_seconds": 12})
    assert interrupted["status"] == "claimed"
    assert claim(home, ROOT, {**grant, "timeout_seconds": 13})["status"] == "uncertain"
    assert claim(home, ROOT, grant) == completed
    saved.write_text(
        json.dumps(
            {
                "source": source,
                "completed": claimed["task"],
                "uncertain": interrupted["task"],
                "digest": digest(report),
            }
        )
    )
    saved.chmod(0o600)
    print("Scout native claims / evidence / replay / run fencing: PASS")


if __name__ == "__main__":
    run(Path(sys.argv[2]), sys.argv[1])
