"""Не вышла ли новая версия моста.

Мост ставят ZIP-архивом и потом про него забывают, а MAX регулярно меняет свой
протокол. В какой-то день мост просто перестанет работать, и человек не поймёт,
что чинить — поэтому лучше предупредить заранее, пока всё ещё работает.
"""

import logging
import tomllib
from pathlib import Path

import aiohttp

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
PROJECT_URL = "https://github.com/dimaad5017-dotcom/max-tg-bridge"
LATEST_URL = "https://raw.githubusercontent.com/dimaad5017-dotcom/max-tg-bridge/master/pyproject.toml"

logger = logging.getLogger("bridge")


def _numbers(version: str) -> tuple[int, ...]:
    """«1.10.0» новее «1.9.0», хотя по алфавиту наоборот — сравнивать надо числами."""
    numbers = []
    for part in version.split("."):
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers)


def newer(published: str, installed: str) -> bool:
    there, here = _numbers(published), _numbers(installed)
    return bool(there) and bool(here) and there > here


def installed_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


async def published_version() -> str | None:
    """Версия на GitHub. None — если не дозвонились: проверка необязательная.

    Ошибку глотаем целиком нарочно. Нет интернета, GitHub прилёг, домен заблокировали —
    ничто из этого не повод не запускать мост и не повод пугать человека в группе.
    """
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
            session.get(LATEST_URL) as response,
        ):
            response.raise_for_status()
            return tomllib.loads(await response.text())["project"]["version"]
    except Exception as error:
        logger.info("про новую версию узнать не вышло: %s", error)
        return None
