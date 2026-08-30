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

        assert "Мост не установлен" in прочитать(папка)

    def test_запускает_питона_из_проекта_а_не_какого_нибудь(self, папка, проект):
        """Полный путь обязателен: автозагрузка стартует не из папки моста."""
        autostart.enable(папка, проект)
        текст = прочитать(папка)

        assert str(проект / ".venv" / "Scripts" / "python.exe") in текст
        assert "-m bridge.run" in текст

    def test_окно_не_разворачивается_на_весь_экран(self, папка, проект):
        """Иначе каждый вход в Windows начинается с чёрного окна во весь экран."""
        autostart.enable(папка, проект)

        assert "/min" in прочитать(папка)

    def test_файл_помечен_как_наш(self, папка, проект):
        autostart.enable(папка, проект)

        assert autostart.MARK in прочитать(папка)

    def test_переводы_строк_виндовые(self, папка, проект):
        """С одними \\n cmd.exe склеивает строки и выполняет не то, что написано."""
        байты = (папка / autostart.NAME) if autostart.enable(папка, проект) else None

        assert b"\n" in байты.read_bytes()
        assert байты.read_bytes().count(b"\n") == байты.read_bytes().count(b"\r\n")
