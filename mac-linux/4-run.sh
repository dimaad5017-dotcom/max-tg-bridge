#!/usr/bin/env bash
# Запускает мост и поднимает его заново, если он упал. Выключить — Ctrl+C.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Окружение не создано. Сначала: bash mac-linux/1-install.sh"
  exit 1
fi

# Ctrl+C должен гасить весь цикл, а не только текущий запуск моста.
trap 'echo; echo "Мост выключен."; exit 0' INT

while true; do
  .venv/bin/python -m bridge.main
  echo
  echo "Мост остановился. Через 10 секунд подниму заново."
  echo "Чтобы выключить совсем — нажми Ctrl+C."
  sleep 10
done
