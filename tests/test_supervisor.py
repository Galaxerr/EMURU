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
    scope = module["main"].__globals__
    real_os = scope["os"]
    before = durable(home)
    monkeypatch.setitem(
        scope,
        "os",
        SimpleNamespace(umask=real_os.umask, getuid=lambda: 0, getgid=lambda: 0),
    )
    with pytest.raises(ValueError, match="unprivileged owner"):
        module["main"]()
    assert durable(home) == before
    monkeypatch.setitem(
        scope,
        "os",
        SimpleNamespace(umask=real_os.umask, getuid=lambda: 1000, getgid=lambda: 1000),
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


@pytest.mark.parametrize("length", [4096, 4097])
def test_receipt_boundary_status_never_changes_durable_bytes(enrollment, length):
    home, policy, grant, card = enrollment
    supervisor.enroll(home, home, ROOT, "coder", policy, card.id, "sha256:" + "a" * 64)
    receipt = home / "emuru-projects/supervisor/00000002.json"
    data = receipt.read_bytes()
    receipt.write_bytes(data + b" " * (length - len(data)))
    before = durable(home)
    if length == 4096:
        assert (
            supervisor.status(home, ROOT, grant["project"], card)["execution"]
            == "unstarted"
        )
    else:
        with pytest.raises(ValueError, match="receipt exceeds 4 KiB"):
            supervisor.status(home, ROOT, grant["project"], card)
    assert durable(home) == before


@pytest.mark.parametrize(
    "counter,ceiling",
    [
        ("coder_runs", 2),
        ("reviewer_runs", 2),
        ("scout_runs", 1),
        ("active_seconds", 1920),
        ("coder_iterations", 24),
        ("reviewer_iterations", 8),
        ("checks", 8),
        ("output_bytes", 655360),
        ("artifact_bytes", 17891328),
    ],
)
def test_project_totals_accept_boundary_refuse_overflow_and_ignore_other_projects(
    counter,
    ceiling,
):
    from emuru.projects.supervisor_scout import reserved

    first = dict.fromkeys(supervisor.COUNTERS, 0)
    first[counter] = ceiling - 1
    last = {**dict.fromkeys(supervisor.COUNTERS, 0), counter: 1}
    records = [
        {
            "kind": "coder-stage",
            "phase": "admitted",
            "project": "project",
            "reserved": first,
        },
        {
            "kind": "reviewer-stage",
            "phase": "admitted",
            "project": "project",
            "reserved": last,
        },
        {"kind": "stage", "phase": "admitted", "project": "other", "reserved": last},
    ]
    assert reserved(records, "project")[counter] == ceiling
    records.append(
        {"kind": "stage", "phase": "admitted", "project": "project", "reserved": last}
    )
    with pytest.raises(ValueError, match="approved ceiling"):
        reserved(records, "project")


def test_continuation_binding_upgrade_preserves_legacy_bytes_and_suffix(
    enrollment, monkeypatch
):
    from emuru import deployment
    from emuru.projects import supervisor_lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, image="sha256:" + "a" * 64
    )
    folder = home / "emuru-projects"
    before = {p.name: p.read_bytes() for p in (folder / "supervisor").iterdir()}
    old_marker = (folder / "supervisor-enabled.json").read_bytes()
    monkeypatch.setattr(supervisor_lifecycle, "inspect_idle", lambda *_: None)
    result = deployment.upgrade_continuation(home, ROOT)
    assert result["state_changed"] is True
    backup = json.loads(Path(result["backup"]).read_bytes())
    assert backup["marker"] == json.loads(old_marker)
    assert backup["files"] == {k: v.decode() for k, v in before.items()}
    assert all((folder / "supervisor" / k).read_bytes() == v for k, v in before.items())
    with supervisor._locked(home, write=True) as (parent, size, marker):
        records, size = supervisor._records(parent, size, marker)
        assert marker["schema"] == 2
        event, _ = supervisor._append(
            folder / "supervisor",
            records,
            size,
            {"kind": "sleep-fence", "active": True},
        )
        assert event["schema"] == 2
    assert supervisor_lifecycle.snapshot(home)[-1]["active"] is True
    assert deployment.upgrade_continuation(home, ROOT)["state_changed"] is False


@pytest.mark.parametrize("checkpoint", ["backup", "recovery", "published"])
def test_continuation_binding_upgrade_interruption_resumes(
    enrollment, monkeypatch, checkpoint
):
    from emuru import deployment
    from emuru.projects import supervisor_lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, image="sha256:" + "a" * 64
    )
    folder = home / "emuru-projects"
    old_marker = (folder / "supervisor-enabled.json").read_bytes()
    monkeypatch.setattr(supervisor_lifecycle, "inspect_idle", lambda *_: None)
    deployment.upgrade_continuation(home, ROOT)
    if checkpoint != "published":
        (folder / "supervisor-enabled.json").write_bytes(old_marker)
    if checkpoint == "backup":
        (folder / "continuation-upgrade.json").unlink()
    assert deployment.upgrade_continuation(home, ROOT)["state_changed"] is (
        checkpoint != "published"
    )
    assert supervisor_lifecycle.snapshot(home)


def test_continuation_binding_upgrade_refuses_active_and_old_executor(
    enrollment, monkeypatch, tmp_path
):
    from emuru import deployment
    from emuru.projects import supervisor_lifecycle

    home, policy, _grant, card = enrollment
    supervisor.enroll(
        home, home, ROOT, "coder", policy, card.id, image="sha256:" + "a" * 64
    )
    monkeypatch.setattr(
        supervisor_lifecycle,
        "inspect_idle",
        lambda *_: (_ for _ in ()).throw(ValueError("active worker")),
    )
    before = {
        p: p.read_bytes() for p in (home / "emuru-projects").rglob("*") if p.is_file()
    }
    with pytest.raises(ValueError, match="active worker"):
        deployment.upgrade_continuation(home, ROOT)
    assert all(p.read_bytes() == data for p, data in before.items())
    monkeypatch.setattr(supervisor_lifecycle, "inspect_idle", lambda *_: None)
    deployment.upgrade_continuation(home, ROOT)
    old = tmp_path / "old"
    (old / "src/emuru/projects").mkdir(parents=True)
    (old / "src/emuru/projects/supervisor_lifecycle.py").write_text(
        "FENCE_SCHEMA = 1\n"
    )
    (old / "src/emuru/projects/supervisor.py").write_text("STATE_SCHEMA = 1\n")
    with pytest.raises(ValueError, match="Continuation state incompatible"):
        deployment.check_state(home, target_root=old)


def test_continuation_binding_upgrade_empty_profile_without_native_database(
    tmp_path, monkeypatch
):
    from emuru import deployment
    from emuru.projects import supervisor_lifecycle

    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(
        supervisor_lifecycle,
        "inspect_idle",
        lambda *_: pytest.fail("Empty profile has no board to read"),
    )
    result = deployment.upgrade_continuation(home, ROOT)
    assert result["state_changed"] is True
    assert not (home / "emuru-projects/kanban.db").exists()
    assert supervisor_lifecycle.snapshot(home) == []
