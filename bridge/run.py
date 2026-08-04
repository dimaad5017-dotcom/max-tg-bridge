"""Запускает мост и поднимает его заново, если он упал.

Отдельный файл, а не цикл прямо в .cmd: батник cmd.exe перечитывает по смещению
в байтах, и на русском тексте с переходом goto смещение уезжает — файл начинает
выполняться с середины строки. Здесь этой проблемы нет.
"""

import subprocess
import sys
import time

PAUSE = 10
# Если мост не прожил и полминуты, дело не в случайном обрыве связи, а в настройках.
QUICK_DEATH = 30
QUICK_DEATHS_ALLOWED = 3
TROUBLESHOOTING = "https://github.com/dimaad5017-dotcom/max-tg-bridge/blob/master/docs/troubleshooting.md"


def main() -> int:
    off = "закрой это окно крестиком" if sys.platform == "win32" else "нажми Ctrl+C"
    print("Мост запускается. Пока он нужен, это окно должно оставаться открытым.")
    print("Проверить, что он живой: напиши /help в своей группе Telegram - он ответит.")
    print(f"Выключить: {off}.")
    print()

    quick_deaths = 0
    while True:
        started = time.monotonic()
        try:
            subprocess.run([sys.executable, "-m", "bridge.main"], check=False)
        except KeyboardInterrupt:
            print()
            print("Мост выключен.")
            return 0

        quick_deaths = quick_deaths + 1 if time.monotonic() - started < QUICK_DEATH else 0
        if quick_deaths >= QUICK_DEATHS_ALLOWED:
            print()
            print("Мост падает сразу после запуска - поднимать его дальше бессмысленно.")
            print("Причина в строках выше. Разбор частых ошибок:")
            print(TROUBLESHOOTING)
            print()
            return 1

        print()
        print(f"Мост остановился. Подниму заново через {PAUSE} секунд.")
        try:
            time.sleep(PAUSE)
        except KeyboardInterrupt:
            print()
            print("Мост выключен.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
