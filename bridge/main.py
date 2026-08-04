import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat
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
    "Вложения пока приходят пометкой <i>[фото]</i>, сам файл не передаётся.\n"
    "Мост живёт, пока открыто окно <code>3-запустить-мост.cmd</code>."
)

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


async def _ensure_topic(chat_id: int, title: str | None = None) -> int:
    topic_id = topics.topic_for_chat(chat_id)
    if topic_id is not None:
        return topic_id

    title = title or await _topic_title(chat_id)
    topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=title[:128])
    topics.link(chat_id, topic.message_thread_id, title)
    logger.info("создана тема %s для чата MAX %s (%s)", topic.message_thread_id, chat_id, title)
    return topic.message_thread_id


async def _render(message: Message) -> str:
    lines = [f"<b>{html.escape(await _sender_name(message.sender))}</b>"]
    if message.text:
        lines.append(html.escape(message.text))
    for attachment in message.attaches:
        kind = getattr(attachment.type, "value", str(attachment.type))
        lines.append(f"<i>[{ATTACHMENT_LABELS.get(kind, kind.lower())}]</i>")
    return "\n".join(lines)


@client.on_start()
async def on_max_start(client: Client) -> None:
    logger.info("MAX подключён, id=%s", client.me.contact.id if client.me else "?")
    max_ready.set()


@client.on_message()
async def on_max_message(message: Message, client: Client) -> None:
    my_id = client.me.contact.id if client.me else None
    if message.chat_id is None or message.sender == my_id:
        return

    topic_id = await _ensure_topic(message.chat_id)
    await bot.send_message(GROUP_ID, await _render(message), message_thread_id=topic_id)


@dp.message(F.chat.id == GROUP_ID, Command("help", "start"))
async def on_help_command(tg_message: TgMessage) -> None:
    await tg_message.answer(HELP, message_thread_id=tg_message.message_thread_id)


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

    await _ensure_topic(chat.id)
    await tg_message.reply(f"Вступил в «{html.escape(chat.title or str(chat.id))}», тема создана.")


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
    await tg_message.reply(f"Отправлено «{html.escape(name)}», дальше пиши в его теме.")


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
