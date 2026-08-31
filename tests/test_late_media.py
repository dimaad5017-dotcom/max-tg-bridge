"""Фотография, которую сервер MAX не отдал вовремя, не должна пропасть насовсем.

Так это выглядело 31 августа. Мама прислала в школьную группу два фото. Сервер
вложений MAX в эту минуту не отвечал — мост сходил за файлом один раз, подождал
полминуты и честно написал в тему «Не доставлено: фото». Строка ушла удачно, и
дальше сработало то, что в обычный день работает правильно: раз сообщение доехало
до Telegram, оно доставлено и прочитано. Обычная догонялка ходит по непрочитанному
и к этому сообщению больше не вернулась.

Через минуту сервер ожил. Но идти к нему было уже некому: фотографии не осталось
нигде, кроме MAX, — куда как раз и не заходят, ради чего мост и писался.

Поэтому здесь проверяется вторая догонялка, поменьше: не за сообщениями, а за
одним вложением. Она помнит долг в базе, а не в памяти, — иначе перезапуск моста
хоронил бы ровно ту фотографию, за которой мост как раз собирался вернуться.
"""

import asyncio
from types import SimpleNamespace

import pytest

from bridge import main
from bridge.storage import TopicMap

ЧАТ, ТЕМА, СТРОКА = 777, 5, 42
ФОТО = main.Late("photo", "https://i.oneme.ru/i?r=nope", "photo.jpg")


class ФальшивыйБот:
    """Запоминает не только что отправили, но и чему это было ответом."""

    def __init__(self):
        self.отправлено = []

    async def send_photo(self, chat_id, photo=None, message_thread_id=None, **прочее):
        self.отправлено.append(("photo", прочее.get("caption"), прочее.get("reply_parameters")))
        return SimpleNamespace(message_id=len(self.отправлено))

    async def send_message(self, chat_id, text=None, message_thread_id=None, **прочее):
        self.отправлено.append(("text", text, прочее.get("reply_parameters")))
        return SimpleNamespace(message_id=len(self.отправлено))


@pytest.fixture
def бот(monkeypatch):
    подделка = ФальшивыйБот()
    monkeypatch.setattr(main, "bot", подделка)
    return подделка


@pytest.fixture
def карта(monkeypatch, tmp_path):
    место = TopicMap(tmp_path / "topics.db")
    место.link(ЧАТ, ТЕМА, "11М Дети")
    monkeypatch.setattr(main, "topics", место)
    return место


@pytest.fixture(autouse=True)
def чистая_очередь():
    """Догонялки живут в общем множестве — чужие задачи в нём тесту только мешают."""
    main._chases.clear()
    yield
    main._chases.clear()


@pytest.fixture
def без_ожидания(monkeypatch):
    """Полчаса ждать в тесте нельзя, а вот записать, сколько ждали, — можно."""
    засечки = []

    async def мгновенно(сколько):
        засечки.append(сколько)

    monkeypatch.setattr(main.asyncio, "sleep", мгновенно)
    return засечки


def отдаёт(*ответы):
    """Поддельный `_download`: выдаёт заготовленные ответы по одному."""
    очередь = list(ответы)
    заходы = []

    async def скачать(url, name, tries=main.FETCH_TRIES):
        заходы.append(url)
        return очередь.pop(0) if очередь else ответы[-1]

    скачать.заходы = заходы
    return скачать


ПРИНЕСЛА = main.Fetched(b"file")
МОЛЧИТ = main.Fetched(None, "не скачалось (ConnectionTimeoutError)", again=True)
ОТКАЗАЛА = main.Fetched(None, "не скачалось (ClientResponseError)", again=False)


def погнаться(долг=1):
    asyncio.run(main._chase(долг, ЧАТ, ТЕМА, СТРОКА, ФОТО))


class TestДогонялкаЗаВложением:
    def test_приносит_файл_ответом_на_ту_самую_строку(self, бот, карта, без_ожидания, monkeypatch):
        """Иначе фото всплывёт в теме через полчаса само по себе — непонятно к чему."""
        monkeypatch.setattr(main, "_download", отдаёт(МОЛЧИТ, ПРИНЕСЛА))

        погнаться()

        вид, подпись, ответ = бот.отправлено[0]
        assert вид == "photo"
        assert ответ.message_id == СТРОКА
        assert "догнали" in подпись

    def test_сначала_ждёт_а_потом_идёт(self, бот, карта, без_ожидания, monkeypatch):
        """Сервер, не ответивший секунду назад, не ответит и через мгновение."""
        скачать = отдаёт(ПРИНЕСЛА)
        monkeypatch.setattr(main, "_download", скачать)

        погнаться()

        assert без_ожидания == [main.LATE_WAITS[0]]
        assert len(скачать.заходы) == 1

    def test_догнав_долг_забывает(self, бот, карта, без_ожидания, monkeypatch):
        monkeypatch.setattr(main, "_download", отдаёт(ПРИНЕСЛА))
        долг = карта.remember_late(ЧАТ, ТЕМА, СТРОКА, *ФОТО)

        asyncio.run(main._chase(долг, ЧАТ, ТЕМА, СТРОКА, ФОТО))

        assert карта.all_late() == []

    def test_не_ходит_по_кругу_вечно(self, бот, карта, без_ожидания, monkeypatch):
        """Долг, за которым ходят бесконечно, — это утечка задач при каждом сбое MAX."""
        скачать = отдаёт(МОЛЧИТ)
        monkeypatch.setattr(main, "_download", скачать)

        погнаться()

        assert len(скачать.заходы) == len(main.LATE_WAITS)
        assert карта.all_late() == []

    def test_не_догнав_говорит_об_этом(self, бот, карта, без_ожидания, monkeypatch):
        """В теме висит обещание догнать. Молча его не сдержать хуже, чем сразу отказать."""
        monkeypatch.setattr(main, "_download", отдаёт(МОЛЧИТ))

        погнаться()

        вид, текст, ответ = бот.отправлено[-1]
        assert вид == "text"
        assert "не удалось" in текст
        assert ответ.message_id == СТРОКА

    def test_осмысленный_отказ_не_гоняет_по_кругу(self, бот, карта, без_ожидания, monkeypatch):
        """«Нет такого файла» через полчаса останется «нет такого файла»."""
        скачать = отдаёт(ОТКАЗАЛА)
        monkeypatch.setattr(main, "_download", скачать)

        погнаться()

        assert len(скачать.заходы) == 1
        assert карта.all_late() == []


class TestДолгПереживаетПерезапуск:
    """Догонялка ждёт полчаса, а мост за это время могут закрыть крестиком.

    Держи долг в памяти — и закрытое окно похоронит фотографию точно так же, как
    хоронила прежняя единственная попытка. Только теперь ещё и с обещанием догнать.
    """

    def test_запись_ложится_в_базу_сразу(self, карта):
        карта.remember_late(ЧАТ, ТЕМА, СТРОКА, *ФОТО)

        (долг,) = карта.all_late()

        assert долг[1:] == (ЧАТ, ТЕМА, СТРОКА, *ФОТО)

    def test_после_запуска_мост_подбирает_долги(self, бот, карта, без_ожидания, monkeypatch):
        карта.remember_late(ЧАТ, ТЕМА, СТРОКА, *ФОТО)
        monkeypatch.setattr(main, "_download", отдаёт(ПРИНЕСЛА))

        async def запуститься():
            main._resume_chases()
            # Догонялки живут отдельными задачами — дождёмся, пока они отработают.
            await asyncio.gather(*main._chases)

        asyncio.run(запуститься())

        assert бот.отправлено[0][0] == "photo"
        assert карта.all_late() == []

    def test_про_подобранные_долги_говорит_в_лог(self, бот, карта, monkeypatch, caplog):
        """Молчание здесь однажды стоило получаса поисков.

        Мост перезапустили, в логе — ни слова про долги. Подобрал он их и они не
        доехали? Или подбирать было нечего? Это два разных ответа на вопрос «где
        фотография», и по логу они выглядели одинаково.
        """
        карта.remember_late(ЧАТ, ТЕМА, СТРОКА, *ФОТО)
        monkeypatch.setattr(main, "_chase", lambda *всё: asyncio.sleep(0))

        async def запуститься():
            main._resume_chases()
            await asyncio.gather(*main._chases)

        with caplog.at_level("INFO"):
            asyncio.run(запуститься())

        assert "не догнано вложений: 1" in caplog.text

    def test_на_пустой_очереди_в_лог_не_сорит(self, карта, caplog):
        """Строка «долгов нет» в каждом запуске — это шум, за которым тонет важное."""
        with caplog.at_level("INFO"):
            main._resume_chases()

        assert "не догнано" not in caplog.text

    def test_догнанное_второй_раз_не_догоняют(self, бот, карта, без_ожидания, monkeypatch):
        """Иначе каждый запуск моста присылал бы одну и ту же фотографию заново."""
        карта.remember_late(ЧАТ, ТЕМА, СТРОКА, *ФОТО)
        monkeypatch.setattr(main, "_download", отдаёт(ПРИНЕСЛА))
        карта.forget_late(карта.all_late()[0][0])

        main._resume_chases()

        assert not main._chases


class ФальшивыйMAX:
    async def read_message(self, message_id, chat_id):
        return None


def фото_из_группы():
    """Сообщение с фотографией — ровно такое пришло 31 августа."""
    фото = SimpleNamespace(type="PHOTO", base_url=ФОТО.url)
    return SimpleNamespace(id="100", text="расписание", attaches=[фото], sender=42, time=1000, link=None)


class TestДоставкаСтавитДолгВОчередь:
    """Та самая точка, где фотографии терялись: доставка кончилась — и всё.

    Долг надо записать здесь и сейчас. Дальше `_deliver` отметит сообщение
    доставленным и прочитанным, и другого повода вернуться к нему уже не будет.
    """

    @pytest.fixture
    def доставка(self, бот, карта, monkeypatch):
        monkeypatch.setattr(main, "client", ФальшивыйMAX())
        monkeypatch.setattr(main, "group_chats", {ЧАТ: False})
        # Настоящую догонялку подменяем: она тут не при чём, а незавершённая задача
        # после `asyncio.run` только сорит предупреждениями в вывод теста.
        monkeypatch.setattr(main, "_chase", lambda *всё: asyncio.sleep(0))

        def доставить():
            asyncio.run(main._deliver(ЧАТ, фото_из_группы()))

        return доставить

    def test_не_скачавшееся_фото_попадает_в_долги(self, доставка, карта, monkeypatch):
        monkeypatch.setattr(main, "_download", отдаёт(МОЛЧИТ))

        доставка()

        (долг,) = карта.all_late()
        assert долг[1:] == (ЧАТ, ТЕМА, 1, *ФОТО), "не за что будет вернуться"

    def test_в_теме_обещание_догнать_а_не_приговор(self, доставка, бот, monkeypatch):
        monkeypatch.setattr(main, "_download", отдаёт(МОЛЧИТ))

        доставка()

        assert "догнать" in бот.отправлено[0][1]

    def test_скачавшееся_в_долги_не_пишут(self, доставка, карта, monkeypatch):
        monkeypatch.setattr(main, "_download", отдаёт(ПРИНЕСЛА))

        доставка()

        assert карта.all_late() == []

    def test_безнадёжное_в_долги_не_пишут(self, доставка, карта, бот, monkeypatch):
        """Гонять за файлом, которого нет, — только копить задачи и врать в теме."""
        monkeypatch.setattr(main, "_download", отдаёт(ОТКАЗАЛА))

        доставка()

        assert карта.all_late() == []
        assert "только в MAX" in бот.отправлено[0][1]


class TestЧтоОбещаемВТеме:
    def test_про_молчащий_сервер_обещаем_догнать(self):
        assert "догнать" in main._lost("PHOTO", "не скачалось", later=True)

    def test_про_безнадёжное_отправляем_в_MAX(self):
        """Обещать догнать то, что не догонится, — хуже, чем сразу сказать правду."""
        строка = main._lost("VIDEO", "весит больше 50 МБ", later=False)

        assert "догнать" not in строка
        assert "только в MAX" in строка
