"""Bounded scout policy, Git snapshot, independent evidence and restart fences."""

import copy
import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

from emuru.projects import scout

ROOT = Path(__file__).resolve().parents[1]
RUNNER = runpy.run_path(str(ROOT / "scripts/hermes/scout.py"))


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("def sample():\n    return 42\n")
    (repo / "README.md").write_text("# Synthetic project\n")
    (repo / "hidden.key").write_text("SYNTHETIC_SECRET_MUST_NOT_APPEAR")
    (repo / "escape.py").symlink_to("/etc/passwd")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    baseline = scout.git(repo, "rev-parse", "HEAD").decode().strip()
    policy = {
        "schema": 1,
        "repository": "synthetic",
        "path": str(repo),
        "baseline": baseline,
        "files": ["README.md", "src/app.py"],
        "tasks": ["t_123abc"],
        "timeout_seconds": 10,
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    return repo, policy, path


def test_committed_snapshot_has_exact_baseline_and_no_source_disclosure(
    repository, tmp_path, monkeypatch
):
    repo, policy, path = repository
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "must-not-run")
    policy = scout.load_policy(path, "t_123abc")
    (repo / "src/app.py").write_text("uncommitted change\n")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    report = scout.snapshot(policy, destination)
    assert report == scout.inspect_snapshot(destination)
    assert report["baseline"] == policy["baseline"]
    assert report["files"][1]["lines"] == 2
    assert (destination / "1").read_text() == "def sample():\n    return 42\n"
    assert sorted(item.name for item in destination.iterdir()) == [
        "0",
        "1",
        "manifest.json",
    ]
    assert "SYNTHETIC_SECRET" not in json.dumps(report)
    assert "return 42" not in json.dumps(report)
    assert "uncommitted change" in (repo / "src/app.py").read_text()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/etc/passwd",
        "src/../app.py",
        "src//app.py",
        ".env",
        "src/.private.py",
        "secret.key",
        "-option",
        "a\n.py",
    ],
)
def test_forbidden_selections(name):
    with pytest.raises(ValueError, match="forbidden"):
        scout.selected_path(name)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(tasks=["t_other"]),
        lambda p: p.update(timeout_seconds=121),
        lambda p: p.update(timeout_seconds=True),
        lambda p: p.update(files=["README.md", "README.md"]),
        lambda p: p.update(baseline="HEAD"),
        lambda p: p.update(files=[]),
        lambda p: p.update(extra="unsupported"),
    ],
)
def test_invalid_or_ungranted_policy(repository, change):
    _, policy, path = repository
    change(policy)
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        scout.load_policy(path, "t_123abc")


def test_private_policy_and_repo_paths(repository, tmp_path):
    repo, policy, path = repository
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        scout.load_policy(path, "t_123abc")
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    policy["path"] = str(link)
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="symlink"):
        scout.load_policy(path, "t_123abc")


@pytest.mark.parametrize(
    "selection", ["escape.py", "missing.py", "README.md/other.py", "src"]
)
def test_missing_and_symlink_blobs_refused(repository, tmp_path, selection):
    _, policy, _ = repository
    policy["files"] = [selection]
    with pytest.raises(ValueError, match="missing, a symlink"):
        scout.snapshot(policy, tmp_path)


def test_stale_baseline_and_source_limits(repository, tmp_path, monkeypatch):
    _, policy, _ = repository
    policy["baseline"] = "0" * 40
    with pytest.raises(ValueError, match="baseline"):
        scout.snapshot(policy, tmp_path)
    policy["baseline"] = repository[1]["baseline"] = (
        scout.git(repository[0], "rev-parse", "HEAD").decode().strip()
    )
    monkeypatch.setattr(scout, "MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        scout.snapshot(policy, tmp_path)


def test_only_one_dispatcher_and_lock_releases_after_failure(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "emuru-projects").mkdir(mode=0o700)
    lock = RUNNER["dispatcher_lock"]
    with lock(home), pytest.raises(BlockingIOError), lock(home):
        pytest.fail("Second dispatcher acquired the lock")
    with lock(home):
        pass


class Backend:
    def __init__(self, report, outcome="claimed"):
        self.report, self.outcome, self.calls = report, outcome, []

    def call(self, service, request=None, timeout=30):
        self.calls.append((service, request, timeout))
        if service == "scout":
            if isinstance(self.report, BaseException):
                raise self.report
            return self.report
        if request["action"] == "claim":
            return {"task": "t_1", "run": 1, "status": self.outcome}
        return {"status": "done" if "report" in request else "blocked"}


@pytest.mark.parametrize("outcome", ["done", "uncertain", "blocked", "archived"])
def test_replay_or_uncertain_attempt_never_spawns_worker(outcome):
    backend = Backend({}, outcome)
    policy = {
        "repository": "sample",
        "baseline": "a" * 40,
        "files": ["app.py"],
        "timeout_seconds": 10,
    }
    assert (
        RUNNER["execute"](backend, policy, "t_123", {"repository_id": "a" * 64})[
            "status"
        ]
        == outcome
    )
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), InterruptedError(), KeyboardInterrupt(), {"forged": "report"}],
)
def test_failed_unverified_or_interrupted_worker_cannot_complete(failure):
    backend = Backend(failure)
    policy = {
        "repository": "sample",
        "baseline": "a" * 40,
        "files": ["app.py"],
        "timeout_seconds": 10,
    }
    with pytest.raises((TimeoutError, InterruptedError, KeyboardInterrupt, ValueError)):
        RUNNER["execute"](
            backend, policy, "t_123", {"repository_id": "a" * 64, "expected": "report"}
        )
    assert backend.calls[-1][1] == {"action": "finish", "task": "t_1", "run": 1}
    assert len(backend.calls) == 3


def test_verified_report_reaches_only_claimed_run():
    report = {"repository_id": "a" * 64, "trusted": "snapshot"}
    backend = Backend(report)
    policy = {
        "repository": "sample",
        "baseline": "a" * 40,
        "files": ["app.py"],
        "timeout_seconds": 10,
    }
    assert RUNNER["execute"](backend, policy, "t_123", copy.deepcopy(report)) == {
        "status": "done"
    }
    assert backend.calls[-1][1]["run"] == 1
    assert backend.calls[-1][1]["report"] == report
    assert backend.calls[0][1]["grant"]["report_sha256"] == scout.digest(report)


def test_compose_worker_has_only_snapshot_no_board_or_secret_mount(
    tmp_path, monkeypatch
):
    if os.getuid() == 0 or os.getgid() == 0:
        with pytest.raises(ValueError, match="unprivileged"):
            RUNNER["DockerScout"](ROOT, "fixture", tmp_path, tmp_path)
        monkeypatch.setattr(os, "getuid", lambda: 1000)
        monkeypatch.setattr(os, "getgid", lambda: 1000)
    staging = tmp_path / "staging"
    staging.mkdir()
    backend = RUNNER["DockerScout"](
        ROOT, "sha256:" + "a" * 64, tmp_path / "home", staging
    )
    config = json.loads((staging / "compose.json").read_text())
    scout_service = config["services"]["scout"]
    assert [mount["target"] for mount in scout_service["volumes"]] == ["/repo"]
    assert scout_service["volumes"][0]["read_only"]
    for service in config["services"].values():
        assert service["network_mode"] == "none"
        assert service["cap_drop"] == ["ALL"]
        assert service["read_only"] and service["pids_limit"] <= 32
        assert all(mount.startswith("/") for mount in service["tmpfs"])
        assert not service.get("secrets")
        assert int(service["user"].split(":")[0]) > 0
        assert all(
            not name.endswith(("KEY", "TOKEN")) for name in service["environment"]
        )
    assert (
        "scout"
        not in yaml.safe_load((ROOT / "infra/docker/compose.yaml").read_text())[
            "services"
        ]
    )
    assert backend.command[:2] == ["docker", "compose"]


def test_process_deadline_has_explicit_handler():
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-c",
            "import time; from emuru.projects.scout import limit_runtime; limit_runtime(1); time.sleep(10)",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "Scout stopped or exceeded its time budget." in result.stderr
