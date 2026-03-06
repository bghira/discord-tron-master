"""Vector memory search for Zork campaigns.

Stores sentence embeddings of narrator turns in a local SQLite database and
performs cosine-similarity search so the LLM can recall events that have
scrolled out of the recent-turns context window.
"""

import logging
import os
import re
import sqlite3
import struct
import threading
import hashlib
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedding helpers – lazy-loaded on first use
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_EMBED_DIM = 384
_MAX_INPUT_CHARS = 512
_SOURCE_SNIPPET_MAX_CHARS = 1200


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

CREATE TABLE IF NOT EXISTS source_material_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL,
    document_key    TEXT    NOT NULL,
    document_label  TEXT    NOT NULL,
    chunk_index     INTEGER NOT NULL,
    chunk_text      TEXT    NOT NULL,
    embedding       BLOB    NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sm_campaign ON source_material_chunks(campaign_id);
CREATE INDEX IF NOT EXISTS idx_sm_campaign_doc ON source_material_chunks(campaign_id, document_key);
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

    @classmethod
    def semantic_similarity(cls, text_a: str, text_b: str) -> Optional[float]:
        """Return cosine similarity between two texts (None on embed failure)."""
        try:
            vec_a = _bytes_to_vector(_embed(str(text_a or "")))
            vec_b = _bytes_to_vector(_embed(str(text_b or "")))
            if vec_a.size == 0 or vec_b.size == 0:
                return None
            score = float(vec_a @ vec_b)
            return max(-1.0, min(1.0, score))
        except Exception:
            logger.debug("Zork memory: semantic similarity unavailable", exc_info=True)
            return None

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
    # Source material memories (chunked campaign canon)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_source_document_key(value: str) -> str:
        key = str(value or "").strip().lower()
        key = "".join(ch if ch.isalnum() else "-" for ch in key)
        key = "-".join(part for part in key.split("-") if part)
        return key[:80] or "source-material"

    @classmethod
    def _split_source_line_fragments(cls, text: str) -> List[str]:
        clean = str(text or "").strip()
        if not clean:
            return []
        fragments = [line.strip() for line in clean.splitlines() if line.strip()]
        if not fragments:
            fragments = [clean]
        out: List[str] = []
        for fragment in fragments:
            if len(fragment) <= _SOURCE_SNIPPET_MAX_CHARS:
                out.append(fragment)
                continue
            words = fragment.split()
            current: List[str] = []
            current_len = 0
            for word in words:
                wlen = len(word) + (1 if current else 0)
                if current and current_len + wlen > _SOURCE_SNIPPET_MAX_CHARS:
                    out.append(" ".join(current).strip())
                    current = [word]
                    current_len = len(word)
                else:
                    current.append(word)
                    current_len += wlen
            if current:
                out.append(" ".join(current).strip())
        return [s for s in out if s]

    @classmethod
    def _normalize_source_unit_mode(cls, mode: str) -> str:
        """Normalize source-material chunking mode names."""
        mode_clean = str(mode or "line").strip().lower()
        if mode_clean in ("story", "paragraph", "paragraphs", "scene"):
            return "story"
        if mode_clean in ("rulebook", "line", "lines"):
            return "rulebook"
        if mode_clean in ("generic", "chunk", "chunked", "dump"):
            return "generic"
        return "line"

    @classmethod
    def _dedupe_source_units(cls, units: List[str]) -> List[str]:
        deduped: List[str] = []
        seen = set()
        for unit in units:
            key = " ".join(str(unit or "").lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(str(unit).strip()[:8000])
        return deduped

    @staticmethod
    def _is_rulebook_fact_line(line: str) -> bool:
        stripped = str(line or "").strip()
        if ":" not in stripped:
            return False
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            return False
        if len(key) > 140:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _/\-()&'.]*", key))

    @classmethod
    def source_material_units_from_chunks(cls, chunks: List[str]) -> List[str]:
        return cls.source_material_units_from_chunks_with_mode(chunks, mode="line")

    @classmethod
    def source_material_units_from_chunks_with_mode(
        cls, chunks: List[str], *, mode: str = "line"
    ) -> List[str]:
        """Convert source-material chunks into source lookup units.

        Supported modes:
        - line (default): one unit per non-empty line.
        - story: split each chunk into paragraph units.
        - generic: preserve chunk boundaries.
        """
        source_mode = cls._normalize_source_unit_mode(mode)
        units: List[str] = []
        for chunk in chunks or []:
            raw = str(chunk or "").strip()
            if not raw.strip():
                continue
            if source_mode == "rulebook":
                current_fact: str | None = None
                for line in raw.splitlines():
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    if cls._is_rulebook_fact_line(line_clean):
                        if current_fact:
                            units.append(current_fact)
                        current_fact = line_clean
                        continue
                    if current_fact:
                        current_fact = f"{current_fact} {line_clean}"
                if current_fact:
                    units.append(current_fact)
                continue
            if source_mode == "story":
                paragraphs = [line.strip() for line in raw.split("\n\n") if line.strip()]
                if not paragraphs:
                    paragraphs = [raw]
                for paragraph in paragraphs:
                    paragraph_unit = " ".join(paragraph.split())
                    if paragraph_unit:
                        units.extend(cls._split_source_line_fragments(paragraph_unit))
                continue

            # generic mode: keep chunk ordering as a fallback dump-like store,
            # while preserving chunk boundaries and truncating to max unit size.
            compact = " ".join(raw.split())
            if compact:
                units.extend(cls._split_source_line_fragments(compact))

        return cls._dedupe_source_units(units)

    @classmethod
    def source_material_count(cls, campaign_id: int) -> int:
        try:
            conn = cls._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM source_material_chunks WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            return int((row[0] if row else 0) or 0)
        except Exception:
            logger.exception(
                "Zork memory: source_material_count failed for campaign %s",
                campaign_id,
            )
            return 0

    @classmethod
    def list_source_material_documents(
        cls,
        campaign_id: int,
        limit: int = 20,
    ) -> List[Dict[str, object]]:
        try:
            conn = cls._get_conn()
            rows = conn.execute(
                """
                SELECT
                    document_key,
                    document_label,
                    COUNT(*) AS n,
                    MAX(created_at) AS last_at
                FROM source_material_chunks
                WHERE campaign_id = ?
                GROUP BY document_key, document_label
                ORDER BY last_at DESC, n DESC
                LIMIT ?
                """,
                (campaign_id, max(1, int(limit))),
            ).fetchall()
            out: List[Dict[str, object]] = []
            for document_key, document_label, count, last_at in rows:
                sample_chunk = ""
                if document_key:
                    sample_rows = conn.execute(
                        """
                        SELECT chunk_text
                        FROM source_material_chunks
                        WHERE campaign_id = ? AND document_key = ?
                        ORDER BY chunk_index ASC
                        LIMIT 6
                        """,
                        (campaign_id, document_key),
                    ).fetchall()
                    sample_parts = [
                        str(sample_row[0] or "").strip()
                        for sample_row in sample_rows
                        if str(sample_row[0] or "").strip()
                    ]
                    if sample_parts:
                        sample_chunk = "\n".join(sample_parts)
                out.append(
                    {
                        "document_key": str(document_key or ""),
                        "document_label": str(document_label or ""),
                        "chunk_count": int(count or 0),
                        "last_at": str(last_at or ""),
                        "sample_chunk": sample_chunk,
                    }
                )
            return out
        except Exception:
            logger.exception(
                "Zork memory: list_source_material_documents failed for campaign %s",
                campaign_id,
            )
            return []

    @classmethod
    def get_source_material_document_units(
        cls,
        campaign_id: int,
        document_key: str,
    ) -> List[str]:
        try:
            key = str(document_key or "").strip()
            if not key:
                return []
            conn = cls._get_conn()
            rows = conn.execute(
                """
                SELECT chunk_text
                FROM source_material_chunks
                WHERE campaign_id = ? AND document_key = ?
                ORDER BY chunk_index ASC
                """,
                (campaign_id, key),
            ).fetchall()
            return [
                str(row[0] or "").strip()
                for row in rows
                if str(row[0] or "").strip()
            ]
        except Exception:
            logger.exception(
                "Zork memory: get_source_material_document_units failed for campaign %s key %s",
                campaign_id,
                document_key,
            )
            return []

    @classmethod
    def _source_units_signature(cls, units: List[str]) -> str:
        normalized = "\n".join(
            " ".join(str(unit or "").strip().lower().split())
            for unit in units
            if str(unit or "").strip()
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def find_duplicate_source_material_document(
        cls,
        campaign_id: int,
        *,
        chunks: List[str],
        source_mode: str = "line",
    ) -> Optional[Dict[str, object]]:
        try:
            candidate_units = cls.source_material_units_from_chunks_with_mode(
                chunks,
                mode=source_mode,
            )
            if not candidate_units:
                return None
            candidate_sig = cls._source_units_signature(candidate_units)
            for row in cls.list_source_material_documents(campaign_id, limit=200):
                document_key = str(row.get("document_key") or "").strip()
                if not document_key:
                    continue
                existing_units = cls.get_source_material_document_units(
                    campaign_id,
                    document_key,
                )
                if not existing_units:
                    continue
                if cls._source_units_signature(existing_units) != candidate_sig:
                    continue
                return {
                    "document_key": document_key,
                    "document_label": str(row.get("document_label") or ""),
                    "chunk_count": int(row.get("chunk_count") or 0),
                }
            return None
        except Exception:
            logger.exception(
                "Zork memory: find_duplicate_source_material_document failed for campaign %s",
                campaign_id,
            )
            return None

    @classmethod
    def delete_source_material_document(
        cls,
        campaign_id: int,
        document_key: str,
    ) -> int:
        try:
            key = str(document_key or "").strip()
            if not key:
                return 0
            conn = cls._get_conn()
            cur = conn.execute(
                """
                DELETE FROM source_material_chunks
                WHERE campaign_id = ? AND document_key = ?
                """,
                (campaign_id, key),
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            logger.exception(
                "Zork memory: delete_source_material_document failed for campaign %s key %s",
                campaign_id,
                document_key,
            )
            return 0

    @classmethod
    def clear_source_material_documents(cls, campaign_id: int) -> int:
        try:
            conn = cls._get_conn()
            cur = conn.execute(
                """
                DELETE FROM source_material_chunks
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            logger.exception(
                "Zork memory: clear_source_material_documents failed for campaign %s",
                campaign_id,
            )
            return 0

    @classmethod
    def store_source_material_chunks(
        cls,
        campaign_id: int,
        *,
        document_label: str,
        chunks: List[str],
        source_mode: str = "line",
        replace_document: bool = True,
    ) -> Tuple[int, str]:
        """Store source-material chunks for a campaign and return (stored_count, document_key)."""
        try:
            label = " ".join(str(document_label or "").strip().split())[:120]
            if not label:
                label = "source-material"
            document_key = cls._normalize_source_document_key(label)
            mode = cls._normalize_source_unit_mode(source_mode)
            clean_chunks = [
                str(chunk or "").strip()
                for chunk in (chunks or [])
                if str(chunk or "").strip()
            ]
            if not clean_chunks:
                return 0, document_key
            sentence_units = cls.source_material_units_from_chunks_with_mode(
                clean_chunks, mode=mode
            )
            if not sentence_units:
                return 0, document_key

            conn = cls._get_conn()
            if replace_document:
                conn.execute(
                    """
                    DELETE FROM source_material_chunks
                    WHERE campaign_id = ? AND document_key = ?
                    """,
                    (campaign_id, document_key),
                )
            for idx, chunk_text in enumerate(sentence_units, start=1):
                conn.execute(
                    """
                    INSERT INTO source_material_chunks
                    (campaign_id, document_key, document_label, chunk_index, chunk_text, embedding)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        document_key,
                        label,
                        idx,
                        chunk_text,
                        _embed(chunk_text),
                    ),
                )
            conn.commit()
            return len(sentence_units), document_key
        except Exception:
            logger.exception(
                "Zork memory: store_source_material_chunks failed for campaign %s",
                campaign_id,
            )
            return 0, "source-material"

    @classmethod
    def search_source_material(
        cls,
        query: str,
        campaign_id: int,
        *,
        document_key: Optional[str] = None,
        top_k: int = 5,
        before_lines: int = 0,
        after_lines: int = 0,
    ) -> List[Tuple[str, str, int, str, float]]:
        """Vector-search source material chunks.

        Returns (document_key, document_label, chunk_index, chunk_text, score).
        """
        import numpy as np

        try:
            query_vec = _bytes_to_vector(_embed(query))
            conn = cls._get_conn()
            before_n = max(0, min(50, int(before_lines)))
            after_n = max(0, min(50, int(after_lines)))
            key = str(document_key or "").strip()
            if key:
                rows = conn.execute(
                    """
                    SELECT document_key, document_label, chunk_index, chunk_text, embedding
                    FROM source_material_chunks
                    WHERE campaign_id = ? AND document_key = ?
                    """,
                    (campaign_id, key),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT document_key, document_label, chunk_index, chunk_text, embedding
                    FROM source_material_chunks
                    WHERE campaign_id = ?
                    """,
                    (campaign_id,),
                ).fetchall()
            if not rows:
                return []

            by_doc: dict[str, dict[int, str]] = {}
            scored: List[Tuple[str, str, int, str, float]] = []
            for row_key, row_label, row_chunk_idx, row_chunk_text, row_blob in rows:
                doc_key = str(row_key or "")
                chunk_idx = int(row_chunk_idx or 0)
                chunk_text = str(row_chunk_text or "")
                if chunk_idx > 0 and chunk_text:
                    by_doc.setdefault(doc_key, {})[chunk_idx] = chunk_text
                vec = _bytes_to_vector(row_blob)
                score = float(np.dot(query_vec, vec))
                scored.append(
                    (
                        doc_key,
                        str(row_label or ""),
                        chunk_idx,
                        chunk_text,
                        score,
                    )
                )
            scored.sort(key=lambda t: t[4], reverse=True)
            selected = scored[: max(1, int(top_k))]
            expanded: List[Tuple[str, str, int, str, float]] = []
            mark_center = bool(before_n or after_n)
            for doc_key, doc_label, center_idx, center_text, score in selected:
                doc_chunks = by_doc.get(doc_key, {})
                if center_idx <= 0:
                    expanded.append((doc_key, doc_label, center_idx, center_text, score))
                    continue
                start_idx = max(1, center_idx - before_n)
                end_idx = center_idx + after_n
                window_parts: list[str] = []
                for idx in range(start_idx, end_idx + 1):
                    part = str(doc_chunks.get(idx) or "").strip()
                    if not part:
                        continue
                    if idx == center_idx and mark_center:
                        window_parts.append(f">> {part}")
                    else:
                        window_parts.append(part)
                if not window_parts:
                    window_parts = [center_text]
                expanded.append(
                    (
                        doc_key,
                        doc_label,
                        center_idx,
                        "\n".join(window_parts),
                        score,
                    )
                )
            return expanded
        except Exception:
            logger.exception(
                "Zork memory: search_source_material failed for campaign %s",
                campaign_id,
            )
            return []

    @classmethod
    def browse_source_keys(
        cls,
        campaign_id: int,
        *,
        document_key: Optional[str] = None,
        wildcard: str = "%",
        limit: int = 255,
    ) -> List[str]:
        """Return a compact source index or matching raw source lines.

        When *wildcard* is omitted / broad (``*`` or ``%``), return a compact
        key listing so the model can see the document taxonomy without burning
        context on full fact bodies. When *wildcard* is specific, return the
        raw matching source lines.
        """
        try:
            conn = cls._get_conn()
            pattern = str(wildcard or "%").strip()
            if not pattern or pattern == "*":
                pattern = "%"
            else:
                pattern = pattern.replace("*", "%")
            broad_browse = pattern in {"%", "%%"}
            key = str(document_key or "").strip()
            if key:
                rows = conn.execute(
                    """
                    SELECT document_key, chunk_text
                    FROM source_material_chunks
                    WHERE campaign_id = ? AND document_key = ?
                      AND chunk_text LIKE ? ESCAPE '\\'
                    ORDER BY chunk_index ASC
                    LIMIT ?
                    """,
                    (campaign_id, key, pattern, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT document_key, chunk_text
                    FROM source_material_chunks
                    WHERE campaign_id = ?
                      AND chunk_text LIKE ? ESCAPE '\\'
                    ORDER BY document_key ASC, chunk_index ASC
                    LIMIT ?
                    """,
                    (campaign_id, pattern, max(1, int(limit))),
                ).fetchall()
            cleaned_rows = []
            for row_doc_key, row_chunk_text in rows:
                chunk_text = str(row_chunk_text or "").strip()
                if not chunk_text:
                    continue
                cleaned_rows.append((str(row_doc_key or "").strip(), chunk_text))
            if not broad_browse:
                return [chunk_text for _, chunk_text in cleaned_rows]

            compact: List[str] = []
            seen = set()
            for row_doc_key, chunk_text in cleaned_rows:
                key_text = chunk_text
                if ":" in chunk_text:
                    key_text = chunk_text.split(":", 1)[0].strip() or chunk_text
                if key:
                    line = key_text
                else:
                    line = f"{row_doc_key}: {key_text}" if row_doc_key else key_text
                normalized = " ".join(line.lower().split())
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                compact.append(line)
            return compact
        except Exception:
            logger.exception(
                "Zork memory: browse_source_keys failed for campaign %s",
                campaign_id,
            )
            return []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @classmethod
    def delete_campaign_embeddings(cls, campaign_id: int) -> int:
        """Remove all campaign memories (turn embeddings + manual + source material)."""
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
            cursor_source = conn.execute(
                "DELETE FROM source_material_chunks WHERE campaign_id = ?",
                (campaign_id,),
            )
            conn.commit()
            deleted = (
                int(cursor_turns.rowcount or 0)
                + int(cursor_manual.rowcount or 0)
                + int(cursor_source.rowcount or 0)
            )
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
