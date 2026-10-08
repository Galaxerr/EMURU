#!/usr/bin/env bash
set -euo pipefail
umask 077

export PATH="$HOME/.local/bin:$PATH"
emuru_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
emuru_workspace="${XDG_STATE_HOME:-$HOME/.local/state}/emuru/workspace"
mkdir -p "$emuru_workspace"
cd "$emuru_workspace"

if [[ $# -eq 0 ]]; then
  set -- chat
fi

exec "$emuru_root/.venv/bin/python" "$emuru_root/scripts/hermes/env_exec.py" hermes -p emuru "$@"
