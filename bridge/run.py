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


def take_lock(port: int = LOCK_PORT) -> socket.socket | None:
    """Занимает место единственного моста. None — значит, мост уже где-то запущен.

    Два моста на одном аккаунте не удваивают надёжность, а мешают друг другу:
    Telegram отдаёт входящее только одному из двух, а MAX при каждом входе меняет
    ключ сессии и выбивает предыдущего. Со стороны это выглядит как «иногда не
    доходит» — то есть как раз то молчание, которого здесь быть не должно.
    """
    lock = socket.socket()
    try:
        # Нарочно без SO_REUSEADDR: нам нужно именно то, чтобы второй раз не занялось.
        lock.bind(("127.0.0.1", port))
    except OSError:
        lock.close()
        return None
    lock.listen(1)
    return lock


# Битики режима консольного окна. Имена — из документации Windows:
# ENABLE_QUICK_EDIT_MODE, ENABLE_MOUSE_INPUT, ENABLE_EXTENDED_FLAGS.
QUICK_EDIT = 0x0040
MOUSE_INPUT = 0x0010
EXTENDED_FLAGS = 0x0080


def deaf_to_mouse(mode: int) -> int:
    """Каким должен стать режим окна, чтобы мышь не могла навредить мосту.

    «Быстрое выделение» выключаем: в консоли Windows выделение текста
    останавливает программу, которая в это окно пишет, — один случайный клик,
    и мост стоит намертво, пока кто-нибудь не нажмёт Enter.

    Мышиный ввод выключаем следом, и это не мелочь. Без «быстрого выделения»
    консоль начинает складывать движения колеса программе во ввод; мост ввод не
    читает — и окно перестаёт листаться колесом вовсе. Забери у ввода мышь
    целиком — и колесо снова листает окно, а клики не делают ничего.
    """
    return (mode & ~QUICK_EDIT & ~MOUSE_INPUT) | EXTENDED_FLAGS


def unfreeze_console() -> None:
    """Просит своё окно не замирать от мыши: клик не морозит мост, колесо листает.

    Мост, замороженный выделением, остаётся в списке процессов и выглядит живым,
    а на деле стоит, пока не нажмёшь Enter. Человек не может о таком догадаться.
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
        kernel32.SetConsoleMode(handle, deaf_to_mouse(mode.value))
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
    print("В окне мост пишет только своё; полная запись - в файле cache\\bridge.log.")
    print("Копировать оттуда: открой посмотреть-лог.cmd - лог откроется Блокнотом.")
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
