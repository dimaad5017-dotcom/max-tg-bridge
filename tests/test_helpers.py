"""Мелочи, из-за которых сообщение выглядит не так, как человек ожидал."""

import asyncio
from types import SimpleNamespace

import pytest

from bridge import main
from bridge.main import (
    _call_line,
    _chunks,
    _control_line,
    _display_name,
    _fit,
    _lost,
    _moment,
    _split_phone,
)


def имя(first=None, last=None, name=None):
    return SimpleNamespace(first_name=first, last_name=last, name=name)


@pytest.mark.parametrize(
    ("написали", "номер", "текст"),
    [
        ("+79250236350 привет", "+79250236350", "привет"),
        ("89250236350 привет", "89250236350", "привет"),
        ("+7 925 023 63 50 привет как дела", "+7 925 023 63 50", "привет как дела"),
        ("+7 (925) 023-63-50 привет", "+7 (925) 023-63-50", "привет"),
        ("+79250236350", "+79250236350", ""),
    ],
)
def test_номер_отделяется_от_текста(написали, номер, текст):
    """Номер люди пишут с пробелами и скобками, поэтому по первому пробелу резать нельзя."""
    assert _split_phone(написали) == (номер, текст)


def test_текст_без_номера_остаётся_текстом():
    assert _split_phone("привет") == ("", "привет")


def test_текст_после_номера_может_начинаться_с_цифры():
    """Иначе «в 10 часов» приклеилось бы к номеру."""
    _, текст = _split_phone("+79250236350 10 часов")

    assert текст == "10 часов"


class TestИмя:
    def test_складывается_из_имени_и_фамилии(self):
        user = SimpleNamespace(names=[имя(first="Александра", last="Н")])

        assert _display_name(user, 1) == "Александра Н"

    def test_обходится_одним_именем(self):
        user = SimpleNamespace(names=[имя(first="Павел")])

        assert _display_name(user, 1) == "Павел"

    def test_берёт_общее_поле_если_имени_и_фамилии_нет(self):
        user = SimpleNamespace(names=[имя(name="Школа 5, 9А")])

        assert _display_name(user, 1) == "Школа 5, 9А"

    def test_пропускает_пустую_запись_и_берёт_следующую(self):
        user = SimpleNamespace(names=[имя(), имя(first="Павел")])

        assert _display_name(user, 1) == "Павел"

    def test_без_имени_подставляет_номер_чтобы_тема_не_осталась_безымянной(self):
        assert _display_name(None, 386174042) == "id386174042"
        assert _display_name(SimpleNamespace(names=[]), 386174042) == "id386174042"

    def test_совсем_без_ничего_говорит_прямо(self):
        assert _display_name(None, None) == "неизвестный"


class TestРезкаДлинного:
    """Telegram не обрезает длинное, а отвергает целиком — режем сами, до отправки."""

    def test_короткое_не_трогает(self):
        assert _chunks("привет", 100) == ["привет"]

    def test_ровно_по_пределу_ещё_целое(self):
        """Предел — это «столько можно», а не «столько уже нельзя»."""
        assert _chunks("я" * 100, 100) == ["я" * 100]

    def test_ничего_не_теряет_и_не_добавляет(self):
        assert "".join(_chunks("я" * 10000, 4096)) == "я" * 10000

    def test_каждый_кусок_влезает(self):
        assert all(len(кусок) <= 4096 for кусок in _chunks("я" * 10000, 4096))

    def test_режет_по_строкам(self):
        """По строке разрыв читается как продолжение, посреди слова — как обрыв связи."""
        куски = _chunks("а" * 60 + "\n" + "б" * 60, 100)

        assert куски == ["а" * 60, "б" * 60]

    def test_не_разрывает_экранированный_символ(self):
        """Половина `&amp;` — это уже не разметка, и Telegram отвергнет саму часть.

        Так выглядит текст, где человек написал «&» или «<»: `html.escape` разворачивает
        их в пять знаков, и попасть краем ровно в середину такой пятёрки — дело времени.
        """
        куски = _chunks("x" * 98 + "&amp;" + "y" * 50, 100)

        assert куски[0] == "x" * 98
        assert куски[1].startswith("&amp;")

    def test_не_разрывает_тег(self):
        куски = _chunks("x" * 98 + "<b>жирно</b>" + "y" * 50, 100)

        assert куски[0] == "x" * 98

    def test_строка_из_одних_переводов_не_даёт_пустых_кусков(self):
        """Пустое сообщение Telegram не примет — а мост бы его отправил и получил отказ."""
        assert all(_chunks("\n" * 300, 100))


class TestОбрезкаПравки:
    """Правка встаёт на место старого сообщения — разложить её на несколько нельзя."""

    def test_короткую_не_трогает(self):
        assert _fit("привет", 100) == "привет"

    def test_длинную_обрезает_до_предела(self):
        assert len(_fit("я" * 500, 100)) <= 100

    def test_говорит_что_обрезал(self):
        """Молча обрезанная правка выглядит как то, что человек так и написал."""
        assert "только в MAX" in _fit("я" * 500, 100)


def test_время_понимается_и_в_секундах_и_в_миллисекундах():
    """MAX шлёт то одно, то другое; человек должен увидеть одну и ту же минуту."""
    assert _moment(1754300000) == _moment(1754300000000)


class TestЗвонок:
    @pytest.mark.parametrize("длительность", [0, None])
    def test_без_длительности_считается_неотвеченным(self, длительность):
        assert _call_line(SimpleNamespace(duration=длительность)) == "<i>звонок в MAX — не отвечен</i>"

    def test_короткий_показывается_в_секундах(self):
        assert _call_line(SimpleNamespace(duration=45)) == "<i>звонок в MAX, 45 с</i>"

    def test_длительность_в_миллисекундах_переводится(self):
        assert _call_line(SimpleNamespace(duration=65000)) == "<i>звонок в MAX, 1 мин 5 с</i>"


class TestНедоставленное:
    def test_называет_вложение_по_русски(self):
        строка = _lost("VIDEO", "весит больше 50 МБ")

        assert строка.startswith("<b>Не доставлено:</b> видео — весит больше 50 МБ")

    def test_всегда_подсказывает_где_посмотреть(self):
        """Главное обещание моста: он не молчит о том, что не доехало."""
        assert "Посмотреть можно только в MAX" in _lost("VIDEO", "неважно")

    def test_незнакомое_вложение_не_ломает_строку(self):
        assert _lost("НЕЧТО", "причина").startswith("<b>Не доставлено:</b> нечто — причина")


class TestСлужебноеСобытиеЧата:
    """«Кого-то добавили», «чат переименовали» — в школьных чатах этого больше, чем разговоров.

    Коды событий MAX нигде не описаны, и в библиотеке их тоже нет. Значит, однажды придёт
    незнакомый — и человек должен увидеть событие чата, а не обломок кода в теме.
    """

    def строка(self, monkeypatch, event, **поля):
        async def имя(user_id):
            return {7: "поля", 8: "HLEB"}[user_id]

        monkeypatch.setattr(main, "_sender_name", имя)
        return asyncio.run(_control_line(SimpleNamespace(event=event, **поля)))

    def test_вступление_по_ссылке_переводит(self, monkeypatch):
        """Живой школьный чат прислал `joinByLink`, и в теме появился голый код."""
        assert self.строка(monkeypatch, "joinByLink", user_ids=[7]) == "<i>вступил по ссылке: поля</i>"

    def test_добавленных_называет_по_именам(self, monkeypatch):
        строка = self.строка(monkeypatch, "add", user_ids=[7, 8])

        assert строка == "<i>добавили в чат: поля, HLEB</i>"

    def test_переименование_показывает_новое_название(self, monkeypatch):
        """«Чат переименовали» без названия — половина новости."""
        строка = self.строка(monkeypatch, "title", title="11М Дети")

        assert строка == "<i>чат переименовали: 11М Дети</i>"

    def test_незнакомый_код_называет_событием_а_не_показывает_голым(self, monkeypatch):
        строка = self.строка(monkeypatch, "removeAdmin")

        assert строка == "<i>служебное событие MAX (removeAdmin)</i>"

    def test_чужие_угловые_скобки_не_ломают_разметку(self, monkeypatch):
        """Название чата пишут люди, а Telegram разбирает угловые скобки как разметку."""
        строка = self.строка(monkeypatch, "title", title="9А <класс>")

        assert "&lt;класс&gt;" in строка
