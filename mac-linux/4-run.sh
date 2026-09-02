#!/usr/bin/env bash
# Запускает мост и поднимает его заново, если он упал. Выключить — Ctrl+C.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Окружение не создано. Сначала: bash mac-linux/1-install.sh"
  exit 1
fi

# Обновляемся с GitHub перед стартом — но только у тех, кто ставил через
# git clone. ZIP-установку это не трогает: там нет папки .git, и до этой
# точки просто не доходит.
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  echo "Проверяю обновления на GitHub..."
  if ! git pull --ff-only; then
    echo "Автообновление не вышло — запускаю то, что есть."
  fi
fi

# Подъём после падения живёт в bridge/run.py — чтобы Windows и Mac вели себя
# одинаково и сообщения были в одном месте.
.venv/bin/python -m bridge.run
