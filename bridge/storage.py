import sqlite3
from pathlib import Path


class TopicMap:
    """Связка «чат в MAX ↔ тема в супергруппе Telegram»."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS topics ("
            "  max_chat_id INTEGER PRIMARY KEY,"
            "  topic_id    INTEGER NOT NULL UNIQUE,"
            "  title       TEXT NOT NULL"
            ")"
        )
        self._db.commit()

    def topic_for_chat(self, max_chat_id: int) -> int | None:
        row = self._db.execute(
            "SELECT topic_id FROM topics WHERE max_chat_id = ?", (max_chat_id,)
        ).fetchone()
        return row[0] if row else None

    def chat_for_topic(self, topic_id: int) -> int | None:
        row = self._db.execute(
            "SELECT max_chat_id FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return row[0] if row else None

    def link(self, max_chat_id: int, topic_id: int, title: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO topics (max_chat_id, topic_id, title) VALUES (?, ?, ?)",
            (max_chat_id, topic_id, title),
        )
        self._db.commit()
