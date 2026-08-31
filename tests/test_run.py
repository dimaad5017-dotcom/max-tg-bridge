"""Запускалка моста: замок на второй экземпляр и незамерзающее окно."""

import socket

from bridge.run import EXTENDED_FLAGS, MOUSE_INPUT, QUICK_EDIT, deaf_to_mouse, take_lock, unfreeze_console


def свободный_порт() -> int:
    """Тесты не занимают настоящий порт моста: иначе они падали бы, пока он работает."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_второй_мост_не_запускается():
    """Два моста на одном аккаунте мешают друг другу, и сообщения теряются молча."""
    порт = свободный_порт()
    first = take_lock(порт)
    assert first is not None

    try:
        assert take_lock(порт) is None
    finally:
        first.close()


def test_после_закрытия_место_освобождается():
    """Иначе после перезапуска мост считал бы сам себя вторым и не поднимался."""
    порт = свободный_порт()
    first = take_lock(порт)
    assert first is not None
    first.close()

    second = take_lock(порт)
    assert second is not None
    second.close()


def test_окно_размораживается_без_окна():
    """На сервере и в тестах консоли нет — это не повод падать при запуске."""
    unfreeze_console()


def test_выделение_выключено():
    """Случайный клик в окно не должен замораживать мост."""
    assert deaf_to_mouse(0xFFFF) & QUICK_EDIT == 0


def test_колесо_возвращено_окну():
    """Мышиный ввод выключен вместе с выделением — иначе окно перестаёт листаться.

    Без «быстрого выделения» консоль скармливает движения колеса программе во
    ввод, мост ввод не читает — и колесо не делает ничего. Ровно так и было:
    выделение выключили, мышь оставили, и окно встало. Правка «нельзя скролить»
    держится на этом битике.
    """
    assert deaf_to_mouse(0xFFFF) & MOUSE_INPUT == 0


def test_разрешение_менять_эти_битики_включено():
    """Без ENABLE_EXTENDED_FLAGS Windows молча не принимает первые два."""
    assert deaf_to_mouse(0) & EXTENDED_FLAGS


def test_остальные_настройки_окна_целы():
    """Мост забирает у окна мышь, а не переустраивает его целиком."""
    чужие = 0xFFFF & ~QUICK_EDIT & ~MOUSE_INPUT & ~EXTENDED_FLAGS
    assert deaf_to_mouse(чужие) & чужие == чужие
