"""Что мост делает, если его запустили раньше, чем поднялась сеть.

Пока мост открывали руками, такого не бывало: человек садится за компьютер, Wi-Fi уже
подключён, и первый же запрос в Telegram проходит. С автозапуском порядок обратный —
вход в Windows быстрее, чем сеть, и мост стартует в тишине, где Telegram недостижим.

Без сети первый запрос не висит, а падает секунд за двенадцать. Дальше срабатывала
защита от кривых настроек: запуск поднимает упавший мост, но трижды подряд умерший
меньше чем за полминуты считает безнадёжным и перестаёт поднимать. Три попытки с
паузами — минута. Wi-Fi, поднявшийся на второй минуте, оставлял мост выключенным
до вечера, и заметить это было неоткуда: окно из автозапуска свёрнуто, а при выходе
закрывается совсем.

Поэтому здесь проверяется ровно одно: отсутствие сети — это подождать, а неверные
настройки — это сдаться. Спутать нельзя ни в ту, ни в другую сторону.
"""

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError

from bridge import main


class ФальшивыйБот:
    """Telegram, который отвечает не сразу: первые сколько-то раз сети нет."""

    def __init__(self, беды):
        self.беды = list(беды)
        self.попыток = 0

    async def set_my_commands(self, commands, scope=None):
        self.попыток += 1
        if self.беды:
            raise self.беды.pop(0)
        return True


@pytest.fixture
def без_пауз(monkeypatch):
    """Настоящие пятнадцать секунд ждать незачем: проверяем, что ждём, а не сколько."""
    паузы = []

    async def мгновенно(сколько):
        паузы.append(сколько)

    monkeypatch.setattr(main.asyncio, "sleep", мгновенно)
    return паузы


def сеть_отвалилась(сколько=1):
    return [TelegramNetworkError(method=None, message="getaddrinfo failed")] * сколько


def прогнать(корутина, срок=5):
    """Со сроком: мост, ждущий сеть вечно, в тесте выглядел бы как зависший тест."""

    async def подождать():
        return await asyncio.wait_for(корутина, timeout=срок)

    return asyncio.run(подождать())


class TestЖдётСетьАНеУмирает:
    def test_без_сети_пробует_снова(self, monkeypatch, без_пауз):
        бот = ФальшивыйБот(сеть_отвалилась(3))
        monkeypatch.setattr(main, "bot", бот)

        прогнать(main._greet_telegram())

        assert бот.попыток == 4, "сдался раньше, чем поднялась сеть"

    def test_между_попытками_выжидает(self, monkeypatch, без_пауз):
        """Иначе это не ожидание, а тысяча запросов в секунду в мёртвую сеть."""
        monkeypatch.setattr(main, "bot", ФальшивыйБот(сеть_отвалилась(2)))

        прогнать(main._greet_telegram())

        assert без_пауз == [main.NET_RETRY, main.NET_RETRY]

    def test_при_живой_сети_не_ждёт_ни_секунды(self, monkeypatch, без_пауз):
        бот = ФальшивыйБот([])
        monkeypatch.setattr(main, "bot", бот)

        прогнать(main._greet_telegram())

        assert бот.попыток == 1
        assert без_пауз == []


class TestНеверныеНастройкиНеЖдут:
    """Ждать тут нечего: чужая группа и битый токен не починятся и через час.

    Мост должен сдаться громко — окно печатает причину и ссылку на разбор ошибок.
    Стоило бы ему принять это за «сети нет», и он молча ждал бы вечно.
    """

    def test_битый_токен_поднимается_наверх(self, monkeypatch, без_пауз):
        monkeypatch.setattr(
            main, "bot", ФальшивыйБот([TelegramUnauthorizedError(method=None, message="Unauthorized")])
        )

        with pytest.raises(TelegramUnauthorizedError):
            прогнать(main._greet_telegram())

    def test_чужая_группа_поднимается_наверх(self, monkeypatch, без_пауз):
        monkeypatch.setattr(
            main, "bot", ФальшивыйБот([TelegramBadRequest(method=None, message="chat not found")])
        )

        with pytest.raises(TelegramBadRequest):
            прогнать(main._greet_telegram())

    def test_и_не_ждёт_перед_тем_как_сдаться(self, monkeypatch, без_пауз):
        monkeypatch.setattr(
            main, "bot", ФальшивыйБот([TelegramBadRequest(method=None, message="chat not found")])
        )

        with pytest.raises(TelegramBadRequest):
            прогнать(main._greet_telegram())

        assert без_пауз == []
