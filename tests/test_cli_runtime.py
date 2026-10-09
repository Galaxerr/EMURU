"""Guarded CLI never launches an unpatched native subprocess."""

import runpy
from pathlib import Path

import pytest

from emuru.hermes.profile import HealthError

arguments = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "infra/hermes/cli-runtime.py")
)["guarded_arguments"]


@pytest.mark.parametrize("command", [[], ["gateway", "run"], ["serve"], ["acp"]])
def test_only_chat_can_enter_guarded_cli(command):
    with pytest.raises(HealthError, match="chat_only"):
        arguments(command)


@pytest.mark.parametrize(
    "option",
    ["--tui", "--tui-native", "--native", "-pother", "-tall", "--provider=ollama"],
)
def test_subprocess_and_policy_overrides_refused(option):
    with pytest.raises(HealthError, match="override_refused"):
        arguments(["chat", option])


def test_classic_cli_forced_even_with_native_tui_environment(monkeypatch):
    monkeypatch.setenv("HERMES_TUI", "1")
    assert arguments(["chat", "-q", "hello"]) == ["chat", "--cli", "-q", "hello"]
