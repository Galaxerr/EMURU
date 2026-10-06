"""Config transports and runtime isolation, independently of profile policy tests."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from emuru import hermes_profile as profile
from emuru.vault_target import TargetError, profile_home

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli():
    return runpy.run_path(str(ROOT / "scripts/hermes-vault-profile.py"))


def test_cli_transport_preserves_raw_strings_structured_values_and_force(cli, tmp_path):
    config = cli["CLIConfig"](tmp_path)
    command = [str(tmp_path / "scripts/hermes-emuru.sh"), "config"]
    with patch(
        "subprocess.run", return_value=NS(stdout='{"provider":"gemini"}')
    ) as transport:
        assert config.get("model") == {"provider": "gemini"}
        assert transport.call_args.args[0] == command + ["get", "model", "--json"]
        assert transport.call_args.kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 30,
        }
        for value in ("gemini", "", False, 0, [], {"enabled": True}):
            config.set("example", value, force=True)
            serialized = value if isinstance(value, str) else json.dumps(value)
            assert transport.call_args.args[0] == command + [
                "set",
                "--force",
                "example",
                serialized,
            ]
        config.set("model.default", "gemini-test")
        assert transport.call_args.args[0] == command + [
            "set",
            "model.default",
            "gemini-test",
        ]


def test_cli_read_failure_redacts_private_stderr(cli):
    with (
        patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, "native config", stderr="SYNTHETIC_SECRET"
            ),
        ),
        pytest.raises(profile.ProfileError, match="unavailable: mcp_servers") as error,
    ):
        cli["CLIConfig"](ROOT).get("mcp_servers")
    assert "SYNTHETIC_SECRET" not in str(error.value)


def test_cli_modes_delegate_to_core_and_audit_has_no_writer(cli, monkeypatch, capsys):
    main = cli["main"]
    for mode in ([], ["--apply"], ["--offline"]):
        monkeypatch.setattr(sys, "argv", ["profile", *mode])
        with (
            patch.object(profile, "configure_profile") as configure,
            patch.object(profile, "expected_settings") as expected,
            patch("subprocess.run") as transport,
        ):
            main()
            if mode == ["--offline"]:
                expected.assert_called_once_with(ROOT)
                configure.assert_not_called()
            else:
                args = configure.call_args.args
                assert args[0] == ROOT and args[1].__name__ == "get"
                assert (args[2] is None) == (mode == [])
            transport.assert_not_called()
        assert "PASS" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["profile", "--apply", "--offline"])
    with (
        patch.object(profile, "configure_profile") as configure,
        pytest.raises(SystemExit),
    ):
        main()
    configure.assert_not_called()


def test_cli_converts_safe_core_failure_to_exit(cli, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["profile"])
    with (
        patch.object(
            profile,
            "configure_profile",
            side_effect=profile.ProfileError("safe failure"),
        ),
        pytest.raises(SystemExit, match="safe failure"),
    ):
        cli["main"]()


def test_native_adapter_reads_config_and_returns_only_safe_codes(monkeypatch):
    runtime = runpy.run_path(str(ROOT / "infra/hermes/telegram-runtime.py"))
    actual = {"model": {"api_key": "SYNTHETIC_SECRET"}}
    read = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        NS(read_user_config_raw=lambda: read.append(True) or actual),
    )
    with pytest.raises(
        profile.HealthError, match="^inline_model_credentials$"
    ) as error:
        runtime["audit_live_profile"]()
    assert read == [True] and "SYNTHETIC_SECRET" not in str(error.value)
    with patch.object(
        profile, "audit_native_profile", return_value={"enabled": True}
    ) as audit:
        assert runtime["audit_live_profile"]() == {"enabled": True}
    audit.assert_called_once_with(ROOT, actual)


def test_profile_core_runs_without_site_packages_or_cli_entry_points(tmp_path):
    # -S blocks third-party packages; temporary root contains no executable scripts.
    directory = tmp_path / "infra/hermes"
    directory.mkdir(parents=True)
    for name in (
        "runtime-settings.json",
        "model-selection.json",
        "telegram-settings.json",
    ):
        (directory / name).write_bytes((ROOT / "infra/hermes" / name).read_bytes())
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from emuru.hermes_profile import expected_settings, expected_server
root = Path(sys.argv[2])
assert not (root / 'scripts').exists()
assert expected_settings(root)['platform_toolsets.cli'] == ['mcp-vault']
assert expected_server(root)['tools']['include'] == ['vault_map', 'vault_search', 'vault_open', 'vault_neighbors', 'vault_write']
"""
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(ROOT / "src"), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("source", ["EMURU_HERMES_PROFILE_HOME", "HERMES_HOME"])
def test_shared_profile_home_precedence_and_symlink_rejection(
    tmp_path, monkeypatch, source
):
    home = tmp_path / "profiles/emuru"
    home.mkdir(parents=True)
    monkeypatch.delenv("EMURU_HERMES_PROFILE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/ignored")
    monkeypatch.setenv(source, str(home))
    assert profile_home is profile.profile_home
    assert profile_home() == home
    # Keep the required final names while symlinking an ancestor.
    parent = tmp_path / "linked"
    parent.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv(source, str(parent / "profiles/emuru"))
    with pytest.raises(TargetError, match="target_symlink_refused"):
        profile_home()
    for invalid in (tmp_path / "alias/emuru", home / "../emuru"):
        monkeypatch.setenv(source, str(invalid))
        with pytest.raises(TargetError, match="profile_location_invalid"):
            profile_home()
