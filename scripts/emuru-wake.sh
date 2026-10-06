#!/usr/bin/env bash
set -euo pipefail
systemctl --user start emuru-telegram.service
systemctl --user show emuru-telegram.service -p ActiveState -p SubState
printf '%s\n' 'Wait for the guarded READY message before sending work.'
