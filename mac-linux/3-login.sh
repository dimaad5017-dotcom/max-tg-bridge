#!/usr/bin/env bash
# Разовый вход в MAX по SMS. Дальше сессия берётся из cache/max.db.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Окружение не создано. Сначала: bash mac-linux/1-install.sh"
  exit 1
fi

.venv/bin/python -m bridge.login
