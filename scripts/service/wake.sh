#!/usr/bin/env bash
# Compose wake; pass --home /absolute/private/home before the implicit command.
set -euo pipefail
emuru_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$emuru_root/.venv/bin/python" "$emuru_root/scripts/hermes/container.py" "$@" wake
