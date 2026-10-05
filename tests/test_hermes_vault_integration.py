"""Exercise the vault server through actual stdio, without provider credentials."""

import subprocess
import sys
from pathlib import Path


def test_hermes_real_stdio_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/hermes-vault.py"), "check"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Stdio retrieval" in result.stdout
