"""Phase 5: Semantic search via cosine similarity using sqlite-vec.

Uses the ``vec0`` virtual table for KNN search when available (index created
with ``fw-context index --embeddings``).  Falls back to brute-force BLOB
search for backward compatibility with older indexes.

When Phase 4 (FTS5) has already produced results, the phase operates as a
**re-rank**: FTS5 candidates are scored by vector distance instead of running
an independent KNN query.  This avoids duplicate results and the need for
expensive merging in the subsequent deduplication phase.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from fw_context_mcp.indexer.db import get_embeddings, search_similar_hybrid, search_similar_vec
from fw_context_mcp.llm.embedder_factory import get_embedder
from fw_context_mcp.llm.ollama import OllamaError
from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

from fw_context_mcp.search.phases.embedding_helpers import (
    brute_force_search,
    table_exists,
    table_has_rows,
)

log = logging.getLogger(__name__)

_table_exists = table_exists  # backward-compat alias
_table_has_rows = table_has_rows
_brute_force_search = brute_force_search


class EmbeddingPhase(Phase):
    """Vector search / re-rank via sqlite-vec or brute-force fallback.

    Embeddings are generated during ``fw-context index`` and stored in both
    the ``embeddings`` table (BLOB, for backward compat) and the ``vec_symbols``
    vec0 virtual table (sqlite-vec, for KNN queries).

    When ``independent=True`` the phase always runs a standalone KNN query via
    ``search_similar_vec()``, ignoring any ``fts5_results`` already in the
    context.  This is used by ``SMART_SEARCH`` for separate FTS5 + Vec
    retrieval prior to adaptive fusion.
    """

    name = "embedding"  #: Phase identifier used in pipeline configuration.

    def __init__(
        self,
        independent: bool = False,
        threshold: float = 0.5,
        overfetch: int = 30,
        source_boost: bool = False,
    ):
        self.independent = independent
        self.threshold = threshold
        self.overfetch = overfetch
        self.source_boost = source_boost

    def should_run(self, ctx) -> bool:
        """Only run when LLM is enabled and no Ollama warning occurred earlier."""
        return ctx.config.llm is not None and ctx.config.llm.enabled and ctx.ollama_warning is None

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Run semantic search via cosine similarity as a re-rank or standalone KNN.

        When FTS5 results exist, operates as a re-rank (scoring FTS5
        candidates by vector distance).  Otherwise runs a standalone KNN
        query via sqlite-vec ``vec0`` virtual table, falling back to
        brute-force BLOB search for legacy indexes.
        """

        def _query(conn, _config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            # (Body indentation kept from the pre-executor version.)
                # Check which embedding storage is available
                has_vec0 = _table_exists(conn, "vec_symbols")
                has_blob = _table_has_rows(conn, "embeddings")

                # vec0 table may exist in sqlite_master but be unusable
                # when the sqlite-vec native extension failed to load
                # (missing system library, different machine, etc.).
                # Probe with a cheap query — if it fails, fall through
                # to the brute-force BLOB path.
                _vec0_usable = has_vec0
                if _vec0_usable:
                    try:
                        conn.execute("SELECT 1 FROM vec_symbols LIMIT 0")
                    except sqlite3.Error:
                        _vec0_usable = False
                        log.warning("vec_symbols table exists but is unusable — "
                                    "sqlite-vec extension not loaded; "
                                    "falling back to BLOB embeddings")

                if not has_vec0 and not has_blob:
                    return ctx.evolve(ollama_warning={"detail": "No embedding tables exist"})  # No embeddings at all

                # Generate query embedding
                try:
                    embedder = get_embedder(ctx.config.llm)
                    query_embs = embedder.embed_queries([ctx.query])
                    query_vec = query_embs[0]
                except OllamaError:
                    return ctx.evolve(ollama_warning={"detail": "Embedding model unavailable"})

                # ---- Hybrid re-rank path (default) ----
                # When FTS5 already produced candidates, re-rank them by
                # vector distance.  This is both faster and produces better
                # merged results than running an independent KNN query.
                if ctx.fts5_results and _vec0_usable and not self.independent:
                    queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
                    if not queries:
                        queries = ctx.query.split()
                    # Expand to prefix match — _expand_query skips wildcards when
                    # the query already contains OR, so we add * per-term here.
                    fts5_query = " OR ".join(
                        (q if q.endswith("*") else f"{q}*") for q in queries
                    )
                    rows = search_similar_hybrid(
                        conn,
                        query_vec,
                        ctx.config_hash,
                        fts5_query=fts5_query,
                        threshold=self.threshold,
                        limit=self.overfetch,
                    )
                    # NOTE: hybrid results are FTS5-filtered, not purely dense —
                    # AdaptiveFusionPhase should treat them as hybrid, not embedding-only.
                    return ctx.evolve(embedding_results=rows, final_results=list(rows))

                # ---- Direct KNN path ----
                if _vec0_usable:
                    try:
                        vec_rows = search_similar_vec(
                            conn,
                            query_vec,
                            ctx.config_hash,
                            threshold=self.threshold,
                            limit=self.overfetch,
                        )
                    except sqlite3.Error as e:
                        log.warning("vec0 KNN query failed — falling back to BLOB: %s", e)
                        _vec0_usable = False
                        vec_rows = []
                    if vec_rows:
                        distance_map = {r["symbol_id"]: dict(r).get("distance", 0.0) for r in vec_rows}
                        sym_ids = list(distance_map.keys())
                        placeholders = ",".join("?" * len(sym_ids))
                        emb_rows = conn.execute(
                            f"""SELECT * FROM symbols
                                WHERE config_hash = ? AND id IN ({placeholders})
                                AND is_definition = 1""",
                            (ctx.config_hash, *sym_ids),
                        ).fetchall()
                        results: list[dict] = []
                        for r in emb_rows:
                            d = dict(r)
                            dist = distance_map.get(d.get("id", -1), 0.0)
                            d["_similarity"] = round(float(1.0 - dist), 4)
                            results.append(d)
                        if self.source_boost:
                            sym_proj: dict[int, int] = {r["id"]: r.get("is_project", 0) for r in emb_rows}
                            scored: list[tuple[float, int, float, dict]] = []
                            for d in results:
                                sid = d.get("id", -1)
                                if sid not in sym_proj:
                                    continue
                                raw_sim = d["_similarity"]
                                boost = 1.2 if sym_proj[sid] == 1 else 0.85
                                d["_similarity"] = round(raw_sim * boost, 4)
                                scored.append((d["_similarity"], sid, raw_sim, d))
                            scored.sort(key=lambda x: -x[0])
                            results = [d for _, _, _, d in scored[:ctx.limit]]
                        return ctx.evolve(embedding_results=results, final_results=list(results))

                # ---- Brute-force fallback (legacy BLOB table) ----
                if has_blob:
                    stored = get_embeddings(conn, ctx.config_hash, ctx.config.llm.embed_key())
                    if not stored:
                        return ctx.evolve(ollama_warning={"detail": "No stored embeddings found"})
                    scored_bf = _brute_force_search(query_vec, stored, threshold=self.threshold)
                    top_ids = [s[0] for s in scored_bf[: self.overfetch]]
                    if not top_ids:
                        return ctx
                    placeholders = ",".join("?" * len(top_ids))  # SAFE: values in params, not f-string
                    emb_rows = conn.execute(
                        f"""SELECT * FROM symbols
                            WHERE config_hash = ? AND id IN ({placeholders})
                            AND is_definition = 1""",
                        (ctx.config_hash, *top_ids),
                    ).fetchall()
                    emb_row_dicts = [dict(r) for r in emb_rows]
                    return ctx.evolve(embedding_results=emb_row_dicts, final_results=emb_row_dicts)

                return ctx

        return ctx.executor.execute_sync(_query, ctx.config_hash)
