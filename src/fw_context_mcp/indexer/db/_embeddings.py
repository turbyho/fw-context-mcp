"""Embedding helpers — pack/unpack float vectors as BLOBs for the embeddings
table.  Vector dimension is detected at runtime from the Ollama response;
no hardcoded value.  The ``build_configs.embedding_dim`` column stores
the detected dimension per build for vec0 table compatibility.
"""

from __future__ import annotations

__all__ = [
    "_blob_to_vec",
    "_cosine_sim",
    "_vec_to_blob",
    "clean_orphan_embeddings",
    "get_embeddings",
    "init_vec_table",
    "search_similar_hybrid",
    "search_similar_vec",
    "upsert_embeddings",
    "upsert_embeddings_vec",
]

import logging
import sqlite3
import struct

from fw_context_mcp.indexer.db._schema import _table_exists
from fw_context_mcp.indexer.db._symbols import search_symbols

log = logging.getLogger(__name__)


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a BLOB for storage.

    Uses ``struct.pack("f" * N, *vec)`` where each float32 is 4 bytes.
    The embedding dimension is determined at runtime from the model response.

    Args:
        vec: Float vector (list of float32 values).

    Returns:
        bytes: BLOB of packed float32 values.
    """
    return struct.pack("f" * len(vec), *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a BLOB back into a float vector.

    Inverse of ``_vec_to_blob``.  Each 4-byte chunk is unpacked as a
    float32 via ``struct.unpack("f" * (len(blob) // 4), blob)``.

    Args:
        blob: BLOB of packed float32 values (must be a multiple of 4 bytes).

    Returns:
        list[float]: The reconstructed float vector.
    """
    return list(struct.unpack("f" * (len(blob) // 4), blob))


def _cosine_sim(query_vec: list[float], blob: bytes) -> float | None:
    """Compute cosine similarity between a query vector and a stored BLOB.

    Returns None when the BLOB cannot be unpacked (dimension mismatch,
    corruption, etc.).  Returns 0.0 when either vector has zero norm.
    """
    import math as _math

    try:
        vec_b = struct.unpack(f"{len(query_vec)}f", blob)
    except (struct.error, TypeError):
        log.warning(
            "Embedding dimension mismatch: query=%d floats, stored=%d bytes. "
            "Run `fw-context index --embeddings` to regenerate with current model.",
            len(query_vec), len(blob),
        )
        return None
    dot = sum(x * y for x, y in zip(query_vec, vec_b, strict=True))
    norm_a = _math.sqrt(sum(x * x for x in query_vec))
    norm_b = _math.sqrt(sum(x * x for x in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def upsert_embeddings(
    conn: sqlite3.Connection,
    rows: list[tuple[int, int, bytes, str, str]],
) -> int:
    """Insert or replace embedding rows.

    Each row is ``(symbol_id, chunk_index, embedding_blob, model, content_hash)``.
    ``content_hash`` is a content-addressable hash of the fields that feed the
    embedding description — used by the incremental re-embedding pass to skip
    symbols whose content did not change.
    Returns number of rows inserted.
    """
    # NOTE: Orphan cleanup moved to clean_orphan_embeddings() — call it once
    # after indexing completes, not on every batch insert.
    cur = conn.executemany(
        """INSERT OR REPLACE INTO embeddings(symbol_id, chunk_index, embedding, model, content_hash, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        rows,
    )
    return cur.rowcount


def clean_orphan_embeddings(conn: sqlite3.Connection) -> int:
    """Delete embeddings whose symbol no longer exists.

    Safe to call when symbols table is empty — the NOT IN subquery
    returns no rows and the DELETE is a no-op.

    Call ONCE after indexing completes — not inside the per-batch insert
    hot path.  Returns number of rows deleted.
    """
    cur = conn.execute(
        """DELETE FROM embeddings WHERE symbol_id NOT IN (
            SELECT id FROM symbols
        )"""
    )
    return cur.rowcount


def clean_orphan_embeddings_vec(conn: sqlite3.Connection) -> int:
    """Delete vec_symbols rows whose symbol no longer exists.

    The vec0 virtual table lacks ON DELETE CASCADE — orphan rows survive
    after reindex_file or partial reindex that re-creates symbols with
    new IDs.  Call ONCE after indexing completes.

    Returns number of rows deleted.
    """
    if not _table_exists(conn, "vec_symbols"):
        return 0
    cur = conn.execute(
        """DELETE FROM vec_symbols WHERE symbol_id NOT IN (
            SELECT id FROM symbols
        )"""
    )
    return cur.rowcount


def get_embeddings(
    conn: sqlite3.Connection,
    config_hash: str,
    model: str,
) -> dict[int, list[float]]:
    """Return {symbol_id: embedding_vector} for a build config and model.

    For multi-chunk symbols, returns only chunk_index=0 (the primary embedding).

    .. deprecated::
        Prefer ``search_similar_vec`` with the sqlite-vec vec0 table for
        production use.  This BLOB-based path is kept for compatibility
        with databases created before vec0 support was added.
    """
    rows = conn.execute(
        """SELECT e.symbol_id, e.embedding
           FROM embeddings e
           JOIN symbols s ON s.id = e.symbol_id
           WHERE s.config_hash = ? AND e.model = ? AND e.chunk_index = 0""",
        (config_hash, model),
    ).fetchall()
    return {r["symbol_id"]: _blob_to_vec(r["embedding"]) for r in rows}


# ---------------------------------------------------------------------------
# Vector search via sqlite-vec (vec0 virtual table)
# ---------------------------------------------------------------------------

_VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_symbols USING vec0(
    embedding float[{dim}] distance_metric=cosine,
    config_hash TEXT,
    symbol_id INTEGER,
    chunk_index INTEGER
);
"""


def init_vec_table(conn: sqlite3.Connection, dim: int | None = None, *, recreate: bool = False) -> None:
    """Create the vec0 virtual table if it does not exist.

    Must be called after ``sqlite_vec.load(conn)``.
    The table stores embeddings keyed by ``symbol_id`` with a per-build
    ``config_hash`` metadata column for filtered KNN queries.

    When *recreate* is ``True`` (and *dim* is a positive int), the table
    is dropped and recreated to match the requested dimension.  Use this
    when the embedding model changed and the dimension no longer matches.

    When *recreate* is ``False`` (default), the table is only created if
    it does not already exist — safe for repeated calls during ``open_db``.
    The *dim* parameter is ignored in this mode.
    """
    if dim is None:
        try:
            row = conn.execute(
                "SELECT embedding_dim FROM build_configs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row and row["embedding_dim"]:
                dim = row["embedding_dim"]
        except sqlite3.OperationalError:
            pass

    if dim is None:
        dim = 1024

    if recreate:
        if not isinstance(dim, int) or dim < 1:
            raise ValueError(f"Invalid embedding dimension: {dim!r} — must be a positive integer")
        conn.execute("DROP TABLE IF EXISTS vec_symbols")
        conn.commit()
    # NOTE: .format() is used because SQLite CREATE VIRTUAL TABLE does not
    # support parameterized dimension.  *dim* is validated as positive int
    # above — no SQL injection risk.
    conn.execute(_VEC_SCHEMA.format(dim=dim))
    conn.commit()


def upsert_embeddings_vec(
    conn: sqlite3.Connection,
    rows: list[tuple[int, int, str, list[float]]],
) -> int:
    """Insert or replace embeddings into the vec0 vector table.

    Each row is ``(symbol_id, chunk_index, config_hash, embedding_vector)``.
    Uses ``symbol_id * 1_000_000 + chunk_index`` as rowid for uniqueness.
    Returns number of rows inserted.

    Raises ``ValueError`` when *chunk_index* exceeds 999_999 — the rowid
    encoding would collide across symbols at that threshold.
    """
    import json

    # Safety check — prevent silent rowid collisions at extreme chunk_index
    _MAX_CHUNK = 999_999
    for _, chunk_index, _, _ in rows:
        if chunk_index > _MAX_CHUNK:
            raise ValueError(
                f"chunk_index {chunk_index} exceeds max safe value {_MAX_CHUNK} "
                f"— rowid encoding would collide"
            )

    # Bulk DELETE + INSERT wrapped in a transaction — a crash between
    # DELETE and INSERT would otherwise cause data loss.
    with conn:
        conn.executemany(
            "DELETE FROM vec_symbols WHERE rowid = ?",
            [(symbol_id * 1_000_000 + chunk_index,) for symbol_id, chunk_index, _, _ in rows],
        )
        conn.executemany(
            "INSERT INTO vec_symbols(rowid, embedding, config_hash, symbol_id, chunk_index) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (symbol_id * 1_000_000 + chunk_index, json.dumps(vec), config_hash, symbol_id, chunk_index)
                for symbol_id, chunk_index, config_hash, vec in rows
            ],
        )
    return len(rows)


def search_similar_vec(
    conn: sqlite3.Connection,
    query_vec: list[float],
    config_hash: str,
    threshold: float = 0.6,
    limit: int = 30,
) -> list[sqlite3.Row]:
    """KNN search over the vec0 table filtered by *config_hash*.

    Returns rows with ``symbol_id`` and ``distance`` (cosine distance,
    range [0, 2], where 0 = identical).  Results are post-filtered by
    *threshold* and deduplicated by ``symbol_id`` (best distance kept
    for multi-chunk symbols).
    """
    import json

    query_json = json.dumps(query_vec)
    # Overfetch by 5× to compensate for multi-chunk dedup (worst case: all
    # candidates have max chunks, but symbols with many chunks don't usually
    # collect *limit* unique symbols.  Uses iterative widening (max 3 passes).

    seen: set[int] = set()
    results: list[sqlite3.Row] = []
    k_mult = 5
    for _ in range(3):  # max 3 iterations — progressively wider fetch
        rows = conn.execute(
            """SELECT symbol_id, distance
               FROM vec_symbols
               WHERE embedding MATCH ?
                 AND config_hash = ?
                 AND k = ?
               ORDER BY distance""",
            (query_json, config_hash, limit * k_mult),
        ).fetchall()
        for r in rows:
            if r["distance"] > (1.0 - threshold):
                continue
            sid = r["symbol_id"]
            if sid in seen:
                continue
            seen.add(sid)
            results.append(r)
            if len(results) >= limit:
                return results
        if len(rows) < limit * k_mult:
            break  # exhausted all candidates
        k_mult *= 2  # double overfetch for next iteration
    return results


def search_similar_hybrid(
    conn: sqlite3.Connection,
    query_vec: list[float],
    config_hash: str,
    fts5_query: str,
    threshold: float = 0.6,
    limit: int = 20,
) -> list[dict]:
    """Hybrid search: combine FTS5 text relevance with vector similarity.

    1. Fetch candidate symbols via FTS5 (broad recall – up to 200 candidates).
    2. Re-rank candidates by cosine distance against *query_vec*.
    3. Return top *limit* results sorted by distance.

    This avoids a full-scan of vec_symbols while still leveraging semantic
    similarity for ranking.
    """

    # Phase 1 — text recall (includes declarations + definitions).
    # The direct KNN path filters is_definition=1 but the hybrid path
    # intentionally doesn't — adaptive fusion prefers definitions anyway,
    # and including declarations in the recall set catches cases
    # where a definition was renamed but the old declaration persists.
    text_candidates = search_symbols(conn, fts5_query, config_hash, limit=200)
    if not text_candidates:
        return []

    candidate_ids = [r["id"] for r in text_candidates]
    placeholders = ",".join("?" * len(candidate_ids))

    # Phase 2 — vector re-rank
    rows = conn.execute(
        f"""SELECT vs.symbol_id, distance
            FROM vec_symbols vs
            WHERE vs.symbol_id IN ({placeholders})
              AND vs.config_hash = ?""",
        (*candidate_ids, config_hash),
    ).fetchall()

    # Build distance map, filter by threshold — keep best (lowest) distance
    # per symbol_id for multi-chunk symbols.
    dist_map: dict[int, float] = {}
    for r in rows:
        d = r["distance"]
        if d <= (1.0 - threshold):
            sid = r["symbol_id"]
            if sid not in dist_map or d < dist_map[sid]:
                dist_map[sid] = d

    # Re-rank candidates by vector distance
    scored = [(dist_map[r["id"]], r) for r in text_candidates if r["id"] in dist_map]
    scored.sort(key=lambda x: x[0])

    top = scored[:limit]
    # Embed distance into result dict
    return [dict(r, _similarity=round(float(1.0 - d), 4)) for d, r in top]
