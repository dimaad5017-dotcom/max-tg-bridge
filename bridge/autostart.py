"""Включатель автозапуска: мост поднимается сам при входе в Windows.

Мост работает, только пока открыто его окно. Выключил компьютер вечером, утром не
вспомнил открыть — и весь день сообщения копятся в MAX, куда ты как раз и не заходишь.
Узнаёшь об этом не из Telegram, а от классного руководителя. Ради этого мост и писался,
и ровно это ломается тише всего: он не падает, не ругается, его просто нет.

Руками это делается за минуту — папка автозагрузки, ярлык, «свёрнутое в значок», — но
шагов шесть, и каждый можно сделать не так. Поэтому здесь то же самое одним запуском.

Ничего хитрого тут нет: в папку автозагрузки кладётся обычный `.cmd`, который заводит
мост свёрнутым. Ни реестра, ни служб, ни планировщика — то же самое место, куда человек
и сам положил бы ярлык, и убирается так же легко: удалить файл.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

# Имя файла в папке автозагрузки. По-русски и с понятным смыслом: человек однажды
# заглянет в эту папку и должен узнать свой мост, а не гадать, что это за штука.
NAME = "мост-MAX-Telegram.cmd"

# Метка «этот файл сделали мы». Выключение — это удаление файла, а имя может однажды
# совпасть с чужим, положенным руками. Без метки мы бы стирали чужое молча.
MARK = "rem max-tg-bridge autostart"

PROJECT = Path(__file__).resolve().parent.parent


def console_encoding() -> str:
    """Кодировка, в которой cmd.exe читает командные файлы.

    Не UTF-8. На русской Windows это cp866, и русские буквы в файле, записанном как
    UTF-8, превращаются в кашу — вместе с путями, если в имени пользователя кириллица.
    А оно кириллицей. Спрашиваем у самой системы, а не угадываем: раскладки бывают разные.
    """
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return "cp866"


def startup_dir() -> Path | None:
    """Папка автозагрузки текущего пользователя. None — значит, мы не в Windows."""
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


LAUNCHER = "4-запустить-мост.cmd"


def script(project: Path) -> str:
    """Тот самый командный файл. Пути полные: автозагрузка стартует не из папки моста.

    Запускаем не питон напрямую, а тот же файл, по которому щёлкают руками. Разница
    в одной строке в его конце — `pause`. Без неё окно моста, который сдался, просто
    исчезает: свёрнутое окно закрылось само, и о том, что весь день моста не было,
    узнать неоткуда. С ней сдавшийся мост остаётся в панели задач вместе с причиной.
    """
    launcher = project / LAUNCHER
    return (
        "@echo off\r\n"
        f"{MARK}\r\n"
        "chcp 866 >nul\r\n"
        f'cd /d "{project}"\r\n'
        "\r\n"
        f'if not exist "{launcher}" (\r\n'
        "  echo Мост не найден: похоже, папку переместили или удалили.\r\n"
        "  echo Запусти 5-автозапуск.cmd из новой папки, чтобы поправить.\r\n"
        "  echo.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "rem Свёрнутым: мост нужен работающим, а не заслоняющим экран при каждом входе.\r\n"
        f'start "Мост MAX - Telegram" /min "{launcher}"\r\n'
    )


def ours(where: Path) -> bool:
    """Наш ли это файл. Читаем байтами: чужой мог быть записан в любой кодировке."""
    try:
        return MARK.encode("ascii") in where.read_bytes()
    except OSError:
        return False


def enabled(folder: Path) -> bool:
    """Включён ли автозапуск моста."""
    return ours(folder / NAME)


def foreign(folder: Path) -> bool:
    """Файл с таким именем есть, но не наш. Трогать нельзя."""
    where = folder / NAME
    return where.exists() and not ours(where)


def stale(folder: Path, project: Path = PROJECT) -> bool:
    """Файл наш, но не тот: мост переехал в другую папку или сам с тех пор изменился.

    Автозагрузка запомнила полный путь один раз и о переезде не узнает. Молча не
    запустится — а это ровно то, ради чего автозапуск и включали.
    """
    where = folder / NAME
    if not ours(where):
        return False
    try:
        return where.read_bytes() != script(project).encode(console_encoding())
    except OSError:
        return False


def enable(folder: Path, project: Path = PROJECT) -> bool:
    """Кладём файл в автозагрузку. False — значит, там уже лежит чужое."""
    if foreign(folder):
        return False
    folder.mkdir(parents=True, exist_ok=True)
    (folder / NAME).write_bytes(script(project).encode(console_encoding()))
    return True


def disable(folder: Path) -> bool:
    """Убираем свой файл. Чужой с тем же именем остаётся на месте."""
    where = folder / NAME
    if not ours(where):
        return False
    where.unlink()
    return True


def main() -> int:
    folder = startup_dir()
    if folder is None:
        print("Это включатель для Windows.")
        print("Для Mac и Linux автозапуск описан в mac-linux/README.md.")
        return 1

    if foreign(folder):
        print("В папке автозагрузки уже лежит чужой файл с таким же именем:")
        print(f"  {folder / NAME}")
        print("Трогать его я не буду. Убери или переименуй его и запусти меня снова.")
        return 1

    now_on = enabled(folder)
    outdated = now_on and stale(folder)

    if outdated:
        print("Автозапуск включён, но записан по-старому: мост с тех пор обновился")
        print("или папку с ним перенесли. При входе в Windows он может не запуститься.")
        print()
        print("Обновить?")
    else:
        print("Автозапуск сейчас:", "включён" if now_on else "выключен")
        print()
        print("Выключить?" if now_on else "Включить?")
    answer = input("Введи «д» и нажми Enter (или просто Enter, чтобы ничего не менять): ")

    if answer.strip().lower() not in {"д", "да", "y", "yes"}:
        print("Ничего не изменил.")
        return 0

    if outdated:
        enable(folder)
        print()
        print("Обновил. Запись в автозагрузке снова верная.")
        print("Чтобы выключить автозапуск совсем, запусти этот файл ещё раз.")
        return 0

    if now_on:
        disable(folder)
        print()
        print("Выключил. Теперь мост запускается только вручную —")
        print("двойным щелчком по 4-запустить-мост.cmd.")
        return 0

    enable(folder)
    print()
    print("Включил. Мост будет подниматься сам при входе в Windows, свёрнутым в значок.")
    print("Проверить: перезагрузи компьютер, через полминуты напиши в группе /help.")
    print()
    print("Прямо сейчас мост от этого не запустится: если он не работает,")
    print("запусти 4-запустить-мост.cmd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
