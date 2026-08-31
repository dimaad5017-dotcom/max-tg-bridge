"""Окно моста: своё — всегда, чужое — только когда кричит. Файл получает всё.

Жалоба, с которой это началось: «там какие-то логи страшные». Страшным было
чужое: aiogram по-английски отчитывался о каждом обновлении, pymax — о каждом
запросе. Свои русские строки моста в этом тонули.
"""

import logging

from bridge import main


def запись(имя: str, уровень: int) -> logging.LogRecord:
    return logging.LogRecord(
        name=имя,
        level=уровень,
        pathname="",
        lineno=0,
        msg="Update id=1 is handled. Duration 0 ms by bot id=1",
        args=(),
        exc_info=None,
    )


class TestЧтоПускатьВОкно:
    def test_свои_строки_проходят(self):
        assert main._screen_worthy(запись("bridge", logging.INFO))

    def test_болтовня_библиотек_не_проходит(self):
        # Ровно те строки, что пугали в окне: отчёт aiogram о каждом обновлении
        # и служебная скороговорка pymax о каждом запросе к MAX.
        assert not main._screen_worthy(запись("aiogram.event", logging.INFO))
        assert not main._screen_worthy(запись("pymax", logging.INFO))

    def test_чужая_беда_проходит(self):
        # Фильтр прячет чужой быт, а не чужие беды: предупреждение о здоровье
        # моста обязано быть на виду, от кого бы оно ни пришло.
        assert main._screen_worthy(запись("aiogram.dispatcher", logging.WARNING))
        assert main._screen_worthy(запись("pymax", logging.ERROR))

    def test_чужое_имя_с_нашим_началом_не_проходит(self):
        # «bridge» — это наш логгер и его дети, а не всякое имя на ту же букву.
        assert not main._screen_worthy(запись("bridgekeeper", logging.INFO))

    def test_фильтр_стоит_на_окне_а_не_на_файле(self):
        """Похудей файл так же — и на вопрос «почему молчал» снова нечем ответить.

        Файл — единственное место, где видно всё; окно можно закрыть, а после
        перезапуска в нём не видно даже последних строк.
        """
        окно, файл = main._logging_setup()
        try:
            assert main._screen_worthy in окно.filters
            assert main._screen_worthy not in файл.filters
        finally:
            файл.close()
