"""Мелочи, из-за которых сообщение выглядит не так, как человек ожидал."""

from types import SimpleNamespace

import pytest

from bridge.main import _call_line, _display_name, _lost, _moment, _split_phone


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
