"""Исправил опечатку в теме — исправляется и у собеседника в MAX.

Правку Telegram присылает отдельным событием, а не сообщением, — мост её долго не
слушал вовсе. Со стороны это выглядело хуже, чем «не умеет»: в теме сообщение
менялось, а у собеседника оставалось прежнее, и узнать об этом было неоткуда.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pymax.exceptions import ApiError

from bridge import main
from bridge.storage import TopicMap

ЧАТ, СООБЩЕНИЕ_MAX, СООБЩЕНИЕ_TG, ТЕМА = 93794816, "117038257748258810", 46, 5


class ФальшивыйMAX:
    def __init__(self, отказ=None):
        self.правки = []
        self._отказ = отказ

    async def edit_message(self, chat_id, message_id, text=None, attachments=None):
        if self._отказ:
            raise self._отказ
        self.правки.append((chat_id, message_id, text))
        return SimpleNamespace(id=message_id)


class ФальшивоеСообщение:
    """Правка из темы. `reply` — единственный способ моста ответить именно на неё."""

    def __init__(self, text=None, caption=None, message_id=СООБЩЕНИЕ_TG):
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.message_thread_id = ТЕМА
        self.сказано = []

    async def reply(self, text, **прочее):
        self.сказано.append(text)


@pytest.fixture
def мост(monkeypatch, tmp_path):
    карта = TopicMap(tmp_path / "topics.db")
    карта.link(ЧАТ, ТЕМА, "Павел")
    карта.pair_messages(ЧАТ, СООБЩЕНИЕ_MAX, СООБЩЕНИЕ_TG)
    monkeypatch.setattr(main, "topics", карта)
    monkeypatch.setattr(main, "max_ready", asyncio.Event())
    main.max_ready.set()

    def собрать(макс=None):
        макс = макс or ФальшивыйMAX()
        monkeypatch.setattr(main, "client", макс)
        return макс

    return собрать


def править(сообщение):
    asyncio.run(main.on_tg_edit(сообщение))


def test_правка_уезжает_в_max(мост):
    """Опечатки правят все — без этого «пульт» остаётся односторонним."""
    макс = мост()
    сообщение = ФальшивоеСообщение(text="привет, я опоздаю")

    править(сообщение)

    assert макс.правки == [(ЧАТ, int(СООБЩЕНИЕ_MAX), "привет, я опоздаю")]
    assert сообщение.сказано == []


def test_номер_сообщения_уходит_числом(мост):
    """MAX хранит номера строками, а в запросе ждёт число — на этом уже спотыкались реакции."""
    макс = мост()

    править(ФальшивоеСообщение(text="ещё раз"))

    ((_, номер, _),) = макс.правки
    assert not isinstance(номер, str)


def test_чужое_сообщение_не_трогаем(мост):
    """Сообщения, не прошедшего через мост, в MAX нет — править нечего, но и ругаться не о чем."""
    макс = мост()
    сообщение = ФальшивоеСообщение(text="правка", message_id=999)

    править(сообщение)

    assert макс.правки == []
    assert сообщение.сказано == []


def test_про_отказ_max_говорит_вслух(мост):
    """В теме стоит «изменено», и ты уверен, что собеседник читает исправленное."""
    макс = мост(ФальшивыйMAX(отказ=ApiError(opcode=67, message="слишком старое")))
    сообщение = ФальшивоеСообщение(text="правка")

    править(сообщение)

    assert макс.правки == []
    assert len(сообщение.сказано) == 1
    assert "Правка не ушла в MAX" in сообщение.сказано[0]
    assert "слишком старое" in сообщение.сказано[0]


def test_подпись_к_файлу_не_правим_а_объясняем(мост):
    """MAX правит вложения вместе с текстом: ради подписи он снёс бы сам файл."""
    макс = мост()
    сообщение = ФальшивоеСообщение(caption="новая подпись")

    править(сообщение)

    assert макс.правки == []
    assert "Правка не ушла" in сообщение.сказано[0]
    assert main.DELETE_MARK in сообщение.сказано[0]
