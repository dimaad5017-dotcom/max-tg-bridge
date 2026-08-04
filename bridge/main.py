import asyncio
import html
import logging
from typing import Any, NamedTuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    MessageReactionUpdated,
    ReactionTypeEmoji,
)
from aiogram.types import Message as TgMessage
from pymax import Client, Message, User
from pymax.exceptions import ApiError
from pymax.files.file import File
from pymax.files.photo import Photo
from pymax.files.video import Video
from pymax.types.events.reaction import ReactionUpdateEvent

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

# Отметки самого Telegram, а не сообщения человека: на них молчим.
SERVICE_CONTENT = {
    ContentType.FORUM_TOPIC_CREATED,
    ContentType.FORUM_TOPIC_EDITED,
    ContentType.FORUM_TOPIC_CLOSED,
    ContentType.FORUM_TOPIC_REOPENED,
    ContentType.GENERAL_FORUM_TOPIC_HIDDEN,
    ContentType.GENERAL_FORUM_TOPIC_UNHIDDEN,
    ContentType.PINNED_MESSAGE,
    ContentType.NEW_CHAT_MEMBERS,
    ContentType.LEFT_CHAT_MEMBER,
    ContentType.NEW_CHAT_TITLE,
    ContentType.NEW_CHAT_PHOTO,
    ContentType.DELETE_CHAT_PHOTO,
    ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
    ContentType.VIDEO_CHAT_SCHEDULED,
    ContentType.VIDEO_CHAT_STARTED,
    ContentType.VIDEO_CHAT_ENDED,
    ContentType.VIDEO_CHAT_PARTICIPANTS_INVITED,
}

CONTENT_NAMES = {
    ContentType.POLL: "опрос",
    ContentType.LOCATION: "геопозицию",
    ContentType.VENUE: "место на карте",
    ContentType.CONTACT: "контакт",
    ContentType.DICE: "кубик",
    ContentType.GAME: "игру",
    ContentType.STORY: "историю",
    ContentType.INVOICE: "счёт",
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


class Fetched(NamedTuple):
    file: BufferedInputFile | None
    problem: str = ""


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


async def _download(url: str, name: str) -> Fetched:
    try:
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            response.raise_for_status()
            if (response.content_length or 0) > UPLOAD_LIMIT:
                return Fetched(None, "весит больше 50 МБ, столько Telegram не принимает")
            return Fetched(BufferedInputFile(await response.read(), filename=name))
    except aiohttp.ClientError as error:
        logger.error("не скачать вложение %s: %s", name, error)
        return Fetched(None, f"не скачалось ({type(error).__name__})")


def _lost(kind: str, reason: str) -> str:
    """Что не доехало и почему — иначе человек не узнает, что вообще что-то было."""
    label = ATTACHMENT_LABELS.get(kind, kind.lower())
    return f"<b>Не доставлено:</b> {label} — {reason}. Посмотреть можно только в MAX."


async def _compose(chat_id: int, message: Message) -> tuple[str, list[Media]]:
    """Текст сообщения и то, что удалось выкачать; про остальное честно пишем в тексте."""
    lines = [f"<b>{html.escape(await _sender_name(message.sender))}</b>"]
    if message.text:
        lines.append(html.escape(message.text))

    media: list[Media] = []
    for attachment in message.attaches:
        kind = getattr(attachment.type, "value", str(attachment.type))

        if kind == "SHARE" and attachment.url:
            lines.append(html.escape(attachment.url))
            continue
        if kind == "CONTACT":
            who = attachment.name or " ".join(
                part for part in (attachment.first_name, attachment.last_name) if part
            )
            lines.append(f"<i>контакт:</i> {html.escape(who or 'без имени')}")
            continue

        source = await _source(chat_id, message, attachment, kind)
        if source is None:
            lines.append(_lost(kind, "мост такое не умеет"))
            continue

        fetched = await _download(source[1], source[2])
        if fetched.file is None:
            lines.append(_lost(kind, fetched.problem))
            continue

        media.append(Media(source[0], fetched.file))

    return "\n".join(lines), media


async def _deliver(chat_id: int, message: Message) -> None:
    topic_id = await _ensure_topic(chat_id)
    caption, media = await _compose(chat_id, message)

    # Подпись вешаем на первый файл; стикер подписи не принимает, длинный текст в неё не влезет.
    inline = bool(media) and media[0].kind != "sticker" and len(caption) <= CAPTION_LIMIT
    first: TgMessage | None = None
    for index, item in enumerate(media):
        method, argument = MEDIA_SENDERS[item.kind]
        payload = {argument: item.file, "message_thread_id": topic_id}
        if inline and index == 0:
            payload["caption"] = caption
        posted = await getattr(bot, method)(GROUP_ID, **payload)
        first = first or posted
        logger.info("переслали %s из чата MAX %s", item.kind, chat_id)

    if not inline:
        posted = await bot.send_message(GROUP_ID, caption, message_thread_id=topic_id)
        first = first or posted

    if first is not None:
        topics.pair_messages(chat_id, message.id, first.message_id)
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


@client.on_reaction_update()
async def on_max_reaction(event: ReactionUpdateEvent, client: Client) -> None:
    tg_message_id = topics.tg_message_for(event.chat_id, event.message_id)
    if tg_message_id is None:
        return

    # Telegram разрешает боту одну реакцию на сообщение, поэтому берём самую популярную.
    top = max(event.counters, key=lambda counter: counter.count, default=None)
    emoji = [ReactionTypeEmoji(emoji=top.reaction)] if top else []
    try:
        await bot.set_message_reaction(GROUP_ID, tg_message_id, reaction=emoji)
    except TelegramBadRequest:
        # Наборы эмодзи у MAX и Telegram разные, и Telegram берёт не всякое — тогда говорим словами.
        if top:
            await bot.send_message(
                GROUP_ID,
                f"реакция {html.escape(top.reaction)}",
                reply_to_message_id=tg_message_id,
            )


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


def _outgoing(tg_message: TgMessage) -> tuple[type[Photo | Video | File], object, str] | None:
    """(чем завернуть, что скачать, имя файла) — MAX принимает картинку, видео и «просто файл»."""
    if tg_message.photo:
        return Photo, tg_message.photo[-1], "photo.jpg"
    if tg_message.video:
        return Video, tg_message.video, tg_message.video.file_name or "video.mp4"
    if tg_message.video_note:
        return Video, tg_message.video_note, "video_note.mp4"
    if tg_message.animation:
        return Video, tg_message.animation, tg_message.animation.file_name or "animation.mp4"
    if tg_message.voice:
        return File, tg_message.voice, "voice.ogg"
    if tg_message.audio:
        return File, tg_message.audio, tg_message.audio.file_name or "audio.mp3"
    if tg_message.sticker:
        return File, tg_message.sticker, "sticker.webp"
    if tg_message.document:
        return File, tg_message.document, tg_message.document.file_name or "file"
    return None


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id.is_not(None))
async def on_tg_message(tg_message: TgMessage) -> None:
    chat_id = topics.chat_for_topic(tg_message.message_thread_id)
    if chat_id is None:
        await tg_message.reply("Эта тема не связана с чатом MAX.")
        return

    text = tg_message.text or tg_message.caption or ""
    outgoing = _outgoing(tg_message)
    if not text and outgoing is None:
        # Молчим только на служебных отметках Telegram. Всё остальное человек прислал сам,
        # и лучше зря предупредить, чем дать ему думать, что сообщение ушло.
        if tg_message.content_type not in SERVICE_CONTENT:
            what = CONTENT_NAMES.get(tg_message.content_type, f"«{tg_message.content_type}»")
            await tg_message.reply(
                f"<b>Не отправлено.</b> MAX не принимает {what} через мост.\n"
                "Умею текст, фото, видео, голосовое, кружок, стикер и файл."
            )
        return

    attachments = None
    if outgoing is not None:
        wrapper, downloadable, name = outgoing
        try:
            # Telegram отдаёт ботам файлы не больше 20 МБ, дальше getFile просто откажет.
            content = await bot.download(downloadable)
        except TelegramBadRequest as error:
            await tg_message.reply(
                "<b>Не отправлено.</b> Telegram не отдал файл боту — обычно это значит, "
                f"что он тяжелее 20 МБ.\n<i>{html.escape(str(error))}</i>"
            )
            return
        attachments = [wrapper(content.read(), name=name)]

    await max_ready.wait()
    try:
        sent = await client.send_message(chat_id, text, attachments=attachments)
    except ApiError as error:
        await tg_message.reply(f"MAX не принял: {html.escape(str(error))}")
        return

    if sent is not None:
        topics.pair_messages(chat_id, sent.id, tg_message.message_id)


@dp.message_reaction(F.chat.id == GROUP_ID)
async def on_tg_reaction(event: MessageReactionUpdated) -> None:
    pair = topics.max_message_for(event.message_id)
    if pair is None:
        return

    chat_id, max_message_id = pair
    emoji = next((item.emoji for item in event.new_reaction if item.type == "emoji"), None)
    await max_ready.wait()
    try:
        if emoji:
            await client.add_reaction(chat_id, max_message_id, emoji)
        else:
            await client.remove_reaction(chat_id, max_message_id)
    except ApiError as error:
        logger.error("MAX не принял реакцию %s: %s", emoji, error)


async def main() -> None:
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=GROUP_ID))
    await asyncio.gather(client.start(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
