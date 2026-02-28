"""Vector memory search for Zork campaigns.

Stores sentence embeddings of narrator turns in a local SQLite database and
performs cosine-similarity search so the LLM can recall events that have
scrolled out of the recent-turns context window.
"""

import logging
import os
import sqlite3
import struct
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedding helpers – lazy-loaded on first use
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_EMBED_DIM = 384
_MAX_INPUT_CHARS = 512


def _get_model():
    """Return the sentence-transformer model, loading it on first call."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        return _model


def _embed(text: str) -> bytes:
    """Return *text*'s embedding as a compact bytes blob (384 × float32)."""
    import numpy as np

    model = _get_model()
    truncated = text[:_MAX_INPUT_CHARS]
    vector = model.encode(truncated, normalize_embeddings=True)
    return np.asarray(vector, dtype=np.float32).tobytes()


def _bytes_to_vector(blob: bytes):
    """Unpack a BLOB back into a numpy float32 array."""
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
_DB_PATH = os.path.join(_DB_DIR, "zork_embeddings.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turn_embeddings (
    turn_id      INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL,
    user_id      INTEGER,
    kind         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    embedding    BLOB    NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_te_campaign ON turn_embeddings(campaign_id);

CREATE TABLE IF NOT EXISTS manual_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    category    TEXT    NOT NULL,
    term        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mm_campaign ON manual_memories(campaign_id);
CREATE INDEX IF NOT EXISTS idx_mm_campaign_category ON manual_memories(campaign_id, category);
CREATE INDEX IF NOT EXISTS idx_mm_campaign_term ON manual_memories(campaign_id, term);
"""


class ZorkMemory:
    """Per-campaign vector memory backed by a local SQLite database."""

    _conn_local = threading.local()

    @classmethod
    def _get_conn(cls) -> sqlite3.Connection:
        conn = getattr(cls._conn_local, "conn", None)
        if conn is not None:
            return conn
        os.makedirs(_DB_DIR, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_SQL)
        cls._conn_local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    @classmethod
    def store_turn_embedding(
        cls,
        turn_id: int,
        campaign_id: int,
        user_id: Optional[int],
        kind: str,
        content: str,
    ) -> None:
        """Compute embedding for *content* and INSERT OR IGNORE into SQLite."""
        try:
            blob = _embed(content)
            conn = cls._get_conn()
            conn.execute(
                "INSERT OR IGNORE INTO turn_embeddings "
                "(turn_id, campaign_id, user_id, kind, content, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (turn_id, campaign_id, user_id, kind, content, blob),
            )
            conn.commit()
        except Exception:
            logger.exception(
                "Zork memory: failed to store embedding for turn %s", turn_id
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @classmethod
    def search(
        cls,
        query: str,
        campaign_id: int,
        top_k: int = 5,
    ) -> List[Tuple[int, str, str, float]]:
        """Return the *top_k* most similar turns for *campaign_id*.

        Returns a list of ``(turn_id, kind, content, score)`` tuples sorted
        by descending cosine similarity.
        """
        import numpy as np

        try:
            query_vec = _bytes_to_vector(_embed(query))
            conn = cls._get_conn()
            rows = conn.execute(
                "SELECT turn_id, kind, content, embedding "
                "FROM turn_embeddings WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchall()
            if not rows:
                return []

            scored: List[Tuple[int, str, str, float]] = []
            for turn_id, kind, content, blob in rows:
                vec = _bytes_to_vector(blob)
                # Both vectors are already L2-normalised → dot == cosine sim.
                score = float(np.dot(query_vec, vec))
                scored.append((turn_id, kind, content, score))

            scored.sort(key=lambda t: t[3], reverse=True)
            return scored[:top_k]
        except Exception:
            logger.exception("Zork memory: search failed for campaign %s", campaign_id)
            return []

    # ------------------------------------------------------------------
    # Manual memories (category keyed)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_key(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def list_manual_memory_terms(
        cls,
        campaign_id: int,
        wildcard: str = "%",
        limit: int = 20,
    ) -> List[Dict[str, object]]:
        """List stored manual-memory terms/categories using SQLite wildcard matching."""
        try:
            conn = cls._get_conn()
            pattern = str(wildcard or "%").strip()
            if not pattern:
                pattern = "%"
            pattern = pattern.replace("*", "%")
            if "%" not in pattern and "_" not in pattern:
                pattern = f"%{pattern}%"
            rows = conn.execute(
                """
                SELECT term, category, COUNT(*) AS n, MAX(created_at) AS last_at
                FROM manual_memories
                WHERE campaign_id = ?
                  AND (term LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')
                GROUP BY term, category
                ORDER BY n DESC, last_at DESC
                LIMIT ?
                """,
                (campaign_id, pattern, pattern, max(1, int(limit))),
            ).fetchall()
            out: List[Dict[str, object]] = []
            for term, category, n, last_at in rows:
                out.append(
                    {
                        "term": str(term or ""),
                        "category": str(category or ""),
                        "count": int(n or 0),
                        "last_at": str(last_at or ""),
                    }
                )
            return out
        except Exception:
            logger.exception(
                "Zork memory: list_manual_memory_terms failed for campaign %s",
                campaign_id,
            )
            return []

    @classmethod
    def store_manual_memory(
        cls,
        campaign_id: int,
        *,
        category: str,
        content: str,
        term: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Store a curated manual memory under a category/term with dedupe checks."""
        try:
            category_clean = cls._normalize_key(category)
            content_clean = " ".join(str(content or "").strip().split())
            term_clean = cls._normalize_key(term or category_clean)

            if not category_clean:
                return False, "missing_category"
            if not content_clean:
                return False, "missing_content"
            if len(content_clean) > 1200:
                content_clean = content_clean[:1200].rstrip()

            conn = cls._get_conn()
            existing_rows = conn.execute(
                """
                SELECT content
                FROM manual_memories
                WHERE campaign_id = ? AND category = ?
                ORDER BY id DESC
                LIMIT 200
                """,
                (campaign_id, category_clean),
            ).fetchall()
            content_l = content_clean.lower()
            for (existing_content,) in existing_rows:
                existing_l = str(existing_content or "").strip().lower()
                if not existing_l:
                    continue
                if existing_l == content_l:
                    return False, "duplicate_exact"
                if content_l in existing_l or existing_l in content_l:
                    return False, "duplicate_overlap"

            blob = _embed(content_clean)
            conn.execute(
                """
                INSERT INTO manual_memories
                (campaign_id, category, term, content, embedding)
                VALUES (?, ?, ?, ?, ?)
                """,
                (campaign_id, category_clean, term_clean, content_clean, blob),
            )
            conn.commit()
            return True, "stored"
        except Exception:
            logger.exception(
                "Zork memory: store_manual_memory failed for campaign %s",
                campaign_id,
            )
            return False, "error"

    @classmethod
    def search_manual_memories(
        cls,
        query: str,
        campaign_id: int,
        *,
        category: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Tuple[str, str, float]]:
        """Vector-search curated manual memories, optionally scoped by category."""
        import numpy as np

        try:
            query_vec = _bytes_to_vector(_embed(query))
            conn = cls._get_conn()
            if category and str(category).strip():
                category_key = cls._normalize_key(str(category))
                rows = conn.execute(
                    """
                    SELECT category, content, embedding
                    FROM manual_memories
                    WHERE campaign_id = ? AND category = ?
                    """,
                    (campaign_id, category_key),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT category, content, embedding
                    FROM manual_memories
                    WHERE campaign_id = ?
                    """,
                    (campaign_id,),
                ).fetchall()

            scored: List[Tuple[str, str, float]] = []
            for mem_category, content, blob in rows:
                vec = _bytes_to_vector(blob)
                score = float(np.dot(query_vec, vec))
                scored.append((str(mem_category or ""), str(content or ""), score))
            scored.sort(key=lambda t: t[2], reverse=True)
            return scored[: max(1, int(top_k))]
        except Exception:
            logger.exception(
                "Zork memory: search_manual_memories failed for campaign %s",
                campaign_id,
            )
            return []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @classmethod
    def delete_campaign_embeddings(cls, campaign_id: int) -> int:
        """Remove all campaign memories (turn embeddings + manual memories)."""
        try:
            conn = cls._get_conn()
            cursor_turns = conn.execute(
                "DELETE FROM turn_embeddings WHERE campaign_id = ?",
                (campaign_id,),
            )
            cursor_manual = conn.execute(
                "DELETE FROM manual_memories WHERE campaign_id = ?",
                (campaign_id,),
            )
            conn.commit()
            deleted = int(cursor_turns.rowcount or 0) + int(cursor_manual.rowcount or 0)
            logger.info(
                "Zork memory: deleted %d total memories for campaign %s",
                deleted,
                campaign_id,
            )
            return deleted
        except Exception:
            logger.exception(
                "Zork memory: failed to delete embeddings for campaign %s",
                campaign_id,
            )
            return 0

    @classmethod
    def delete_turns_after(cls, campaign_id: int, turn_id: int) -> int:
        """Remove embeddings for *campaign_id* where turn_id > *turn_id*.

        Returns rows deleted.
        """
        try:
            conn = cls._get_conn()
            cursor = conn.execute(
                "DELETE FROM turn_embeddings WHERE campaign_id = ? AND turn_id > ?",
                (campaign_id, turn_id),
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.info(
                "Zork memory: deleted %d embeddings after turn %s for campaign %s",
                deleted,
                turn_id,
                campaign_id,
            )
            return deleted
        except Exception:
            logger.exception(
                "Zork memory: failed to delete embeddings after turn %s for campaign %s",
                turn_id,
                campaign_id,
            )
            return 0

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    @classmethod
    def backfill_campaign(cls, campaign_id: int) -> int:
        """Embed all narrator turns for *campaign_id* not yet in SQLite.

        Returns the number of newly embedded turns.
        """
        from discord_tron_master.classes.app_config import AppConfig
        from discord_tron_master.models.zork import ZorkTurn

        app = AppConfig.get_flask()
        if app is None:
            return 0

        with app.app_context():
            turns = (
                ZorkTurn.query.filter_by(campaign_id=campaign_id, kind="narrator")
                .order_by(ZorkTurn.id.asc())
                .all()
            )

        conn = cls._get_conn()
        existing = set()
        for row in conn.execute(
            "SELECT turn_id FROM turn_embeddings WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall():
            existing.add(row[0])

        count = 0
        for turn in turns:
            if turn.id in existing:
                continue
            cls.store_turn_embedding(
                turn.id, campaign_id, turn.user_id, turn.kind, turn.content
            )
            count += 1
        return count
