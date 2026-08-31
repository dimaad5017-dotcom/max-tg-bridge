import asyncio
import html
import logging
import re
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
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    ErrorEvent,
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

# Поднята ли связь с MAX. Telegram-поллинг стартует раньше, чем сессия MAX, и всё,
# что идёт в MAX, сперва дожидается этой отметки.
#
# Гасим её при обрыве, а не только зажигаем при старте. Иначе она означала бы «связь
# когда-то была», а спрашивают у неё «связь есть сейчас» — и мост, оставшийся без MAX,
# продолжал бы считать себя целым.
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

# Заглушить тему командой мост не может, и никакой бот не может: уведомления — настройка
# твоего Telegram, а не группы. Объясняем это в двух местах, поэтому текст один на оба.
MUTE_HOW = "нажми на неё в списке тем правой кнопкой (на телефоне — задержи палец) → «Отключить уведомления»."

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
    "<b>Если чат надоел</b>\n"
    f"Заглушить — не команда моста, а сам Telegram: {MUTE_HOW}\n"
    "Сообщения будут приходить молча: в отличие от блокировки, так ничего не теряется.\n"
    "Заблокировать человека мост не умеет — в MAX это делает только само приложение.\n\n"
    "Писать надо внутри темы: из общей части группы в MAX ничего не уходит, "
    "там мост только отвечает на команды.\n"
    "Мост живёт, пока открыто окно <code>4-запустить-мост.cmd</code>."
)

# Столько сообщений MAX отдаёт за один запрос истории — не наш выбор, а его размер страницы.
HISTORY_PAGE = 40
# Потолок догона на чат. Нужен не ради Telegram, а ради чата, которого мост не видел
# никогда: «всё непрочитанное» там может означать тысячи сообщений за все годы.
HISTORY_LIMIT = 300
# Сколько ждём догонялку. Считаем по Telegram, а не по MAX: в группу он пускает около
# двадцати сообщений в минуту, и сотня накопившихся — это пять минут одних только пауз.
CATCH_UP_TIMEOUT = 900
# Как часто перечитываем список чатов на случай, что о новом MAX не сказал.
NEW_CHAT_SCAN = 300
# Сколько ждём связь с MAX, прежде чем сказать «не отправлено».
#
# Ждать вообще нужно: Telegram поднимается за секунду, MAX — за несколько, и сообщение,
# написанное сразу после запуска, должно дождаться, а не отвалиться. Но ждать без конца
# нельзя: если MAX не поднимется вовсе, ожидание длится вечно и молча.
#
# Двадцать секунд — с запасом на обычный вход и мало для человека, который смотрит в
# экран. Ошибиться в короткую сторону не страшно: скажем «повтори», и он повторит.
MAX_WAIT = 20
# Сколько раз пробуем положить сообщение в Telegram, если сеть моргнула, и с какой
# паузы начинаем. Паузы удваиваются: 2, 4, 8, 16 — полминуты на всё. Дольше держать
# сообщение в руках незачем: за полминуты короткий обрыв проходит, а долгий не пройдёт
# и за пять, и тогда честнее сказать «не доставлено», чем молчать ещё дольше.
BLIP_TRIES = 5
FIRST_BLIP_PAUSE = 2
# Пауза между попытками достучаться до Telegram, пока сети ещё нет.
#
# Пятнадцать секунд — чтобы не молотить впустую и не проспать: обычный Wi-Fi
# поднимается за это время, а если он поднимется на минуту позже, мост потеряет
# на ожидании те же пятнадцать секунд, а не рабочий день.
NET_RETRY = 15

NO_TOPIC = "но тема не создалась — дай боту право «Управление темами», пока пишу сюда, в General."

# Первым делом, до всего остального: человек спросил «живой ли мост» и должен узнать,
# что половина его сейчас не работает, а не вычитывать это между строк справки.
NO_MAX = (
    "⚠️ <b>Связи с MAX нет.</b> Отсюда сейчас ничего не отправится, и оттуда ничего "
    "не придёт. Мост восстанавливает связь сам — если через несколько минут не "
    "заработает, проверь интернет и перезапусти его."
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
# Списка кодов MAX нигде не публикует, и в библиотеке его тоже нет: этот собран по тому,
# что реально приходило в чаты. Новый код когда-нибудь придёт — про него скажет лог.
CONTROL_EVENTS = {
    "new": "чат создан",
    "add": "добавили в чат",
    "remove": "убрали из чата",
    "leave": "вышел из чата",
    "joinByLink": "вступил по ссылке",
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

# Метки, которыми в групповом чате различают людей. Цвет — главный признак, форма —
# запасной: цветов всего семь, а в школьной группе бывает и два десятка человек.
# Чёрного и белого тут нарочно нет: один пропадает в тёмной теме, другой в светлой.
NAME_MARKS = (
    "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤",
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫",
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🤎",
)  # fmt: skip

# Из группы MAX обратно пускают только по приглашению — такое подтверждаем словом.
YES_WORDS = {"да", "yes", "точно"}

# Так выглядит команда Telegram: косая черта, имя, иногда «@имя_бота» от подсказки.
# Свои команды мост разбирает раньше отправки, поэтому под этот шаблон попадает
# только то, чего он не знает, — опечатки и придуманные на ходу команды.
COMMAND_SHAPE = re.compile(r"/([A-Za-z0-9_]{1,32})(@[A-Za-z0-9_]+)?(\s|$)")

# Эти две ищут в боте чаще всего, и обе там искать бесполезно. Ответить «такой команды
# нет» здесь мало: человек пойдёт искать её в MAX — то есть туда, откуда мост его увёл.
ASKED_OFTEN = {
    "mute": f"Заглушить чат командой нельзя, это настройка твоего Telegram, а не бота: {MUTE_HOW}",
    "block": "Заблокировать человека мост не умеет: в MAX это делает только само приложение.",
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
# Столько ждём один файл. Полста мегабайт — это меньше минуты даже на слабом канале;
# если не уложились, дело не в размере, а в том, что отдача встала совсем.
DOWNLOAD_TIMEOUT = 120

# Сколько раз подряд просим у MAX один и тот же файл и с какой паузы начинаем.
# Паузы утраиваются: 5, 15. Одна попытка была ошибкой — сервер вложений MAX умеет
# не отвечать минуту и ожить сам, а мост в этот момент хоронил фотографию навсегда.
# Но и держать очередь чата дольше минуты нельзя: за фотографией стоит текст.
FETCH_TRIES = 3
FIRST_FETCH_PAUSE = 5

# Через сколько секунд после неудачи возвращаемся за вложением — и так пять раз.
# Полчаса в сумме, и это не запас на всякий случай: сервер вложений MAX падает на
# минуты, а не на часы. Не ожил за полчаса — не оживёт и к вечеру, а ссылка на файл
# к тому времени всё равно протухнет, и ходить будет уже некуда.
LATE_WAITS = (30, 120, 300, 600, 900)

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
    # Беда временная — есть смысл вернуться за файлом позже. «Слишком большое» и
    # «нет такого файла» через полчаса не поправятся, а «сервер не ответил» поправится.
    again: bool = False


class Late(NamedTuple):
    """Вложение, которое не скачалось сразу, но может скачаться позже."""

    kind: str
    url: str
    name: str


class Composed(NamedTuple):
    """Готовое к отправке: текст, выкачанные файлы и то, за чем придётся вернуться."""

    text: str
    media: list[Media]
    late: tuple[Late, ...] = ()


class MaxOffline(Exception):
    """Связи с MAX нет, и распоряжение из Telegram выполнить нечем."""


async def _wait_max() -> None:
    """Дождаться связь с MAX или сказать вслух, что её нет.

    Мост состоит из двух половин, и Telegram-половина поднимается первой. Всё, что идёт
    в MAX, ждёт вторую — и раньше ждало без срока. Пока MAX поднимается за пару секунд,
    разницы нет. Но pymax при обрыве не сдаётся: он молча уходит в вечный цикл
    «подождать и попробовать снова». Мост при этом жив, Telegram отвечает, окно выглядит
    рабочим — а половины, которая слушает MAX, нет.

    Вот тогда ожидание без срока и становится той самой бедой, от которой мост написан.
    Пишешь в тему «заберите ребёнка» — сообщение принято, галочка стоит, ответа нет.
    Не «не отправлено», не ошибка — вообще ничего. И оно даже не полежит до лучших
    времён: Telegram считает его отданным, так что перезапуск моста его не воскресит.

    Поэтому со сроком и с ошибкой, а не с бесконечным ожиданием. Пусть лучше человек
    увидит «не отправлено, повтори» и повторит, чем будет ждать ответа, которого нет.
    """
    try:
        await asyncio.wait_for(max_ready.wait(), timeout=MAX_WAIT)
    except TimeoutError:
        raise MaxOffline from None


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


def _mark(chat_id: int, user_id: int | None) -> str:
    """Цветная метка человека — чтобы в групповом чате было видно, где чьё сообщение.

    Все сообщения приходят от одного бота, поэтому ни своей аватарки, ни своего цвета
    имени у человека из MAX быть не может: Telegram рисует отправителя по аккаунту, а
    аккаунт тут один на всех. Покрасить само имя тоже нечем — в разметке Telegram есть
    жирный, курсив и ссылка, но цвета нет. Остаётся знак перед именем, и он единственный,
    кто в этой ленте одинаковых жирных строк держит разговор на несколько голосов.

    Цвета раздаём по одному на чат и запоминаем за человеком навсегда — не считаем из
    номера. Расчёт из номера ничего не хранит и потому подкупает, но раскидывает людей
    по двум десяткам ячеек вразнобой: в чате на девятнадцать человек больше половины
    оказывались бы одного цвета с кем-то ещё, и метка перестала бы что-либо значить.
    Раздача по очереди тратит цвета по штуке — пока людей меньше, чем цветов, повторов
    нет вовсе. Держится цвет за номером в MAX, а не за именем: имя человек меняет, и
    цвет прыгал бы вместе с ним.

    Считаем по чату, а не на всех: в каждой теме важно различать своих, и один и тот же
    человек в двух чатах может быть разного цвета — рядом-то он всё равно не окажется.
    """
    if user_id is None:
        # Отправителя не назвали — тогда и метка никакая: серый кружок, ничей цвет.
        return "🔘"
    return NAME_MARKS[topics.mark_for(chat_id, user_id, len(NAME_MARKS))]


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


def _ms(value: int | None) -> int | None:
    """Миллисекунды, которых просит история MAX. None означает «от сейчас».

    Само время MAX шлёт то в секундах, то в миллисекундах, и в запрос истории секунды
    ушли бы как семидесятый год: история вернулась бы пустая, а догонялка — молча ни с чем.
    """
    if value is None or value > 10**11:
        return value
    return value * 1000


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


def _lock_for(queue: dict[int, asyncio.Lock], chat_id: int) -> asyncio.Lock:
    """Замок на один чат. Заводим по надобности: чатов десятки, а не тысячи."""
    lock = queue.get(chat_id)
    if lock is None:
        lock = queue[chat_id] = asyncio.Lock()
    return lock


# Тему одного чата заводим по одному разу. Проверить «темы нет» и создать её —
# два действия, между которыми мост уходит ждать ответа Telegram, а в это время
# тем же самым занят кто-то ещё: MAX отдаёт каждое входящее отдельной задачей, да
# и обход чатов раз в пять минут ходит той же дорогой. Оба увидят «темы нет», обе
# темы создадутся, и одна останется сиротой — сообщения в ней есть, а ответить из
# неё нельзя: мост про такую тему не знает.
_topic_queue: dict[int, asyncio.Lock] = {}


async def _ensure_topic(chat_id: int, title: str | None = None) -> int | None:
    """None означает «темы не будет» — сообщение уйдёт в General, но не пропадёт."""
    topic_id = topics.topic_for_chat(chat_id)
    if topic_id is not None:
        return topic_id

    async with _lock_for(_topic_queue, chat_id):
        # Пока стояли в очереди, тему мог завести тот, кто стоял перед нами.
        topic_id = topics.topic_for_chat(chat_id)
        if topic_id is not None:
            return topic_id

        title = title or await _topic_title(chat_id)
        try:
            topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=title[: _cut(title, TITLE_LIMIT)])
        except TelegramBadRequest as error:
            logger.error("не создать тему для чата MAX %s: %s", chat_id, error)
            return None

        topics.link(chat_id, topic.message_thread_id, title)
        logger.info("создана тема %s для чата MAX %s (%s)", topic.message_thread_id, chat_id, title)
        return topic.message_thread_id


def _tg_len(text: str) -> int:
    """Длина по счёту Telegram, а не по счёту Python.

    Telegram меряет сообщение в единицах UTF-16 и всё, что не поместилось в основную
    таблицу Unicode, считает за два знака: 🔴, 👶, 🧒. Python считает такое за один.
    Разница вылезает ровно там, где эмодзи много, — а это школьные чаты: «9Б Дети
    👶👶👶🧒🧒🧒». Померив по-своему, мост отправил бы кусок, который Telegram отвергнет,
    и длинное сообщение снова пропало бы — с той же тишиной, ради которой всё затевалось.
    """
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, limit: int) -> int:
    """Сколько знаков Python влезает в `limit` единиц Telegram.

    Режем всегда по границе знака, поэтому пара суррогатов не разъезжается пополам:
    в Python эмодзи — один неделимый символ, даже если Telegram считает его за два.
    """
    if _tg_len(text) <= limit:
        return len(text)

    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def _safe_edge(piece: str) -> int:
    """Где резать строку, чтобы не остаться с половиной тега или половиной `&amp;`.

    Половина `<b` или `&am` — это уже не разметка, и Telegram отвергает такой кусок целиком.
    Поэтому отступаем к началу незакрытого куска. Если он начался с самого края (такого в
    жизни не бывает — тег длиной в 4096 знаков), режем как есть: сломанная разметка лучше,
    чем вечный цикл, в котором мост перестанет отвечать вообще.
    """
    edges = [len(piece)]
    for opener, closer in (("<", ">"), ("&", ";")):
        start = piece.rfind(opener)
        if start > 0 and closer not in piece[start:]:
            edges.append(start)
    return min(edges)


def _chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст на куски, которые Telegram примет.

    Сообщение длиннее 4096 знаков Telegram не обрезает, а отвергает целиком. В школьных
    чатах такие бывают: что принести на выезд, расписание на четверть, объявление на две
    страницы. Раньше оно не доезжало никак — ни в тему, ни строкой «не доставлено»:
    отказ улетал в библиотеку, и о сообщении можно было узнать, только открыв MAX.

    Режем по строкам: внутри строки теги `<b>` и `<i>` уже закрыты, и разрыв их не заденет.
    Строку длиннее предела режем по живому, но отступив от края, — см. `_safe_edge`.

    Держится это на том, что теги мост ставит только в короткие строки, которые сочиняет
    сам («<b>Имя</b>», «<i>не доставлено</i>»), а длинным бывает лишь текст человека — но он
    проходит через `html.escape`, и тегов в нём уже нет. Появится длинная строка с тегами —
    половина `<b>` уедет во вторую часть, и Telegram отвергнет её как сломанную разметку.
    """
    parts: list[str] = []
    rest = text
    while _tg_len(rest) > limit:
        piece = rest[: _cut(rest, limit)]
        edge = piece.rfind("\n")
        if edge <= 0:
            edge = _safe_edge(piece)
        parts.append(rest[:edge])
        rest = rest[edge + 1 :] if rest[edge] == "\n" else rest[edge:]
    parts.append(rest)
    # Пустые куски дают подряд идущие переводы строки на стыке — слать их незачем.
    return [part for part in parts if part] or [text[: _cut(text, limit)]]


TOO_LONG = "\n<i>…дальше не влезло — целиком видно только в MAX</i>"


def _fit(text: str, limit: int) -> str:
    """Обрезает по месту — но не молча: без пометки человек решит, что так и было написано."""
    if _tg_len(text) <= limit:
        return text
    return _chunks(text, limit - _tg_len(TOO_LONG))[0] + TOO_LONG


async def _post(method: str, topic_id: int | None, **payload: Any) -> TgMessage:
    """Отправляет сообщение в тему, разложив его на части, если оно длиннее предела Telegram.

    Разложить умеет только сам `_post`, и это нарочно: обещание «ничего не теряем» должно
    жить в одном месте. Разбросай его по вызывающим — и однажды кто-то забудет, а узнается
    об этом опять из MAX. Наверх возвращаем первую часть: по ней мост потом ищет сообщение,
    чтобы правку показать на месте и ответ процитировать в нужную точку.
    """
    text = payload.get("text")
    parts = _chunks(text) if isinstance(text, str) else [""]
    if len(parts) == 1:
        return await _post_one(method, topic_id, **payload)

    logger.info("сообщение длиннее предела Telegram — разложил на %s части", len(parts))
    first = await _post_one(method, topic_id, **{**payload, "text": parts[0]})
    for part in parts[1:]:
        # Цитату вешаем только на первую часть: остальные продолжают её, а не отвечают снова.
        tail = {**payload, "text": part}
        tail.pop("reply_parameters", None)
        await _post_one(method, topic_id, **tail)
    return first


async def _through_blip(send: Callable[..., Any], *args: Any, **payload: Any) -> Any:
    """Отправка, переживающая моргнувшую сеть.

    Тут была последняя дыра из тех, что молчат. Обе половины моста умеют ждать связь
    сами, но между ними есть щель: MAX жив, сообщение уже пришло, а Telegram именно в
    эту секунду не отвечает — Wi-Fi моргнул, VPN переключился. Отказ приходит не
    «подожди» и не «нельзя», а «сети нет», и его никто не ловил.

    Дальше сообщение не пропадало совсем — отметку «доставлено» мост ставит только
    после удачной отправки, так что догонялка притащила бы его снова. Но догонялка
    бывает раз в жизни: при запуске и при переподключении MAX. То есть письмо из
    школьного чата ждало бы следующей перезагрузки. И сказать об этом было некому:
    строка «не доставлено» уходит туда же, в тот же неотвечающий Telegram.

    Повтор с растущей паузой — тридцать секунд на всё. Да, если сеть отвалилась не
    до отправки, а сразу после неё, повтор положит сообщение дважды. Это правильный
    размен: увидеть одно и то же дважды неприятно, не увидеть вовсе — то, ради чего
    мост и написан.
    """
    pause = FIRST_BLIP_PAUSE
    for _ in range(BLIP_TRIES - 1):
        try:
            return await send(*args, **payload)
        except TelegramNetworkError as error:
            logger.warning("Telegram не отозвался (%s) — повторю через %s с", error, pause)
            await asyncio.sleep(pause)
            pause *= 2
    return await send(*args, **payload)


async def _post_one(method: str, topic_id: int | None, **payload: Any) -> TgMessage:
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
    send = partial(_through_blip, getattr(bot, method))
    try:
        return await send(GROUP_ID, message_thread_id=topic_id, **payload)
    except TelegramRetryAfter as error:
        # Telegram просит сбавить ход — не отказ, а просьба подождать. Раньше она летела
        # мимо всех починок и оборачивалась строкой «не доставлено», хотя сообщение было
        # цело: достаточно было выждать. Секунда сверху — на расхождение часов.
        logger.warning("Telegram просит подождать %s с — жду и повторяю", error.retry_after)
        await asyncio.sleep(error.retry_after + 1)
        return await send(GROUP_ID, message_thread_id=topic_id, **payload)
    except TelegramBadRequest as error:
        if topic_id is None:
            raise
        if "TOPIC_CLOSED" not in str(error).upper():
            return await _past_broken_topic(send, topic_id, error, **payload)

    try:
        await bot.reopen_forum_topic(GROUP_ID, topic_id)
    except TelegramBadRequest as error:
        # Права «Управление темами» может и не быть. Тогда в общий раздел: там сообщение
        # увидят, а в закрытой теме — нет. Так же мост поступает, когда темы вовсе не вышло.
        logger.error("тема %s закрыта, открыть не дали (%s) — пишу в общий раздел", topic_id, error)
        return await send(GROUP_ID, message_thread_id=None, **payload)

    logger.info("тема %s была закрыта — открыл заново", topic_id)
    return await send(GROUP_ID, message_thread_id=topic_id, **payload)


async def _past_broken_topic(
    send: Callable[..., Any], topic_id: int, error: TelegramBadRequest, **payload: Any
) -> TgMessage:
    """Тема отказала не по-знакомому — пробуем общий раздел и по ответу понимаем, кто виноват.

    Тему в Telegram можно удалить, и связка «чат ↔ тема» об этом не узнаёт: мост
    продолжает слать в номер, которого больше нет. Отказ приходит не TOPIC_CLOSED, а
    какой-то другой, и раньше на этом всё и кончалось. Хуже, что следом пропадала и
    строка «не доставлено» — она летела в ту же несуществующую тему. Чат замолкал
    начисто и навсегда, а понять это можно было, только открыв MAX: то есть никогда.

    Разбирать текст отказа не станем. Telegram волен переписать свои формулировки, и
    тогда починка перестанет срабатывать — так же тихо, как ломалось до неё. Вместо
    чтения ставим опыт: шлём то же самое в общий раздел. Прошло — виновата тема, и
    связку надо рвать, чтобы следующее сообщение завело новую. Не прошло — виновато
    само сообщение, и отказ уходит наверх нетронутым, как и раньше.
    """
    try:
        posted = await send(GROUP_ID, message_thread_id=None, **payload)
    except TelegramBadRequest:
        raise error from None

    logger.error("тема %s не принимает (%s) — написал в General, связку рву", topic_id, error)
    chat_id = topics.chat_for_topic(topic_id)
    if chat_id is not None:
        # Рвём связку один раз: следующие части того же сообщения сюда уже не зайдут,
        # и человек не получит одно и то же объяснение подряд несколько раз.
        topics.forget_topic(chat_id)
        with suppress(TelegramBadRequest):
            await bot.send_message(
                GROUP_ID,
                "<i>тема этого чата больше не отвечает — пишу сюда. Следующее сообщение "
                "из него заведёт новую тему.</i>",
            )
    return posted


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


async def _fetch_once(url: str, name: str) -> Fetched:
    """Тянем кусками и считаем байты сами: заявленному размеру верить нельзя.

    Раньше проверка смотрела на размер, объявленный в заголовке, и на этом успокаивалась.
    Но объявлять его никто не обязан: отдают файл потоком, размера не называют — и `or 0`
    превращает «не знаю» в «ноль байт», то есть в разрешение. Дальше мост читал ответ
    целиком в память, и двухчасовое видео с утренника укладывало машину в своп.

    Поэтому режем по-настоящему: как только накопилось больше положенного, бросаем качать
    и говорим словами. Сроку тоже нужен свой. По умолчанию ожидание тянется пять минут,
    а у догонялки весь бюджет — пятнадцать: три зависших файла, и она кончилась,
    толком не начавшись.

    Сетевые беды отсюда летят наружу: повторять или сдаваться — решают выше.
    """
    too_big = "весит больше 50 МБ, столько Telegram не принимает"
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        if (response.content_length or 0) > UPLOAD_LIMIT:
            return Fetched(None, too_big)

        body = bytearray()
        async for piece in response.content.iter_chunked(64 * 1024):
            body += piece
            # Обрываем на месте, а не после. Иначе смысл проверки только в том,
            # чтобы сказать про уже съеденную память, что её съели.
            if len(body) > UPLOAD_LIMIT:
                return Fetched(None, too_big)
        return Fetched(BufferedInputFile(bytes(body), filename=name))


def _worth_retrying(error: BaseException) -> bool:
    """Отказ отказу рознь: «сервер не отвечает» пройдёт само, «нет такого файла» — нет.

    Разбираем по коду ответа: всё, что 4xx, — это сервер сказал «нет» осмысленно, и
    через полчаса он скажет ровно то же. Кроме двух: 408 и 429 значат «не сейчас».
    """
    status = getattr(error, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return status in (408, 429)
    return True


async def _download(url: str, name: str, tries: int = FETCH_TRIES) -> Fetched:
    """Попросить файл у MAX, а на «сервер молчит» — попросить ещё раз.

    Одна попытка стоила школьных фотографий. Сервер вложений MAX не отозвался минуту,
    мост честно написал «не доставлено» — и на этом закончил: строка ушла удачно, значит
    сообщение обработано, отмечено прочитанным, и догонялка за ним уже не вернётся.
    Фотографии не стало нигде, кроме MAX, куда ты как раз и не заходишь.
    """
    pause = FIRST_FETCH_PAUSE
    for _ in range(tries - 1):
        try:
            return await _fetch_once(url, name)
        except (aiohttp.ClientError, TimeoutError) as error:
            if not _worth_retrying(error):
                logger.error("не скачать вложение %s: %s", name, error)
                return Fetched(None, f"не скачалось ({type(error).__name__})")
            logger.warning("вложение %s не скачалось (%s) — повторю через %s с", name, error, pause)
            await asyncio.sleep(pause)
            pause *= 3

    try:
        return await _fetch_once(url, name)
    except (aiohttp.ClientError, TimeoutError) as error:
        logger.error("не скачать вложение %s: %s", name, error)
        return Fetched(None, f"не скачалось ({type(error).__name__})", _worth_retrying(error))


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
    """Служебная строка чата: кто пришёл, кто вышел, что переименовали.

    Незнакомый код раньше выводился как есть, и в теме появлялось голое `joinByLink` —
    со стороны это выглядит поломкой моста, а не событием чата. Прятать такое тоже нельзя:
    в чате что-то произошло. Поэтому говорим словами, а код оставляем — и в скобках, чтобы
    было видно, что он от MAX, и в логе, чтобы по нему дописать перевод.
    """
    what = CONTROL_EVENTS.get(attachment.event)
    if what is None:
        logger.info("незнакомое служебное событие MAX: %s", attachment.event)
        what = f"служебное событие MAX ({attachment.event})"

    # Кого именно добавили или убрали, pymax не разбирает — поле доезжает как «лишнее».
    who = getattr(attachment, "user_ids", None) or getattr(attachment, "userIds", None) or []
    details = ", ".join([await _sender_name(int(user_id)) for user_id in who])
    # При переименовании MAX кладёт рядом новое название. «Чат переименовали» без него —
    # половина новости: в школьных чатах имя меняют часто и не всегда безобидно.
    details = details or str(getattr(attachment, "title", "") or "")
    return f"<i>{html.escape(what)}{': ' + html.escape(details) if details else ''}</i>"


def _lost(kind: str, reason: str, later: bool = False) -> str:
    """Что не доехало и почему — иначе человек не узнает, что вообще что-то было.

    Конец строки разный не для красоты. «Смотри в MAX» — приговор, и писать его, когда
    мост через полминуты вернётся за файлом сам, значит гнать человека ставить MAX
    на ровном месте. А писать «догоню», когда догонять нечего, — обещание впустую.
    """
    label = ATTACHMENT_LABELS.get(kind, kind.lower())
    end = "Попробую догнать в ближайшие полчаса." if later else "Посмотреть можно только в MAX."
    return f"<b>Не доставлено:</b> {label} — {reason}. {end}"


async def _compose(chat_id: int, message: Message) -> Composed:
    """Текст сообщения и то, что удалось выкачать; про остальное честно пишем в тексте."""
    lines: list[str] = []
    if await _is_group(chat_id):
        name = html.escape(await _sender_name(message.sender))
        lines.append(f"{_mark(chat_id, message.sender)} <b>{name}</b>")
    if message.text:
        lines.append(html.escape(message.text))

    media: list[Media] = []
    late: list[Late] = []
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
            lines.append(_lost(kind, fetched.problem, fetched.again))
            if fetched.again:
                late.append(Late(source[0], source[1], source[2]))
            continue

        media.append(Media(source[0], fetched.file))

    # Ни текста, ни вложений — так выглядят служебные отметки MAX. Лучше показать их
    # одной строкой, чем молча проглотить и оставить человека гадать.
    if not lines and not media:
        lines.append(f"<i>служебное сообщение MAX ({html.escape(message.type)})</i>")

    return Composed("\n".join(lines), media, tuple(late))


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


# Догонялки за вложениями, которые сейчас в работе. Держим ссылки: задачу, на которую
# никто не смотрит, мусорщик вправе убрать прямо посреди ожидания.
_chases: set[asyncio.Task[None]] = set()


async def _chase(row_id: int, chat_id: int, topic_id: int, answer_to: int, item: Late) -> None:
    """Вернуться за вложением, которое сервер MAX не отдал сразу, и прислать его следом.

    Приходит оно ответом на ту самую строку «Не доставлено» — иначе фотография всплывёт
    посреди темы через полчаса, без объяснений и не пойми к чему.

    Кончились попытки — говорим и об этом. Строка «не доставлено» осталась висеть с
    обещанием догнать, и молча его не сдержать хуже, чем сразу сказать «не вышло»:
    человек будет ждать фотографию, которая уже не придёт.
    """
    for wait in LATE_WAITS:
        await asyncio.sleep(wait)
        try:
            # Одной попыткой: пауза до следующего захода и так больше любой из внутренних.
            fetched = await _download(item.url, item.name, tries=1)
            if fetched.file is None:
                if fetched.again:
                    continue
                break

            method, argument = MEDIA_SENDERS[item.kind]
            payload: dict[str, Any] = {argument: fetched.file}
            # Стикер подписи не принимает — про него скажет уже то, что он пришёл ответом.
            if item.kind != "sticker":
                payload["caption"] = "<i>догнали: сервер MAX отдал файл не сразу</i>"
            await _post(
                method,
                topic_id,
                reply_parameters=ReplyParameters(message_id=answer_to, allow_sending_without_reply=True),
                **payload,
            )
        except Exception:
            # Споткнулись на одном заходе — это не повод бросать остальные.
            logger.exception("догонялка за вложением %s из чата MAX %s споткнулась", item.name, chat_id)
            continue

        logger.info("догнали %s из чата MAX %s", item.kind, chat_id)
        topics.forget_late(row_id)
        return

    topics.forget_late(row_id)
    logger.error("вложение %s из чата MAX %s догнать не вышло", item.name, chat_id)
    with suppress(Exception):
        await _post(
            "send_message",
            topic_id,
            text="<i>догнать не удалось: сервер MAX так и не отдал файл</i>",
            reply_parameters=ReplyParameters(message_id=answer_to, allow_sending_without_reply=True),
        )


def _chase_later(chat_id: int, topic_id: int, answer_to: int, item: Late) -> None:
    """Записать вложение в долги и пустить за ним догонялку."""
    row_id = topics.remember_late(chat_id, topic_id, answer_to, item.kind, item.url, item.name)
    task = asyncio.create_task(_chase(row_id, chat_id, topic_id, answer_to, item))
    _chases.add(task)
    task.add_done_callback(_chases.discard)


def _resume_chases() -> None:
    """Подобрать долги, оставшиеся с прошлого запуска.

    Мост могли закрыть в те полчаса, пока он собирался вернуться за фотографией. Без
    этого она пропала бы совсем: сообщение уже отмечено доставленным, и обычная
    догонялка по истории к нему не вернётся.

    О подобранном говорим в лог. Молчание здесь однажды уже стоило получаса: долги
    подобрались или их не было — по логу было не отличить, а разница между этими
    двумя вещами и есть весь ответ на вопрос «где фотография».
    """
    debts = topics.all_late()
    for row_id, chat_id, topic_id, answer_to, kind, url, name in debts:
        task = asyncio.create_task(_chase(row_id, chat_id, topic_id, answer_to, Late(kind, url, name)))
        _chases.add(task)
        task.add_done_callback(_chases.discard)
    if debts:
        logger.info("с прошлого запуска не догнано вложений: %s — иду за ними", len(debts))


async def _deliver(chat_id: int, message: Message) -> None:
    if topics.tg_message_for(chat_id, message.id) is not None:
        # Это сообщение уже в теме. Живое событие и догонялка приносят одно и то же,
        # когда чат пишет ровно в те секунды, пока догонялка тянет его историю.
        return

    topic_id = await _ensure_topic(chat_id)
    made = await _compose(chat_id, message)
    caption, media = made.text, made.media
    reply = _quoted(chat_id, message)

    # Подпись вешаем на первый файл; стикер подписи не принимает, длинный текст в неё не влезет.
    inline = (
        bool(caption) and bool(media) and media[0].kind != "sticker" and _tg_len(caption) <= CAPTION_LIMIT
    )
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
        # Строка «не доставлено» уже в теме, и сообщение вот-вот станет доставленным
        # и прочитанным — обратной дороги через историю не будет. Значит, за файлом
        # надо возвращаться отдельно, и помнить об этом надо начиная с этой секунды.
        for item in made.late:
            _chase_later(chat_id, topic_id, first.message_id, item)
    topics.remember_delivered(chat_id, message.time)

    # Раз сообщение доехало в Telegram — в MAX оно прочитано: иначе собеседник
    # вечно видит непрочитанное, а счётчик непрочитанных мешает догону при рестарте.
    try:
        await client.read_message(message.id, chat_id)
    except Exception as error:
        logger.error("не отметить прочтение в MAX чата %s: %s", chat_id, error)


# Сообщения одного разговора доставляем строго по одному.
#
# MAX отдаёт каждое входящее отдельной задачей, и они бегут наперегонки. Сообщение
# с фотографией ждёт, пока она выкачается, а текст, написанный следом, улетает в
# Telegram мгновенно — и разговор в теме читается вперемешку. Это не редкая
# случайность: достаточно, чтобы в середине очереди оказался файл.
#
# Хуже перепутанного порядка то, что за ним стоит. Метка «докуда доставлено» ставится
# в конце доставки, и быстрое сообщение успевает передвинуть её через ту фотографию,
# которая ещё качается. Выключи мост в эту секунду — и фотографию не догонит уже
# никто: метка говорит, что до неё всё доставлено. Молчание, ради которого мост писался.
#
# Замок именно на чат, а не на весь мост: школьные чаты идут независимо, и медленная
# картинка в одном не должна задерживать «заберите ребёнка» в другом.
_chat_queue: dict[int, asyncio.Lock] = {}


async def _try_deliver(chat_id: int, message: Message) -> None:
    """Доставить и, если не вышло, сказать об этом словами. Промолчать нельзя.

    Про то, что мост умеет не доставить, он говорит и так: слишком большое видео, файл,
    который Telegram не отдал. Но это заранее известные беды, а бывают неизвестные —
    длинное сообщение было ровно такой, пока не починили. Любая из них раньше кончалась
    молчанием: отказ уходил в лог, а человек не узнавал даже, что сообщение было.

    Молчание тут — худший исход из возможных. В MAX ты не заходишь, значит непрочитанное
    там так и останется. Пусть лучше в теме будет строка «не доставлено» с причиной: по
    ней видно, что сообщение есть, и понятно, куда идти смотреть.
    """
    async with _lock_for(_chat_queue, chat_id):
        try:
            await _deliver(chat_id, message)
        except Exception as error:
            logger.exception("не доставить сообщение из чата MAX %s", chat_id)
            # Здесь уже нечем починить: если и эта отправка не пройдёт, остаётся только лог.
            with suppress(Exception):
                await _post(
                    "send_message",
                    topics.topic_for_chat(chat_id),
                    text=_lost("сообщение", html.escape(str(error) or type(error).__name__)),
                )


async def _missed_since(
    client: Client, chat_id: int, delivered: int | None, unread: int, my_id: int | None
) -> tuple[list[Message], bool]:
    """Пропущенные сообщения чата — страница за страницей вглубь, а не одной пачкой.

    MAX отдаёт историю кусками по сорок, и мост брал ровно один кусок. Пока он стоял
    час, этого хватало с избытком. Но неделя простоя — и в школьном чате полторы сотни
    сообщений: сорок доезжали, сто десять исчезали. Причём беззвучно, что тут хуже
    всего: метка «докуда доставлено» после догона прыгала в самый конец, и пропавшие
    сто десять не всплывали уже никогда — ни в этот запуск, ни в любой следующий.

    Поэтому листаем назад, пока не упрёмся в уже доставленное. Потолок всё равно нужен,
    но по другой причине: в чате, которого мост не видел ни разу, «всё непрочитанное»
    может означать тысячи сообщений за все годы, и вываливать их в тему разом незачем.
    Зато если потолок сработал, мост об этом скажет — вторым возвращаемым значением.

    Возвращаем (что доставить, упёрлись ли в потолок). Из потолка оставляем свежее:
    сегодняшнее «заберите ребёнка» важнее прошлогоднего.
    """
    collected: dict[str, Message] = {}
    edge: int | None = None
    more = False
    while True:
        page = await client.fetch_history(chat_id, backward=HISTORY_PAGE, from_time=_ms(edge)) or []
        if not page:
            break

        for message in page:
            if message.sender != my_id and (delivered is None or message.time > delivered):
                collected[str(message.id)] = message

        oldest = min(message.time for message in page)
        # Дошли до уже доставленного — дальше в прошлое незачем, там всё знакомое.
        reached = delivered is not None and oldest <= delivered
        if len(collected) >= HISTORY_LIMIT:
            more = not reached
            break
        if reached:
            break
        # Чат мост видит впервые — тогда глубину задаёт счётчик непрочитанных.
        if delivered is None and len(collected) >= unread:
            break
        # История кончилась или MAX отдаёт ту же страницу — иначе листали бы вечно.
        if edge is not None and oldest >= edge:
            break
        edge = oldest

    missed = sorted(collected.values(), key=lambda message: message.time)
    return missed[-HISTORY_LIMIT:], more


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
            await _sync_chat(chat)
        except Exception:
            # Один странный чат не должен утащить за собой всю догонялку.
            logger.exception("не вышло разобрать чат MAX %s", chat.id)

        unread = chat.new_messages or 0
        delivered = topics.delivered_until(chat.id)
        if not unread and delivered is None:
            continue

        missed, more = await _missed_since(client, chat.id, delivered, unread, my_id)
        if more:
            # Обрезали — значит, надо сказать. Молча потерянное сообщение и есть то самое
            # молчание, ради которого мост писался: в MAX ты не заходишь и не проверишь.
            with suppress(Exception):
                await _post(
                    "send_message",
                    await _ensure_topic(chat.id),
                    text="<i>пока моста не было, сообщений накопилось больше, чем он забирает "
                    "за раз. Всё, что старше следующего, осталось только в MAX.</i>",
                )
        for message in missed:
            # По одному: на одном спотыкающемся сообщении догонялка раньше обрывалась
            # целиком — вместе со всеми чатами, до которых ещё не дошла очередь.
            await _try_deliver(chat.id, message)
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
    # Зажигаем сразу, а не в конце. Связь уже есть — с этой секунды отправить в MAX можно,
    # и держать написанное в Telegram незачем. А в конце эта строка стоила дорого: догон
    # длится до пятнадцати минут, и все пятнадцать любое твоё сообщение молча ждало бы его.
    max_ready.set()
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

    text = (await _compose(message.chat_id, message)).text
    # Правку показываем на месте старого сообщения, поэтому разложить её на несколько,
    # как обычную длинную, нельзя: сообщение здесь одно. Тогда обрезаем — но с пометкой.
    text = _fit(f"{text}\n<i>(исправлено)</i>", MESSAGE_LIMIT)
    try:
        await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=tg_message_id)
    except TelegramBadRequest:
        # У сообщения с файлом правится не текст, а подпись — Telegram считает это разными вещами.
        with suppress(TelegramBadRequest):
            await bot.edit_message_caption(
                chat_id=GROUP_ID, message_id=tg_message_id, caption=_fit(text, CAPTION_LIMIT)
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


async def _rename_topic(chat: Chat) -> None:
    """Чат переименовали в MAX — переименовываем и тему, иначе список тем врёт.

    Школьные чаты переименовывают: «9А класс» становится «10А», к названию дописывают
    год или имя учителя. Про само переименование мост говорит строкой в теме, но имя
    темы оставалось прежним навсегда — и через год в списке висел класс, которого уже
    нет. А список тем здесь единственный способ найти нужный чат: ищешь по имени и не
    находишь либо находишь не то.
    """
    topic_id = topics.topic_for_chat(chat.id)
    title = (chat.title or "").strip()
    if topic_id is None or not title or title == topics.title_for_chat(chat.id):
        return

    try:
        await bot.edit_forum_topic(
            chat_id=GROUP_ID, message_thread_id=topic_id, name=title[: _cut(title, TITLE_LIMIT)]
        )
    except TelegramBadRequest as error:
        # Без права «Управление темами» переименовать нельзя. Не беда: про переименование
        # человек всё равно узнает — строкой в самой теме, её пишет `_control_line`.
        logger.error("не переименовать тему %s: %s", topic_id, error)
        return

    topics.link(chat.id, topic_id, title)
    logger.info("тема %s переименована: %s", topic_id, title)


async def _sync_chat(chat: Chat) -> None:
    """Всё, что мост подтягивает из чата MAX: новую группу и новое имя.

    Вместе и в одном месте — потому что мест, откуда это зовут, три: живое событие,
    запуск и дозор. Разведи их по отдельности, и однажды в одном из трёх забудут
    про переименование, а искать такое придётся по несовпадению имён.
    """
    await _greet_new_chat(chat)
    await _rename_topic(chat)


@client.on_chat_update()
async def on_max_chat_update(chat: Chat, client: Client) -> None:
    await _sync_chat(chat)


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
    # Здесь без срока и нарочно: это не распоряжение человека, а фоновый обход. Ему
    # некому жаловаться и некуда торопиться — пусть просто дождётся связи и пойдёт.
    await max_ready.wait()
    while True:
        await asyncio.sleep(NEW_CHAT_SCAN)
        try:
            for chat in await client.fetch_chats() or []:
                await _sync_chat(chat)
        except Exception:
            # Сеть моргнула или MAX ответил не так. Это не повод бросать проверку
            # навсегда — через пять минут спросим снова.
            logger.exception("не вышло перечитать список чатов MAX")


@client.on_disconnect()
async def on_max_disconnect(error: Exception, reconnect: bool, delay: float) -> None:
    """Связь с MAX оборвалась — и мост обязан перестать считать себя целым.

    pymax после обрыва не сдаётся и не падает: он молча уходит в цикл «подождать и
    попробовать снова», и цикл этот бесконечный. Снаружи мост выглядит живым — окно
    открыто, Telegram отвечает, — но половины, которая слушает MAX, у него нет.

    Пока отметку только зажигали при старте, она означала «связь когда-то была», а
    спрашивали у неё «связь есть сейчас». Гасим — и всё, что идёт в MAX, снова начинает
    честно ждать её и честно говорить, если не дождалось.
    """
    max_ready.clear()
    logger.warning("MAX разорвал связь (%s), переподключение: %s", error, reconnect)


@client.on_message()
async def on_max_message(message: Message, client: Client) -> None:
    my_id = client.me.contact.id if client.me else None
    if message.chat_id is None or message.sender == my_id:
        return

    await _try_deliver(message.chat_id, message)


@dp.message(F.chat.id == GROUP_ID, Command("help", "start"))
async def on_help_command(tg_message: TgMessage) -> None:
    """Единственная команда, которая отвечает даже без MAX, — и потому обязана не врать.

    Инструкция называет `/help` проверкой «мост живой»: ответил — работает. Но отвечает
    на него Telegram-половина, а она поднимается первой и живёт своей жизнью. Пока MAX
    лежит, `/help` бодро отвечал «всё работает» — то есть ровно та проверка, которую
    человеку велено делать, показывала зелёное на сломанном мосте.
    """
    await tg_message.answer(HELP if max_ready.is_set() else NO_MAX + "\n\n" + HELP)


@dp.message(F.chat.id == GROUP_ID, Command("status"))
async def on_status_command(tg_message: TgMessage) -> None:
    chat_id = topics.chat_for_topic(tg_message.message_thread_id or 0)
    if chat_id is None:
        await tg_message.reply("Эту команду надо звать внутри темы собеседника.")
        return

    await _wait_max()
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

    await _wait_max()
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

    await _wait_max()
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
    await _wait_max()
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
        lines.append(f"• <b>{html.escape(title[: _cut(title, TITLE_LIMIT)])}</b> — {kind}{where}")

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
    room = MESSAGE_LIMIT - _tg_len(f"<b>Чаты в MAX: {len(lines)}</b>") - _tg_len("\n<i>…и ещё 000</i>")
    for line in lines:
        if _tg_len(line) + 1 > room:
            break
        room -= _tg_len(line) + 1
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

    await _wait_max()
    if not await _is_group(chat_id):
        # Советовать «заблокируй в MAX» — значит гнать человека туда, куда он не ходит:
        # ради этого мост и написан. Говорим про то, что делается здесь и одним движением.
        await tg_message.reply(
            "Это личка, из неё не выходят — в MAX такого действия просто нет.\n\n"
            f"Надоел — заглуши тему: {MUTE_HOW}\n"
            "Сообщения будут приходить молча, и ни одно не потеряется.\n\n"
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

    await _wait_max()
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

    await _wait_max()
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

    if outgoing is None and (shape := COMMAND_SHAPE.match(text)):
        # Свои команды мост разбирает раньше, до этого места они не доходят. Значит, здесь
        # либо опечатка, либо команда, которой у моста нет, — и человек ждёт от неё действия,
        # а не того, что она уйдёт собеседнику. Молча отправить такое в чужой чат — худшее
        # из возможного: узнаешь ты об этом из его ответа «ты чего мне прислал?».
        why = ASKED_OFTEN.get(shape.group(1).lower()) or (
            "Похоже на команду, а такой у моста нет — собеседнику она ушла бы простым "
            "текстом.\nВсе команды — <code>/help</code>."
        )
        await tg_message.reply(
            f"<b>Не отправлено.</b> {why}\n\n"
            "<i>Если это правда сообщение, поставь перед косой чертой пробел.</i>"
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

    await _wait_max()
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


@dp.errors()
async def on_tg_error(event: ErrorEvent) -> None:
    """Последняя сеть под всем, что мост делает по команде из Telegram.

    Отправку в MAX мост прикрывает и сам — «MAX не принял ...». Но ловит он там только
    отказ MAX, а сорваться может что угодно ещё: сеть, неожиданный ответ, ошибка в самом
    мосте. Тогда aiogram запишет её в лог и замолчит — а человек останется уверен, что
    сообщение ушло. В эту сторону молчание опаснее всего: про недоставленное входящее
    хотя бы видно, что его нет, а тут ты просто ждёшь ответа, которого не будет, потому
    что твоего сообщения никто не получил.

    Отвечаем прямо туда, откуда пришли: в теме собеседника это видно рядом с самим
    сообщением, и сразу понятно, какое именно не ушло.
    """
    offline = isinstance(event.exception, MaxOffline)
    if offline:
        logger.error("связи с MAX нет — распоряжение из Telegram выполнить нечем")
    else:
        logger.error("сорвалось на сообщении из Telegram", exc_info=event.exception)

    message = event.update.message or event.update.edited_message
    if message is None:
        return

    # Про потерянную связь говорим отдельно, а не «мост споткнулся». Разница не в
    # вежливости: тут человеку понятно и что делать (повторить), и что виноват не он.
    text = (
        "<b>Не отправлено.</b> Мост потерял связь с MAX и сейчас восстанавливает её сам.\n"
        "Твоё сообщение никуда не ушло — повтори его через минуту.\n"
        "<i>Если так и не заработает, проверь интернет и перезапусти мост.</i>"
        if offline
        else "<b>Не отправлено.</b> Мост споткнулся: "
        f"<i>{html.escape(str(event.exception) or type(event.exception).__name__)}</i>\n"
        "Попробуй ещё раз. Если повторится — загляни в окно моста, там записано подробно."
    )
    # Если и ответить не вышло, остаётся лог: больше сказать уже нечем.
    with suppress(Exception):
        await message.reply(text)


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

    await _wait_max()
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


def _max_invoker() -> Any:
    """Тот кусок pymax, который умеет слать сырые запросы в MAX.

    Лезть во внутренности библиотеки приходится (см. `_react`), и это уже сломалось.
    Дорога сюда шла через `client.messages`, а потом библиотека заняла это имя под
    свой список сообщений. Мост стал падать на каждой реакции — и падать молча:
    `AttributeError` не подходил ни под один `except`, обработчик рушился целиком,
    в теме не появлялось ничего. Ты ставишь реакцию, видишь её под сообщением и
    уверен, что она ушла в MAX. А она никуда не уходила.

    Поэтому имя ищем, а не помним, и не найдя — говорим словами. Заново каждый раз:
    при переподключении к MAX библиотека заводит эту внутренность заново, и
    припасённая ссылка указывала бы на прошлое, уже закрытое соединение.
    """
    for where in (getattr(client, "_app", None), getattr(client, "app", None)):
        if hasattr(where, "invoke"):
            return where
    raise RuntimeError("библиотеку pymax перестроили изнутри — мосту нечем послать реакцию")


async def _react(chat_id: int, max_message_id: str, emoji: str | None) -> None:
    """Ставит или снимает реакцию в MAX.

    Мимо `client.add_reaction` нарочно. Библиотека кладёт номер сообщения строкой, а MAX
    на этом месте ждёт число: он отвечает «Expected number» и следом рвёт связь. В удалении
    и правке та же библиотека шлёт число — потому они и работают. Так что шлём сами: тот же
    опкод, тот же вид запроса, но номер числом. Починят наверху — этот кусок можно выбросить.
    """
    payload: dict[str, Any] = {"chatId": chat_id, "messageId": int(max_message_id)}
    api = _max_invoker()
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
    except Exception as error:
        # Тоже всё подряд: перечислять беды поимённо здесь особенно дорого. Это не
        # украшение, а распоряжение стереть сообщение у собеседника, и непойманная
        # беда означает, что ты уйдёшь уверенным, будто стёр, а оно осталось лежать.
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

    # Сняли значок стирания — это отмена команды, а не отмена реакции. В MAX её и не было:
    # значок наверх не уходит. Послать туда «отмени реакцию» значит попросить отменить то,
    # чего нет, и получить отказ — а с ним и строку «реакция не ушла» на ровном месте.
    was_mark = any(item.emoji == DELETE_MARK for item in event.old_reaction if item.type == "emoji")
    if emoji is None and was_mark:
        return

    try:
        await _wait_max()
    except MaxOffline:
        # Общая сеть под ошибками сюда не дотягивается: у события «поставили реакцию» нет
        # сообщения, в ответ на которое она отвечает. Поэтому отвечаем здесь сами.
        #
        # Особенно важно для «корзины»: это не украшение, а распоряжение стереть сообщение
        # у собеседника. Промолчи мост — и ты уйдёшь уверенным, что стёр, а оно на месте.
        with suppress(Exception):
            await bot.send_message(
                GROUP_ID,
                "<b>Не сделано.</b> Мост потерял связь с MAX и восстанавливает её сам.\n"
                "Сними реакцию и поставь заново через минуту."
                + ("\n<i>Сообщение у собеседника осталось.</i>" if emoji == DELETE_MARK else ""),
                reply_to_message_id=event.message_id,
            )
        return

    if emoji == DELETE_MARK:
        # Наверх не пересылаем: это не реакция собеседнику, а распоряжение мосту.
        try:
            await _erase(chat_id, max_message_id, event.message_id)
        except Exception as error:
            # Последняя сетка: сам `_erase` про свои беды говорит, но если он свалится
            # на чём-то, чего не ждал, распоряжение «сотри» пропадёт беззвучно.
            logger.exception("сорвалось на удалении сообщения %s", max_message_id)
            with suppress(Exception):
                await bot.send_message(
                    GROUP_ID,
                    f"<b>Не удалено.</b> Мост споткнулся: {html.escape(str(error))}\n"
                    "<i>Сообщение у собеседника, скорее всего, осталось — проверь в MAX.</i>",
                    reply_to_message_id=event.message_id,
                )
        return

    try:
        await _react(chat_id, max_message_id, emoji)
    except Exception as error:
        # Ловим всё подряд, и это нарочно. Раньше здесь стоял список из трёх бед, и
        # четвёртая — библиотека переставила у себя имя — прошла мимо: обработчик
        # рухнул целиком, в теме не появилось ни слова, а реакция под сообщением
        # осталась стоять. Ты видишь её и уверен, что она ушла в MAX. Список бед,
        # о которых мост умеет говорить, всегда короче списка бед.
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


async def _greet_telegram() -> None:
    """Достучаться до Telegram, а если сети ещё нет — дождаться её, а не умереть.

    Мост теперь поднимается вместе с Windows, и к этой секунде Wi-Fi обычно ещё не
    подключился: вход в систему быстрее, чем сеть. Без сети первый же запрос падает
    примерно за двенадцать секунд — то есть не «долго висит», а именно падает.

    Дальше срабатывала защита от кривых настроек: запуск поднимает упавший мост, но
    трижды подряд умерший меньше чем за полминуты он считает безнадёжным и перестаёт
    поднимать. Три попытки с паузами — это минута. Wi-Fi, поднявшийся на второй минуте,
    оставлял мост выключенным до вечера. А поскольку из автозапуска окно открывается
    свёрнутым и при выходе закрывается совсем, на экране не оставалось даже следа.

    Ждать здесь безопасно: обе половины и так умеют переподключаться сами, и всё
    ожидание видно в окне строкой «сети нет». Плохой токен или чужая группа — другое
    дело: это не пройдёт и через час, поэтому такую беду пропускаем наверх, чтобы
    мост сдался громко и со ссылкой на разбор частых ошибок.
    """
    since = time.monotonic()
    attempt = 0
    while True:
        try:
            # Заодно проверка, что токен рабочий и бот в группе: команды ставятся ей.
            await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=GROUP_ID))
            break
        except TelegramNetworkError as error:
            # Первый раз — сразу, дальше раз в минуту: журнал не должен состоять из этой строки.
            if attempt % 4 == 0:
                logger.warning("сети нет (%s) — жду и пробую снова", error)
            attempt += 1
            await asyncio.sleep(NET_RETRY)

    if attempt:
        logger.info("сеть появилась через %.0f секунд ожидания", time.monotonic() - since)


async def main() -> None:
    # До входа, чтобы и первый запрос ушёл с честным признаком: применяется он
    # при следующем login или ping, а не мгновенно.
    client.set_presence(online=SHOW_ONLINE)
    logger.info("в MAX буду показываться %s", "в сети" if SHOW_ONLINE else "не в сети")

    await _greet_telegram()
    # Первая строка, по которой видно, что токен рабочий и группа на месте: без неё
    # окно молчит до первого сообщения, и непонятно, живой мост или нет.
    logger.info("Telegram на связи, слушаю группу %s", GROUP_ID)

    # Отдельными задачами и нарочно не в halves: без них мост остаётся мостом, а вот
    # ждать из-за них запуск или падать вместе с ними — незачем. Имена держим, иначе
    # задачу без ссылки соберёт мусорщик прямо на ходу.
    extras = [asyncio.create_task(_tell_about_update()), asyncio.create_task(_watch_new_chats())]

    # За вложениями, которые не скачались перед прошлым выключением, тоже надо вернуться.
    _resume_chases()

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
