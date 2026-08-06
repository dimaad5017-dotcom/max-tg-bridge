"""Команды-пульт: `/chats`, `/leave`, `/hidden`.

Смысл моста в том, чтобы не открывать MAX. Пока список чатов, выход из группы и
скрытый режим живут только в самом MAX, туда всё равно приходится заходить —
значит, и они должны работать из темы в Telegram.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pymax.exceptions import ApiError

from bridge import main
from bridge.storage import TopicMap

ГРУППА, ЛИЧКА, ТЕМА = 777, 93794816, 5


def чат(chat_id=ГРУППА, title="9А класс", status="ACTIVE"):
    return SimpleNamespace(id=chat_id, title=title, status=status)


class ФальшивыйMAX:
    def __init__(self, чаты=(), отказ=None):
        self._чаты = list(чаты)
        self._отказ = отказ
        self.покинуто = []
        self.настройки = []

    async def fetch_chats(self):
        return self._чаты

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, title="Чат", type="CHAT", participants=[])

    async def leave_group(self, chat_id):
        if self._отказ:
            raise self._отказ
        self.покинуто.append(chat_id)

    async def change_profile_settings(self, settings):
        if self._отказ:
            raise self._отказ
        self.настройки.append(settings)
        return True


class ФальшивоеСообщение:
    def __init__(self, тема=ТЕМА):
        self.message_thread_id = тема
        self.сказано = []

    async def reply(self, text, **прочее):
        self.сказано.append(text)


class ФальшивыйБот:
    def __init__(self):
        self.закрыто = []

    async def close_forum_topic(self, chat_id, message_thread_id):
        self.закрыто.append(message_thread_id)


@pytest.fixture
def мост(monkeypatch, tmp_path):
    карта = TopicMap(tmp_path / "topics.db")
    карта.link(ГРУППА, ТЕМА, "9А класс")
    monkeypatch.setattr(main, "topics", карта)
    monkeypatch.setattr(main, "max_ready", asyncio.Event())
    monkeypatch.setattr(main, "group_chats", {ГРУППА: True, ЛИЧКА: False})
    main.max_ready.set()
    бот = ФальшивыйБот()
    monkeypatch.setattr(main, "bot", бот)

    def собрать(макс=None):
        макс = макс or ФальшивыйMAX()
        monkeypatch.setattr(main, "client", макс)
        return макс, бот

    return собрать


def приказ(args=""):
    return SimpleNamespace(args=args)


class TestСписокЧатов:
    def запросить(self, сообщение):
        asyncio.run(main.on_chats_command(сообщение))

    def test_показывает_и_заведённые_и_ещё_безымянные(self, мост):
        """Тему заводит первое сообщение, поэтому часть чатов в Telegram ещё не видна."""
        мост(ФальшивыйMAX([чат(), чат(chat_id=888, title="Родители 9А")]))
        сообщение = ФальшивоеСообщение()

        self.запросить(сообщение)

        ответ = сообщение.сказано[0]
        assert "9А класс" in ответ and "тема есть" in ответ
        assert "Родители 9А" in ответ and "темы ещё нет" in ответ

    def test_покинутые_чаты_не_показывает(self, мост):
        """Чат, из которого нас выгнали, в списке — только лишний шум."""
        мост(ФальшивыйMAX([чат(status="REMOVED")]))
        сообщение = ФальшивоеСообщение()

        self.запросить(сообщение)

        assert "9А класс" not in сообщение.сказано[0]

    def test_пустой_список_объясняет_а_не_молчит(self, мост):
        """Ноль чатов у нового аккаунта — норма, но выглядит как поломка."""
        мост(ФальшивыйMAX([]))
        сообщение = ФальшивоеСообщение()

        self.запросить(сообщение)

        assert "/write" in сообщение.сказано[0]


class TestВыходИзЧата:
    def выйти(self, сообщение, args=""):
        asyncio.run(main.on_leave_command(сообщение, приказ(args)))

    def test_с_первого_раза_не_выходит_а_переспрашивает(self, мост):
        """Темы стоят в списке вплотную, и промах стоит нового приглашения."""
        макс, _ = мост()
        сообщение = ФальшивоеСообщение()

        self.выйти(сообщение)

        assert макс.покинуто == []
        assert "/leave да" in сообщение.сказано[0]

    def test_по_подтверждению_выходит_и_закрывает_тему(self, мост):
        макс, бот = мост()
        сообщение = ФальшивоеСообщение()

        self.выйти(сообщение, "да")

        assert макс.покинуто == [ГРУППА]
        assert бот.закрыто == [ТЕМА]

    def test_из_лички_выйти_нельзя(self, мост):
        """В MAX личка не «покидается» — обещать такое значит соврать."""
        макс, _ = мост()
        main.topics.link(ЛИЧКА, 9, "Павел")
        сообщение = ФальшивоеСообщение(тема=9)

        self.выйти(сообщение, "да")

        assert макс.покинуто == []
        assert "личка" in сообщение.сказано[0]

    def test_вне_темы_объясняет_куда_писать(self, мост):
        мост()
        сообщение = ФальшивоеСообщение(тема=None)

        self.выйти(сообщение, "да")

        assert "внутри темы" in сообщение.сказано[0]

    def test_отказ_max_не_остаётся_незамеченным(self, мост):
        """Тема закрылась бы, а из чата мы бы не вышли — самая вредная половина дела."""
        _, бот = мост(ФальшивыйMAX(отказ=ApiError(opcode=75, message="нельзя")))
        сообщение = ФальшивоеСообщение()

        self.выйти(сообщение, "да")

        assert бот.закрыто == []
        assert "MAX не дал выйти" in сообщение.сказано[0]


class TestСкрытыйРежим:
    def спрятать(self, сообщение, args=""):
        asyncio.run(main.on_hidden_command(сообщение, приказ(args)))

    def test_включает_скрытие(self, мост):
        """Мост держит связь сутками, и без этого ты для всех вечно в сети."""
        макс, _ = мост()
        сообщение = ФальшивоеСообщение()

        self.спрятать(сообщение, "on")

        (настройка,) = макс.настройки
        assert настройка.hide_online_status is True

    def test_выключает_скрытие(self, мост):
        макс, _ = мост()

        self.спрятать(ФальшивоеСообщение(), "off")

        assert макс.настройки[0].hide_online_status is False

    def test_без_слова_объясняет_а_не_гадает(self, мост):
        """«/hidden» без «on» может значить и то, и другое — молча выбрать нельзя."""
        макс, _ = мост()
        сообщение = ФальшивоеСообщение()

        self.спрятать(сообщение)

        assert макс.настройки == []
        assert "/hidden on" in сообщение.сказано[0]
