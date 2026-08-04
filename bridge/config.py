import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "cache"
SESSION_NAME = "max.db"
MAP_DB = WORK_DIR / "topics.db"

load_dotenv(ROOT / ".env")


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"В .env не заполнено {name}. Смотри .env.example")
    return value
