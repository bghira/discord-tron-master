"""Long-term semantic memory for ordinary Discord LLM conversations."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from typing import Iterable

logger = logging.getLogger(__name__)

EMBED_SOURCE_SNOWFLAKE = "snowflake"
_EMBED_DIM = 384
_MAX_INPUT_CHARS = 512
_SNOWFLAKE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_MODEL = None
_MODEL_LOCK = threading.Lock()
_EMBED_FALLBACK_WARNED = False
_DB_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "data",
        "discord_memory.db",
    )
)
_DB_PATH_OVERRIDE: str | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS discord_memories (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id      TEXT NOT NULL,
    user_message_id      TEXT NOT NULL UNIQUE,
    assistant_message_id TEXT,
    guild_id              TEXT,
    channel_id            TEXT,
    author_id             TEXT,
    author_name           TEXT,
    content               TEXT NOT NULL,
    embedding             BLOB NOT NULL,
    embed_source          TEXT NOT NULL DEFAULT 'snowflake',
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dm_conversation
    ON discord_memories(conversation_id);
CREATE INDEX IF NOT EXISTS idx_dm_conversation_created
    ON discord_memories(conversation_id, created_at);
"""


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer

            _MODEL = SentenceTransformer("Snowflake/snowflake-arctic-embed-s")
    return _MODEL


def _embed(text: str, *, query: bool = False) -> bytes:
    """Return a normalized Snowflake embedding, using its query prefix only for searches."""
    import numpy as np

    raw = str(text or "")
    if query:
        max_text = _MAX_INPUT_CHARS - len(_SNOWFLAKE_QUERY_PREFIX)
        raw = _SNOWFLAKE_QUERY_PREFIX + raw[:max_text]
    else:
        raw = raw[:_MAX_INPUT_CHARS]
    try:
        vector = _get_model().encode(raw, normalize_embeddings=True)
    except Exception:
        global _EMBED_FALLBACK_WARNED
        with _MODEL_LOCK:
            if not _EMBED_FALLBACK_WARNED:
                logger.warning(
                    "Discord memory embeddings unavailable; keyword recall remains active.",
                    exc_info=True,
                )
                _EMBED_FALLBACK_WARNED = True
        return np.zeros(_EMBED_DIM, dtype=np.float32).tobytes()
    return np.asarray(vector, dtype=np.float32).tobytes()


def _text_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class DiscordMemory:
    """SQLite-backed semantic recall scoped to one user or group conversation."""

    _conn_local = threading.local()

    @classmethod
    def configure(cls, *, db_path: str | None = None) -> None:
        """Override the database path, primarily for tests and local deployments."""
        global _DB_PATH_OVERRIDE
        conn = getattr(cls._conn_local, "conn", None)
        if conn is not None:
            conn.close()
            cls._conn_local.conn = None
        _DB_PATH_OVERRIDE = db_path

    @classmethod
    def _get_conn(cls) -> sqlite3.Connection:
        conn = getattr(cls._conn_local, "conn", None)
        if conn is not None:
            return conn
        db_path = _DB_PATH_OVERRIDE or _DB_PATH
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_SQL)
        cls._conn_local.conn = conn
        return conn

    @classmethod
    def store_exchange(
        cls,
        *,
        conversation_id: object,
        user_message_id: object,
        assistant_message_id: object = None,
        guild_id: object = None,
        channel_id: object = None,
        author_id: object = None,
        author_name: str = "",
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Embed and persist one user/assistant exchange."""
        scope = _text_id(conversation_id)
        source_message_id = _text_id(user_message_id)
        if not scope or not source_message_id:
            raise ValueError("conversation_id and user_message_id are required")
        content = (
            f"User {str(author_name or '').strip() or _text_id(author_id) or 'unknown'}: "
            f"{str(user_text or '').strip()}\n"
            f"Assistant: {str(assistant_text or '').strip()}"
        ).strip()
        if not content:
            return
        embedding = _embed(content)
        conn = cls._get_conn()
        conn.execute(
            """
            INSERT INTO discord_memories (
                conversation_id, user_message_id, assistant_message_id, guild_id,
                channel_id, author_id, author_name, content, embedding, embed_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_message_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                assistant_message_id = excluded.assistant_message_id,
                guild_id = excluded.guild_id,
                channel_id = excluded.channel_id,
                author_id = excluded.author_id,
                author_name = excluded.author_name,
                content = excluded.content,
                embedding = excluded.embedding,
                embed_source = excluded.embed_source
            """,
            (
                scope,
                source_message_id,
                _text_id(assistant_message_id),
                _text_id(guild_id),
                _text_id(channel_id),
                _text_id(author_id),
                str(author_name or "").strip(),
                content,
                embedding,
                EMBED_SOURCE_SNOWFLAKE,
            ),
        )
        conn.commit()

    @staticmethod
    def _keyword_score(query: str, content: str) -> float:
        query_lower = str(query or "").lower().strip()
        content_lower = str(content or "").lower()
        if not query_lower:
            return 0.0
        if query_lower in content_lower:
            return 1.0
        tokens = set(re.findall(r"[a-z0-9_'-]{2,}", query_lower))
        if not tokens:
            return 0.0
        return sum(token in content_lower for token in tokens) / len(tokens)

    @classmethod
    def search(
        cls,
        *,
        conversation_id: object,
        queries: Iterable[str],
        top_k: int = 5,
    ) -> list[dict[str, object]]:
        """Return hybrid semantic/keyword matches for several query phrasings."""
        import numpy as np

        scope = _text_id(conversation_id)
        cleaned_queries = [
            str(query).strip()[:240]
            for query in queries
            if isinstance(query, str) and query.strip()
        ][:4]
        if not scope or not cleaned_queries:
            return []
        rows = (
            cls._get_conn()
            .execute(
                """
            SELECT id, user_message_id, assistant_message_id, author_id,
                   author_name, content, embedding, created_at
            FROM discord_memories
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT 5000
            """,
                (scope,),
            )
            .fetchall()
        )
        if not rows:
            return []

        query_vectors = [
            np.frombuffer(_embed(query, query=True), dtype=np.float32)
            for query in cleaned_queries
        ]
        scored: list[dict[str, object]] = []
        for row in rows:
            (
                memory_id,
                user_message_id,
                assistant_message_id,
                author_id,
                author_name,
                content,
                blob,
                created_at,
            ) = row
            vector = np.frombuffer(blob, dtype=np.float32)
            best_score = -1.0
            best_semantic = 0.0
            best_keyword = 0.0
            for query, query_vector in zip(cleaned_queries, query_vectors):
                semantic = (
                    float(np.dot(query_vector, vector))
                    if query_vector.size == vector.size and vector.size
                    else 0.0
                )
                keyword = cls._keyword_score(query, content)
                score = (max(semantic, 0.0) * 0.8) + (keyword * 0.2)
                if score > best_score:
                    best_score = score
                    best_semantic = semantic
                    best_keyword = keyword
            scored.append(
                {
                    "memory_id": int(memory_id),
                    "user_message_id": str(user_message_id or ""),
                    "assistant_message_id": str(assistant_message_id or ""),
                    "author_id": str(author_id or ""),
                    "author_name": str(author_name or ""),
                    "content": str(content or "")[:4000],
                    "created_at": str(created_at or ""),
                    "score": round(float(best_score), 4),
                    "semantic_score": round(float(best_semantic), 4),
                    "keyword_score": round(float(best_keyword), 4),
                }
            )
        scored.sort(
            key=lambda item: (float(item["score"]), int(item["memory_id"])),
            reverse=True,
        )
        return scored[: max(1, min(int(top_k), 10))]

    @classmethod
    def delete_conversation(cls, conversation_id: object) -> int:
        scope = _text_id(conversation_id)
        if not scope:
            return 0
        conn = cls._get_conn()
        cursor = conn.execute(
            "DELETE FROM discord_memories WHERE conversation_id = ?",
            (scope,),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
