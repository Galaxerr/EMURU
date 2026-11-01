"""Confined selected-file workspaces; no native/provider or Docker execution."""

import copy
import json
import os
import subprocess

import pytest

from emuru.projects import coder, scout


@pytest.fixture
def grant(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src/sum_prices.py").write_text("def sum_prices(prices):\n    return 0\n")
    (repo / "README.md").write_text("Synthetic fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return {
        "schema": 1,
        "grant_id": "1" * 32,
        "owner": 123,
        "bot": 456,
        "vault": "2" * 64,
        "project": "3" * 64,
        "proposal": "4" * 32,
        "approval": "5" * 64,
        "revision": "6" * 64,
        "task": "t_123abc",
        "task_digest": "7" * 64,
        "repository": "synthetic",
        "path": str(repo),
        "baseline": scout.git(repo, "rev-parse", "HEAD").decode().strip(),
        "read_files": ["README.md", "src/sum_prices.py", "tests/test_prices.py"],
        "write_files": ["src/sum_prices.py"],
        "create_files": ["tests/test_prices.py"],
        "check_ids": ["python_unittest"],
        "catalogue": "8" * 64,
        "network": "none",
        "limits": {
            "concurrent_tasks": 1,
            "attempt_seconds": 600,
            "model_iterations": 12,
            "check_executions": 4,
            "check_seconds": 60,
            "cpus": 1,
            "memory_bytes": 512 * 1024 * 1024,
            "pids": 64,
            "files": 100,
            "file_bytes": 256 * 1024,
            "total_bytes": 2 * 1024 * 1024,
            "check_output_bytes": 64 * 1024,
            "total_output_bytes": 256 * 1024,
            "artifact_bytes": 8 * 1024 * 1024,
            "scratch_bytes": 64 * 1024 * 1024,
            "scratch_inodes": 1024,
        },
    }


def test_grant_is_private_bounded_and_has_only_exact_authority(grant, tmp_path):
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(grant))
    path.chmod(0o600)
    assert coder.load_grant(path) == grant
    loaded = coder.validate_grant(grant)
    loaded["read_files"].append("elsewhere.py")
    assert "elsewhere.py" not in grant["read_files"]
    path.chmod(0o644)
    with pytest.raises(ValueError):
        coder.load_grant(path)


def test_workspace_exports_committed_bytes_and_replaces_only_expected_content(
    grant, tmp_path
):
    repo = tmp_path / "repository"
    (repo / "src/sum_prices.py").write_text("uncommitted source remains intact\n")
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    before = workspace.read("src/sum_prices.py")
    assert before["content"] == "def sum_prices(prices):\n    return 0\n"
    with pytest.raises(ValueError):
        workspace.write("src/sum_prices.py", "return 5\n")
    after = workspace.write(
        "src/sum_prices.py",
        "def sum_prices(prices):\n    return sum(prices)\n",
        before["sha256"],
    )
    assert workspace.read("src/sum_prices.py")["sha256"] == after
    with pytest.raises(ValueError):
        workspace.write("src/sum_prices.py", "stale\n", before["sha256"])
    with pytest.raises(ValueError):
        workspace.write("README.md", "changed\n", workspace.read("README.md")["sha256"])
    workspace.write("tests/test_prices.py", "# supplemental test\n")
    assert workspace.read("tests/test_prices.py")["content"] == "# supplemental test\n"
    with pytest.raises(ValueError):
        workspace.write("tests/test_prices.py", "overwrite without digest\n")
    assert (
        repo / "src/sum_prices.py"
    ).read_text() == "uncommitted source remains intact\n"
    assert not (tmp_path / "workspace/.git").exists()
    with pytest.raises(FileExistsError):
        coder.Workspace(tmp_path / "workspace", grant)


def test_frozen_snapshot_is_independent_and_diff_binds_exact_changes(grant, tmp_path):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    source = "src/sum_prices.py"
    old = workspace.read(source)
    workspace.write(
        source, "def sum_prices(prices):\n    return sum(prices)\n", old["sha256"]
    )
    destination = tmp_path / "snapshot"
    frozen = workspace.snapshot(destination)
    assert frozen == workspace.manifest()
    assert (destination / source).stat().st_mode & 0o777 == 0o444
    assert (destination / source).stat().st_ino != (
        workspace.folder / source
    ).stat().st_ino
    assert (destination / source).stat().st_nlink == 1
    assert "-    return 0\n+    return sum(prices)\n" in workspace.diff()
    workspace.write(source, "# later edit\n", workspace.read(source)["sha256"])
    assert (
        destination / source
    ).read_text() == "def sum_prices(prices):\n    return sum(prices)\n"
    assert frozen != workspace.manifest()
    with pytest.raises(FileExistsError):
        workspace.snapshot(destination)


def test_workspace_cannot_be_created_inside_original_repository(grant):
    from pathlib import Path

    destination = Path(grant["path"]) / "workspace"
    with pytest.raises(ValueError):
        coder.Workspace(destination, grant)
    assert not destination.exists()


def test_duplicate_grant_keys_fail_closed(grant, tmp_path):
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(grant)[:-1] + ', "network": "none"}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        coder.load_grant(path)


@pytest.mark.parametrize(
    "change",
    [
        lambda g: g.update(extra=True),
        lambda g: g.update(schema=True),
        lambda g: g.update(owner=True),
        lambda g: g.update(network="bridge"),
        lambda g: g.update(check_ids=["python_unittest", "shell"]),
        lambda g: g.update(baseline="HEAD"),
        lambda g: g.update(write_files=["outside.py"]),
        lambda g: g.update(create_files=["src/sum_prices.py"]),
        lambda g: g.update(read_files=["src/sum_prices.py", "../escape.py"]),
        lambda g: g.update(read_files=["src/sum_prices.py", ".git/config"]),
        lambda g: g.update(read_files=["src/sum_prices.py", "src/sum_prices.py"]),
        lambda g: g["limits"].update(attempt_seconds=601),
        lambda g: g["limits"].update(file_bytes=True),
        lambda g: g["limits"].update(extra=1),
        lambda g: g["limits"].update(files=2),
    ],
)
def test_invalid_authority_never_provisions_workspace(grant, tmp_path, change):
    change(grant)
    with pytest.raises((ValueError, OSError)):
        coder.Workspace(tmp_path / "workspace", grant)
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize(
    "attack",
    [
        "symlink",
        "hardlink",
        "ancestor",
        "extra",
        "mode",
        "delete",
        "readonly",
        "binary",
    ],
)
def test_workspace_tampering_cannot_become_checked_source(grant, tmp_path, attack):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    target = workspace.folder / "src/sum_prices.py"
    canary = tmp_path / "canary"
    canary.write_text("protected\n")
    if attack == "symlink":
        target.unlink()
        target.symlink_to(canary)
    elif attack == "hardlink":
        target.unlink()
        os.link(canary, target)
    elif attack == "ancestor":
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(tmp_path)
    elif attack == "extra":
        (workspace.folder / "unexpected.py").write_text("unauthorized\n")
    elif attack == "mode":
        target.chmod(0o755)
    elif attack == "delete":
        target.unlink()
    elif attack == "readonly":
        (workspace.folder / "README.md").write_text("changed\n")
    elif attack == "binary":
        target.write_bytes(b"bad\0bytes")
    with pytest.raises((ValueError, OSError)):
        workspace.snapshot(tmp_path / "snapshot")
    with pytest.raises((ValueError, OSError)):
        workspace.write("src/sum_prices.py", "overwrite\n", "0" * 64)
    assert canary.read_text() == "protected\n"
    assert not (tmp_path / "snapshot").exists()


def test_source_quotas_reject_edit_before_changing_bytes(grant, tmp_path):
    grant["limits"].update(file_bytes=64, total_bytes=64)
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    before = workspace.manifest()
    with pytest.raises(ValueError, match="size limit"):
        workspace.write(
            "src/sum_prices.py", "x" * 65, before["src/sum_prices.py"]["sha256"]
        )
    with pytest.raises(ValueError, match="size limit"):
        workspace.write("tests/test_prices.py", "x" * 32)
    assert workspace.manifest() == before


def test_source_baseline_and_creation_targets_are_revalidated(grant, tmp_path):
    stale = copy.deepcopy(grant)
    stale["baseline"] = "0" * 40
    with pytest.raises(ValueError, match="baseline"):
        coder.Workspace(tmp_path / "stale", stale)
    existing = copy.deepcopy(grant)
    existing["create_files"] = ["README.md"]
    with pytest.raises(ValueError, match="already exists"):
        coder.Workspace(tmp_path / "existing", existing)
    assert not (tmp_path / "stale").exists()
    assert not (tmp_path / "existing").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../canary",
        "/etc/passwd",
        ".git/config",
        "src//sum_prices.py",
        "src/../README.md",
        "unselected.py",
    ],
)
def test_narrow_tools_reject_escaping_or_unselected_paths(grant, tmp_path, name):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    with pytest.raises(ValueError):
        workspace.read(name)
    with pytest.raises(ValueError):
        workspace.write(name, "changed\n")


@pytest.mark.parametrize("attack", ["hardlink", "symlink", "ancestor", "fifo", "large"])
def test_unsafe_grant_files_never_confer_authority(grant, tmp_path, attack):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "grant.json"
    path.write_text(json.dumps(grant))
    path.chmod(0o600)
    if attack == "hardlink":
        os.link(path, parent / "copy.json")
    elif attack == "symlink":
        path.rename(parent / "real.json")
        path.symlink_to(parent / "real.json")
    elif attack == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        path = alias / "grant.json"
    elif attack == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif attack == "large":
        path.write_bytes(b" " * 32769)
    with pytest.raises((ValueError, OSError)):
        coder.load_grant(path)


def test_creation_ancestors_cannot_redirect_writes(grant, tmp_path):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.folder / "tests").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        workspace.write("tests/test_prices.py", "escape attempt\n")
    assert list(outside.iterdir()) == []


def test_creation_publishes_without_overwriting_a_racing_file(
    grant, tmp_path, monkeypatch
):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    target = workspace.folder / "tests/test_prices.py"
    original_fsync = os.fsync

    def create_at_publication(fd):
        if not target.exists():
            target.write_text("concurrent creation\n")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", create_at_publication)
    with pytest.raises(FileExistsError):
        workspace.write("tests/test_prices.py", "must not overwrite\n")
    assert target.read_text() == "concurrent creation\n"
    assert list(target.parent.iterdir()) == [target]


def test_diff_preserves_missing_final_newline(grant, tmp_path):
    workspace = coder.Workspace(tmp_path / "workspace", grant)
    workspace.write("tests/test_prices.py", "# no final newline")
    assert workspace.diff().endswith(
        "+# no final newline\n\\ No newline at end of file\n"
    )


@pytest.mark.parametrize("kind", ["symlink", "binary", "directory", "missing"])
def test_export_rejects_nonregular_nontext_or_missing_committed_source(
    grant, tmp_path, kind
):
    from pathlib import Path

    repo = Path(grant["path"])
    target = repo / "bad.py"
    if kind == "symlink":
        target.symlink_to("/etc/passwd")
    elif kind == "binary":
        target.write_bytes(b"binary\0data")
    elif kind == "directory":
        target.mkdir()
        (target / "child.py").write_text("# child\n")
    if kind != "missing":
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "unsafe fixture",
            ],
            check=True,
        )
        grant["baseline"] = scout.git(repo, "rev-parse", "HEAD").decode().strip()
    grant["read_files"].append("bad.py")
    with pytest.raises(ValueError):
        coder.Workspace(tmp_path / "workspace", grant)
    assert not (tmp_path / "workspace").exists()
