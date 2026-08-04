#!/usr/bin/env bash
# Открывает .env — туда вписываются токен бота, номер и ID группы.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

if [ -n "${EDITOR:-}" ]; then
  "$EDITOR" .env
elif [ "$(uname)" = "Darwin" ]; then
  # -e открывает TextEdit и сразу возвращает управление, -W ждёт, пока закроют окно.
  open -e -W .env
elif command -v nano >/dev/null 2>&1; then
  nano .env
else
  vi .env
fi

echo "Сохранил. Дальше: bash mac-linux/3-login.sh"
