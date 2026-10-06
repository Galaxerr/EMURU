#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop emuru-telegram.service
systemctl --user show emuru-telegram.service -p ActiveState -p SubState
