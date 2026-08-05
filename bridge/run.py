"""Запускает мост и поднимает его заново, если он упал.

Отдельный файл, а не цикл прямо в .cmd: батник cmd.exe перечитывает по смещению
в байтах, и на русском тексте с переходом goto смещение уезжает — файл начинает
выполняться с середины строки. Здесь этой проблемы нет.
"""

import socket
import subprocess
import sys
import time

PAUSE = 10
# Если мост не прожил и полминуты, дело не в случайном обрыве связи, а в настройках.
QUICK_DEATH = 30
QUICK_DEATHS_ALLOWED = 3
TROUBLESHOOTING = "https://github.com/dimaad5017-dotcom/max-tg-bridge/blob/master/docs/troubleshooting.md"
# Порт ничего не слушает по делу — он нужен только как замок: занять его дважды
# нельзя, и операционная система освобождает его сама, когда мост закрывают.
LOCK_PORT = 47653


def take_lock() -> socket.socket | None:
    """Занимает место единственного моста. None — значит, мост уже где-то запущен.

    Два моста на одном аккаунте не удваивают надёжность, а мешают друг другу:
    Telegram отдаёт входящее только одному из двух, а MAX при каждом входе меняет
    ключ сессии и выбивает предыдущего. Со стороны это выглядит как «иногда не
    доходит» — то есть как раз то молчание, которого здесь быть не должно.
    """
    lock = socket.socket()
    try:
        # Нарочно без SO_REUSEADDR: нам нужно именно то, чтобы второй раз не занялось.
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        lock.close()
        return None
    lock.listen(1)
    return lock


def unfreeze_console() -> None:
    """Отключает «быстрое выделение» в своём окне — иначе клик мышью замораживает мост.

    В консоли Windows выделение текста останавливает программу, которая в это окно
    пишет. Мост при этом остаётся в списке процессов и выглядит живым, а на деле
    стоит намертво, пока не нажмёшь Enter. Человек не может о таком догадаться.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # ввод консоли
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        quick_edit, extended = 0x0040, 0x0080
        kernel32.SetConsoleMode(handle, (mode.value & ~quick_edit) | extended)
    except Exception:
        # Окна может не быть вовсе (служба, планировщик) — это не повод не запускаться.
        pass


def main() -> int:
    unfreeze_console()

    lock = take_lock()
    if lock is None:
        print("Мост уже запущен в другом окне.")
        print("Двух сразу быть не должно: они мешают друг другу, и сообщения теряются.")
        print()
        print("Если не понимаешь, какое окно лишнее - закрой все и запусти этот файл заново.")
        print()
        return 1

    return _serve()


def _serve() -> int:
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
