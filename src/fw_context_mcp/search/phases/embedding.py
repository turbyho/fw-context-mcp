"""Phase 4: Semantic search via cosine similarity using sqlite-vec.

Uses the ``vec0`` virtual table for KNN search when available (index created
with ``fw-context index --embeddings``).  Falls back to brute-force BLOB
search for backward compatibility with older indexes.

When Phase 3 (FTS5) has already produced results, the phase operates as a
**re-rank**: FTS5 candidates are scored by vector distance instead of running
an independent KNN query.  This avoids duplicate results and the need for
expensive merging in the subsequent deduplication phase.
"""

from __future__ import annotations

import logging

from fw_context_mcp.search.phases.base import Phase

log = logging.getLogger(__name__)


class EmbeddingPhase(Phase):
    """Vector search / re-rank via sqlite-vec or brute-force fallback.

    Embeddings are generated during ``fw-context index`` and stored in both
    the ``embeddings`` table (BLOB, for backward compat) and the ``vec_symbols``
    vec0 virtual table (sqlite-vec, for KNN queries).
    """

    name = "embedding"

    def should_run(self, ctx) -> bool:
        return ctx.config.llm.enabled and ctx.ollama_warning is None

    async def run(self, ctx):
        import json

        from fw_context_mcp.indexer.db import (
            get_embeddings,
            open_db,
            search_similar_hybrid,
            search_similar_vec,
        )
        from fw_context_mcp.llm.ollama import OllamaError, call_ollama_embed

        conn = open_db(ctx.db_path)
        try:
            with conn:
                # Check which embedding storage is available
                has_vec0 = _table_exists(conn, "vec_symbols")
                has_blob = _table_has_rows(conn, "embeddings")

                if not has_vec0 and not has_blob:
                    return ctx  # No embeddings at all

                # Generate query embedding
                try:
                    query_embs = call_ollama_embed([ctx.query], ctx.config.llm)
                    query_vec = query_embs[0]
                except OllamaError:
                    return ctx  # Embedding model unavailable

                # ---- Hybrid re-rank path ----
                # When FTS5 already produced candidates, re-rank them by
                # vector distance.  This is both faster and produces better
                # merged results than running an independent KNN query.
                if ctx.fts5_results and has_vec0:
                    queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
                    fts5_query = " OR ".join(queries) if queries else ctx.query
                    rows = search_similar_hybrid(
                        conn,
                        query_vec,
                        ctx.config_hash,
                        fts5_query=fts5_query,
                        threshold=0.5,
                        limit=30,
                    )
                    return ctx.evolve(embedding_results=rows)

                # ---- Direct KNN path ----
                if has_vec0:
                    rows = search_similar_vec(
                        conn,
                        query_vec,
                        ctx.config_hash,
                        threshold=0.5,
                        limit=30,
                    )
                    if rows:
                        sym_ids = [r["symbol_id"] for r in rows]
                        placeholders = ",".join("?" * len(sym_ids))
                        emb_rows = conn.execute(
                            f"""SELECT * FROM symbols
                                WHERE config_hash = ? AND id IN ({placeholders})
                                AND is_definition = 1""",
                            (ctx.config_hash, *sym_ids),
                        ).fetchall()
                        return ctx.evolve(embedding_results=[dict(r) for r in emb_rows])
                    return ctx

                # ---- Brute-force fallback (legacy BLOB table) ----
                if has_blob:
                    stored = get_embeddings(conn, ctx.config_hash, ctx.config.llm.embed_model)
                    if not stored:
                        return ctx
                    scored = _brute_force_search(query_vec, stored, threshold=0.5)
                    top_ids = [s[0] for s in scored[:30]]
                    if not top_ids:
                        return ctx
                    placeholders = ",".join("?" * len(top_ids))
                    emb_rows = conn.execute(
                        f"""SELECT * FROM symbols
                            WHERE config_hash = ? AND id IN ({placeholders})
                            AND is_definition = 1""",
                        (ctx.config_hash, *top_ids),
                    ).fetchall()
                    return ctx.evolve(embedding_results=[dict(r) for r in emb_rows])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_exists(conn, name: str) -> bool:
    """Check whether a table or virtual table exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _table_has_rows(conn, name: str) -> bool:
    """Check whether a table exists and contains at least one row."""
    if not _table_exists(conn, name):
        return False
    row = conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
    return row is not None


def _brute_force_search(
    query_vec: list[float],
    stored: dict[int, list[float]],
    threshold: float = 0.5,
) -> list[tuple[int, float]]:
    """Legacy brute-force cosine similarity (fallback for old BLOB indexes).

    Returns list of ``(symbol_id, similarity)`` sorted by similarity descending.
    """
    import math

    scored: list[tuple[int, float]] = []
    for sym_id, emb_vec in stored.items():
        dot = sum(x * y for x, y in zip(query_vec, emb_vec))
        norm_a = math.sqrt(sum(x * x for x in query_vec))
        norm_b = math.sqrt(sum(x * x for x in emb_vec))
        if norm_a == 0 or norm_b == 0:
            sim = 0.0
        else:
            sim = dot / (norm_a * norm_b)
        if sim > threshold:
            scored.append((sym_id, sim))
    scored.sort(key=lambda x: -x[1])
    return scored
