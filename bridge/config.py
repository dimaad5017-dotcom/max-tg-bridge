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


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"В .env не заполнено {name}. Смотри .env.example")
    return value
