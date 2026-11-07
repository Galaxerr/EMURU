"""Operator enrollment and observational supervisor status; disposable profiles only."""

import json
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from project_board_case import SPEC

from emuru.projects import board, coder_board, supervisor
from emuru.projects.coder_runtime import qualification_fixture
from emuru.projects.scout import digest
from emuru.projects.spec import spec_markdown

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    _, grant = qualification_fixture(tmp_path / "fixture")
    home = tmp_path / "profile"
    (home / "emuru-projects").mkdir(mode=0o700, parents=True)
    home.chmod(0o700)
    (home / "emuru-projects/scout.lock").touch(mode=0o600)
    grant["project"] = board._digest(
        [str(grant["owner"]), str(grant["bot"]), str(home), "10_Projects/Test.md"]
    )
    policy = tmp_path / "grant.json"
    policy.write_text(json.dumps(grant))
    policy.chmod(0o600)
    card = SimpleNamespace(
        id=grant["task"],
        tenant=grant["project"],
        current_run_id=None,
        status="blocked",
        assignee=None,
        created_by="emuru-owner",
        scout_children=[],
        body=json.dumps(
            {
                "schema": 1,
                "proposal": grant["proposal"],
                "revision": grant["revision"],
                "approval": grant["approval"],
                "task": SPEC["tasks"][0],
            }
        ),
    )

    class Native:
        def get_task(self, _db, task):
            assert task == card.id
            return card

        def list_tasks(self, _db, **_kwargs):
            return [card, *card.scout_children]

    @contextmanager
    def readonly(*_):
        yield Native(), None

    monkeypatch.setattr(board, "project_board_readonly", readonly)
    monkeypatch.setattr(coder_board, "authorize", lambda *a, **kw: grant)
    monkeypatch.setattr(coder_board, "ensure_idle", lambda *a, **kw: None)
    monkeypatch.setattr(supervisor, "boot_id", lambda: "boot-one")
    monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: 1000000000)
    return home, policy, grant, card


@pytest.fixture
def scout_enrollment(enrollment, monkeypatch):
    home, policy, grant, card = enrollment
    preview = {
        "path": "10_Projects/Test.md",
        "current": True,
        "after_revision": grant["revision"],
        "approval_digest": grant["approval"],
        "content": spec_markdown(SPEC),
    }
    monkeypatch.setattr(
        supervisor.mutations, "approved_project", lambda *a, **kw: preview
    )
    scout = policy.with_name("scout-policy.json")
    scout.write_text(
        json.dumps(
            {
                "schema": 1,
                "repository": grant["repository"],
                "path": grant["path"],
                "baseline": grant["baseline"],
                "files": grant["read_files"],
                "tasks": [card.id],
                "timeout_seconds": 120,
            }
        )
    )
    scout.chmod(0o600)
    return home, policy, grant, card, scout


def test_scout_then_coder_enrollment_share_project_totals(scout_enrollment):
    home, policy, grant, card, scout = scout_enrollment
    supervisor.enroll(
        home,
        home,
        ROOT,
        "scout",
        scout,
        card.id,
        "sha256:" + "a" * 64,
        owner=grant["owner"],
        bot=grant["bot"],
    )
    zero = (home / "emuru-projects/supervisor/00000001.json").read_bytes()
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    assert supervisor.status(home, ROOT, grant["project"], card)["stage"] == "coder"
    assert (home / "emuru-projects/supervisor/00000001.json").read_bytes() == zero


@pytest.mark.parametrize("native_state", ["done", "archived", "blocked", "running"])
@pytest.mark.parametrize("suffix", ["", "/", "/./"])
def test_attempted_legacy_scout_cannot_be_enrolled_as_unstarted(
    scout_enrollment, native_state, suffix
):
    home, _policy, grant, card, scout = scout_enrollment
    policy = json.loads(scout.read_text())
    policy["path"] += suffix
    scout.write_text(json.dumps(policy))
    prior = {
        "repository": grant["repository"],
        "baseline": grant["baseline"],
        "files": grant["read_files"],
        "timeout_seconds": 120,
        "source_task": card.id,
        "repository_id": digest([grant["repository"], grant["path"]]),
        "report_sha256": "a" * 64,
    }
    card.scout_children.append(
        SimpleNamespace(
            body=json.dumps({"schema": 1, "scout": prior}), status=native_state
        )
    )
    before = durable(home)
    with pytest.raises(ValueError, match="already attempted"):
        supervisor.enroll(
            home,
            home,
            ROOT,
            "scout",
            scout,
            card.id,
            "sha256:" + "a" * 64,
            owner=grant["owner"],
            bot=grant["bot"],
        )
    assert durable(home) == before


def durable(home):
    return {
        str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()
    }


def test_operator_enroll_then_status_survives_restart_without_writes(enrollment):
    home, policy, grant, card = enrollment
    registered = supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64
    )
    before = durable(home)
    result = supervisor.status(home, ROOT, grant["project"], card)
    assert result["authority"] == "enrolled"
    assert result["deadline_ns"] == 86401000000000
    assert result["generation"] == registered["generation"]
    assert len(result["token"]) == 64
    assert result["reserved"]["coder_runs"] == 0
    assert result["provider_usage"] == "unavailable"
    assert result["execution"] == "unstarted"
    assert result["stop_proof"] == "unavailable"
    assert durable(home) == before
    with pytest.raises(ValueError, match="adapter unavailable"):
        supervisor.refuse_unsupported(home)
    assert len(supervisor.projection(home, ROOT, grant["project"], card)) <= 3500


def test_never_opted_in_launch_preserves_legacy_profile(tmp_path):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    supervisor.refuse_unsupported(home)
    assert list(home.iterdir()) == []


@pytest.mark.parametrize(
    "clock,boot", [(86401000000000, "boot-one"), (1000000000, "boot-two")]
)
def test_restart_deadline_or_reboot_expires_without_writing(
    enrollment, monkeypatch, clock, boot
):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    before = durable(home)
    monkeypatch.setattr(supervisor.time, "monotonic_ns", lambda: clock)
    monkeypatch.setattr(supervisor, "boot_id", lambda: boot)
    assert (
        supervisor.status(home, ROOT, grant["project"], card)["authority"] == "expired"
    )
    assert durable(home) == before


def test_enrollment_renewal_retains_initial_project_record(enrollment):
    home, policy, grant, card = enrollment
    supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64, seconds=60
    )
    zero = home / "emuru-projects/supervisor/00000001.json"
    initial = zero.read_bytes()
    token = supervisor.status(home, ROOT, grant["project"], card)["token"]
    supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64, seconds=30
    )
    result = supervisor.status(home, ROOT, grant["project"], card)
    assert result["token"] != token
    assert zero.read_bytes() == initial
    assert result["deadline_ns"] == 31000000000
    assert result["reserved"] == dict.fromkeys(supervisor.COUNTERS, 0)


@pytest.mark.parametrize(
    "change", ["missing", "symlink", "public", "oversize", "schema", "counter"]
)
def test_invalid_opted_in_state_never_downgrades(enrollment, change):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    path = home / "emuru-projects/supervisor/00000001.json"
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.rename(path.with_suffix(".old"))
        path.symlink_to(path.with_suffix(".old"))
    elif change == "public":
        path.chmod(0o644)
    elif change == "oversize":
        path.write_bytes(b"x" * 4097)
    else:
        value = json.loads(path.read_bytes())
        if change == "schema":
            value["schema"] = 999
        else:
            del value["reserved"]["checks"]
        path.write_text(json.dumps(value))
    before = durable(home)
    with pytest.raises((ValueError, OSError)):
        supervisor.status(home, ROOT, grant["project"], card)
    with pytest.raises(ValueError, match="adapter unavailable"):
        supervisor.refuse_unsupported(home)
    assert durable(home) == before


def test_changed_grant_and_attempted_enrollment_refuse_without_writes(enrollment):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    before = durable(home)
    grant["grant_id"] = "e" * 32
    policy.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match="another grant"):
        supervisor.enroll(
            home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64
        )
    assert durable(home) == before
    coder_board.attempt_path(home, grant).mkdir(parents=True)
    with pytest.raises(ValueError, match="Attempted work"):
        supervisor.enroll(
            home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64
        )


def test_metadata_admission_leaves_emergency_reserve(enrollment, monkeypatch):
    home, policy, _grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    before = durable(home)
    size = 4096 + sum(len(v) for k, v in before.items() if "/supervisor/" in k)
    # Exercise the public admission boundary with a narrowed storage ceiling.
    monkeypatch.setattr(supervisor, "METADATA_BYTES", size + 4096)
    with pytest.raises(ValueError, match="admission exhausted"):
        supervisor.enroll(
            home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64
        )
    assert durable(home) == before


def test_deleted_last_generation_refuses_instead_of_reverting_authority(enrollment):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    (home / "emuru-projects/supervisor/00000002.json").unlink()
    before = durable(home)
    with pytest.raises(ValueError, match="committed history missing"):
        supervisor.status(home, ROOT, grant["project"], card)
    assert durable(home) == before


def test_source_image_schema_mismatch_refuses_observationally(enrollment, monkeypatch):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    before = durable(home)
    actual = supervisor.identity
    monkeypatch.setattr(
        supervisor,
        "identity",
        lambda root, image: {**actual(root, image), "schema": 999},
    )
    with pytest.raises(ValueError, match="incompatible"):
        supervisor.status(home, ROOT, grant["project"], card)
    assert durable(home) == before


def test_operator_cli_enrolls_only_explicit_supported_arguments(
    enrollment, monkeypatch, capsys
):
    home, policy, _grant, card = enrollment
    module = runpy.run_path(str(ROOT / "scripts/hermes/supervisor.py"))
    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda _path: {
            "ensure_image": lambda **kwargs: ("source", "sha256:" + "a" * 64)
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "supervisor.py",
            "--home",
            str(home),
            "--vault",
            str(home),
            "--stage",
            "coder",
            "--policy",
            str(policy),
            "--task",
            card.id,
            "--seconds",
            "30",
        ],
    )
    assert module["main"]() == 0
    assert json.loads(capsys.readouterr().out)["deadline_ns"] == 31000000000
    before = durable(home)
    monkeypatch.setattr(sys, "argv", ["supervisor.py", "--stage", "execute"])
    with pytest.raises(SystemExit):
        module["main"]()
    assert durable(home) == before


@pytest.mark.parametrize(
    "entry", ["attempt", "claim", "scout", "review", "review_execute"]
)
def test_all_supervised_execution_entrances_refuse_before_native_calls(
    enrollment, entry
):
    from emuru.projects import reviewer, scout_board

    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    before = durable(home)
    calls = {
        "attempt": lambda: coder_board.create_attempt(home, grant),
        "claim": lambda: coder_board.claim(home, home, ROOT, card.id, grant, home),
        "scout": lambda: scout_board.claim(home, ROOT, {}),
        "review": lambda: reviewer.begin(home, home, ROOT, card.id, grant, home, {}),
        "review_execute": lambda: reviewer.execute(
            home, home, ROOT, card.id, grant, home
        ),
    }
    with pytest.raises(ValueError, match="adapter unavailable"):
        calls[entry]()
    assert durable(home) == before
