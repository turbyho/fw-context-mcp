"""Phase 4: Semantic search via cosine similarity on stored embeddings."""

from __future__ import annotations

import logging
import math

from fw_context_mcp.search.phases.base import Phase

log = logging.getLogger(__name__)


class EmbeddingPhase(Phase):
    """Search symbol embeddings via cosine similarity.

    Embeddings are generated during ``fw-context index`` and stored in the
    ``embeddings`` table.  This phase embeds the query, computes cosine
    similarity against all stored embeddings, and returns symbols above the
    similarity threshold.

    Skipped when Ollama is disabled or when no embeddings exist in the DB.
    """

    name = "embedding"
    EMBEDDING_DIM = 1024

    def should_run(self, ctx) -> bool:
        return ctx.config.llm.enabled and ctx.ollama_warning is None

    async def run(self, ctx):
        from fw_context_mcp.indexer.db import get_embeddings, open_db
        from fw_context_mcp.llm.ollama import OllamaError, call_ollama_embed

        conn = open_db(ctx.db_path)
        with conn:
            stored = get_embeddings(conn, ctx.config_hash, ctx.config.llm.embed_model)
            if not stored:
                return ctx  # No embeddings in DB — skip silently

            # Embed the query
            try:
                query_embs = call_ollama_embed([ctx.query], ctx.config.llm)
                query_vec = query_embs[0]
            except OllamaError:
                return ctx  # Embedding model unavailable — skip

            # Cosine similarity against all stored embeddings
            scored = []
            for sym_id, emb_vec in stored.items():
                sim = _cosine_similarity(query_vec, emb_vec)
                if sim > 0.5:
                    scored.append((sym_id, sim))

            scored.sort(key=lambda x: -x[1])
            top_ids = [s[0] for s in scored[:30]]

            if not top_ids:
                return ctx

            # Fetch matching symbols
            placeholders = ",".join("?" * len(top_ids))
            emb_rows = conn.execute(
                f"""SELECT * FROM symbols
                    WHERE config_hash = ? AND id IN ({placeholders})
                    AND is_definition = 1""",
                (ctx.config_hash, *top_ids),
            ).fetchall()

            embedding_results = [dict(r) for r in emb_rows]
            return ctx.evolve(embedding_results=embedding_results)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
