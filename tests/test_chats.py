"""Новая группа в MAX: мост обязан сказать о ней, даже если там ещё никто не написал.

Живое событие о добавлении приходит только работающему мосту. Если в это время он был
выключен, узнать о группе можно лишь при следующем запуске — по списку чатов. Пока это
не работало, добавление в школьный чат оставалось невидимым до первого сообщения.
"""

import asyncio
from types import SimpleNamespace

import pytest

from bridge import main
from bridge.storage import TopicMap


def чат(chat_id=777, kind="CHAT", status="ACTIVE", title="9А класс"):
    """Чат из списка MAX. Непрочитанных нет нарочно: проверяем именно тишину."""
    return SimpleNamespace(
        id=chat_id,
        type=kind,
        status=status,
        title=title,
        new_messages=0,
        invited_by=None,
        participants=[],
        participants_count=12,
        description=None,
    )


class ФальшивыйБот:
    def __init__(self):
        self.отправлено = []

    async def send_message(self, chat_id, text, **прочее):
        self.отправлено.append(text)


class ФальшивыйMAX:
    def __init__(self, чаты):
        self._чаты = чаты
        self.me = SimpleNamespace(contact=SimpleNamespace(id=1))

    async def fetch_chats(self):
        return self._чаты

    async def fetch_history(self, chat_id, backward):
        return []


@pytest.fixture
def мост(monkeypatch, tmp_path):
    """Мост с настоящей базой тем, но без выхода в сеть."""
    бот = ФальшивыйБот()
    monkeypatch.setattr(main, "bot", бот)
    monkeypatch.setattr(main, "topics", TopicMap(tmp_path / "topics.db"))

    async def тема(chat_id, title=None):
        return 5

    monkeypatch.setattr(main, "_ensure_topic", тема)
    return бот


def догонялка(чаты):
    asyncio.run(main._catch_up(ФальшивыйMAX(чаты)))


def test_догонялка_замечает_группу_где_ещё_никто_не_писал(мост):
    """Иначе про добавление в школьный чат узнаёшь только с первым сообщением — то есть не скоро."""
    догонялка([чат(title="9А класс")])

    assert len(мост.отправлено) == 1
    assert "9А класс" in мост.отправлено[0]


def test_про_личку_догонялка_молчит(мост):
    """Личный чат заводится сам от первого сообщения, объявлять о нём нечего."""
    догонялка([чат(kind="DIALOG", title="Павел")])

    assert мост.отправлено == []


@pytest.mark.parametrize("статус", ["LEFT", "REMOVED", "CLOSED"])
def test_про_покинутую_группу_догонялка_молчит(мост, статус):
    """Чат, из которого нас выгнали, темой заводить незачем — он уже ничей."""
    догонялка([чат(status=статус)])

    assert мост.отправлено == []


def test_о_знакомой_группе_второй_раз_не_объявляет(мост):
    """Мост запускают каждый день; повторяющееся объявление человек перестанет читать."""
    main.topics.link(777, 5, "9А класс")

    догонялка([чат()])

    assert мост.отправлено == []


def test_странный_чат_не_ломает_догонялку(мост, monkeypatch):
    """Одна непонятная запись не должна лишить остальные чаты проверки пропущенного."""

    async def падает(chat_id, title=None):
        raise RuntimeError("тема не создалась")

    monkeypatch.setattr(main, "_ensure_topic", падает)

    догонялка([чат(chat_id=1), чат(chat_id=2)])

    assert мост.отправлено == []
