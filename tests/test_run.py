"""Запускалка моста: замок на второй экземпляр и незамерзающее окно."""

from bridge.run import take_lock, unfreeze_console


def test_второй_мост_не_запускается():
    """Два моста на одном аккаунте мешают друг другу, и сообщения теряются молча."""
    first = take_lock()
    assert first is not None

    try:
        assert take_lock() is None
    finally:
        first.close()


def test_после_закрытия_место_освобождается():
    """Иначе после перезапуска мост считал бы сам себя вторым и не поднимался."""
    first = take_lock()
    assert first is not None
    first.close()

    second = take_lock()
    assert second is not None
    second.close()


def test_окно_размораживается_без_окна():
    """На сервере и в тестах консоли нет — это не повод падать при запуске."""
    unfreeze_console()
