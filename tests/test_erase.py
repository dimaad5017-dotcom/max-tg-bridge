"""Стереть сообщение везде, а не только у себя.

Telegram не сообщает ботам об удалении сообщений — такого события нет в его API.
Поэтому «убрать» приходится показывать реакцией: её мост увидеть может. Главное
здесь — не оставить работу сделанной наполовину: человек решил, что сообщения
быть не должно, а оно осталось лежать у собеседника.
"""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from pymax.exceptions import ApiError

from bridge import main
from bridge.storage import TopicMap

ЧАТ, СООБЩЕНИЕ_MAX, СООБЩЕНИЕ_TG = 93794816, "117038257748258810", 46


class ФальшивыйБот:
    id = 777

    def __init__(self, удаление=None):
        self.сказано = []
        self.удалено = []
        self._удаление = удаление

    async def send_message(self, chat_id, text, **прочее):
        self.сказано.append(text)

    async def delete_message(self, chat_id, message_id):
        if self._удаление:
            raise self._удаление
        self.удалено.append(message_id)


class ФальшивыйMAX:
    def __init__(self, отказ=None):
        self.стёрто = []
        self.реакции = []
        self._отказ = отказ

    async def delete_message(self, chat_id, message_ids, for_me):
        if self._отказ:
            raise self._отказ
        self.стёрто.append((chat_id, message_ids, for_me))
        return True

    async def add_reaction(self, chat_id, message_id, emoji):
        self.реакции.append(emoji)

    async def remove_reaction(self, chat_id, message_id):
        self.реакции.append(None)


@pytest.fixture
def мост(monkeypatch, tmp_path):
    """Мост с настоящей базой связок, но без выхода в сеть."""
    карта = TopicMap(tmp_path / "topics.db")
    карта.pair_messages(ЧАТ, СООБЩЕНИЕ_MAX, СООБЩЕНИЕ_TG)
    monkeypatch.setattr(main, "topics", карта)
    monkeypatch.setattr(main, "max_ready", asyncio.Event())
    main.max_ready.set()

    def собрать(бот=None, макс=None):
        бот = бот or ФальшивыйБот()
        макс = макс or ФальшивыйMAX()
        monkeypatch.setattr(main, "bot", бот)
        monkeypatch.setattr(main, "client", макс)
        return бот, макс

    return собрать


def реакция(emoji, message_id=СООБЩЕНИЕ_TG, кто=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=кто),
        message_id=message_id,
        new_reaction=[SimpleNamespace(type="emoji", emoji=emoji)] if emoji else [],
    )


def стереть():
    asyncio.run(main._erase(ЧАТ, СООБЩЕНИЕ_MAX, СООБЩЕНИЕ_TG))


class TestСтираниеВездеСразу:
    def test_убирает_и_в_max_и_в_telegram(self, мост):
        """Стереть только у себя — это не «передумал», это самообман."""
        бот, макс = мост()

        стереть()

        assert макс.стёрто == [(ЧАТ, [int(СООБЩЕНИЕ_MAX)], False)]
        assert бот.удалено == [СООБЩЕНИЕ_TG]
        assert бот.сказано == []

    def test_max_отказал_значит_и_здесь_не_трогаем(self, мост):
        """Иначе копия исчезнет, а оригинал останется — и ты будешь думать, что стёр."""
        бот, _ = мост(макс=ФальшивыйMAX(отказ=ApiError(opcode=64, message="слишком старое")))

        стереть()

        assert бот.удалено == []
        assert len(бот.сказано) == 1
        assert "Не удалено в MAX" in бот.сказано[0]

    def test_про_свою_неудачу_тоже_говорит(self, мост):
        """В MAX уже стёрто. Промолчать здесь — значит соврать, что ничего не вышло."""
        отказ = TelegramBadRequest(method=None, message="not enough rights")
        бот, макс = мост(бот=ФальшивыйБот(удаление=отказ))

        стереть()

        assert макс.стёрто != []
        assert "В MAX стёрто, а здесь не смог" in бот.сказано[0]

    def test_нечисловой_номер_не_роняет_мост(self, мост):
        """MAX нумерует по-своему, и на битой связке мост должен пожаловаться, а не упасть."""
        бот, макс = мост()

        asyncio.run(main._erase(ЧАТ, "не-число", СООБЩЕНИЕ_TG))

        assert макс.стёрто == []
        assert "Не удалено в MAX" in бот.сказано[0]


class TestЗначокСтирания:
    def test_значок_стирает_а_не_уезжает_реакцией(self, мост):
        """Иначе собеседник увидит 💩 под своим сообщением — вместо того чтобы оно исчезло."""
        бот, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(main.DELETE_MARK)))

        assert макс.реакции == []
        assert макс.стёрто != []
        assert бот.удалено == [СООБЩЕНИЕ_TG]

    def test_обычная_реакция_по_прежнему_уезжает(self, мост):
        _, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция("❤")))

        assert макс.реакции == ["❤"]
        assert макс.стёрто == []

    def test_снятая_реакция_ничего_не_стирает(self, мост):
        """Снять реакцию — это не «удали»: пустой список не должен читаться как значок."""
        _, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(None)))

        assert макс.реакции == [None]
        assert макс.стёрто == []

    def test_свою_отметку_о_прочтении_мост_не_принимает_за_приказ(self, мост):
        """Отметки ставит сам бот, и они прилетают сюда же — иначе вышла бы петля."""
        бот, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(main.DELETE_MARK, кто=бот.id)))

        assert макс.стёрто == []

    def test_на_незнакомом_сообщении_значок_ничего_не_делает(self, мост):
        """Сообщения, не прошедшего через мост, в MAX нет — стирать нечего."""
        бот, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(main.DELETE_MARK, message_id=999)))

        assert макс.стёрто == []
        assert бот.удалено == []
