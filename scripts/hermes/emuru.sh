#!/usr/bin/env bash
set -euo pipefail
umask 077

export PATH="$HOME/.local/bin:$PATH"
emuru_workspace="${XDG_STATE_HOME:-$HOME/.local/state}/emuru/workspace"
mkdir -p "$emuru_workspace"
cd "$emuru_workspace"

if [[ $# -eq 0 ]]; then
  set -- chat
fi

exec hermes -p emuru "$@"