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
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS delivered ("
            "  max_chat_id  INTEGER PRIMARY KEY,"
            "  message_time INTEGER NOT NULL"
            ")"
        )
        # MAX нумерует сообщения по-своему, Telegram — по-своему; без этой связки
        # нельзя ни поставить реакцию на нужное сообщение, ни ответить на него.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "  max_chat_id    INTEGER NOT NULL,"
            "  max_message_id TEXT NOT NULL,"
            "  tg_message_id  INTEGER NOT NULL,"
            "  PRIMARY KEY (max_chat_id, max_message_id)"
            ")"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS messages_by_tg ON messages (tg_message_id)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outgoing ("
            "  max_chat_id   INTEGER PRIMARY KEY,"
            "  tg_message_id INTEGER NOT NULL"
            ")"
        )
        # Про новую версию говорим один раз, а не при каждом запуске: мост,
        # повторяющий одно и то же, человек перестаёт читать.
        self._db.execute("CREATE TABLE IF NOT EXISTS announced (version TEXT PRIMARY KEY)")
        # Реакция у бота одна на сообщение. Помним, где стоит настоящая реакция
        # собеседника, чтобы отметка о прочтении её не затёрла.
        self._db.execute("CREATE TABLE IF NOT EXISTS reacted (tg_message_id INTEGER PRIMARY KEY)")
        self._db.commit()

    def topic_for_chat(self, max_chat_id: int) -> int | None:
        row = self._db.execute("SELECT topic_id FROM topics WHERE max_chat_id = ?", (max_chat_id,)).fetchone()
        return row[0] if row else None

    def chat_for_topic(self, topic_id: int) -> int | None:
        row = self._db.execute("SELECT max_chat_id FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        return row[0] if row else None

    def title_for_chat(self, max_chat_id: int) -> str | None:
        """Имя, под которым чат уже заведён темой: спрашивать его у MAX заново незачем."""
        row = self._db.execute("SELECT title FROM topics WHERE max_chat_id = ?", (max_chat_id,)).fetchone()
        return row[0] if row else None

    def link(self, max_chat_id: int, topic_id: int, title: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO topics (max_chat_id, topic_id, title) VALUES (?, ?, ?)",
            (max_chat_id, topic_id, title),
        )
        self._db.commit()

    def pair_messages(self, max_chat_id: int, max_message_id: int | str, tg_message_id: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO messages (max_chat_id, max_message_id, tg_message_id) VALUES (?, ?, ?)",
            (max_chat_id, str(max_message_id), tg_message_id),
        )
        self._db.commit()

    def tg_message_for(self, max_chat_id: int, max_message_id: int | str) -> int | None:
        row = self._db.execute(
            "SELECT tg_message_id FROM messages WHERE max_chat_id = ? AND max_message_id = ?",
            (max_chat_id, str(max_message_id)),
        ).fetchone()
        return row[0] if row else None

    def max_message_for(self, tg_message_id: int) -> tuple[int, str] | None:
        row = self._db.execute(
            "SELECT max_chat_id, max_message_id FROM messages WHERE tg_message_id = ?",
            (tg_message_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def remember_outgoing(self, max_chat_id: int, tg_message_id: int) -> None:
        """Последнее отправленное нами — на него вешаем отметку, когда собеседник прочитал."""
        self._db.execute(
            "INSERT OR REPLACE INTO outgoing (max_chat_id, tg_message_id) VALUES (?, ?)",
            (max_chat_id, tg_message_id),
        )
        self._db.commit()

    def last_outgoing(self, max_chat_id: int) -> int | None:
        row = self._db.execute(
            "SELECT tg_message_id FROM outgoing WHERE max_chat_id = ?", (max_chat_id,)
        ).fetchone()
        return row[0] if row else None

    def already_announced(self, version: str) -> bool:
        row = self._db.execute("SELECT 1 FROM announced WHERE version = ?", (version,)).fetchone()
        return row is not None

    def remember_announced(self, version: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO announced (version) VALUES (?)", (version,))
        self._db.commit()

    def has_reaction(self, tg_message_id: int) -> bool:
        """Стоит ли на сообщении настоящая реакция собеседника — её затирать нельзя."""
        row = self._db.execute("SELECT 1 FROM reacted WHERE tg_message_id = ?", (tg_message_id,)).fetchone()
        return row is not None

    def remember_reaction(self, tg_message_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO reacted (tg_message_id) VALUES (?)", (tg_message_id,))
        self._db.commit()

    def forget_reaction(self, tg_message_id: int) -> None:
        """Реакцию сняли — ячейка снова свободна под отметку о прочтении."""
        self._db.execute("DELETE FROM reacted WHERE tg_message_id = ?", (tg_message_id,))
        self._db.commit()

    def delivered_until(self, max_chat_id: int) -> int | None:
        """Время последнего сообщения, доехавшего до Telegram: с него догоняем после простоя."""
        row = self._db.execute(
            "SELECT message_time FROM delivered WHERE max_chat_id = ?", (max_chat_id,)
        ).fetchone()
        return row[0] if row else None

    def remember_delivered(self, max_chat_id: int, message_time: int) -> None:
        self._db.execute(
            "INSERT INTO delivered (max_chat_id, message_time) VALUES (?, ?)"
            " ON CONFLICT(max_chat_id) DO UPDATE SET message_time = max(message_time, excluded.message_time)",
            (max_chat_id, message_time),
        )
        self._db.commit()
