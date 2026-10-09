"""Run a command with owner-managed credentials from the workspace .env."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from emuru.environment import load_env

if len(sys.argv) < 2:
    raise SystemExit("A command is required")
load_env(ROOT / ".env")
if sys.argv[1:4] == ["hermes", "-p", "emuru"] and sys.argv[4:5] != ["config"]:
    from emuru.hermes import profile
    from emuru.hermes.launch import launch_native

    native = profile.installed_runtime(ROOT)
    os.environ["HERMES_HOME"] = str(profile.profile_home())
    arguments = [str(ROOT / "infra/hermes/cli-runtime.py"), *sys.argv[4:]]
    try:
        launch_native(native, arguments)
    except profile.HealthError as error:
        raise SystemExit(str(error)) from None
os.execvp(sys.argv[1], sys.argv[1:])
