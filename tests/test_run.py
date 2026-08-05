"""Запускалка моста: замок на второй экземпляр и незамерзающее окно."""

import socket

from bridge.run import take_lock, unfreeze_console


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
