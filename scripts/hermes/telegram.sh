#!/usr/bin/env bash
set -euo pipefail
umask 077

emuru_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export HERMES_HOME="${EMURU_HERMES_PROFILE_HOME:-$HOME/.hermes/profiles/emuru}"
emuru_state="$HERMES_HOME/emuru-telegram"
emuru_workspace="${XDG_STATE_HOME:-$HOME/.local/state}/emuru/workspace"
if [[ $# -eq 0 || "$1" == "--launch" ]]; then
  "$emuru_root/.venv/bin/python" "$emuru_root/scripts/hermes/telegram.py" --preflight
  mkdir -p "$emuru_state" "$emuru_workspace"
  chmod 700 "$emuru_state"
  exec 9>"$emuru_state/instance.lock"
  chmod 600 "$emuru_state/instance.lock"
  flock -n 9 || { echo "EMURU Telegram is already running" >&2; exit 1; }
  export EMURU_TELEGRAM_LOCK_FD=9
  set -- --launch
  cd "$emuru_workspace"
fi
exec "$emuru_root/.venv/bin/python" "$emuru_root/scripts/hermes/telegram.py" "$@"
