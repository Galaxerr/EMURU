"""Load owner-managed API credentials from the workspace .env file."""

import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

API_KEYS = {
    "EMURU_GATEWAY_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
}
ENVIRONMENT_VALUES = API_KEYS | {"EMURU_TELEGRAM_OWNER_ID"}


def read_env(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Workspace .env symlinks are refused")
    if not path.exists():
        return {}
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Workspace .env must be owner-owned and mode 0600")

    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        if (
            not separator
            or key not in ENVIRONMENT_VALUES
            or not re.fullmatch(r"[A-Z_]+", key)
        ):
            raise ValueError(f"Invalid workspace .env entry on line {number}")
        try:
            words = shlex.split(raw, comments=True)
        except ValueError as error:
            raise ValueError(
                f"Invalid workspace .env entry on line {number}"
            ) from error
        if len(words) > 1:
            raise ValueError(f"Invalid workspace .env entry on line {number}")
        if key in values:
            raise ValueError(f"Duplicate workspace .env key on line {number}")
        values[key] = words[0] if words else ""
    return values


def load_env(path, environ=None):
    environ = os.environ if environ is None else environ
    for key, value in read_env(path).items():
        environ.setdefault(key, value)
    return environ


def write_env(path, updates):
    path = Path(path)
    values = read_env(path) if path.exists() else {}
    for key, value in updates.items():
        if key not in ENVIRONMENT_VALUES or not isinstance(value, str) or "\n" in value:
            raise ValueError("Invalid workspace .env update")
        values[key] = value
    pending = set(updates)
    lines = []
    if path.exists():
        for line in path.read_text().splitlines():
            candidate = line.strip().removeprefix("export ")
            key, separator, _ = candidate.partition("=")
            if separator and key in updates:
                lines.append(f"{key}={shlex.quote(values[key])}")
                pending.discard(key)
            else:
                lines.append(line)
    lines.extend(f"{key}={shlex.quote(values[key])}" for key in sorted(pending))
    content = "\n".join(lines) + "\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
