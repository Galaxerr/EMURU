"""Load Compose secret files without putting credentials in arguments or config."""

import os
import stat
import sys
from pathlib import Path

for name in (
    "EMURU_GATEWAY_KEY",
    "OLLAMA_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
):
    path = Path("/run/secrets") / name.lower()
    if path.exists():
        info = path.stat()
        private_mount = (
            info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
        )
        compose_mount = info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o444
        if not stat.S_ISREG(info.st_mode) or not (private_mount or compose_mount):
            raise SystemExit(
                "Secret file must be a private file or Docker-managed secret"
            )
        value = path.read_text().strip()
        if "\n" in value:
            raise SystemExit("Invalid secret file")
        if value:
            os.environ[name] = value
if "EMURU_GATEWAY_KEY" not in os.environ:
    raise SystemExit("Gateway secret file required")
os.umask(0o077)
os.execvp(sys.argv[1], sys.argv[1:])
