"""Disposable qualification controller; no live grants, board writes or dispatch."""

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from emuru.projects.coder import LIMITS, Workspace, source_diff, validate_grant
from emuru.projects.coder_native import validate_call
from emuru.projects.coder_sandbox import CASES
from emuru.projects.scout import digest, encoded, git
from emuru.vault.mutations import directory, regular


def catalogue_digest():
    from emuru.projects import coder_sandbox

    return digest(
        {
            "schema": 1,
            "check": "python_unittest",
            "cases": CASES,
            "runner": hashlib.sha256(
                Path(coder_sandbox.__file__).read_bytes()
            ).hexdigest(),
            "callable": "sum_prices.py:sum_prices",
            "developer_pattern": "test_*.py",
        }
    )


def qualification_fixture(folder):
    """Create only a new synthetic repository; no production note/receipt is used."""
    import subprocess

    folder = Path(folder)
    folder.mkdir(mode=0o700)
    repo = folder / "repository"
    repo.mkdir(mode=0o700)
    (repo / "sum_prices.py").write_text("def sum_prices(prices):\n    return 0\n")
    (repo / "test_sum_prices.py").write_text(
        "import unittest\nfrom sum_prices import sum_prices\n\n"
        "class Prices(unittest.TestCase):\n"
        "    def test_positive(self):\n        self.assertEqual(sum_prices([1,2,3]),6)\n"
    )
    for args in (
        ["init", "-q"],
        ["add", "sum_prices.py", "test_sum_prices.py"],
        [
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "qualification baseline",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    grant = {
        "schema": 1,
        "grant_id": uuid.uuid4().hex,
        "owner": 42,
        "bot": 7,
        "vault": "a" * 64,
        "project": "b" * 64,
        "proposal": "c" * 32,
        "approval": "d" * 64,
        "revision": "e" * 64,
        "task": "t_123abc",
        "task_digest": digest({"fixture": "sum_prices", "cases": CASES}),
        "repository": "coder-qualification",
        "path": str(repo),
        "baseline": git(repo, "rev-parse", "HEAD").decode().strip(),
        "read_files": ["sum_prices.py", "test_sum_prices.py"],
        "write_files": ["sum_prices.py"],
        "create_files": [],
        "check_ids": ["python_unittest"],
        "catalogue": catalogue_digest(),
        "network": "none",
        "limits": dict(LIMITS),
    }
    return repo, grant


def _read_private(path, limit):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as stream:
            regular(stream.fileno(), private=True)
            value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Qualification artifact limit exceeded.")
    return value


def _tree_matches(folder, manifest, *, frozen):
    found = set()
    contents = {}
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise ValueError("Qualification evidence contains a symlink.")
        found.update(str((Path(root) / name).relative_to(folder)) for name in files)
    if found != set(manifest):
        raise ValueError("Qualification evidence file scope changed.")
    for name, item in manifest.items():
        path = folder / name
        with directory(path.parent) as parent:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as stream:
                regular(stream.fileno())
                info = os.fstat(stream.fileno())
                data = stream.read(item["size"] + 1)
        mode = 0o444 if frozen else item["mode"]
        if (
            info.st_uid != os.getuid()
            or info.st_mode & 0o7777 != mode
            or len(data) != item["size"]
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            raise ValueError("Qualification checked bytes or modes changed.")
        contents[name] = data
    return contents


def _artifact_size(folder):
    total = 0
    for root, dirs, files in os.walk(folder, followlinks=False):
        if any((Path(root) / name).is_symlink() for name in dirs):
            raise ValueError("Qualification artifact directory is a symlink.")
        with directory(Path(root)) as parent:
            for name in files:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
                with os.fdopen(fd, "rb") as stream:
                    regular(stream.fileno())
                    info = os.fstat(stream.fileno())
                    if info.st_uid != os.getuid():
                        raise ValueError("Qualification artifact owner changed.")
                    total += info.st_size
    return total


def _accepted(result):
    if result.get("success") is not True or result.get("case_count") != len(CASES):
        return False
    cases = result.get("cases", [])
    if len(cases) != len(CASES):
        return False
    for record, (prices, expected) in zip(cases, CASES, strict=True):
        if (
            record.get("input") != prices
            or record.get("expected") != expected
            or record.get("host_call_returned") is not True
            or record.get("accepted") is not True
            or record.get("returned_type") != "int"
            or type(record.get("value")) is not int
            or record["value"] != expected
        ):
            return False
    observation = result.get("host_observation")
    if (
        not isinstance(observation, dict)
        or observation.get("stopped") is not True
        or not observation.get("confinement")
    ):
        return False
    developer = result.get("developer_tests")
    return developer is None or (
        isinstance(developer, dict)
        and developer.get("exit_code") == 0
        and developer.get("completion_verified") is False
    )


def verify_evidence(folder, expected_digest, *, task_run=None):
    """Reopen actual source/check artifacts independently of native/model success."""
    folder = Path(folder)
    data = _read_private(folder / "evidence.json", LIMITS["artifact_bytes"])
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("Qualification evidence digest changed.")
    report = json.loads(data)
    if task_run is None:
        if (
            report.get("qualification_only") is not True
            or report.get("native_task_run") is not None
        ):
            raise ValueError("Live task evidence needs its native run binding.")
    elif (
        report.get("qualification_only") is not False
        or report.get("native_task_run") != task_run
    ):
        raise ValueError("Coder evidence does not match its native task run.")
    grant_data = _read_private(folder / "grant.json", 32768)
    grant = validate_grant(json.loads(grant_data))
    if _artifact_size(folder) > grant["limits"]["artifact_bytes"]:
        raise ValueError("Qualification artifact budget exceeded.")
    if (
        digest(grant) != report["grant_sha256"]
        or grant["catalogue"] != catalogue_digest()
    ):
        raise ValueError("Qualification grant or catalogue changed.")
    trees = {}
    for key, subfolder in (
        ("initial_manifest", "initial-source"),
        ("final_manifest", "final-source"),
    ):
        trees[key] = _tree_matches(folder / subfolder, report[key], frozen=True)
    _tree_matches(folder / "source", report["final_manifest"], frozen=False)
    initial, final = report["initial_manifest"], report["final_manifest"]
    changed = sorted(name for name in final if final[name] != initial.get(name))
    if (
        set(initial) != set(grant["read_files"]) - set(grant["create_files"])
        or not set(initial) <= set(final) <= set(grant["read_files"])
        or not set(changed) <= set(grant["write_files"] + grant["create_files"])
        or report["changed_files"] != changed
        or report["source_head"] != grant["baseline"]
        or any(item["mode"] not in {0o644, 0o755} for item in initial.values())
        or any(
            item["mode"] != initial.get(name, {"mode": 0o644})["mode"]
            for name, item in final.items()
        )
    ):
        raise ValueError("Qualification artifact scope or metadata changed.")
    patch = _read_private(folder / "diff.patch", grant["limits"]["artifact_bytes"])
    if (
        hashlib.sha256(patch).hexdigest() != report["diff_sha256"]
        or patch
        != source_diff(trees["initial_manifest"], trees["final_manifest"]).encode()
    ):
        raise ValueError("Qualification diff changed.")
    if (
        git(Path(grant["path"]), "rev-parse", "HEAD").decode().strip()
        != report["source_head"]
    ):
        raise ValueError("Qualification source baseline changed.")
    checks = report["checks"]
    verified = (
        not report.get("failure")
        and all(_accepted(check["result"]) for check in checks)
        and bool(report["changed_files"])
        and report["native"].get("success") is True
        and bool(checks)
        and checks[-1]["snapshot_sha256"] == digest(report["final_manifest"])
        and _accepted(checks[-1]["result"])
        and not report["missing_checks"]
    )
    if report["success"] is not verified:
        raise ValueError(
            "Qualification success is not supported by independent evidence."
        )
    return report


def _remove_snapshot(folder):
    # Only controller-created, never sandbox-writable snapshot directories reach here.
    for root, _, _ in os.walk(folder):
        Path(root).chmod(0o700)
    shutil.rmtree(folder)


class Qualification:
    """One bounded task attempt; live attempts bind evidence to their native run."""

    def __init__(self, folder, grant, image, *, task_run=None):
        self.folder, self.grant, self.image = Path(folder), validate_grant(grant), image
        self.task_run = task_run
        if grant["catalogue"] != catalogue_digest() or (
            task_run is None and grant["repository"] != "coder-qualification"
        ):
            raise ValueError("Only the approved coder check catalogue is supported.")
        if task_run is not None and (
            set(task_run) != {"task", "run"}
            or task_run["task"] != grant["task"]
            or type(task_run["run"]) is not int
            or task_run["run"] <= 0
        ):
            raise ValueError("Invalid native task run binding.")
        if task_run is None:
            if self.folder.exists():
                raise ValueError(
                    "An existing qualification cannot be started again; inspect its evidence."
                )
            self.folder.mkdir(mode=0o700)
            self._save("grant.json", encoded(grant))
            self._save(
                "intent.json",
                encoded(
                    {
                        "grant": digest(grant),
                        "qualification_only": True,
                        "native_task_run": None,
                    }
                ),
            )
        else:
            saved_grant = validate_grant(
                json.loads(_read_private(self.folder / "grant.json", 32768))
            )
            intent = json.loads(_read_private(self.folder / "intent.json", 32768))
            run = json.loads(_read_private(self.folder / "run.json", 32768))
            if (
                digest(saved_grant) != digest(grant)
                or intent.get("grant_sha256") != digest(grant)
                or run != task_run
            ):
                raise ValueError("Existing coder intent differs from its task run.")
        self.workspace = Workspace(self.folder / "source", grant, reserve=self._reserve)
        self.workspace.snapshot(self.folder / "initial-source")
        self.deadline = time.monotonic() + grant["limits"]["attempt_seconds"]
        self.checks, self.trace = [], []
        self.failure = None

    def _reserve(self, additional):
        if (
            _artifact_size(self.folder) + additional
            > self.grant["limits"]["artifact_bytes"]
        ):
            raise ValueError("Qualification artifact budget exceeded.")

    def _save(self, name, data):
        self._reserve(len(data))
        with directory(self.folder) as parent:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)

    def run(self, native, sandbox):
        def execute(name, arguments):
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Qualification attempt deadline exhausted.")
            validate_call(name, arguments)
            self.trace.append({"name": name, "arguments_sha256": digest(arguments)})
            if name == "coder_read":
                return self.workspace.read(arguments["path"])
            if name == "coder_write":
                return {
                    "sha256": self.workspace.write(
                        arguments["path"],
                        arguments["content"],
                        arguments["expected_sha256"],
                    )
                }
            check_id = arguments["check_id"]
            snapshot = self.folder / ("check-source-" + str(len(self.checks)))
            manifest = self.workspace.snapshot(snapshot)
            try:
                result = sandbox.check(
                    snapshot,
                    check_id,
                    remaining_seconds=self.deadline - time.monotonic(),
                )
                self.checks.append(
                    {"snapshot_sha256": digest(manifest), "result": result}
                )
                if not _accepted(result) or sandbox.failed:
                    self.failure = (
                        self.failure
                        or getattr(sandbox, "failure", None)
                        or "tests_failed"
                    )
                return {
                    "success": not self.failure and result["success"],
                    "case_count": result["case_count"],
                    "failure": self.failure,
                }
            finally:
                if all(process.get("stopped") is True for process in sandbox.processes):
                    _remove_snapshot(snapshot)

        def dispatch(name, arguments):
            if self.failure:
                raise ValueError("Qualification authority is closed.")
            try:
                return execute(name, arguments)
            except Exception as error:
                self.failure = self.failure or (
                    "timeout" if isinstance(error, TimeoutError) else "execution_failed"
                )
                raise

        try:
            native_result = native(dispatch)
        except Exception as error:  # noqa: BLE001 -- persist sanitized failure, never provider/source diagnostics
            self.failure = self.failure or (
                "timeout" if isinstance(error, TimeoutError) else "execution_failed"
            )
            native_result = {"success": False, "error": "coder_execution_failed"}
        final = self.workspace.manifest()
        if native_result.get("success") is True and not self.failure:
            dispatch("coder_check", {"check_id": "python_unittest"})
        self.workspace.snapshot(self.folder / "final-source")
        patch = self.workspace.diff().encode()
        self._save("diff.patch", patch)
        changed = [
            name
            for name in final
            if final.get(name) != self.workspace.initial_manifest.get(name)
        ]
        valid = (
            not self.failure
            and native_result.get("success") is True
            and bool(changed)
            and bool(self.checks)
            and _accepted(self.checks[-1]["result"])
            and not sandbox.failed
            and self.checks[-1]["snapshot_sha256"] == digest(final)
        )
        report = {
            "schema": 1,
            "qualification_only": self.task_run is None,
            "success": valid,
            "failure": self.failure,
            "grant_sha256": digest(self.grant),
            "source_head": self.grant["baseline"],
            "task_head": None,
            "task_branch": None,
            "task_commits": [],
            "native_task_run": self.task_run,
            "image": self.image,
            "native": native_result,
            "initial_manifest": self.workspace.initial_manifest,
            "final_manifest": final,
            "changed_files": changed,
            "diff_sha256": hashlib.sha256(patch).hexdigest(),
            "checks": self.checks,
            "tool_trace": self.trace,
            "missing_checks": [] if valid else ["independent_local_acceptance"],
            "usage": None,
        }
        data = encoded(report)
        self._save("evidence.json", data)
        self.evidence_sha256 = hashlib.sha256(data).hexdigest()
        return verify_evidence(
            self.folder, self.evidence_sha256, task_run=self.task_run
        )
