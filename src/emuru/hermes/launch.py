"""Execute an external bridge with the pinned Hermes interpreter and bootstrap."""

import json
import os
import subprocess
import sys
from pathlib import Path

from emuru.hermes.profile import HealthError


def launch_native(native, arguments):
    """Exec preserves inherited locks and delivers shutdown signals to Hermes."""
    os.environ["UV_OFFLINE"] = "1"
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    python = native / ".venv/bin/python"
    if python.is_file() and os.access(python, os.X_OK):
        command = [
            str(python),
            "-I",
            "-c",
            "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_path(sys.argv.pop(1),run_name='__main__')",
            str(native),
            *arguments,
        ]
    else:
        hermes = native / ".hermes/bin/hermes"
        if not hermes.is_file() or not os.access(hermes, os.X_OK):
            raise HealthError("runtime_python_missing")
        result = subprocess.run(
            [
                str(hermes),
                "--print-runtime-command",
                "--module",
                "runpy",
                "--",
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            command = json.loads(result.stdout)
        except ValueError:
            raise HealthError("runtime_launcher_contract_changed") from None
        entry = "runpy.run_module('runpy', run_name='__main__', alter_sys=True)"
        if (
            not isinstance(command, list)
            or len(command) < 4
            or not all(type(value) is str for value in command)
            or command[1:3] != ["-I", "-c"]
            or not command[3].endswith(entry)
            or not Path(command[0]).is_absolute()
            or command[4:] != list(arguments)
        ):
            raise HealthError("runtime_launcher_contract_changed")
        command[3] = (
            command[3].removesuffix(entry)
            + "runpy.run_path(sys.argv.pop(1), run_name='__main__')"
        )
    sys.stdout.flush()
    os.execv(command[0], command)
