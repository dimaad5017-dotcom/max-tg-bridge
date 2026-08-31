"""Что мост делает, когда связи с MAX нет.

Мост состоит из двух половин, и они поднимаются порознь: Telegram за секунду, MAX за
несколько. Всё, что идёт в MAX, ждёт вторую половину — и раньше ждало без срока.

Само по себе это было безобидно ровно до тех пор, пока MAX поднимался. А pymax при
обрыве не падает и не сдаётся: он молча уходит в бесконечный цикл «подождать и
попробовать снова». Мост при этом жив, окно открыто, Telegram отвечает — а половины,
которая говорит с MAX, у него нет, и не будет, пока связь не вернётся.

Тогда ожидание без срока превращалось в ту самую беду, ради которой мост писался.
Пишешь в тему «заберите ребёнка» — сообщение принято, галочка стоит, ответа нет. Не
«не отправлено», не ошибка — вообще ничего. Хуже того: Telegram считает сообщение
отданным мосту, так что перезапуск его не воскресит. Оно исчезает совсем.

Проверки здесь со сроком нарочно: зависший мост в тесте выглядел бы как зависший тест,
и разбираться пришлось бы уже не с мостом, а с тем, почему проверки не кончаются.
"""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.types import ErrorEvent

from bridge import main
from bridge.storage import TopicMap

ЧАТ, ТЕМА, СООБЩЕНИЕ = 555, 7, 4242

ВЛОЖЕНИЯ = ("photo", "video", "video_note", "animation", "voice", "audio", "sticker", "document")


class ФальшивоеПисьмо:
    def __init__(self, text="заберите ребёнка", тема=ТЕМА):
        self.message_thread_id = тема
        self.message_id = СООБЩЕНИЕ
        self.text = text
        self.caption = None
        self.content_type = "text"
        self.reply_to_message = None
        self.сказано = []
        for поле in ВЛОЖЕНИЯ:
            setattr(self, поле, None)

    async def reply(self, text, **прочее):
        self.сказано.append(text)

    async def answer(self, text, **прочее):
        self.сказано.append(text)


class ФальшивыйMAX:
    def __init__(self):
        self.отправлено = []
        self.удалено = []

    async def send_message(self, chat_id, text, reply_to=None, attachments=None):
        self.отправлено.append((chat_id, text))
        return SimpleNamespace(id="1")

    async def delete_message(self, chat_id, ids, for_me=False):
        self.удалено.append((chat_id, ids))


class ФальшивыйБот:
    id = 1

    def __init__(self):
        self.сказано = []

    async def send_message(self, chat_id, text, **прочее):
        self.сказано.append(text)


@pytest.fixture
def мост(monkeypatch, tmp_path):
    """Мост с картой чатов и подставным MAX. Связи с MAX по умолчанию нет."""
    карта = TopicMap(tmp_path / "topics.db")
    карта.link(ЧАТ, ТЕМА, "9А класс")
    карта.pair_messages(ЧАТ, "300", СООБЩЕНИЕ)
    monkeypatch.setattr(main, "topics", карта)
    monkeypatch.setattr(main, "max_ready", asyncio.Event())
    monkeypatch.setattr(main, "group_chats", {ЧАТ: True})
    # Настоящие двадцать секунд ждать незачем: проверяем, что срок есть, а не какой он.
    monkeypatch.setattr(main, "MAX_WAIT", 0.05)
    макс, бот = ФальшивыйMAX(), ФальшивыйБот()
    monkeypatch.setattr(main, "client", макс)
    monkeypatch.setattr(main, "bot", бот)
    return SimpleNamespace(max=макс, bot=бот, topics=карта)


def прогнать(корутина, срок=5):
    """Со сроком: зависшую проверку иначе не отличить от долгой."""

    async def подождать():
        return await asyncio.wait_for(корутина, timeout=срок)

    return asyncio.run(подождать())


class TestБезСвязиНеЗависаем:
    def test_сообщение_не_ждёт_вечно_а_сознаётся(self, мост):
        письмо = ФальшивоеПисьмо()

        with pytest.raises(main.MaxOffline):
            прогнать(main.on_tg_message(письмо))

        assert мост.max.отправлено == []

    def test_дождётся_если_связь_появилась_чуть_позже(self, мост, monkeypatch):
        """Срок нужен не чтобы отказывать, а чтобы отказ вообще был возможен.

        Обычный запуск: Telegram уже отвечает, MAX ещё поднимается. Написанное в эти
        секунды должно уехать, а не отвалиться.
        """
        monkeypatch.setattr(main, "MAX_WAIT", 5)
        письмо = ФальшивоеПисьмо()

        async def почти_сразу():
            задержка = asyncio.create_task(asyncio.sleep(0.05))
            задержка.add_done_callback(lambda _: main.max_ready.set())
            await main.on_tg_message(письмо)

        прогнать(почти_сразу())

        assert мост.max.отправлено == [(ЧАТ, "заберите ребёнка")]

    def test_со_связью_уходит_сразу(self, мост):
        main.max_ready.set()
        письмо = ФальшивоеПисьмо()

        прогнать(main.on_tg_message(письмо))

        assert мост.max.отправлено == [(ЧАТ, "заберите ребёнка")]


class TestГоворимЧтоНеУшло:
    def сорвать(self, беда):
        письмо = ФальшивоеПисьмо()
        событие = ErrorEvent.model_construct(
            update=SimpleNamespace(message=письмо, edited_message=None), exception=беда
        )
        прогнать(main.on_tg_error(событие))
        return письмо.сказано[0] if письмо.сказано else ""

    def test_называет_причиной_потерянную_связь_а_не_загадку(self, мост):
        """«Мост споткнулся: MaxOffline» человеку не говорит ничего."""
        сказано = self.сорвать(main.MaxOffline())

        assert "Не отправлено" in сказано
        assert "связь с MAX" in сказано

    def test_говорит_прямо_что_сообщение_не_ушло(self, мост):
        """Иначе останется думать, что оно всё-таки где-то уехало, и не повторит."""
        сказано = self.сорвать(main.MaxOffline())

        assert "никуда не ушло" in сказано
        assert "повтори" in сказано.lower()

    def test_обычную_беду_объясняет_по_прежнему(self, мост):
        сказано = self.сорвать(RuntimeError("сеть отвалилась"))

        assert "сеть отвалилась" in сказано


class TestПроверкаЖивойЛиМост:
    """Инструкция велит проверять мост командой `/help`. Значит, она не смеет врать."""

    def test_без_связи_help_предупреждает_первой_строкой(self, мост):
        письмо = ФальшивоеПисьмо()

        прогнать(main.on_help_command(письмо))

        assert письмо.сказано[0].startswith(main.NO_MAX)

    def test_со_связью_help_не_пугает_зря(self, мост):
        main.max_ready.set()
        письмо = ФальшивоеПисьмо()

        прогнать(main.on_help_command(письмо))

        assert письмо.сказано == [main.HELP]


class TestОтметкаСвязи:
    def test_обрыв_гасит_отметку(self, мост):
        main.max_ready.set()

        прогнать(main.on_max_disconnect(ConnectionError("сеть"), True, 5.0))

        assert not main.max_ready.is_set()

    def test_связь_считается_живой_уже_во_время_догонялки(self, мост, monkeypatch):
        """Раньше отметка зажигалась после догона, а он длится до пятнадцати минут.

        Всё это время написанное в Telegram молча стояло в очереди — при живой связи,
        по которой прекрасно могло уехать.
        """
        видел = []

        async def догонялка(client):
            видел.append(main.max_ready.is_set())

        monkeypatch.setattr(main, "_catch_up", догонялка)
        monkeypatch.setattr(main, "_hide_presence", лишь_бы_не_лезть_в_сеть)

        прогнать(main.on_max_start(SimpleNamespace(me=None)))

        assert видел == [True]
        assert main.max_ready.is_set()


async def лишь_бы_не_лезть_в_сеть(client):
    return None


class TestКорзинаБезСвязи:
    """Реакция-корзина — не украшение, а распоряжение стереть сообщение у собеседника."""

    def реакция(self, emoji):
        return SimpleNamespace(
            chat=SimpleNamespace(id=main.GROUP_ID),
            user=SimpleNamespace(id=999),
            message_id=СООБЩЕНИЕ,
            new_reaction=[SimpleNamespace(type="emoji", emoji=emoji)],
            old_reaction=[],
        )

    def test_не_делает_вид_что_стёрла(self, мост):
        прогнать(main.on_tg_reaction(self.реакция(main.DELETE_MARK)))

        assert мост.max.удалено == []
        assert мост.bot.сказано, "промолчала — значит, человек уверен, что стёр"
        assert "осталось" in мост.bot.сказано[0]

    def test_про_обычную_реакцию_тоже_не_молчит(self, мост):
        прогнать(main.on_tg_reaction(self.реакция("👍")))

        assert мост.bot.сказано and "Не сделано" in мост.bot.сказано[0]
