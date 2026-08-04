"""Чем мост заворачивает вложение из Telegram, когда несёт его в MAX.

Эта таблица уже дважды ломалась молча: сообщение уезжало, но приходило не тем.
Здесь закреплено и то, что работает, и то, что сознательно сделано хуже.
"""

from functools import partial
from types import SimpleNamespace

import pytest
from pymax.files.file import File
from pymax.files.photo import Photo
from pymax.files.video import Video, VideoNote

from bridge.main import _outgoing

ВИДЫ = ("photo", "video", "video_note", "animation", "voice", "audio", "sticker", "document")


def сообщение(**чем):
    """Телеграмное сообщение ровно с одним заполненным вложением."""
    поля = dict.fromkeys(ВИДЫ)
    поля.update(чем)
    return SimpleNamespace(**поля)


def test_без_вложения_ничего_не_заворачиваем():
    assert _outgoing(сообщение()) is None


def test_фото_берётся_самое_крупное():
    """Telegram отдаёт лесенку размеров; человеку нужен последний, а не превью."""
    мелкое, крупное = SimpleNamespace(), SimpleNamespace()

    wrapper, что, имя = _outgoing(сообщение(photo=[мелкое, крупное]))

    assert wrapper is Photo
    assert что is крупное
    assert имя == "photo.jpg"


def test_видео_сохраняет_имя_файла():
    видео = SimpleNamespace(file_name="отпуск.mp4")

    wrapper, что, имя = _outgoing(сообщение(video=видео))

    assert (wrapper, что, имя) == (Video, видео, "отпуск.mp4")


def test_видео_без_имени_получает_запасное():
    wrapper, _, имя = _outgoing(сообщение(video=SimpleNamespace(file_name=None)))

    assert (wrapper, имя) == (Video, "video.mp4")


def test_кружок_уходит_кружком_и_с_длительностью_в_миллисекундах():
    """MAX ждёт миллисекунды, Telegram отдаёт секунды. Без пересчёта кружок не примут."""
    кружок = SimpleNamespace(duration=5)

    wrapper, что, имя = _outgoing(сообщение(video_note=кружок))

    assert isinstance(wrapper, partial)
    assert wrapper.func is VideoNote
    assert wrapper.keywords["duration"] == 5000
    assert (что, имя) == (кружок, "video_note.mp4")


def test_гифка_уходит_видео():
    гифка = SimpleNamespace(file_name=None)

    wrapper, _, имя = _outgoing(сообщение(animation=гифка))

    assert (wrapper, имя) == (Video, "animation.mp4")


def test_голосовое_уходит_файлом_и_это_нарочно():
    """Голосовым MAX его не принимает: библиотека минуту ждёт подтверждения и сдаётся.

    Файлом доезжает мгновенно и слушается. Если однажды почините на той стороне —
    этот тест и надо будет переписать первым.
    """
    голосовое = SimpleNamespace()

    wrapper, что, имя = _outgoing(сообщение(voice=голосовое))

    assert (wrapper, что, имя) == (File, голосовое, "voice.ogg")


def test_музыка_уходит_файлом():
    трек = SimpleNamespace(file_name="песня.mp3")

    wrapper, _, имя = _outgoing(сообщение(audio=трек))

    assert (wrapper, имя) == (File, "песня.mp3")


@pytest.mark.parametrize(
    ("стикер", "ожидаем", "имя"),
    [
        (SimpleNamespace(is_animated=True, is_video=False), File, "sticker.tgs"),
        (SimpleNamespace(is_animated=False, is_video=True), Video, "sticker.webm"),
        (SimpleNamespace(is_animated=False, is_video=False), Photo, "sticker.webp"),
    ],
    ids=["анимированный", "видео", "обычный"],
)
def test_стикер_каждого_вида_едет_своим_способом(стикер, ожидаем, имя):
    wrapper, что, получилось = _outgoing(сообщение(sticker=стикер))

    assert (wrapper, что, получилось) == (ожидаем, стикер, имя)


def test_документ_без_имени_не_остаётся_безымянным():
    wrapper, _, имя = _outgoing(сообщение(document=SimpleNamespace(file_name=None)))

    assert (wrapper, имя) == (File, "file")
