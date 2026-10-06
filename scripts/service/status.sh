#!/usr/bin/env bash
set -euo pipefail
emuru_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
systemctl --user show emuru-telegram.service -p ActiveState -p SubState -p NRestarts
"$emuru_root/scripts/hermes/telegram.sh" --status
