"""Закрытая тема не должна съедать сообщения.

Тему закрывает `/leave`, её можно закрыть и руками, прибираясь в списке. Связка
чат↔тема при этом остаётся, и первое же новое сообщение из MAX упирается в
TOPIC_CLOSED. Пока это не чинилось, сообщение пропадало совсем: в теме пусто, в
логе исключение, а узнать о письме можно было, только открыв MAX, — то есть ровно
то молчание, ради отсутствия которого мост и написан.
"""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from bridge import main
from bridge.storage import TopicMap

ЧАТ, ТЕМА = 777, 5


def сообщение(text="привет", message_id="100"):
    return SimpleNamespace(id=message_id, text=text, attaches=[], sender=42, time=1000, link=None)


class ФальшивыйБот:
    """Telegram, который отвергает закрытую тему ровно так же, как настоящий."""

    def __init__(self, закрытые=(), открывать=True):
        self.закрытые = set(закрытые)
        self.открывать = открывать
        self.отправлено = []
        self.открыто = []

    async def send_message(self, chat_id, text=None, message_thread_id=None, **прочее):
        if message_thread_id in self.закрытые:
            raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
        # Настоящий Telegram длинное не обрезает, а отвергает целиком — иначе тест
        # «доехало» проходил бы и тогда, когда в жизни не доезжает ничего.
        if text is not None and len(text) > main.MESSAGE_LIMIT:
            raise TelegramBadRequest(method=None, message="Bad Request: message is too long")
        self.отправлено.append((message_thread_id, text))
        return SimpleNamespace(message_id=len(self.отправлено))

    async def send_photo(self, chat_id, photo=None, message_thread_id=None, **прочее):
        if message_thread_id in self.закрытые:
            raise TelegramBadRequest(method=None, message="Bad Request: TOPIC_CLOSED")
        self.отправлено.append((message_thread_id, "фото"))
        return SimpleNamespace(message_id=len(self.отправлено))

    async def reopen_forum_topic(self, chat_id, message_thread_id):
        if not self.открывать:
            raise TelegramBadRequest(method=None, message="not enough rights")
        self.открыто.append(message_thread_id)
        self.закрытые.discard(message_thread_id)


class ФальшивыйMAX:
    async def read_message(self, message_id, chat_id):
        return None


@pytest.fixture
def мост(monkeypatch, tmp_path):
    карта = TopicMap(tmp_path / "topics.db")
    карта.link(ЧАТ, ТЕМА, "9А класс")
    monkeypatch.setattr(main, "topics", карта)
    monkeypatch.setattr(main, "client", ФальшивыйMAX())
    monkeypatch.setattr(main, "group_chats", {ЧАТ: False})

    def собрать(бот):
        monkeypatch.setattr(main, "bot", бот)
        return бот

    return собрать


def доставить(msg=None):
    asyncio.run(main._deliver(ЧАТ, msg or сообщение()))


class TestЗакрытаяТема:
    def test_открывает_её_и_доносит_сообщение(self, мост):
        """Главное здесь — что сообщение всё-таки в теме, а не что тема открылась."""
        бот = мост(ФальшивыйБот(закрытые=[ТЕМА]))

        доставить()

        assert бот.открыто == [ТЕМА]
        assert бот.отправлено == [(ТЕМА, "привет")]

    def test_с_открытой_темой_ничего_не_трогает(self, мост):
        """Открывать открытое — лишний запрос к Telegram на каждое сообщение."""
        бот = мост(ФальшивыйБот())

        доставить()

        assert бот.открыто == []

    def test_файл_доносит_так_же_как_текст(self, мост, monkeypatch):
        """Вложения уходят другим методом бота — мимо такой починки они бы и остались."""
        бот = мост(ФальшивыйБот(закрытые=[ТЕМА]))

        async def одно_фото(chat_id, message):
            return "", [main.Media("photo", b"file")]

        monkeypatch.setattr(main, "_compose", одно_фото)

        доставить()

        assert бот.открыто == [ТЕМА]
        assert бот.отправлено == [(ТЕМА, "фото")]

    def test_если_открыть_не_дали_уходит_в_общий_раздел(self, мост):
        """Без права «Управление темами» открыть нельзя — но потерять сообщение всё равно нельзя.

        В общем разделе оно на виду, в закрытой теме его нет вовсе. Так же мост
        поступает, когда тему не удалось даже создать.
        """
        бот = мост(ФальшивыйБот(закрытые=[ТЕМА], открывать=False))

        доставить()

        assert бот.отправлено == [(None, "привет")]

    def test_длинное_сообщение_не_путает_с_закрытой_темой(self, мост):
        """Обе беды приходят одним и тем же TelegramBadRequest, а чинятся по-разному."""
        бот = мост(ФальшивыйБот(закрытые=[ТЕМА]))

        доставить(сообщение(text="я" * 5000))

        assert бот.открыто == [ТЕМА]
        assert "".join(текст for _, текст in бот.отправлено) == "я" * 5000

    def test_чужой_отказ_не_выдаёт_за_закрытую_тему(self, мост):
        """Молча «чинить» непонятную ошибку — значит спрятать её и чинить не то."""

        class Вредный(ФальшивыйБот):
            async def send_message(self, chat_id, text=None, message_thread_id=None, **прочее):
                raise TelegramBadRequest(method=None, message="Bad Request: chat not found")

        бот = мост(Вредный())

        with pytest.raises(TelegramBadRequest):
            доставить()

        assert бот.открыто == []


class TestДлинноеСообщение:
    """Длиннее 4096 знаков Telegram не обрезает, а отвергает целиком.

    В школьных чатах такие пишут: что взять на выезд, расписание на четверть, объявление
    на две страницы. Пока это не чинилось, сообщение не доезжало вообще никак — ни текстом,
    ни строкой «не доставлено», — и узнать о нём можно было, только открыв MAX.
    """

    def test_доезжает_целиком(self, мост):
        бот = мост(ФальшивыйБот())

        доставить(сообщение(text="я" * 10000))

        assert "".join(текст for _, текст in бот.отправлено) == "я" * 10000

    def test_каждая_часть_влезает_в_предел(self, мост):
        бот = мост(ФальшивыйБот())

        доставить(сообщение(text="я" * 10000))

        assert [len(текст) for _, текст in бот.отправлено] == [4096, 4096, 1808]

    def test_короткое_остаётся_одним_сообщением(self, мост):
        """Разложить нечего — значит и лишних отправок быть не должно."""
        бот = мост(ФальшивыйБот())

        доставить(сообщение(text="привет"))

        assert бот.отправлено == [(ТЕМА, "привет")]

    def test_части_уходят_в_ту_же_тему(self, мост):
        бот = мост(ФальшивыйБот())

        доставить(сообщение(text="я" * 10000))

        assert {тема for тема, _ in бот.отправлено} == {ТЕМА}

    def test_правка_ищется_по_первой_части(self, мост):
        """На вторую половину не сошлёшься: правку и цитату человек ждёт в начале сообщения."""
        мост(ФальшивыйБот())

        доставить(сообщение(text="я" * 10000, message_id="777"))

        assert main.topics.tg_message_for(ЧАТ, "777") == 1

    def test_режет_по_строкам_а_не_по_живому(self, мост):
        """Разрыв посреди строки читается как обрыв связи, а по строке — как продолжение."""
        бот = мост(ФальшивыйБот())
        строки = [f"пункт {номер}" for номер in range(500)]

        доставить(сообщение(text="\n".join(строки)))

        склеено = "\n".join(текст for _, текст in бот.отправлено)
        assert склеено.split("\n") == строки
