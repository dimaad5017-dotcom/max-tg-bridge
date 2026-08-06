"""Новая группа в MAX: мост обязан сказать о ней, даже если там ещё никто не написал.

Живое событие о добавлении приходит только работающему мосту. Если в это время он был
выключен, узнать о группе можно лишь при следующем запуске — по списку чатов. Пока это
не работало, добавление в школьный чат оставалось невидимым до первого сообщения.
"""

import asyncio
from contextlib import suppress
from itertools import count
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

    номера = count(100)

    async def тема(chat_id, title=None):
        """Настоящая заводилка тем запоминает связку. Без этого мост объявит тот же чат снова."""
        topic_id = next(номера)
        main.topics.link(chat_id, topic_id, title or "")
        return topic_id

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


@pytest.fixture
def дозор(мост, monkeypatch):
    """Гоняет фоновую проверку списка чатов ровно столько кругов, сколько ей дали ответов.

    Настоящий дозор крутится вечно и спит по пять минут. Здесь сон убираем в ноль,
    а кончившиеся ответы обрываем отменой — так же, как обрывает задачу сам мост.
    """

    def запустить(*круги):
        осталось = list(круги)

        class MAXпоКругам:
            async def fetch_chats(self):
                if not осталось:
                    raise asyncio.CancelledError
                ответ = осталось.pop(0)
                if isinstance(ответ, Exception):
                    raise ответ
                return ответ

        monkeypatch.setattr(main, "client", MAXпоКругам())
        monkeypatch.setattr(main, "NEW_CHAT_SCAN", 0)
        monkeypatch.setattr(main, "max_ready", asyncio.Event())

        async def прогнать():
            main.max_ready.set()
            with suppress(asyncio.CancelledError):
                await main._watch_new_chats()

        asyncio.run(прогнать())

    return запустить


class TestДозорЗаНовымиЧатами:
    """Событие о добавлении в группу приходит от MAX, и проверить его нечем: добавляют не мы.

    Поэтому мост ещё и сам перечитывает список. Иначе тихая группа, где никто не написал,
    оставалась бы невидимой до перезапуска — а мост держат запущенным неделями.
    """

    def test_замечает_группу_появившуюся_на_ходу(self, мост, дозор):
        дозор([], [чат(title="9А класс")])

        assert len(мост.отправлено) == 1
        assert "9А класс" in мост.отправлено[0]

    def test_об_одной_группе_дважды_не_говорит(self, мост, дозор):
        """Дозор видит тот же список каждые пять минут — объявление не должно повторяться."""
        дозор([чат()], [чат()])

        assert len(мост.отправлено) == 1

    def test_сбой_max_не_обрывает_дозор(self, мост, дозор):
        """Сеть моргает; если после этого дозор замолчит, добавление в чат мы пропустим."""
        дозор(RuntimeError("MAX не ответил"), [чат(title="9А класс")])

        assert len(мост.отправлено) == 1
