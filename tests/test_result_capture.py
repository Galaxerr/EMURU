"""Capture formatting preserves the original task and unrelated note bytes."""

import pytest

from emuru.projects import results


def test_capture_preserves_task_checkbox_and_block_id():
    note = "# Project\r\n- [x] Task ^task-sample-first\r\n  - Human detail\r\n"
    anchor = "- [x] Task ^task-sample-first"
    after = results.format_capture(note, anchor, "Verified artifact-only result.")
    assert (
        after
        == "# Project\r\n- [x] Task ^task-sample-first\r\n  - Verified artifact-only result.\r\n  - Human detail\r\n"
    )


@pytest.mark.parametrize("anchor", ["", "- [ ] Missing", "- [x] Task\nInjected"])
def test_capture_refuses_invalid_anchor(anchor):
    with pytest.raises(ValueError):
        results.format_capture("- [x] Task\n", anchor, "Verified result.")


def test_missing_real_attempt_cannot_be_captured(tmp_path):
    with pytest.raises(ValueError, match="No verified coding attempt"):
        results.verify_completion(tmp_path, tmp_path, tmp_path, "a" * 32, 1, 2, 42, 7)


def test_capture_flag_cannot_authorize_execution(tmp_path):
    from emuru.projects import coder_board

    with pytest.raises(ValueError, match="cannot authorize execution"):
        coder_board.authorize(tmp_path, tmp_path, tmp_path, 1, {}, capture=True)


@pytest.fixture
def capture_scope(tmp_path):
    from test_proposal_status import scope

    yield from scope.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("incomplete", None),
        ("coder_run", True),
        ("review_run", 1),
        ("evidence_sha256", "unverified"),
        ("vault", "relative/vault"),
        ("schema", 2),
        ("extra", "unsupported"),
        ("completion_key", "a" * 64),
        ("owner", "43"),
        ("bot", "8"),
        ("vault", "/other-vault"),
    ],
)
def test_capture_refuses_incomplete_binding_without_persisting(
    capture_scope, field, value
):
    import hashlib

    from vault_helpers import capture_binding

    from emuru.projects.scout import digest, encoded
    from emuru.vault import mutations as m

    home, vault, _, request = capture_scope
    path = "10_Projects/incomplete.md"
    before = "- [ ] Task ^task-sample-first\n"
    (vault / path).write_text(before)
    database = home / "emuru-mutations/mutations.sqlite"
    retained = database.read_bytes()
    binding = capture_binding(vault)
    if field == "incomplete":
        binding = {"schema": 1}
    elif field != "completion_key":
        binding[field] = value
    key = value if field == "completion_key" else digest(binding)
    with pytest.raises(ValueError, match="capture metadata"):
        m.prepare_result_capture(
            home,
            vault,
            request,
            key,
            encoded(binding).decode(),
            path,
            hashlib.sha256(before.encode()).hexdigest(),
            before.strip(),
            results.format_capture(before, before.strip(), "Verified result."),
        )
    assert database.read_bytes() == retained
    assert (vault / path).read_text() == before


def test_atomic_capture_rollback_and_concurrent_reuse(capture_scope, monkeypatch):
    import hashlib
    from concurrent.futures import ThreadPoolExecutor

    from vault_helpers import capture_binding

    from emuru.projects.scout import digest, encoded
    from emuru.vault import mutations as m

    home, vault, _, request = capture_scope
    binding = capture_binding(vault)
    path = "10_Projects/atomic.md"
    before = "- [ ] Task ^task-sample-first\n"
    (vault / path).write_text(before)
    after = results.format_capture(
        before, before.strip(), "Verified artifact-only result."
    )
    arguments = (
        home,
        vault,
        request,
        digest(binding),
        encoded(binding).decode(),
        path,
        hashlib.sha256(before.encode()).hexdigest(),
        before.strip(),
        after,
    )
    original = m._preview

    def interrupted(*args):
        raise ValueError("Injected pre-commit interruption")

    monkeypatch.setattr(m, "_preview", interrupted)
    with pytest.raises(ValueError, match="pre-commit"):
        m.prepare_result_capture(*arguments)
    monkeypatch.setattr(m, "_preview", original)
    with m._read_database(home / "emuru-mutations/mutations.sqlite") as db:
        assert (
            db.execute("SELECT count(*) FROM project_result_captures").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        previews = list(
            pool.map(lambda _: m.prepare_result_capture(*arguments), range(2))
        )
    assert previews[0]["proposal_id"] == previews[1]["proposal_id"]
    assert (vault / path).read_text() == before
    with m._read_database(home / "emuru-mutations/mutations.sqlite") as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    from telegram_helpers import update

    old_identity = previews[0]["proposal_id"]
    m.cancel_proposal(home, old_identity)
    queue = capture_scope[2]
    queue.finish(1, True)
    queue.stage(7, [update(2, "renew capture")])
    queue.claim(7)
    renewal, _ = m.begin_request(home, vault, 42, 7, 2, "renew capture", fresh=True)
    m.activate_request(home, renewal, queue.path, 7, 2)
    renewed_arguments = (*arguments[:2], renewal, *arguments[3:])

    def interrupted_renewal(db, identity):
        if identity != old_identity:
            raise ValueError("Injected renewal pre-commit interruption")
        return original(db, identity)

    monkeypatch.setattr(m, "_preview", interrupted_renewal)
    with pytest.raises(ValueError, match="renewal pre-commit"):
        m.prepare_result_capture(*renewed_arguments, renew=True)
    monkeypatch.setattr(m, "_preview", original)
    with m._read_database(home / "emuru-mutations/mutations.sqlite") as db:
        assert db.execute(
            "SELECT generation,state FROM project_result_captures"
        ).fetchall() == [(1, "active")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        renewed = list(
            pool.map(
                lambda _: m.prepare_result_capture(*renewed_arguments, renew=True),
                range(2),
            )
        )
    assert renewed[0]["proposal_id"] == renewed[1]["proposal_id"] != old_identity
    m.cancel_proposal(home, renewed[0]["proposal_id"])
    assert (
        m.prepare_result_capture(*renewed_arguments, renew=True)["outcome"]
        == "cancelled"
    )
    with m._read_database(home / "emuru-mutations/mutations.sqlite") as db:
        assert db.execute(
            "SELECT generation,state FROM project_result_captures ORDER BY generation"
        ).fetchall() == [(1, "closed"), (2, "active")]
    assert (vault / path).read_text() == before
    # A newer formatter must be retained, never silently interpreted or migrated.
    with m.journal(home) as db:
        db.execute("UPDATE project_result_captures SET formatter_version=2")
        db.commit()
    database = home / "emuru-mutations/mutations.sqlite"
    retained = database.read_bytes()
    with (
        pytest.raises(ValueError, match="Unsupported capture metadata"),
        m.journal(home),
    ):
        pass
    assert database.read_bytes() == retained


def test_operator_subprocess_refuses_missing_evidence(tmp_path, monkeypatch):
    import sys

    from emuru.hermes import profile

    runtime = tmp_path / "runtime"
    (runtime / ".venv/bin").mkdir(parents=True)
    (runtime / ".venv/bin/python").symlink_to(sys.executable)
    monkeypatch.setattr(profile, "installed_runtime", lambda root: runtime)
    binding = {
        "spec": "a" * 32,
        "task": "t_123abc",
        "review_run": 2,
        "owner": "42",
        "bot": "7",
    }
    with pytest.raises(ValueError, match="Pinned-runtime capture verification refused"):
        results.operator_verification(
            tmp_path / "missing-profile", tmp_path / "missing-vault", binding
        )
    assert not (tmp_path / "missing-profile").exists()
    assert not (tmp_path / "missing-vault").exists()


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "incomplete",
        "completion_key",
        "generation",
        "formatter_version",
        "owner",
        "bot",
        "vault",
        "note_revision",
        "anchor",
        "request_id",
    ],
)
def test_capture_status_is_observational_and_owner_bound(
    capture_scope, monkeypatch, corruption
):
    import hashlib
    from pathlib import Path

    from telegram_helpers import update
    from vault_helpers import capture_binding

    from emuru.projects.scout import digest, encoded
    from emuru.vault import mutations as m

    home, vault, queue, request = capture_scope
    before = "- [ ] Task ^task-sample-first\n"
    path = "10_Projects/status.md"
    (vault / path).write_text(before)
    binding = capture_binding(vault)
    preview = m.prepare_result_capture(
        home,
        vault,
        request,
        digest(binding),
        encoded(binding).decode(),
        path,
        hashlib.sha256(before.encode()).hexdigest(),
        before.strip(),
        results.format_capture(
            before, before.strip(), "Verified artifact-only result."
        ),
    )
    text = "/project capture-status " + "a" * 32 + " t_123abc 2"
    queue.finish(1, True)
    queue.stage(7, [update(2, text)])
    queue.claim(7)
    status_request, _ = m.begin_request(home, vault, 42, 7, 2, text, fresh=True)
    m.activate_request(home, status_request, queue.path, 7, 2)
    database = home / "emuru-mutations/mutations.sqlite"
    if corruption:
        import sqlite3

        with sqlite3.connect(database) as db:
            if corruption == "incomplete":
                db.execute("UPDATE project_result_captures SET binding='{}'")
            elif corruption in {"owner", "bot", "vault"}:
                binding[corruption] = "/other-vault" if corruption == "vault" else "8"
                db.execute(
                    "UPDATE project_result_captures SET binding=?,completion_key=?",
                    (encoded(binding).decode(), digest(binding)),
                )
            elif corruption in {"note_revision", "anchor", "request_id"}:
                db.execute(f"UPDATE project_result_captures SET {corruption}='wrong'")
            else:
                db.execute(
                    f"UPDATE project_result_captures SET {corruption}=?",
                    ("0" * 64 if corruption == "completion_key" else 0,),
                )
    retained = database.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Status must not verify, recover, expire or apply")

    monkeypatch.setattr(results, "verify_completion", forbidden)
    monkeypatch.setattr(m, "expire_proposals", forbidden)
    monkeypatch.setattr(m, "recover_proposals", forbidden)
    root = Path(__file__).resolve().parents[1]
    if corruption:
        with pytest.raises(ValueError, match="Unsupported capture metadata"):
            results.command(home, vault, root, text, 42, 7)
        with (
            pytest.raises(ValueError, match="Unsupported capture metadata"),
            m.journal(home),
        ):
            pass
        assert database.read_bytes() == retained
        assert (vault / path).read_text() == before
        return
    assert results.command(home, vault, root, text, 42, 7) == {
        "proposal_id": preview["proposal_id"],
        "path": path,
        "outcome": "proposed",
    }
    with pytest.raises(ValueError, match="Explicit authenticated owner"):
        results.command(home, vault, root, text, 43, 7)
    assert database.read_bytes() == retained
    assert (vault / path).read_text() == before
