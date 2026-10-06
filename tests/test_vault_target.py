"""Private target selection and synthetic-mode isolation; no credentials/network."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuru.vault.target import TargetError, load_target, profile_home

REPO = Path(__file__).resolve().parents[1]


class TargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "project"
        self.home = self.base / "profiles/emuru"
        self.vault = self.base / "owner-vault"
        for path in (
            self.repo,
            self.home,
            self.vault / "00_Inbox",
            self.vault / ".git",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.record = self.home / "vault-target.json"

    def write_record(self, data):
        self.record.write_text(json.dumps(data), encoding="utf-8")
        self.record.chmod(0o600)

    def real_record(self, root=None):
        self.write_record({"mode": "real", "vault_path": str(root or self.vault)})

    def test_missing_record_stays_synthetic(self):
        self.assertEqual(
            load_target(self.repo, self.home),
            ("synthetic", self.repo / ".runtime/vault"),
        )

    def test_explicit_synthetic_needs_no_real_path(self):
        self.write_record({"mode": "synthetic"})
        self.assertEqual(load_target(self.repo, self.home)[0], "synthetic")

    def test_valid_real_root_is_selected_without_writing(self):
        self.real_record()
        before = sorted(str(p.relative_to(self.vault)) for p in self.vault.rglob("*"))
        self.assertEqual(load_target(self.repo, self.home), ("real", self.vault))
        self.assertEqual(
            sorted(str(p.relative_to(self.vault)) for p in self.vault.rglob("*")),
            before,
        )
        self.assertFalse((self.vault / "_index").exists())

    def test_unsafe_permissions_fail_without_path_disclosure(self):
        self.real_record()
        self.record.chmod(0o644)
        with self.assertRaisesRegex(
            TargetError, "target_record_permissions_invalid"
        ) as error:
            load_target(self.repo, self.home)
        self.assertNotIn(str(self.vault), str(error.exception))

    def test_malformed_explicit_record_never_falls_back(self):
        self.record.write_text("{", encoding="utf-8")
        self.record.chmod(0o600)
        with self.assertRaises(TargetError):
            load_target(self.repo, self.home)

    def test_unknown_fields_modes_and_relative_paths_fail(self):
        for data in (
            [],
            {"mode": "other"},
            {"mode": "synthetic", "vault_path": str(self.vault)},
            {"mode": "real", "vault_path": "relative/vault"},
            {"mode": "real", "vault_path": str(self.base / "other/../owner-vault")},
            {"mode": "real", "vault_path": 1},
        ):
            with self.subTest(data=data):
                self.write_record(data)
                with self.assertRaises(TargetError):
                    load_target(self.repo, self.home)

    def test_missing_real_root_fails(self):
        self.real_record(self.base / "absent")
        with self.assertRaisesRegex(TargetError, "target_root_unavailable"):
            load_target(self.repo, self.home)

    def test_project_or_profile_overlap_is_refused(self):
        for root in (self.repo, self.repo / "nested", self.base, self.home):
            with self.subTest(root=root):
                root.mkdir(exist_ok=True)
                self.real_record(root)
                with self.assertRaisesRegex(TargetError, "target_repository_overlap"):
                    load_target(self.repo, self.home)

    def test_record_and_root_symlinks_are_refused(self):
        self.real_record()
        original = self.home / "original.json"
        self.record.rename(original)
        self.record.symlink_to(original)
        with self.assertRaisesRegex(TargetError, "target_symlink_refused"):
            load_target(self.repo, self.home)
        self.record.unlink()
        alias = self.base / "alias"
        alias.symlink_to(self.vault, target_is_directory=True)
        self.real_record(alias)
        with self.assertRaisesRegex(TargetError, "target_symlink_refused"):
            load_target(self.repo, self.home)

    def test_git_and_inbox_are_required(self):
        self.real_record()
        (self.vault / ".git").rmdir()
        with self.assertRaisesRegex(TargetError, "target_git_root_missing"):
            load_target(self.repo, self.home)
        (self.vault / ".git").mkdir()
        (self.vault / "00_Inbox").rmdir()
        with self.assertRaisesRegex(TargetError, "target_inbox_unavailable"):
            load_target(self.repo, self.home)

    def test_symlink_inbox_and_derived_outputs_are_refused(self):
        self.real_record()
        (self.vault / "00_Inbox").rmdir()
        (self.vault / "00_Inbox").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(TargetError, "target_inbox_unavailable"):
            load_target(self.repo, self.home)
        (self.vault / "00_Inbox").unlink()
        (self.vault / "00_Inbox").mkdir()
        (self.vault / "_index").mkdir()
        (self.vault / "_index/graph.json").symlink_to(self.base / "outside.json")
        with self.assertRaisesRegex(TargetError, "target_derived_file_invalid"):
            load_target(self.repo, self.home)

    def test_profile_precedence_and_validation(self):
        with patch.dict(
            os.environ,
            {"EMURU_HERMES_PROFILE_HOME": str(self.home), "HERMES_HOME": "/ignored"},
        ):
            self.assertEqual(profile_home(), self.home)
        with (
            patch.dict(os.environ, {"EMURU_HERMES_PROFILE_HOME": "relative"}),
            self.assertRaisesRegex(TargetError, "profile_location_invalid"),
        ):
            profile_home()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "target_adapter", REPO / "scripts/hermes/vault.py"
        )
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)

    def test_prepare_refuses_a_symlink_to_another_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "other-vault"
            real.mkdir()
            alias = root / "synthetic"
            alias.symlink_to(real, target_is_directory=True)
            with (
                patch.object(self.adapter, "backend") as backend,
                self.assertRaisesRegex(
                    SystemExit, "Synthetic vault path must not contain symlinks"
                ),
            ):
                self.adapter.prepare(alias)
            backend.assert_not_called()

    def test_prepare_inspect_and_check_never_resolve_private_target(self):
        for mode in ("prepare", "inspect", "check"):
            with (
                self.subTest(mode=mode),
                patch.object(sys, "argv", ["adapter", mode]),
                patch.object(
                    self.adapter,
                    "load_target",
                    side_effect=AssertionError("Private target read"),
                ),
                patch.object(self.adapter, "prepare"),
                patch.object(self.adapter, "inspect_or_check") as inspect,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.adapter.main()
                if mode != "prepare":
                    vault, inspect_mode = inspect.call_args.args
                    self.assertNotEqual(vault, self.adapter.RUNTIME)
                    self.assertEqual(inspect_mode, mode == "inspect")

    def test_serve_uses_selected_root_and_no_fixture_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(sys, "argv", ["adapter", "serve"]),
                patch.object(self.adapter, "profile_home", return_value=root),
                patch.object(self.adapter, "load_target", return_value=("real", root)),
                patch.object(
                    self.adapter, "prepare", side_effect=AssertionError("Fixture copy")
                ),
                patch.object(
                    self.adapter, "backend", return_value=(["/uv", "server"], {})
                ) as backend,
                patch.object(self.adapter.os, "chdir"),
                patch.object(self.adapter.os, "execvpe") as execute,
            ):
                self.adapter.main()
            backend.assert_called_once_with(root, "emuru-vault-mcp")
            execute.assert_called_once_with("/uv", ["/uv", "server"], {})

    def test_real_requirement_refuses_missing_binding(self):
        with (
            patch.object(sys, "argv", ["adapter", "target", "--require-real"]),
            patch.object(
                self.adapter,
                "profile_home",
                return_value=Path("/synthetic/profiles/emuru"),
            ),
            patch.object(
                self.adapter,
                "load_target",
                return_value=("synthetic", Path("/synthetic/vault")),
            ),
            self.assertRaisesRegex(SystemExit, "real_vault_not_selected"),
        ):
            self.adapter.main()

    def test_target_output_contains_mode_not_real_path(self):
        stream = io.StringIO()
        with (
            patch.object(sys, "argv", ["adapter", "target", "--require-real"]),
            patch.object(
                self.adapter,
                "profile_home",
                return_value=Path("/synthetic/profiles/emuru"),
            ),
            patch.object(
                self.adapter,
                "load_target",
                return_value=("real", Path("/PRIVATE_PATH")),
            ),
            contextlib.redirect_stdout(stream),
        ):
            self.adapter.main()
        self.assertEqual(json.loads(stream.getvalue()), {"mode": "real"})


if __name__ == "__main__":
    unittest.main()
