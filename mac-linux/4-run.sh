#!/usr/bin/env bash
# Запускает мост и поднимает его заново, если он упал. Выключить — Ctrl+C.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Окружение не создано. Сначала: bash mac-linux/1-install.sh"
  exit 1
fi

# Подъём после падения живёт в bridge/run.py — чтобы Windows и Mac вели себя
# одинаково и сообщения были в одном месте.
.venv/bin/python -m bridge.run
