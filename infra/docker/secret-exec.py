"""Load Compose secret files without putting credentials in arguments or config."""

import os
import stat
import sys
from pathlib import Path

if os.environ.get("EMURU_SOURCE_ID"):
    sys.path.insert(0, "/opt/emuru/src")
    from emuru.deployment import source_identity

    if source_identity(Path("/opt/emuru")) != os.environ["EMURU_SOURCE_ID"]:
        raise SystemExit("Worker source identity mismatch")
if os.environ.get("EMURU_GATEWAY_ID"):
    from emuru.deployment import gateway_identity

    if (
        gateway_identity(Path("/opt/emuru"), Path(__file__))
        != os.environ["EMURU_GATEWAY_ID"]
    ):
        raise SystemExit("Gateway source identity mismatch")

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
