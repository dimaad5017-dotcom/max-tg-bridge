import asyncio
import html
import logging
from typing import Any, NamedTuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, BufferedInputFile
from aiogram.types import Message as TgMessage
from pymax import Client, Message, User
from pymax.exceptions import ApiError

from .config import MAP_DB, SESSION_NAME, WORK_DIR, normalize_phone, require
from .storage import TopicMap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bridge")

GROUP_ID = int(require("TG_GROUP_ID"))

bot = Bot(require("TG_BOT_TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
client = Client(
    phone=normalize_phone(require("MAX_PHONE")),
    work_dir=str(WORK_DIR),
    session_name=SESSION_NAME,
)
topics = TopicMap(MAP_DB)

# Telegram-поллинг стартует раньше, чем поднимется сессия MAX.
max_ready = asyncio.Event()

COMMANDS = [
    BotCommand(command="write", description="написать первым по номеру"),
    BotCommand(command="join", description="вступить в чат MAX по ссылке"),
    BotCommand(command="help", description="памятка"),
]

HELP = (
    "<b>Каждый чат MAX — своя тема.</b> Пишешь в теме — уходит собеседнику.\n\n"
    "<code>/write +7 999 123-45-67 привет</code> — написать первым\n"
    "<code>/join ссылка</code> — вступить в чат по приглашению\n\n"
    "Пропущенное за время простоя мост досылает сам при запуске.\n"
    "Мост живёт, пока открыто окно <code>3-запустить-мост.cmd</code>."
)

# Потолок догона: если мост стоял неделю, лучше отдать хвост, чем завалить группу.
HISTORY_LIMIT = 40

NO_TOPIC = "но тема не создалась — дай боту право «Управление темами», пока пишу сюда, в General."

ATTACHMENT_LABELS = {
    "PHOTO": "фото",
    "VIDEO": "видео",
    "FILE": "файл",
    "STICKER": "стикер",
    "AUDIO": "голосовое",
    "CONTACT": "контакт",
    "CALL": "звонок",
    "SHARE": "ссылка",
}

UPLOAD_LIMIT = 50 * 1024 * 1024  # Столько бот вправе залить в Telegram.
CAPTION_LIMIT = 1024  # Подпись под файлом короче обычного сообщения.

# Чем слать: метод бота и имя аргумента под файл.
MEDIA_SENDERS = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "voice": ("send_voice", "voice"),
    "document": ("send_document", "document"),
    "sticker": ("send_sticker", "sticker"),
}


class Media(NamedTuple):
    kind: str
    file: BufferedInputFile


def _display_name(user: User | None, user_id: int | None) -> str:
    for name in user.names if user else []:
        full = " ".join(part for part in (name.first_name, name.last_name) if part)
        if full:
            return full
        if name.name:
            return name.name
    return f"id{user_id}" if user_id else "неизвестный"


async def _sender_name(user_id: int | None) -> str:
    if user_id is None:
        return "неизвестный"
    return _display_name(await client.get_user(user_id), user_id)


async def _topic_title(chat_id: int) -> str:
    chat = await client.get_chat(chat_id)
    if chat.title:
        return chat.title

    my_id = client.me.contact.id if client.me else None
    peer_id = next((uid for uid in chat.participants if uid != my_id), None)
    return await _sender_name(peer_id) if peer_id else f"Чат {chat_id}"


async def _ensure_topic(chat_id: int, title: str | None = None) -> int | None:
    """None означает «темы не будет» — сообщение уйдёт в General, но не пропадёт."""
    topic_id = topics.topic_for_chat(chat_id)
    if topic_id is not None:
        return topic_id

    title = title or await _topic_title(chat_id)
    try:
        topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=title[:128])
    except TelegramBadRequest as error:
        logger.error("не создать тему для чата MAX %s: %s", chat_id, error)
        return None

    topics.link(chat_id, topic.message_thread_id, title)
    logger.info("создана тема %s для чата MAX %s (%s)", topic.message_thread_id, chat_id, title)
    return topic.message_thread_id


async def _source(chat_id: int, message: Message, attachment: Any, kind: str) -> tuple[str, str, str] | None:
    """(чем слать, откуда качать, имя файла). У видео и файлов ссылку выдают отдельным запросом."""
    if kind == "PHOTO" and attachment.base_url:
        return "photo", attachment.base_url, "photo.jpg"
    if kind == "AUDIO" and attachment.url:
        return "voice", attachment.url, "voice.ogg"
    if kind == "STICKER" and attachment.url:
        return "sticker", attachment.url, "sticker.webp"
    if kind == "VIDEO":
        video = await client.get_video_by_id(chat_id, message.id, attachment.video_id)
        return ("video", video.url, "video.mp4") if video and video.url else None
    if kind == "FILE":
        file = await client.get_file_by_id(chat_id, message.id, attachment.file_id)
        return ("document", file.url, attachment.name or "file") if file and file.url else None
    return None


async def _download(url: str, name: str) -> BufferedInputFile | None:
    try:
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            response.raise_for_status()
            if (response.content_length or 0) > UPLOAD_LIMIT:
                logger.info("вложение %s больше лимита Telegram, отдаём пометкой", name)
                return None
            return BufferedInputFile(await response.read(), filename=name)
    except aiohttp.ClientError as error:
        logger.error("не скачать вложение %s: %s", name, error)
        return None


async def _compose(chat_id: int, message: Message) -> tuple[str, list[Media]]:
    """Текст сообщения и то, что удалось выкачать; невыкачанное остаётся пометкой."""
    lines = [f"<b>{html.escape(await _sender_name(message.sender))}</b>"]
    if message.text:
        lines.append(html.escape(message.text))

    media: list[Media] = []
    for attachment in message.attaches:
        kind = getattr(attachment.type, "value", str(attachment.type))
        source = await _source(chat_id, message, attachment, kind)
        file = await _download(source[1], source[2]) if source else None
        if file is not None:
            media.append(Media(source[0], file))
        elif kind == "SHARE" and attachment.url:
            lines.append(html.escape(attachment.url))
        else:
            lines.append(f"<i>[{ATTACHMENT_LABELS.get(kind, kind.lower())}]</i>")

    return "\n".join(lines), media


async def _deliver(chat_id: int, message: Message) -> None:
    topic_id = await _ensure_topic(chat_id)
    caption, media = await _compose(chat_id, message)

    # Подпись вешаем на первый файл; стикер подписи не принимает, длинный текст в неё не влезет.
    inline = bool(media) and media[0].kind != "sticker" and len(caption) <= CAPTION_LIMIT
    for index, item in enumerate(media):
        method, argument = MEDIA_SENDERS[item.kind]
        payload = {argument: item.file, "message_thread_id": topic_id}
        if inline and index == 0:
            payload["caption"] = caption
        await getattr(bot, method)(GROUP_ID, **payload)
        logger.info("переслали %s из чата MAX %s", item.kind, chat_id)

    if not inline:
        await bot.send_message(GROUP_ID, caption, message_thread_id=topic_id)

    topics.remember_delivered(chat_id, message.time)


async def _catch_up(client: Client) -> None:
    """MAX отдаёт сообщения живым потоком, поэтому написанное при выключенном мосте берём историей."""
    my_id = client.me.contact.id if client.me else None
    # client.chats на старте бывает ещё пустым — список догоняет логин, поэтому спрашиваем сами.
    for chat in await client.fetch_chats() or []:
        unread = chat.new_messages or 0
        delivered = topics.delivered_until(chat.id)
        if not unread and delivered is None:
            continue

        depth = min(unread, HISTORY_LIMIT) if delivered is None else HISTORY_LIMIT
        history = await client.fetch_history(chat.id, backward=max(depth, 1)) or []
        missed = [
            message
            for message in sorted(history, key=lambda message: message.time)
            if message.sender != my_id and (delivered is None or message.time > delivered)
        ]
        for message in missed:
            await _deliver(chat.id, message)
        if missed:
            logger.info("догнали %s пропущенных из чата MAX %s", len(missed), chat.id)


@client.on_start()
async def on_max_start(client: Client) -> None:
    logger.info("MAX подключён, id=%s", client.me.contact.id if client.me else "?")
    try:
        await _catch_up(client)
    except Exception:
        logger.exception("не вышло догнать пропущенные сообщения")
    max_ready.set()


@client.on_message()
async def on_max_message(message: Message, client: Client) -> None:
    my_id = client.me.contact.id if client.me else None
    if message.chat_id is None or message.sender == my_id:
        return

    await _deliver(message.chat_id, message)


@dp.message(F.chat.id == GROUP_ID, Command("help", "start"))
async def on_help_command(tg_message: TgMessage) -> None:
    await tg_message.answer(HELP)


@dp.message(F.chat.id == GROUP_ID, Command("join"))
async def on_join_command(tg_message: TgMessage, command: CommandObject) -> None:
    if not command.args:
        await tg_message.reply("Пришли ссылку-приглашение: <code>/join ссылка</code>")
        return

    await max_ready.wait()
    try:
        chat = await client.join_group(command.args.strip())
    except ApiError as error:
        await tg_message.reply(f"MAX не пустил по этой ссылке: {html.escape(str(error))}")
        return

    topic_id = await _ensure_topic(chat.id)
    where = "тема создана." if topic_id else NO_TOPIC
    await tg_message.reply(f"Вступил в «{html.escape(chat.title or str(chat.id))}», {where}")


def _split_phone(args: str) -> tuple[str, str]:
    """Номер люди пишут как «+7 925 023 63 50», поэтому текст начинается там, где кончился он."""
    digits = 0
    index = 0
    while index < len(args) and (args[index].isdigit() or args[index] in "+()- "):
        if args[index].isdigit():
            digits += 1
            if digits == 11:
                index += 1
                break
        index += 1
    return args[:index].strip(), args[index:].strip()


@dp.message(F.chat.id == GROUP_ID, Command("write"))
async def on_write_command(tg_message: TgMessage, command: CommandObject) -> None:
    raw_phone, text = _split_phone((command.args or "").strip())
    if not raw_phone or not text:
        await tg_message.reply("Формат: <code>/write +79991234567 привет</code>")
        return

    await max_ready.wait()
    phone = normalize_phone(raw_phone)
    try:
        user = await client.search_by_phone(phone)
    except ApiError:
        await tg_message.reply(f"В MAX нет аккаунта на номере {html.escape(phone)}.")
        return

    chat_id = client.get_chat_id(user.id, client.me.contact.id)
    await client.send_message(chat_id, text)

    name = _display_name(user, user.id)
    topic_id = await _ensure_topic(chat_id, name)
    await bot.send_message(GROUP_ID, html.escape(text), message_thread_id=topic_id)
    where = "дальше пиши в его теме." if topic_id else NO_TOPIC
    await tg_message.reply(f"Отправлено «{html.escape(name)}», {where}")


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id.is_not(None), F.text)
async def on_tg_message(tg_message: TgMessage) -> None:
    chat_id = topics.chat_for_topic(tg_message.message_thread_id)
    if chat_id is None:
        await tg_message.reply("Эта тема не связана с чатом MAX.")
        return

    await max_ready.wait()
    await client.send_message(chat_id, tg_message.text)


async def main() -> None:
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=GROUP_ID))
    await asyncio.gather(client.start(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
