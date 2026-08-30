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
from aiogram.exceptions import TelegramBadRequest

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
    def __init__(self, переименовывать=True):
        self.отправлено = []
        self.переименовано = []
        self.переименовывать = переименовывать

    async def send_message(self, chat_id, text, **прочее):
        self.отправлено.append(text)

    async def edit_forum_topic(self, chat_id, message_thread_id, name):
        if not self.переименовывать:
            raise TelegramBadRequest(method=None, message="not enough rights")
        self.переименовано.append((message_thread_id, name))


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

    def test_дозор_подхватывает_и_переименование(self, мост, дозор):
        """Переименовать могли и пока мост молчал: тогда живого события уже не будет."""
        main.topics.link(777, 5, "9А класс")

        дозор([чат(title="10А класс")])

        assert мост.переименовано == [(5, "10А класс")]


class TestПереименование:
    """Чат переименовали в MAX — тема обязана назваться так же.

    Школьные чаты переименовывают: «9А класс» становится «10А», к названию дописывают
    год или имя учителя. Про само переименование мост говорил строкой в теме, а имя темы
    оставалось прежним навсегда — и через год в списке висел класс, которого уже нет.
    Список тем здесь единственный способ найти нужный чат: не найдёшь либо найдёшь не то.
    """

    def подтянуть(self, **поля):
        asyncio.run(main._sync_chat(чат(**поля)))

    def test_тема_получает_новое_имя(self, мост):
        main.topics.link(777, 5, "9А класс")

        self.подтянуть(title="10А класс")

        assert мост.переименовано == [(5, "10А класс")]

    def test_второй_раз_то_же_имя_не_трогает(self, мост):
        """Дозор приносит тот же список каждые пять минут — это был бы запрос на пустом месте."""
        main.topics.link(777, 5, "9А класс")

        self.подтянуть(title="10А класс")
        self.подтянуть(title="10А класс")

        assert len(мост.переименовано) == 1

    def test_совпадающее_имя_вообще_не_повод_ходить_в_telegram(self, мост):
        main.topics.link(777, 5, "9А класс")

        self.подтянуть(title="9А класс")

        assert мост.переименовано == []

    def test_длинное_имя_обрезает_до_предела_telegram(self, мост):
        """Длиннее 128 знаков Telegram имя темы не примет — а название пишут люди."""
        main.topics.link(777, 5, "старое")

        self.подтянуть(title="я" * 300)

        assert len(мост.переименовано[0][1]) == 128

    def test_без_права_управлять_темами_не_ломает_мост(self, мост, monkeypatch):
        """Права может не быть. Про переименование человек всё равно узнает строкой в теме."""
        monkeypatch.setattr(main, "bot", ФальшивыйБот(переименовывать=False))
        main.topics.link(777, 5, "9А класс")

        self.подтянуть(title="10А класс")

        assert main.topics.title_for_chat(777) == "9А класс"

    def test_про_чат_без_темы_молчит(self, мост):
        """Темы нет — переименовывать нечего, а заводить её из-за одного имени незачем."""
        self.подтянуть(chat_id=999, kind="DIALOG", title="Павел")

        assert мост.переименовано == []
