"""Настройки из `.env`: их заполняет человек в блокноте, а не программа."""

import pytest

from bridge.config import flag, normalize_phone


@pytest.mark.parametrize(
    "written",
    [
        "+79250236350",
        "89250236350",
        "9250236350",
        "+7 (925) 023-63-50",
        "8 925 023 63 50",
        "  +7-925-023-63-50  ",
    ],
)
def test_любую_запись_приводит_к_одному_виду(written):
    assert normalize_phone(written) == "+79250236350"


def test_не_трогает_номер_другой_страны():
    """Восьмёрку меняем на семёрку только у одиннадцатизначных: это российская привычка."""
    assert normalize_phone("+380671234567") == "+380671234567"


@pytest.mark.parametrize("написано", ["да", "Да", " ДА ", "yes", "on", "1", "true"])
def test_согласие_понимает_как_его_ни_напиши(monkeypatch, написано):
    """Человек пишет в блокноте по-своему, и «Да» не должно значить «нет»."""
    monkeypatch.setenv("ПРОБА", написано)

    assert flag("ПРОБА") is True


@pytest.mark.parametrize("написано", ["", "  ", "нет", "no", "0", "потом"])
def test_всё_остальное_считает_отказом(monkeypatch, написано):
    """Настройка про приватность: сомнение толкуем в пользу молчания, а не показа."""
    monkeypatch.setenv("ПРОБА", написано)

    assert flag("ПРОБА") is False


def test_незаполненной_строки_достаточно(monkeypatch):
    monkeypatch.delenv("ПРОБА", raising=False)

    assert flag("ПРОБА") is False
