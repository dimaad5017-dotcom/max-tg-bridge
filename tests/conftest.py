"""Подготовка окружения до того, как соберётся `bridge.main`.

Мост при импорте читает `.env` и открывает `cache/`. В тестах ни того, ни другого
быть не должно: настоящий токен и настоящая сессия MAX сюда попасть не могут.
"""

import os
import tempfile
from pathlib import Path

import pytest

# Присваиванием, а не setdefault: `load_dotenv` не перебивает уже заданные значения,
# поэтому так настоящие токен и номер из `.env` не попадут в тесты даже на машине,
# где `.env` лежит рядом.
os.environ["TG_BOT_TOKEN"] = "123456789:тестовый-токен-не-настоящий"
os.environ["TG_GROUP_ID"] = "-1001234567890"
os.environ["MAX_PHONE"] = "+79000000000"

# Импорт нарочно ниже присваиваний: config читает `.env` прямо при загрузке.
from bridge import config

# `bridge.main` при импорте создаёт TopicMap и клиента MAX. Уводим их в отдельную
# папку, иначе тесты писали бы в рабочую базу тем и трогали файл сессии.
_SANDBOX = Path(tempfile.mkdtemp(prefix="max-tg-bridge-tests-"))
config.WORK_DIR = _SANDBOX
config.MAP_DB = _SANDBOX / "topics.db"


@pytest.fixture(autouse=True)
def _чистые_очереди():
    """Замки на чаты — с чистого листа на каждый тест.

    Мост живёт в одном цикле событий, а тесты заводят по своему на каждый: замок,
    подождавший своей очереди в прошлом тесте, запомнил тот цикл и в следующем
    падает с «bound to a different event loop». Правится не в мосте, а здесь: это
    не его беда, а особенность того, как мы его запускаем.
    """
    from bridge import main

    main._chat_queue.clear()
    main._topic_queue.clear()
    yield
    main._chat_queue.clear()
    main._topic_queue.clear()


@pytest.fixture
def topics(tmp_path):
    """Пустое хранилище связок на один тест."""
    from bridge.storage import TopicMap

    return TopicMap(tmp_path / "topics.db")
