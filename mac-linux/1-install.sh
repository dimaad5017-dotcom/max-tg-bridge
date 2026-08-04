#!/usr/bin/env bash
# Ставит мост: отдельное окружение Python и библиотеки. Запускать один раз.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не найден."
  echo "  macOS:  brew install python@3.12   (или скачай с python.org)"
  echo "  Ubuntu: sudo apt install python3 python3-venv"
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Нужен Python 3.11 или новее, а установлен $(python3 -V)."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Создаю отдельное окружение для моста..."
  # На Ubuntu venv живёт в отдельном пакете, и без него ошибка выглядит непонятно.
  python3 -m venv .venv || {
    echo "Не создать окружение. На Ubuntu/Debian это лечится так:"
    echo "  sudo apt install python3-venv"
    exit 1
  }
fi

echo "Ставлю библиотеки, это займёт минуту..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo
echo "Готово. Дальше по порядку:"
echo "  bash mac-linux/2-settings.sh"
echo "  bash mac-linux/3-login.sh"
echo "  bash mac-linux/4-run.sh"
