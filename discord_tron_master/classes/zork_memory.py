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
from typing import List, Optional, Tuple

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
            logger.exception("Zork memory: failed to store embedding for turn %s", turn_id)

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
    # Delete
    # ------------------------------------------------------------------

    @classmethod
    def delete_campaign_embeddings(cls, campaign_id: int) -> int:
        """Remove all embeddings for *campaign_id*. Returns rows deleted."""
        try:
            conn = cls._get_conn()
            cursor = conn.execute(
                "DELETE FROM turn_embeddings WHERE campaign_id = ?",
                (campaign_id,),
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.info(
                "Zork memory: deleted %d embeddings for campaign %s",
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
