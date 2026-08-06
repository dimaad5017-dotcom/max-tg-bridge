import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
WORK_DIR = ROOT / "cache"
SESSION_NAME = "max.db"
MAP_DB = WORK_DIR / "topics.db"

load_dotenv(ENV_FILE)


def normalize_phone(raw: str) -> str:
    """MAX принимает только +7XXXXXXXXXX, а люди пишут 8XXX, +7 (XXX) XXX и прочее."""
    digits = "".join(char for char in raw if char.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}"


def optional(name: str, default: str) -> str:
    """Настройка, которую можно не заполнять: пустая строка в .env значит «как обычно»."""
    return (os.getenv(name) or "").strip() or default


def flag(name: str) -> bool:
    """Настройка «да или нет». Пусто — значит нет; писать можно по-русски и по-английски."""
    return (os.getenv(name) or "").strip().lower() in {"да", "yes", "on", "true", "1"}


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"В .env не заполнено {name}. Смотри .env.example")
    return value


# Telegram берёт реакции только из своего списка, галочки в нём нет — «посмотрели» ближе всего.
# Этим значком мост отмечает прочитанное собеседником, поэтому занять его под другое нельзя.
SEEN_MARK = "👀"


def delete_mark() -> str:
    """Значок «стереть везде» — единственная настройка, которую можно испортить наверняка.

    Совпади он с отметкой о прочтении — мост стирал бы сообщения ровно в тот миг,
    когда собеседник их прочитал, и понять это со стороны было бы невозможно.
    Поэтому проверяем сразу и не запускаемся вовсе: не работать хуже, чем работать,
    но лучше, чем молча уничтожать переписку.

    Проверка живёт здесь, рядом с `require`, а не в модуле моста: настройки в .env
    пишет человек в блокноте, и разбираться с его опечатками — дело этого файла.
    """
    mark = optional("TG_DELETE_MARK", "💩")
    if mark == SEEN_MARK:
        raise SystemExit(
            f"В .env TG_DELETE_MARK={mark} — этим значком мост отмечает прочитанное. "
            "Возьми любой другой, иначе прочитанное будет стираться само."
        )
    return mark
