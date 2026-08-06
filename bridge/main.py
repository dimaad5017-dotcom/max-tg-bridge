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
from pymax import Chat, Client, Message, PrivacySettingsUpdate, User
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

from .config import (
    MAP_DB,
    SEEN_MARK,
    SESSION_NAME,
    WORK_DIR,
    delete_mark,
    flag,
    normalize_phone,
    require,
)
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

# Об удалении сообщения Telegram боту не сообщает вовсе — такого события просто нет в его API.
# Поэтому «убрать» приходится показывать тем, что мост увидеть может: реакцией. Этот значок
# наверх не уходит и реакцией в MAX не становится — он значит «сотри везде». Годится любая
# реакция, какую Telegram даёт поставить; 💩 хороша тем, что всерьёз её не отправишь.
# Сам значок и проверка на совпадение с отметкой о прочтении — в config.py, к настройкам.
DELETE_MARK = delete_mark()

# Свежее этого MAX ещё показывает человека онлайн.
ONLINE_WINDOW = 90

# «В сети» в MAX — это не человек у экрана, а признак interactive в опросе связи:
# библиотека шлёт его каждые полминуты. Мост держит связь круглые сутки, поэтому
# по умолчанию мы этот признак снимаем — иначе собеседники видят вечный онлайн там,
# где никого нет. Кому такой онлайн нужен, ставит в .env MAX_SHOW_ONLINE=да.
SHOW_ONLINE = flag("MAX_SHOW_ONLINE")

COMMANDS = [
    BotCommand(command="write", description="написать первым по номеру"),
    BotCommand(command="chats", description="все чаты MAX и их темы"),
    BotCommand(command="join", description="вступить в чат MAX по ссылке"),
    BotCommand(command="leave", description="выйти из чата — внутри его темы"),
    BotCommand(command="del", description="ответом: стереть и здесь, и в MAX"),
    BotCommand(command="status", description="кто в теме и когда был в сети"),
    BotCommand(command="hidden", description="прятать ли «был в сети» в MAX"),
    BotCommand(command="help", description="памятка"),
]

HELP = (
    "<b>Каждый чат MAX — своя тема.</b> Пишешь в теме — уходит собеседнику.\n\n"
    "<b>Команды</b>\n"
    "<code>/write +7 999 123-45-67 привет</code> — написать первым, даже если чата ещё нет\n"
    "<code>/chats</code> — все чаты MAX и какие из них уже стали темами\n"
    "<code>/join ссылка</code> — вступить в чат MAX по приглашению\n"
    "<code>/leave</code> — внутри темы: выйти из этого чата MAX. Спросит подтверждение: "
    "обратно пустят только по новому приглашению\n"
    "<code>/del</code> — ответом на сообщение: стереть его и здесь, и в MAX\n"
    "<code>/status</code> — внутри темы: кто это и когда был в сети\n"
    "<code>/hidden off</code> — показаться в сети (мост прячет это сам)\n"
    "<code>/help</code> — эта памятка\n\n"
    "<b>Что мост делает сам</b>\n"
    "Ответь на сообщение — ответ уйдёт в MAX тоже ответом.\n"
    "Исправил своё сообщение — исправится и у собеседника в MAX. Подпись под картинкой "
    "мост не правит: MAX при правке потерял бы сам файл.\n"
    "Реакция под сообщением уходит в MAX и приходит обратно.\n"
    f"{SEEN_MARK} под твоим сообщением — значит, собеседник его прочитал.\n"
    "Пропущенное за время простоя мост досылает сам при запуске.\n\n"
    "<b>Как стереть</b>\n"
    f"{DELETE_MARK} под сообщением или <code>/del</code> ответом на него — одно и то же: "
    "значок быстрее, команда виднее. Стирается у всех: и здесь, и в MAX.\n"
    "Просто удалить в Telegram мало: про удаление Telegram боту не говорит, "
    "и в MAX сообщение останется лежать.\n"
    "Своё старше двух суток Telegram боту стирать не даёт: в MAX уйдёт, "
    "а здесь придётся убрать руками. Мост об этом скажет.\n\n"
    "<b>Про «в сети»</b>\n"
    "Мост держит связь с MAX круглые сутки, а MAX считает это «человек у экрана». "
    "Поэтому мост при каждом запуске прячет твоё «был в сети» — иначе ты был бы "
    "онлайн вечно, даже ночью. Просить об этом не надо, само.\n"
    "<code>/hidden off</code> — показаться, если вдруг понадобилось. До перезапуска.\n"
    "Нужно наоборот и насовсем — впиши <code>MAX_SHOW_ONLINE=да</code> в настройки.\n\n"
    "Писать надо внутри темы: из общей части группы в MAX ничего не уходит, "
    "там мост только отвечает на команды.\n"
    "Мост живёт, пока открыто окно <code>4-запустить-мост.cmd</code>."
)

# Потолок догона: если мост стоял неделю, лучше отдать хвост, чем завалить группу.
HISTORY_LIMIT = 40
# Сколько ждём догонялку. С запасом: сорок сообщений на чат — это ещё и картинки.
CATCH_UP_TIMEOUT = 180
# Как часто перечитываем список чатов на случай, что о новом MAX не сказал.
NEW_CHAT_SCAN = 300

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

# Потолок одного сообщения в Telegram. Сверх него оно не обрезается, а не уходит вовсе.
MESSAGE_LIMIT = 4096

# Столько знаков Telegram даёт на название темы; длиннее он всё равно не примет.
TITLE_LIMIT = 128

# Из группы MAX обратно пускают только по приглашению — такое подтверждаем словом.
YES_WORDS = {"да", "yes", "точно"}

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


async def _topic_title(chat_id: int, chat: Any = None) -> str:
    """Чат берём готовым, если он уже на руках: лишний запрос к MAX ничего не добавит."""
    chat = chat if chat is not None else await client.get_chat(chat_id)
    if chat.title:
        return chat.title

    peer_id = _peer_id(chat)
    return await _sender_name(peer_id) if peer_id else f"Чат {chat_id}"


def _is_group_chat(chat: Any) -> bool:
    """Групповой чат или личка — по типу, который MAX кладёт в сам чат."""
    return str(getattr(chat.type, "value", chat.type)) != "DIALOG"


async def _is_group(chat_id: int) -> bool:
    """То же самое, но когда на руках один номер чата. Спрашиваем MAX и запоминаем ответ."""
    if chat_id not in group_chats:
        group_chats[chat_id] = _is_group_chat(await client.get_chat(chat_id))
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
        topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=title[:TITLE_LIMIT])
    except TelegramBadRequest as error:
        logger.error("не создать тему для чата MAX %s: %s", chat_id, error)
        return None

    topics.link(chat_id, topic.message_thread_id, title)
    logger.info("создана тема %s для чата MAX %s (%s)", topic.message_thread_id, chat_id, title)
    return topic.message_thread_id


async def _post(method: str, topic_id: int | None, **payload: Any) -> TgMessage:
    """Кладёт сообщение в тему и сам открывает её, если она закрыта.

    Закрытая тема — не редкость. Её закрывает `/leave`, её можно закрыть руками,
    прибираясь в списке, а потом в тот же чат снова придёт сообщение: тебя вернут
    в школьную группу по новому приглашению или просто напишут в теме, которую ты
    закрыл сгоряча. Связка чат↔тема при этом остаётся, и Telegram отвечает
    TOPIC_CLOSED — раньше на этом всё и заканчивалось: исключение улетало в
    библиотеку, в теме не появлялось ничего, и про сообщение можно было узнать,
    только открыв MAX. Ровно то молчание, которого мост не должен допускать.

    Открывать тему обратно правильнее, чем рвать связку при выходе: переписка
    остаётся на месте, и вернувшись, ты продолжаешь старую тему, а не заводишь
    рядом вторую такую же.
    """
    send = getattr(bot, method)
    try:
        return await send(GROUP_ID, message_thread_id=topic_id, **payload)
    except TelegramBadRequest as error:
        if topic_id is None or "TOPIC_CLOSED" not in str(error).upper():
            raise

    try:
        await bot.reopen_forum_topic(GROUP_ID, topic_id)
    except TelegramBadRequest as error:
        # Права «Управление темами» может и не быть. Тогда в общий раздел: там сообщение
        # увидят, а в закрытой теме — нет. Так же мост поступает, когда темы вовсе не вышло.
        logger.error("тема %s закрыта, открыть не дали (%s) — пишу в общий раздел", topic_id, error)
        return await send(GROUP_ID, message_thread_id=None, **payload)

    logger.info("тема %s была закрыта — открыл заново", topic_id)
    return await send(GROUP_ID, message_thread_id=topic_id, **payload)


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
        payload: dict[str, Any] = {argument: item.file}
        if index == 0 and reply is not None:
            payload["reply_parameters"] = reply
        if inline and index == 0:
            payload["caption"] = caption
        posted = await _post(method, topic_id, **payload)
        first = first or posted
        logger.info("переслали %s из чата MAX %s", item.kind, chat_id)

    if caption and not inline:
        posted = await _post(
            "send_message",
            topic_id,
            text=caption,
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


async def _hide_presence(client: Client) -> None:
    """Прячет «был в сети» при каждом запуске, а не ждёт, пока про это вспомнят.

    Настройка приватности живёт в самом MAX и держится сама — но ставится один раз
    и руками. Цена забывчивости здесь выше обычной: мост держит связь круглые сутки,
    и всё это время присутствие видно всем. Значит, решать это должен не человек,
    который может не вспомнить, а мост, который запускается каждый раз.

    Отказ MAX глушим нарочно: приватность важна, но не настолько, чтобы из-за неё
    остаться вообще без моста. В окно напишем — оно у человека перед глазами.
    """
    try:
        await client.change_profile_settings(PrivacySettingsUpdate(hide_online_status=not SHOW_ONLINE))
    except Exception:
        logger.exception("не вышло спрятать «был в сети» — сделай /hidden on вручную")
        return
    logger.info("«был в сети» в MAX %s", "показываю" if SHOW_ONLINE else "спрятал")


@client.on_start()
async def on_max_start(client: Client) -> None:
    logger.info("MAX подключён, id=%s", client.me.contact.id if client.me else "?")
    await _hide_presence(client)
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
        # В закрытую тему «печатает…» не встанет. Открывать её ради этого не стоит:
        # содержания в нём нет, а следом придёт само сообщение — вот оно тему и откроет.
        with suppress(TelegramBadRequest):
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
    if not _is_group_chat(chat):
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

    await _post("send_message", topic_id, text="\n".join(lines))
    logger.info("новый чат MAX %s (%s)", chat.id, title)


@client.on_chat_update()
async def on_max_chat_update(chat: Chat, client: Client) -> None:
    await _greet_new_chat(chat)


async def _watch_new_chats() -> None:
    """Раз в пять минут перечитывает список чатов: вдруг тебя куда-то добавили, а мост не заметил.

    О новом чате MAX присылает событие, и обычно тема заводится сразу. Но что событие
    приходит всегда, проверить нельзя: для этого нужно, чтобы кто-то добавил тебя в
    группу — в чужих руках. А пропустить его дорого. Тихую группу, где ещё никто не
    написал, по сообщениям не найти: сообщений нет. Значит, она осталась бы невидимой
    до следующего запуска моста — а мост работает неделями подряд.

    Поэтому не полагаемся на одно событие. Один запрос в пять минут не стоит ничего,
    а школьный чат, о котором не узнал, стоит дорого. Объявление от этого не задвоится:
    чат с уже заведённой темой `_greet_new_chat` пропускает.
    """
    await max_ready.wait()
    while True:
        await asyncio.sleep(NEW_CHAT_SCAN)
        try:
            for chat in await client.fetch_chats() or []:
                await _greet_new_chat(chat)
        except Exception:
            # Сеть моргнула или MAX ответил не так. Это не повод бросать проверку
            # навсегда — через пять минут спросим снова.
            logger.exception("не вышло перечитать список чатов MAX")


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
    await _post("send_message", topic_id, text=f"<b>Ты:</b> {html.escape(text)}")
    where = "дальше пиши в его теме." if topic_id else NO_TOPIC
    await tg_message.reply(f"Отправлено «{html.escape(name)}», {where}")


@dp.message(F.chat.id == GROUP_ID, Command("chats"))
async def on_chats_command(tg_message: TgMessage) -> None:
    """Что вообще есть в MAX и что из этого уже стало темой.

    Тему заводит первое сообщение, поэтому половина чатов может быть ещё не видна.
    Без такого списка о них неоткуда узнать, не открывая сам MAX, — а весь смысл
    моста в том, чтобы туда не заходить.
    """
    await max_ready.wait()
    chats = await client.fetch_chats() or []
    lines = []
    for chat in chats:
        if str(chat.status).upper() in GONE_STATUSES:
            continue
        # Всё нужное MAX уже прислал в самом списке. Спрашивать про каждый чат отдельно —
        # это запрос на чат: на полусотне школьных чатов ответ ползёт, а MAX вправе начать
        # придерживать такую очередь. Заодно запоминаем тип: он же понадобится при отправке.
        group_chats[chat.id] = _is_group_chat(chat)
        topic_id = topics.topic_for_chat(chat.id)
        title = chat.title or topics.title_for_chat(chat.id) or await _topic_title(chat.id, chat)
        kind = "" if group_chats[chat.id] else "личка, "
        where = "тема есть" if topic_id else "темы ещё нет — заведётся с первым сообщением"
        lines.append(f"• <b>{html.escape(title[:TITLE_LIMIT])}</b> — {kind}{where}")

    if not lines:
        await tg_message.reply(
            "В аккаунте MAX не видно ни одного чата. Если он новый — так и должно быть: "
            "напиши кому-нибудь через <code>/write</code>, и тема появится."
        )
        return

    # Считаем знаки, а не строки. Сообщение сверх потолка Telegram не обрезает, а отвергает
    # целиком: полсотни длинных названий — и вместо списка не приходит ничего. Место под
    # заголовок и хвост «…и ещё N» держим заранее — дописывать их в полное сообщение поздно.
    head: list[str] = []
    room = MESSAGE_LIMIT - len(f"<b>Чаты в MAX: {len(lines)}</b>") - len("\n<i>…и ещё 000</i>")
    for line in lines:
        if len(line) + 1 > room:
            break
        room -= len(line) + 1
        head.append(line)

    tail = f"\n<i>…и ещё {len(lines) - len(head)}</i>" if len(lines) > len(head) else ""
    await tg_message.reply(f"<b>Чаты в MAX: {len(lines)}</b>\n" + "\n".join(head) + tail)


@dp.message(F.chat.id == GROUP_ID, Command("leave"))
async def on_leave_command(tg_message: TgMessage, command: CommandObject) -> None:
    """Выйти из группы MAX, не открывая MAX.

    Спрашиваем подтверждение нарочно: вернуться можно только по новому приглашению,
    а команду легко бросить не в ту тему — темы в списке стоят рядом.
    """
    chat_id = topics.chat_for_topic(tg_message.message_thread_id or 0)
    if chat_id is None:
        await tg_message.reply("Эту команду надо звать внутри темы того чата, из которого выходишь.")
        return

    await max_ready.wait()
    if not await _is_group(chat_id):
        # Советовать «заблокируй в MAX» — значит гнать человека туда, куда он не ходит:
        # ради этого мост и написан. Говорим про то, что делается здесь и одним движением.
        await tg_message.reply(
            "Это личка, из неё не выходят — в MAX такого действия просто нет.\n"
            "Надоел — заглуши тему: долгое нажатие на неё в списке → «Отключить уведомления». "
            "Сообщения будут приходить молча, и ни одно не потеряется.\n"
            "Заблокировать через мост пока нельзя: в MAX это умеет только само приложение."
        )
        return

    # Имя темы уже записано в связке — лишний раз спрашивать его у MAX незачем.
    title = topics.title_for_chat(chat_id) or await _topic_title(chat_id)
    if (command.args or "").strip().lower() not in YES_WORDS:
        await tg_message.reply(
            f"Выйти из «{html.escape(title)}»? Обратно пустят только по новому приглашению.\n"
            "Если решил — <code>/leave да</code>"
        )
        return

    try:
        await client.leave_group(chat_id)
    except ApiError as error:
        await tg_message.reply(f"MAX не дал выйти: {html.escape(str(error))}")
        return

    logger.info("вышли из чата MAX %s (%s)", chat_id, title)
    await tg_message.reply(f"Вышел из «{html.escape(title)}». Тему закрываю, переписка останется.")
    # Тему не удаляем: написанное в ней — единственный след разговора, который в MAX
    # уже не открыть. Закрытая тема перестаёт мозолить глаза, но остаётся читаемой.
    with suppress(TelegramBadRequest):
        await bot.close_forum_topic(GROUP_ID, tg_message.message_thread_id)


@dp.message(F.chat.id == GROUP_ID, Command("hidden"))
async def on_hidden_command(tg_message: TgMessage, command: CommandObject) -> None:
    """Ручка к тому, что мост и так делает сам при запуске.

    Прятать — состояние по умолчанию, и команда нужна ровно затем, чтобы разово
    передумать: показаться на вечер, если понадобилось. Насовсем это не меняет,
    следующий запуск снова спрячет.
    """
    choice = (command.args or "").strip().lower()
    if choice not in {"on", "off"}:
        await tg_message.reply(
            "«Был в сети» мост прячет сам при каждом запуске — он держит связь круглые "
            "сутки, и без этого ты для всех вечно в сети.\n\n"
            "<code>/hidden off</code> — показать «был в сети» собеседникам\n"
            "<code>/hidden on</code> — снова спрятать\n\n"
            "Это до перезапуска: потом мост опять спрячет."
        )
        return

    await max_ready.wait()
    try:
        await client.change_profile_settings(PrivacySettingsUpdate(hide_online_status=choice == "on"))
    except ApiError as error:
        await tg_message.reply(f"MAX не принял настройку: {html.escape(str(error))}")
        return

    logger.info("скрытый режим: %s", choice)
    await tg_message.reply(
        "Спрятал: «был в сети» собеседники больше не видят."
        if choice == "on"
        else "«Был в сети» снова виден собеседникам, и с работающим мостом это «всегда». "
        "До перезапуска: дальше мост опять спрячет."
    )


@dp.message(F.chat.id == GROUP_ID, Command("del"))
async def on_del_command(tg_message: TgMessage) -> None:
    """То же самое, что значок стирания, только словом.

    Значком быстрее — одно касание вместо набора. Но значок ниоткуда не виден: о том,
    что стирать вообще можно, узнаёшь, только если тебе об этом сказали. Команда стоит
    в списке команд, и её видно. Пусть будут обе дороги — ведут они в одно место.

    Стоять этот хендлер обязан выше `on_tg_message`: тот забирает всё, что написано
    в теме, и разбор идёт по порядку записи. Окажись он ниже — «/del» просто уехало бы
    собеседнику текстом, а команда бы даже не позвалась.
    """
    target = tg_message.reply_to_message
    if target is None:
        await tg_message.reply(
            "Ответь этой командой на то сообщение, которое надо стереть.\n"
            f"Или поставь под ним {DELETE_MARK} — это то же самое, только быстрее."
        )
        return

    pair = topics.max_message_for(target.message_id)
    if pair is None:
        # Служебные записи темы, старое до моста, эхо чужих ботов — в MAX его просто нет.
        await tg_message.reply(
            "Это сообщение через мост не проходило: в MAX его нет, стирать там нечего. Здесь убери его сам."
        )
        return

    await max_ready.wait()
    chat_id, max_message_id = pair
    await _erase(chat_id, max_message_id, target.message_id)
    # Саму команду убираем следом: сообщения, к которому она относилась, уже нет,
    # а висящее в теме «/del» — мусор. Не вышло — не беда, о главном мост уже сказал.
    with suppress(TelegramBadRequest):
        await bot.delete_message(GROUP_ID, tg_message.message_id)


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


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id.is_(None))
async def on_tg_outside_topic(tg_message: TgMessage) -> None:
    """Написанное в общей части группы не уходит никуда — и молчать об этом нельзя.

    За темой стоит чат MAX, за общей частью — ничего: тут мост только отвечает на
    команды. Написанное сюда просто ложится и лежит, а человек уверен, что отправил.
    Ровно тот случай, ради которого заведено правило «мост никогда не молчит».
    """
    if tg_message.content_type in SERVICE_CONTENT:
        return
    await tg_message.reply(
        "<b>Не отправлено.</b> Отсюда в MAX ничего не уходит — здесь мост только "
        "отвечает на команды.\nПиши внутри темы того, кому адресовано. Нет темы — "
        "заведи её: <code>/write +7 999 123-45-67 привет</code>."
    )


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
    # До входа, чтобы и первый запрос ушёл с честным признаком: применяется он
    # при следующем login или ping, а не мгновенно.
    client.set_presence(online=SHOW_ONLINE)
    logger.info("в MAX буду показываться %s", "в сети" if SHOW_ONLINE else "не в сети")

    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=GROUP_ID))
    # Первая строка, по которой видно, что токен рабочий и группа на месте: без неё
    # окно молчит до первого сообщения, и непонятно, живой мост или нет.
    logger.info("Telegram на связи, слушаю группу %s", GROUP_ID)

    # Отдельными задачами и нарочно не в halves: без них мост остаётся мостом, а вот
    # ждать из-за них запуск или падать вместе с ними — незачем. Имена держим, иначе
    # задачу без ссылки соберёт мусорщик прямо на ходу.
    extras = [asyncio.create_task(_tell_about_update()), asyncio.create_task(_watch_new_chats())]

    halves = [asyncio.create_task(client.start()), asyncio.create_task(dp.start_polling(bot))]
    # Половина моста без второй бесполезна и незаметна: Telegram продолжит принимать
    # сообщения в никуда. Лучше упасть целиком — запуск поднимет нас заново.
    done, pending = await asyncio.wait(halves, return_when=asyncio.FIRST_COMPLETED)
    for task in [*extras, *pending]:
        task.cancel()
    await asyncio.gather(*extras, *pending, return_exceptions=True)
    for task in done:
        task.result()
    logger.error("одна из половин моста остановилась — выхожу, чтобы запуститься заново")


if __name__ == "__main__":
    asyncio.run(main())
