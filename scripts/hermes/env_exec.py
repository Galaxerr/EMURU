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
os.execvp(sys.argv[1], sys.argv[1:])
