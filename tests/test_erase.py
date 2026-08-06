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
from pymax.protocol import Opcode

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
    """Реакции мост шлёт не методом библиотеки, а запросом — за него и цепляемся."""

    def __init__(self, отказ=None, реакция_отказ=None):
        self.стёрто = []
        self.запросы = []
        self._отказ = отказ
        self._реакция_отказ = реакция_отказ
        self.messages = SimpleNamespace(app=SimpleNamespace(invoke=self._invoke))

    async def delete_message(self, chat_id, message_ids, for_me):
        if self._отказ:
            raise self._отказ
        self.стёрто.append((chat_id, message_ids, for_me))
        return True

    async def _invoke(self, opcode, payload):
        if self._реакция_отказ:
            raise self._реакция_отказ
        self.запросы.append((opcode, payload))

    @property
    def реакции(self):
        """Что именно уехало реакцией: эмодзи или None, если реакцию сняли."""
        return [
            payload["reaction"]["id"] if opcode == Opcode.MSG_REACTION else None
            for opcode, payload in self.запросы
        ]


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


class ФальшивоеСообщение:
    """Команда в теме: её собственный номер и то, на что она отвечает."""

    def __init__(self, ответ_на=СООБЩЕНИЕ_TG, message_id=100):
        self.message_id = message_id
        self.reply_to_message = SimpleNamespace(message_id=ответ_на) if ответ_на else None
        self.ответы = []

    async def reply(self, text, **прочее):
        self.ответы.append(text)


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
        assert "В MAX стёрто, а здесь нет" in бот.сказано[0]

    def test_называет_настоящую_причину_а_не_первую_попавшуюся(self, мост):
        """Мост винил нехватку прав. Право было на месте, а сообщение — старше 48 часов.

        Цена ошибки здесь не в коде: человек идёт проверять настройки группы, ничего там
        не находит и решает, что мост сломан.
        """
        отказ = TelegramBadRequest(method=None, message="message can't be deleted")
        бот, _ = мост(бот=ФальшивыйБот(удаление=отказ))

        стереть()

        assert "48 часов" in бот.сказано[0]
        assert "право" not in бот.сказано[0].lower()

    def test_про_нехватку_права_говорит_именно_про_право(self, мост):
        отказ = TelegramBadRequest(method=None, message="not enough rights to delete messages")
        бот, _ = мост(бот=ФальшивыйБот(удаление=отказ))

        стереть()

        assert "Удаление сообщений" in бот.сказано[0]

    def test_незнакомый_отказ_показывает_как_есть(self, мост):
        """Придумывать объяснение нельзя, но и молчать нельзя — отдаём чужие слова."""
        отказ = TelegramBadRequest(method=None, message="что-то новенькое")
        бот, _ = мост(бот=ФальшивыйБот(удаление=отказ))

        стереть()

        assert "что-то новенькое" in бот.сказано[0]

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

    def test_значок_можно_поменять_в_настройках(self, monkeypatch, мост):
        """💩 нравится не всем, а значок — дело вкуса, не устройства моста."""
        monkeypatch.setattr(main, "DELETE_MARK", "🖕")
        бот, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция("🖕")))

        assert макс.стёрто != []
        assert бот.удалено == [СООБЩЕНИЕ_TG]

    def test_на_незнакомом_сообщении_значок_ничего_не_делает(self, мост):
        """Сообщения, не прошедшего через мост, в MAX нет — стирать нечего."""
        бот, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(main.DELETE_MARK, message_id=999)))

        assert макс.стёрто == []
        assert бот.удалено == []


class TestКомандаСтирания:
    """Значок быстрее, но о нём неоткуда узнать: команда стоит в списке и видна.

    Раз дороги две, вести они обязаны в одно и то же место — иначе человек будет
    гадать, какая из них «настоящая».
    """

    def test_команда_стирает_ровно_как_значок(self, мост):
        бот, макс = мост()
        команда = ФальшивоеСообщение()

        asyncio.run(main.on_del_command(команда))

        assert макс.стёрто == [(ЧАТ, [int(СООБЩЕНИЕ_MAX)], False)]
        assert СООБЩЕНИЕ_TG in бот.удалено

    def test_саму_команду_за_собой_убирает(self, мост):
        """Сообщения уже нет, а «/del» осталось бы висеть в теме мусором."""
        бот, _ = мост()
        команда = ФальшивоеСообщение(message_id=100)

        asyncio.run(main.on_del_command(команда))

        assert бот.удалено == [СООБЩЕНИЕ_TG, 100]

    def test_без_ответа_объясняет_а_не_молчит(self, мост):
        """Иначе решишь, что команда не работает, — а мост просто не понял, что стирать."""
        _, макс = мост()
        команда = ФальшивоеСообщение(ответ_на=None)

        asyncio.run(main.on_del_command(команда))

        assert макс.стёрто == []
        assert "Ответь этой командой" in команда.ответы[0]
        assert main.DELETE_MARK in команда.ответы[0]

    def test_на_чужом_сообщении_говорит_что_стирать_нечего(self, мост):
        """Через мост оно не проходило — в MAX его нет, и здесь мост его не тронет."""
        бот, макс = мост()
        команда = ФальшивоеСообщение(ответ_на=999)

        asyncio.run(main.on_del_command(команда))

        assert макс.стёрто == []
        assert бот.удалено == []
        assert "через мост не проходило" in команда.ответы[0]

    def test_отказ_max_доходит_до_человека(self, мост):
        """Ровно как со значком: наполовину сделанная работа хуже несделанной."""
        бот, _ = мост(макс=ФальшивыйMAX(отказ=ApiError(opcode=64, message="слишком старое")))

        asyncio.run(main.on_del_command(ФальшивоеСообщение()))

        assert СООБЩЕНИЕ_TG not in бот.удалено
        assert "Не удалено в MAX" in бот.сказано[0]


class TestРеакцияДоезжаетДоMAX:
    """Полгода мост уверял, что реакции работают. В MAX не ушла ни одна.

    Библиотека клала номер сообщения строкой, MAX на этом месте ждёт число: отвечал
    «Expected number» и рвал связь. Заметить это было неоткуда — мост писал про отказ
    только в лог, а под сообщением в Telegram реакция стояла как ни в чём не бывало.
    """

    def test_номер_сообщения_уходит_числом(self, мост):
        """Тот самый гвоздь: строка вместо числа — и MAX отказывает."""
        _, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция("❤")))

        ((опкод, запрос),) = макс.запросы
        assert опкод == Opcode.MSG_REACTION
        assert запрос["messageId"] == int(СООБЩЕНИЕ_MAX)
        assert not isinstance(запрос["messageId"], str)
        assert запрос == {
            "chatId": ЧАТ,
            "messageId": int(СООБЩЕНИЕ_MAX),
            "reaction": {"reactionType": "EMOJI", "id": "❤"},
        }

    def test_снятие_реакции_тоже_числом(self, мост):
        _, макс = мост()

        asyncio.run(main.on_tg_reaction(реакция(None)))

        ((опкод, запрос),) = макс.запросы
        assert опкод == Opcode.MSG_CANCEL_REACTION
        assert запрос == {"chatId": ЧАТ, "messageId": int(СООБЩЕНИЕ_MAX)}

    def test_отказ_не_остаётся_в_логе(self, мост):
        """Ты видишь свою реакцию под сообщением и уверен, что она ушла. Надо сказать."""
        отказ = ApiError(opcode=178, message="Expected number at 24")
        бот, _ = мост(макс=ФальшивыйMAX(реакция_отказ=отказ))

        asyncio.run(main.on_tg_reaction(реакция("❤")))

        assert len(бот.сказано) == 1
        assert "Реакция не ушла в MAX" in бот.сказано[0]

    def test_битая_связка_не_роняет_мост(self, мост):
        """MAX нумерует по-своему; на нечисловом номере мост должен пожаловаться, а не упасть."""
        бот, макс = мост()
        main.topics.pair_messages(ЧАТ, "не-число", 999)

        asyncio.run(main.on_tg_reaction(реакция("❤", message_id=999)))

        assert макс.запросы == []
        assert "Реакция не ушла в MAX" in бот.сказано[0]
