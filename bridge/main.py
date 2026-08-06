import asyncio
import html
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from functools import partial
from logging.handlers import RotatingFileHandler
from typing import Any, NamedTuple, TypeAlias

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
    ReplyParameters,
)
from aiogram.types import Message as TgMessage
from pymax import Chat, Client, Message, User
from pymax.exceptions import ApiError
from pymax.files.file import File
from pymax.files.photo import Photo
from pymax.files.video import Video, VideoNote
from pymax.protocol import Opcode
from pymax.types.domain.presence import Presence
from pymax.types.events.mark import MessageReadEvent
from pymax.types.events.message import MessageDeleteEvent
from pymax.types.events.presence import PresenceEvent
from pymax.types.events.reaction import ReactionUpdateEvent
from pymax.types.events.typing import TypingEvent

from .config import MAP_DB, SESSION_NAME, WORK_DIR, normalize_phone, optional, require
from .storage import TopicMap
from .version import PROJECT_URL, installed_version, newer, published_version


def _logging_setup() -> None:
    """Пишем и в окно, и в файл: окно можно закрыть, а разбираться придётся потом.

    Без файла на вопрос «почему мост молчал» ответить нечем: в окне видно только
    последние строки, а после перезапуска не видно и их.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # Не даём логу расти без края: три файла по два мегабайта — это недели работы.
    to_file = RotatingFileHandler(
        WORK_DIR / "bridge.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), to_file],
    )


_logging_setup()
logger = logging.getLogger("bridge")

Attachment: TypeAlias = Photo | Video | VideoNote | File

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

# «Был в сети» MAX присылает событиями, хранить между запусками нечего.
presence: dict[int, Presence] = {}

# В переписке один на один имя над каждой строкой — лишний шум: в теме и так один человек.
group_chats: dict[int, bool] = {}

# Telegram берёт реакции только из своего списка, галочки в нём нет — «посмотрели» ближе всего.
SEEN_MARK = "👀"

# Об удалении сообщения Telegram боту не сообщает вовсе — такого события просто нет в его API.
# Поэтому «убрать» приходится показывать тем, что мост увидеть может: реакцией. Этот значок
# наверх не уходит и реакцией в MAX не становится — он значит «сотри везде». Годится любая
# реакция, какую Telegram даёт поставить; 💩 хороша тем, что всерьёз её не отправишь.
DELETE_MARK = optional("TG_DELETE_MARK", "💩")

if DELETE_MARK == SEEN_MARK:
    raise SystemExit(
        f"В .env TG_DELETE_MARK={DELETE_MARK} — этим значком мост отмечает прочитанное. "
        "Возьми любой другой, иначе прочитанное будет стираться само."
    )

# Свежее этого MAX ещё показывает человека онлайн.
ONLINE_WINDOW = 90

COMMANDS = [
    BotCommand(command="write", description="написать первым по номеру"),
    BotCommand(command="join", description="вступить в чат MAX по ссылке"),
    BotCommand(command="status", description="кто в теме и когда был в сети"),
    BotCommand(command="help", description="памятка"),
]

HELP = (
    "<b>Каждый чат MAX — своя тема.</b> Пишешь в теме — уходит собеседнику.\n\n"
    "<code>/write +7 999 123-45-67 привет</code> — написать первым\n"
    "<code>/join ссылка</code> — вступить в чат по приглашению\n"
    "<code>/status</code> — внутри темы: кто это и когда был в сети\n\n"
    "Ответь на сообщение — ответ уйдёт в MAX тоже ответом.\n"
    "Исправил своё сообщение — исправится и у собеседника в MAX.\n"
    "Реакция под сообщением уходит в MAX и приходит обратно.\n"
    f"{SEEN_MARK} под твоим сообщением — собеседник его прочитал.\n"
    f"{DELETE_MARK} под сообщением — стереть его и здесь, и в MAX у всех.\n"
    "Просто удалить в Telegram мало: в MAX оно останется, "
    "Telegram про удаление боту не говорит.\n\n"
    "Пропущенное за время простоя мост досылает сам при запуске.\n"
    "Мост живёт, пока открыто окно <code>4-запустить-мост.cmd</code>."
)

# Потолок догона: если мост стоял неделю, лучше отдать хвост, чем завалить группу.
HISTORY_LIMIT = 40
# Сколько ждём догонялку. С запасом: сорок сообщений на чат — это ещё и картинки.
CATCH_UP_TIMEOUT = 180

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

# Служебные события чата MAX присылает кодом — без расшифровки это просто «CONTROL».
CONTROL_EVENTS = {
    "new": "чат создан",
    "add": "добавили в чат",
    "remove": "убрали из чата",
    "leave": "вышел из чата",
    "title": "чат переименовали",
    "icon": "сменили картинку чата",
    "pin": "закрепили сообщение",
    "unpin": "открепили сообщение",
    "system": "служебное событие",
}

# Чат, из которого мы ушли или где нас выгнали, темой заводить незачем.
GONE_STATUSES = {"LEFT", "REMOVED", "CLOSED"}

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


def _peer_id(chat: Any) -> int | None:
    my_id = client.me.contact.id if client.me else None
    return next((uid for uid in chat.participants if uid != my_id), None)


async def _topic_title(chat_id: int) -> str:
    chat = await client.get_chat(chat_id)
    if chat.title:
        return chat.title

    peer_id = _peer_id(chat)
    return await _sender_name(peer_id) if peer_id else f"Чат {chat_id}"


async def _is_group(chat_id: int) -> bool:
    if chat_id not in group_chats:
        chat = await client.get_chat(chat_id)
        group_chats[chat_id] = str(getattr(chat.type, "value", chat.type)) != "DIALOG"
    return group_chats[chat_id]


def _moment(value: int) -> str:
    """MAX шлёт время то в секундах, то в миллисекундах — приводим к одному виду."""
    seconds = value / 1000 if value > 10**11 else value
    return datetime.fromtimestamp(seconds).strftime("%d.%m.%Y в %H:%M")


def _last_seen(user_id: int) -> str:
    state = presence.get(user_id)
    if state is None or state.seen is None:
        return "<i>про «был в сети» MAX сообщает событиями — мост ещё ни одного не слышал</i>"

    seconds = state.seen / 1000 if state.seen > 10**11 else state.seen
    if time.time() - seconds < ONLINE_WINDOW:
        return "<i>сейчас в сети</i>"
    return f"<i>был в сети {_moment(state.seen)}</i>"


async def _profile(user_id: int) -> str:
    """Всё, что MAX вообще отдаёт про человека: остального у него просто нет."""
    user = await client.get_user(user_id)
    lines = [f"<b>{html.escape(_display_name(user, user_id))}</b>", _last_seen(user_id)]
    if user is None:
        return "\n".join(lines)

    if user.description:
        lines.append(html.escape(user.description))
    if user.link:
        lines.append(f"профиль: {html.escape(user.link)}")
    if user.phone:
        lines.append(f"телефон: +{user.phone}")
    if user.registration_time:
        lines.append(f"в MAX с {_moment(user.registration_time)}")
    return "\n".join(lines)


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


def _call_line(attachment: Any) -> str:
    """Звонок целиком мост не перенесёт, но про сам факт звонка человек знать должен."""
    duration = attachment.duration or 0
    if not duration:
        return "<i>звонок в MAX — не отвечен</i>"

    seconds = duration // 1000 if duration > 10_000 else duration
    minutes, rest = divmod(int(seconds), 60)
    length = f"{minutes} мин {rest} с" if minutes else f"{rest} с"
    return f"<i>звонок в MAX, {length}</i>"


async def _control_line(attachment: Any) -> str:
    what = CONTROL_EVENTS.get(attachment.event, attachment.event)
    # Кого именно добавили или убрали, pymax не разбирает — поле доезжает как «лишнее».
    who = getattr(attachment, "user_ids", None) or getattr(attachment, "userIds", None) or []
    names = ", ".join([await _sender_name(int(user_id)) for user_id in who])
    return f"<i>{html.escape(what)}{': ' + html.escape(names) if names else ''}</i>"


def _lost(kind: str, reason: str) -> str:
    """Что не доехало и почему — иначе человек не узнает, что вообще что-то было."""
    label = ATTACHMENT_LABELS.get(kind, kind.lower())
    return f"<b>Не доставлено:</b> {label} — {reason}. Посмотреть можно только в MAX."


async def _compose(chat_id: int, message: Message) -> tuple[str, list[Media]]:
    """Текст сообщения и то, что удалось выкачать; про остальное честно пишем в тексте."""
    lines: list[str] = []
    if await _is_group(chat_id):
        lines.append(f"<b>{html.escape(await _sender_name(message.sender))}</b>")
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
        if kind == "CALL":
            lines.append(_call_line(attachment))
            continue
        if kind == "CONTROL":
            lines.append(await _control_line(attachment))
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

    # Ни текста, ни вложений — так выглядят служебные отметки MAX. Лучше показать их
    # одной строкой, чем молча проглотить и оставить человека гадать.
    if not lines and not media:
        lines.append(f"<i>служебное сообщение MAX ({html.escape(message.type)})</i>")

    return "\n".join(lines), media


def _quoted(chat_id: int, message: Message) -> ReplyParameters | None:
    """Ответ MAX кладёт в поле link, которого нет в модели pymax — оно доезжает как «лишнее»."""
    link = getattr(message, "link", None)
    if not isinstance(link, dict) or link.get("type") != "REPLY":
        return None

    quoted = link.get("messageId") or (link.get("message") or {}).get("id")
    tg_message_id = topics.tg_message_for(chat_id, quoted) if quoted else None
    if tg_message_id is None:
        return None

    # На сообщение, стёртое из Telegram, ответить нельзя — тогда просто отправим без цитаты.
    return ReplyParameters(message_id=tg_message_id, allow_sending_without_reply=True)


async def _deliver(chat_id: int, message: Message) -> None:
    topic_id = await _ensure_topic(chat_id)
    caption, media = await _compose(chat_id, message)
    reply = _quoted(chat_id, message)

    # Подпись вешаем на первый файл; стикер подписи не принимает, длинный текст в неё не влезет.
    inline = bool(caption) and bool(media) and media[0].kind != "sticker" and len(caption) <= CAPTION_LIMIT
    first: TgMessage | None = None
    for index, item in enumerate(media):
        method, argument = MEDIA_SENDERS[item.kind]
        payload: dict[str, Any] = {argument: item.file, "message_thread_id": topic_id}
        if index == 0 and reply is not None:
            payload["reply_parameters"] = reply
        if inline and index == 0:
            payload["caption"] = caption
        posted = await getattr(bot, method)(GROUP_ID, **payload)
        first = first or posted
        logger.info("переслали %s из чата MAX %s", item.kind, chat_id)

    if caption and not inline:
        posted = await bot.send_message(
            GROUP_ID,
            caption,
            message_thread_id=topic_id,
            reply_parameters=reply if first is None else None,
        )
        first = first or posted

    if first is not None:
        topics.pair_messages(chat_id, message.id, first.message_id)
    topics.remember_delivered(chat_id, message.time)

    # Раз сообщение доехало в Telegram — в MAX оно прочитано: иначе собеседник
    # вечно видит непрочитанное, а счётчик непрочитанных мешает догону при рестарте.
    try:
        await client.read_message(message.id, chat_id)
    except Exception as error:
        logger.error("не отметить прочтение в MAX чата %s: %s", chat_id, error)


async def _catch_up(client: Client) -> None:
    """MAX отдаёт сообщения живым потоком, поэтому написанное при выключенном мосте берём историей."""
    my_id = client.me.contact.id if client.me else None
    # client.chats на старте бывает ещё пустым — список догоняет логин, поэтому спрашиваем сами.
    chats = await client.fetch_chats() or []
    # Пишем даже про ноль: иначе «ничего не пропустили» и «не увидели ни одного чата»
    # выглядят в логе одинаково — молчанием, а это совсем разные беды.
    logger.info("проверяю пропущенное, чатов: %s", len(chats))
    total = 0
    for chat in chats:
        # Пока мост был выключен, тебя могли добавить в группу, где ещё никто не написал.
        # Живое событие о ней уже не придёт, а по сообщениям её не найти — их нет. Такая
        # группа осталась бы невидимой до первого сообщения, то есть, может быть, надолго.
        try:
            await _greet_new_chat(chat)
        except Exception:
            # Один странный чат не должен утащить за собой всю догонялку.
            logger.exception("не вышло поздороваться с чатом MAX %s", chat.id)

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
            total += len(missed)
            logger.info("догнали %s пропущенных из чата MAX %s", len(missed), chat.id)

    # Говорим и про пустой результат. Начатая и не оконченная работа выглядит как
    # зависание, и человек будет гадать, ждать ему или перезапускать.
    logger.info("проверка окончена, %s", f"догнал {total}" if total else "пропущенного нет")


@client.on_start()
async def on_max_start(client: Client) -> None:
    logger.info("MAX подключён, id=%s", client.me.contact.id if client.me else "?")
    try:
        # Со сроком: запрос к MAX может не получить ответа никогда, и тогда мост
        # застрянет на полпути — живой с виду, но не работающий. Лучше бросить
        # догонялку и работать дальше: свежие сообщения важнее старых.
        await asyncio.wait_for(_catch_up(client), timeout=CATCH_UP_TIMEOUT)
    except TimeoutError:
        logger.error("догонялка не уложилась в %s секунд — иду дальше без неё", CATCH_UP_TIMEOUT)
    except Exception:
        logger.exception("не вышло догнать пропущенные сообщения")
    max_ready.set()


@client.on_presence()
async def on_max_presence(event: PresenceEvent, client: Client) -> None:
    presence[event.user_id] = event.presence


@client.on_typing()
async def on_max_typing(event: TypingEvent, client: Client) -> None:
    topic_id = topics.topic_for_chat(event.chat_id)
    if topic_id is not None:
        await bot.send_chat_action(GROUP_ID, "typing", message_thread_id=topic_id)


@client.on_message_read()
async def on_max_read(event: MessageReadEvent, client: Client) -> None:
    """Telegram не умеет галочек под чужими сообщениями, поэтому отмечаем прочтение реакцией."""
    my_id = client.me.contact.id if client.me else None
    if event.user_id == my_id or event.set_as_unread:
        return

    tg_message_id = topics.last_outgoing(event.chat_id)
    if tg_message_id is None:
        return

    # Реакция у бота одна на сообщение. Если собеседник уже поставил свою — молчим:
    # отметка о прочтении затёрла бы её, а раз отреагировал, значит и прочитал.
    if topics.has_reaction(tg_message_id):
        return

    try:
        await bot.set_message_reaction(GROUP_ID, tg_message_id, reaction=[ReactionTypeEmoji(emoji=SEEN_MARK)])
    except TelegramBadRequest as error:
        logger.error("не отметить прочтение в чате MAX %s: %s", event.chat_id, error)


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
        topics.forget_reaction(tg_message_id)
        if top:
            await bot.send_message(
                GROUP_ID,
                f"реакция {html.escape(top.reaction)}",
                reply_to_message_id=tg_message_id,
            )
    else:
        # Пометка для отметки о прочтении: занята эта ячейка или свободна.
        if top:
            topics.remember_reaction(tg_message_id)
        else:
            topics.forget_reaction(tg_message_id)


@client.on_message_edit()
async def on_max_edit(message: Message, client: Client) -> None:
    if message.chat_id is None:
        return

    tg_message_id = topics.tg_message_for(message.chat_id, message.id)
    if tg_message_id is None:
        return

    text, _ = await _compose(message.chat_id, message)
    text = f"{text}\n<i>(исправлено)</i>"
    try:
        await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=tg_message_id)
    except TelegramBadRequest:
        # У сообщения с файлом правится не текст, а подпись — Telegram считает это разными вещами.
        with suppress(TelegramBadRequest):
            await bot.edit_message_caption(
                chat_id=GROUP_ID, message_id=tg_message_id, caption=text[:CAPTION_LIMIT]
            )


@client.on_message_delete()
async def on_max_delete(event: MessageDeleteEvent, client: Client) -> None:
    """Стирать в Telegram не станем: пропавшее сообщение — тоже информация."""
    for max_message_id in event.message_ids:
        tg_message_id = topics.tg_message_for(event.chat_id, max_message_id)
        if tg_message_id is None:
            continue

        with suppress(TelegramBadRequest):
            await bot.send_message(
                GROUP_ID,
                "<i>это сообщение удалили в MAX</i>",
                reply_parameters=ReplyParameters(message_id=tg_message_id, allow_sending_without_reply=True),
            )


async def _greet_new_chat(chat: Chat) -> None:
    """Тему заводит только пришедшее сообщение — а в новой группе может долго стоять тишина."""
    if topics.topic_for_chat(chat.id) is not None:
        return
    if str(getattr(chat.type, "value", chat.type)) == "DIALOG":
        return
    if str(chat.status).upper() in GONE_STATUSES:
        return

    title = chat.title or f"Чат {chat.id}"
    topic_id = await _ensure_topic(chat.id, title)
    group_chats[chat.id] = True

    invited_by = await _sender_name(chat.invited_by) if chat.invited_by else ""
    lines = [
        f"<b>Тебя добавили в чат «{html.escape(title)}»</b>"
        if invited_by
        else f"<b>Новый чат в MAX: «{html.escape(title)}»</b>"
    ]
    if invited_by:
        lines.append(f"пригласил: {html.escape(invited_by)}")
    count = chat.participants_count or len(chat.participants)
    if count:
        lines.append(f"участников: {count}")
    if chat.description:
        lines.append(html.escape(chat.description))

    await bot.send_message(GROUP_ID, "\n".join(lines), message_thread_id=topic_id)
    logger.info("новый чат MAX %s (%s)", chat.id, title)


@client.on_chat_update()
async def on_max_chat_update(chat: Chat, client: Client) -> None:
    await _greet_new_chat(chat)


@client.on_disconnect()
async def on_max_disconnect(error: Exception, reconnect: bool, delay: float) -> None:
    logger.warning("MAX разорвал связь (%s), переподключение: %s", error, reconnect)


@client.on_message()
async def on_max_message(message: Message, client: Client) -> None:
    my_id = client.me.contact.id if client.me else None
    if message.chat_id is None or message.sender == my_id:
        return

    await _deliver(message.chat_id, message)


@dp.message(F.chat.id == GROUP_ID, Command("help", "start"))
async def on_help_command(tg_message: TgMessage) -> None:
    await tg_message.answer(HELP)


@dp.message(F.chat.id == GROUP_ID, Command("status"))
async def on_status_command(tg_message: TgMessage) -> None:
    chat_id = topics.chat_for_topic(tg_message.message_thread_id or 0)
    if chat_id is None:
        await tg_message.reply("Эту команду надо звать внутри темы собеседника.")
        return

    await max_ready.wait()
    chat = await client.get_chat(chat_id)
    peer_id = None if await _is_group(chat_id) else _peer_id(chat)
    if peer_id is None:
        count = chat.participants_count or len(chat.participants)
        await tg_message.reply(
            f"<b>{html.escape(chat.title or str(chat_id))}</b>\nгрупповой чат, участников: {count}"
        )
        return

    await tg_message.reply(await _profile(peer_id))


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
    # Без подписи это эхо неотличимо от входящего, и выходит, будто собеседник написал
    # первым, хотя он ещё вообще ничего не написал.
    await bot.send_message(GROUP_ID, f"<b>Ты:</b> {html.escape(text)}", message_thread_id=topic_id)
    where = "дальше пиши в его теме." if topic_id else NO_TOPIC
    await tg_message.reply(f"Отправлено «{html.escape(name)}», {where}")


def _outgoing(tg_message: TgMessage) -> tuple[Callable[..., Attachment], object, str] | None:
    """(чем завернуть, что скачать, имя файла)."""
    if tg_message.photo:
        return Photo, tg_message.photo[-1], "photo.jpg"
    if tg_message.video:
        return Video, tg_message.video, tg_message.video.file_name or "video.mp4"
    if tg_message.video_note:
        # Длительность MAX ждёт в миллисекундах, Telegram отдаёт в секундах. Без неё
        # pymax полез бы читать её из самого файла и потребовал лишнюю библиотеку.
        note = partial(VideoNote, duration=tg_message.video_note.duration * 1000)
        return note, tg_message.video_note, "video_note.mp4"
    if tg_message.animation:
        return Video, tg_message.animation, tg_message.animation.file_name or "animation.mp4"
    if tg_message.voice:
        # Голосовым уехать не может: pymax минуту ждёт от MAX сигнала о готовности, а MAX
        # его не присылает. Файлом доезжает мгновенно и слушается.
        return File, tg_message.voice, "voice.ogg"
    if tg_message.audio:
        return File, tg_message.audio, tg_message.audio.file_name or "audio.mp3"
    if tg_message.sticker:
        # Стикеры в Telegram трёх разных пород, и заворачивать их надо по-разному:
        # обычный — картинка, «живой» — короткое видео, а .tgs не умеет никто, кроме
        # самого Telegram, так что он честно уезжает файлом со своим именем.
        if tg_message.sticker.is_animated:
            return File, tg_message.sticker, "sticker.tgs"
        if tg_message.sticker.is_video:
            return Video, tg_message.sticker, "sticker.webm"
        return Photo, tg_message.sticker, "sticker.webp"
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
        raw = content.read()
        attachments = [wrapper(raw, name=name)]

    # Ответом на служебную «шапку» темы Telegram считает первое сообщение в ней —
    # такого в связке нет, поэтому цитата просто не найдётся, и это правильно.
    quoted = tg_message.reply_to_message
    pair = topics.max_message_for(quoted.message_id) if quoted else None
    reply_to = int(pair[1]) if pair else None

    await max_ready.wait()
    try:
        sent = await client.send_message(chat_id, text, reply_to=reply_to, attachments=attachments)
    except ApiError as error:
        # Кружком MAX принимает только своё: 480x480, 30 кадров в секунду. Телефоны пишут
        # по-разному, и когда формат не подошёл, лучше отправить то же самое обычным
        # видео, чем не отправить вовсе. Отказ приходит за доли секунды.
        if tg_message.video_note is None:
            await tg_message.reply(f"MAX не принял: {html.escape(str(error))}")
            return
        logger.info("кружок не принят (%s), отправляю обычным видео", error)
        try:
            sent = await client.send_message(
                chat_id, text, reply_to=reply_to, attachments=[Video(raw, name="video_note.mp4")]
            )
        except ApiError as plain_error:
            await tg_message.reply(f"MAX не принял: {html.escape(str(plain_error))}")
            return

    if sent is not None:
        topics.pair_messages(chat_id, sent.id, tg_message.message_id)
    topics.remember_outgoing(chat_id, tg_message.message_id)


@dp.edited_message(F.chat.id == GROUP_ID, F.message_thread_id.is_not(None))
async def on_tg_edit(tg_message: TgMessage) -> None:
    """Исправил опечатку в теме — исправляем и у собеседника в MAX.

    Правки Telegram присылает отдельным событием, не сообщением, — поэтому и хендлер
    отдельный. Молчим только при удаче: правка и так видна в теме пометкой «изменено».
    """
    pair = topics.max_message_for(tg_message.message_id)
    if pair is None:
        # Сообщение не проходило через мост — править в MAX нечего.
        return

    chat_id, max_message_id = pair
    if not tg_message.text:
        # У сообщения с файлом Telegram правит подпись, а MAX при правке ждёт список
        # вложений заново: пустой список он поймёт как «вложений больше нет» и снесёт
        # сам файл. Ради подписи терять картинку — плохая сделка.
        await tg_message.reply(
            "<b>Правка не ушла.</b> Подпись к файлу мост не правит: MAX при правке "
            f"потерял бы сам файл. Поставь {DELETE_MARK} и отправь заново."
        )
        return

    await max_ready.wait()
    try:
        await client.edit_message(chat_id, int(max_message_id), text=tg_message.text)
    except (ApiError, ValueError, TypeError) as error:
        # Ты видишь в теме «изменено» и уверен, что собеседник читает исправленное.
        logger.error("MAX не принял правку сообщения %s: %s", max_message_id, error)
        await tg_message.reply(
            f"<b>Правка не ушла в MAX.</b> У собеседника осталось прежнее. "
            f"MAX отказал: {html.escape(str(error))}"
        )
        return
    logger.info("правка уехала в MAX: чат %s, сообщение %s", chat_id, max_message_id)


def _why_telegram_refused(error: TelegramBadRequest) -> str:
    """Называет настоящую причину, а не первую правдоподобную.

    Раньше мост на любой отказ говорил «не хватает права». Право оказывалось на месте,
    человек шёл его проверять и терял время впустую. Лучше сказать точно или честно
    показать чужие слова, чем гадать.
    """
    words = str(error).lower()
    if "message can't be deleted" in words:
        return "Telegram не даёт ботам стирать сообщения старше 48 часов, а это — старше."
    if "not enough rights" in words:
        return "Боту не хватает права «Удаление сообщений» в настройках группы."
    if "message to delete not found" in words:
        return "Сообщения здесь уже нет — видимо, его удалили раньше."
    return f"Telegram отказал: {html.escape(str(error))}"


async def _react(chat_id: int, max_message_id: str, emoji: str | None) -> None:
    """Ставит или снимает реакцию в MAX.

    Мимо `client.add_reaction` нарочно. Библиотека кладёт номер сообщения строкой, а MAX
    на этом месте ждёт число: он отвечает «Expected number» и следом рвёт связь. В удалении
    и правке та же библиотека шлёт число — потому они и работают. Так что шлём сами: тот же
    опкод, тот же вид запроса, но номер числом. Починят наверху — этот кусок можно выбросить.
    """
    payload: dict[str, Any] = {"chatId": chat_id, "messageId": int(max_message_id)}
    api = client.messages.app
    if emoji:
        payload["reaction"] = {"reactionType": "EMOJI", "id": emoji}
        await api.invoke(Opcode.MSG_REACTION, payload)
    else:
        await api.invoke(Opcode.MSG_CANCEL_REACTION, payload)


async def _erase(chat_id: int, max_message_id: str, tg_message_id: int) -> None:
    """Стирает сообщение и в MAX, и в Telegram.

    Половина дела здесь хуже, чем ничего: человек решил, что сообщения быть не должно,
    а оно осталось лежать у собеседника. Поэтому сначала MAX, и только если получилось —
    Telegram. Не вышло — говорим словами, молча не бросаем.
    """
    try:
        await client.delete_message(chat_id, [int(max_message_id)], for_me=False)
    except (ApiError, ValueError, TypeError) as error:
        logger.error("MAX не дал удалить сообщение %s: %s", max_message_id, error)
        await bot.send_message(
            GROUP_ID,
            "<b>Не удалено в MAX.</b> Сообщение осталось у собеседника — "
            f"MAX отказал: {html.escape(str(error))}",
            reply_to_message_id=tg_message_id,
        )
        return

    topics.forget_reaction(tg_message_id)
    try:
        await bot.delete_message(GROUP_ID, tg_message_id)
    except TelegramBadRequest as error:
        # В MAX уже стёрли, так что промолчать нельзя: иначе решишь, что не сработало вовсе.
        logger.error("в MAX удалено, а в Telegram нет: %s", error)
        await bot.send_message(
            GROUP_ID,
            f"<b>В MAX стёрто, а здесь нет.</b> {_why_telegram_refused(error)} "
            "Копию удали сам — у собеседника её уже нет.",
            reply_to_message_id=tg_message_id,
        )
        return
    logger.info("стёрто везде: чат MAX %s, сообщение %s", chat_id, max_message_id)


@dp.message_reaction(F.chat.id == GROUP_ID)
async def on_tg_reaction(event: MessageReactionUpdated) -> None:
    # Отметку о прочтении мы ставим сами, и она тоже прилетает сюда — иначе получится петля.
    if event.user is not None and event.user.id == bot.id:
        return

    pair = topics.max_message_for(event.message_id)
    if pair is None:
        return

    chat_id, max_message_id = pair
    emoji = next((item.emoji for item in event.new_reaction if item.type == "emoji"), None)
    await max_ready.wait()

    if emoji == DELETE_MARK:
        # Наверх не пересылаем: это не реакция собеседнику, а распоряжение мосту.
        await _erase(chat_id, max_message_id, event.message_id)
        return

    try:
        await _react(chat_id, max_message_id, emoji)
    except (ApiError, ValueError, TypeError) as error:
        # Молча проглотить нельзя: ты видишь свою реакцию под сообщением и уверен, что она ушла.
        logger.error("MAX не принял реакцию %s: %s", emoji, error)
        await bot.send_message(
            GROUP_ID,
            f"<b>Реакция не ушла в MAX.</b> {html.escape(str(error))}",
            reply_to_message_id=event.message_id,
        )


async def _tell_about_update() -> None:
    """Пишем в общий раздел группы: новая версия — не про какой-то один чат.

    Молчим, если GitHub недоступен или версия та же. Про одну и ту же версию
    говорим один раз за всё время, даже если мост перезапускали.
    """
    there = await published_version()
    if there is None:
        return

    here = installed_version()
    if not newer(there, here) or topics.already_announced(there):
        return

    topics.remember_announced(there)
    logger.info("вышла версия %s, у нас %s", there, here)
    with suppress(TelegramBadRequest):
        await bot.send_message(
            GROUP_ID,
            f"<b>Вышла новая версия моста: {html.escape(there)}</b> (у тебя {html.escape(here)}).\n\n"
            "MAX время от времени меняет свой протокол, и старые версии однажды перестают "
            f'работать. Как обновиться — <a href="{PROJECT_URL}#как-обновиться">короткая '
            "инструкция</a>, минут на пять.",
            disable_web_page_preview=True,
        )


async def main() -> None:
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=GROUP_ID))
    # Первая строка, по которой видно, что токен рабочий и группа на месте: без неё
    # окно молчит до первого сообщения, и непонятно, живой мост или нет.
    logger.info("Telegram на связи, слушаю группу %s", GROUP_ID)

    # Отдельной задачей и нарочно не в halves: мост не должен ни ждать GitHub при
    # запуске, ни падать из-за него. Имя держим, иначе задачу соберёт мусорщик.
    about_update = asyncio.create_task(_tell_about_update())

    halves = [asyncio.create_task(client.start()), asyncio.create_task(dp.start_polling(bot))]
    # Половина моста без второй бесполезна и незаметна: Telegram продолжит принимать
    # сообщения в никуда. Лучше упасть целиком — запуск поднимет нас заново.
    done, pending = await asyncio.wait(halves, return_when=asyncio.FIRST_COMPLETED)
    about_update.cancel()
    for task in pending:
        task.cancel()
    await asyncio.gather(about_update, *pending, return_exceptions=True)
    for task in done:
        task.result()
    logger.error("одна из половин моста остановилась — выхожу, чтобы запуститься заново")


if __name__ == "__main__":
    asyncio.run(main())
