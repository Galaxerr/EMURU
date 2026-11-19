#!/usr/bin/env bash
# Run the project's venv Python with the given arguments.
# When invoked as root (CI runners under act), drop to the unprivileged
# "nobody" user while keeping access to the Docker socket.
#
# Usage: bash scripts/ci/as-coder.sh <python args...>
#   e.g. bash scripts/ci/as-coder.sh scripts/hermes/scout-check.py
#        bash scripts/ci/as-coder.sh - < scripts/ci/rework-matrix.py
set -euo pipefail

runner=(.venv/bin/python)
mkdir -p .runtime

if [ "$(id -u)" -eq 0 ]; then
  uid=$(id -u nobody)
  gid=$(id -g nobody)

  # Never change ownership of a host user's bind-mounted runtime directory.
  runtime_uid=$(stat -c %u .runtime)
  test "$runtime_uid" -eq 0 || test "$runtime_uid" -eq "$uid"
  install -d -o "$uid" -g "$gid" -m 700 .runtime

  home=$(mktemp -d "${RUNNER_TEMP:-/tmp}/coder-home.XXXXXX")
  chown "$uid:$gid" "$home"
  trap 'rm -rf -- "$home"' EXIT

  runner=(env HOME="$home" setpriv
    --reuid="$uid" --regid="$gid"
    --groups="$(stat -c %g /var/run/docker.sock)" .venv/bin/python)
fi

"${runner[@]}" "$@"
