"""Включатель автозапуска не должен трогать чужое и обязан писать читаемый текст.

Две вещи здесь неочевидны, ради них всё и проверяется.

Первое — кодировка. cmd.exe читает командные файлы не в UTF-8, а в кодировке консоли:
на русской Windows это cp866. Запиши файл как UTF-8 — и русские строки в нём станут
кашей вместе с путями, если в имени пользователя кириллица. А оно кириллицей.

Второе — чужой файл. Выключение это удаление файла, и имя однажды может совпасть с
чем-то, что человек положил в автозагрузку сам. Молча стереть чужое хуже, чем не
выключить своё.

В настоящую папку автозагрузки тесты не лезут: всё происходит во временной.
"""

import pytest

from bridge import autostart


@pytest.fixture
def папка(tmp_path):
    """Поддельная папка автозагрузки."""
    место = tmp_path / "Startup"
    место.mkdir()
    return место


@pytest.fixture
def проект(tmp_path):
    return tmp_path / "мост"


def прочитать(папка):
    """Файл автозапуска так, как его прочтёт cmd.exe."""
    return (папка / autostart.NAME).read_bytes().decode(autostart.console_encoding())


class TestВключатель:
    def test_включает_и_выключает(self, папка, проект):
        assert not autostart.enabled(папка)

        assert autostart.enable(папка, проект)
        assert autostart.enabled(папка)

        assert autostart.disable(папка)
        assert not autostart.enabled(папка)
        assert not (папка / autostart.NAME).exists()

    def test_включить_дважды_не_плодит_файлов(self, папка, проект):
        autostart.enable(папка, проект)
        autostart.enable(папка, проект)

        assert len(list(папка.iterdir())) == 1

    def test_выключить_невключённое_не_падает(self, папка):
        assert not autostart.disable(папка)

    def test_чужой_файл_с_тем_же_именем_остаётся_на_месте(self, папка, проект):
        """Без метки внутри файл не наш — значит, его положил человек, и это не наше дело."""
        чужой = папка / autostart.NAME
        чужой.write_text("@echo off\r\nrem это положили сюда руками\r\n", encoding="cp866")

        assert autostart.foreign(папка)
        assert not autostart.enabled(папка)
        assert not autostart.disable(папка)
        assert not autostart.enable(папка, проект)
        assert "руками" in чужой.read_text(encoding="cp866")


class TestСодержимое:
    def test_текст_читается_в_кодировке_консоли(self, папка, проект):
        """Тот самый случай: UTF-8 превратил бы и текст, и путь в кашу."""
        autostart.enable(папка, проект)

        assert "похоже, папку переместили" in прочитать(папка)

    def test_запускает_тот_же_файл_что_и_рука(self, папка, проект):
        """Не питона напрямую: в конце того файла есть `pause`, и он тут главный.

        Питон, запущенный из автозагрузки, при выходе уносит окно с собой. Мост,
        который сдался, исчезал бы вместе с причиной — а окно и так свёрнуто, так
        что не заметить его исчезновение проще простого.
        """
        autostart.enable(папка, проект)
        текст = прочитать(папка)

        assert str(проект / autostart.LAUNCHER) in текст
        assert "python.exe" not in текст

    def test_окно_не_разворачивается_на_весь_экран(self, папка, проект):
        """Иначе каждый вход в Windows начинается с чёрного окна во весь экран."""
        autostart.enable(папка, проект)

        assert "/min" in прочитать(папка)

    def test_файл_помечен_как_наш(self, папка, проект):
        autostart.enable(папка, проект)

        assert autostart.MARK in прочитать(папка)

    def test_старая_запись_видна_как_старая(self, папка, проект, tmp_path):
        """Автозагрузка помнит полный путь. Переехал мост — запись врёт и молча не сработает."""
        autostart.enable(папка, проект)
        assert not autostart.stale(папка, проект)

        assert autostart.stale(папка, tmp_path / "мост-на-новом-месте")

    def test_свежую_запись_старой_не_обзывает(self, папка, проект):
        """Иначе включатель при каждом запуске звал бы чинить исправное."""
        autostart.enable(папка, проект)
        autostart.enable(папка, проект)

        assert not autostart.stale(папка, проект)

    def test_чужой_файл_старым_не_считается(self, папка, проект):
        """Чужое не наше дело — ни стирать, ни «обновлять»."""
        (папка / autostart.NAME).write_text("@echo off\r\nrem чужое\r\n", encoding="cp866")

        assert not autostart.stale(папка, проект)

    def test_переводы_строк_виндовые(self, папка, проект):
        """С одними \\n cmd.exe склеивает строки и выполняет не то, что написано."""
        байты = (папка / autostart.NAME) if autostart.enable(папка, проект) else None

        assert b"\n" in байты.read_bytes()
        assert байты.read_bytes().count(b"\n") == байты.read_bytes().count(b"\r\n")
