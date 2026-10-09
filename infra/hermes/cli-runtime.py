"""Run CLI inference in the pinned Hermes interpreter with gateway guards."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from emuru.hermes import profile
from emuru.hermes.inference import install_guard


def guarded_arguments(arguments):
    if not arguments or arguments[0] != "chat":
        raise profile.HealthError("cli_chat_only")
    for argument in arguments[1:]:
        if argument.split("=", 1)[0] in {
            "-p",
            "--profile",
            "-m",
            "--model",
            "--provider",
            "--base-url",
            "--api-key",
            "-t",
            "--toolsets",
            "--max-turns",
            "--tui",
            "--tui-native",
            "--native",
        }:
            raise profile.HealthError("cli_policy_override_refused")
        if argument.startswith(("-p", "-m", "-t")) and not argument.startswith("--"):
            raise profile.HealthError("cli_policy_override_refused")
    # Native TUI launches a new process and would discard the installed guards.
    return ["chat", "--cli", *arguments[1:]]


def main():
    arguments = guarded_arguments(sys.argv[1:])
    from hermes_cli.config import read_user_config_raw
    from run_agent import AIAgent

    profile.audit_native_profile(ROOT, read_user_config_raw())
    if not os.environ.get("EMURU_GATEWAY_KEY"):
        raise profile.HealthError("gateway_credentials_missing")
    install_guard(AIAgent)
    from hermes_cli.main import main as hermes_main

    sys.argv = ["hermes", *arguments]
    hermes_main()


if __name__ == "__main__":
    try:
        main()
    except (profile.ProfileError, profile.HealthError) as error:
        raise SystemExit("EMURU CLI: " + getattr(error, "code", str(error))) from None
