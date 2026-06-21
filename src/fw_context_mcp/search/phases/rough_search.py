"""Phase 1: Rough search to gather sample symbols for LLM context.

Tries **embedding-based** search first (semantic similarity) when embeddings
are available — this produces conceptually relevant samples even when the
query words don't appear literally in symbol names.

Falls back to FTS5 phrase/word search when embeddings or Ollama are unavailable.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)

# Words that add no search signal
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "and", "or", "not", "but", "if",
    "then", "else", "when", "up", "down", "out", "off", "over", "under",
    "again", "how", "what", "where", "which", "who", "whom", "why",
    "handle", "handler", "using", "that", "this", "it", "its",
})


class RoughSearchPhase(Phase):
    """Gather 12-20 sample symbols via embedding or FTS5 search.

    Prefers embedding search when available — semantic similarity finds
    conceptually related symbols even when query words don't literally
    appear in their names (e.g. "parcel locker" → get_door_state).

    Falls back to FTS5 word-pair + single-word search when embeddings
    or Ollama are unavailable.

    These samples are shown to the LLM in ``LLMQueryPhase`` so it can learn
    the project's naming conventions before generating search terms.
    """

    name = "rough_search"

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        from fw_context_mcp.indexer.db import open_db as _open_db

        query = ctx.query
        config_hash = ctx.config_hash

        # Extract content words for FTS5 fallback — from both translated
        # and original query so local-language code names also match
        raw_words = re.findall(r"\w+", query.lower())
        content_words = [w for w in raw_words if w not in _STOP_WORDS and len(w) > 1]
        rough_terms: list[str] = content_words if content_words else [query]

        # Merge original-language terms when query was translated
        if ctx.translated_from:
            orig_raw = re.findall(r"[a-zA-Z0-9_]+", ctx.translated_from.lower())
            orig_words = [w for w in orig_raw if w not in _STOP_WORDS and len(w) > 1]
            for w in orig_words:
                if w not in rough_terms:
                    rough_terms.append(w)

        # ── Try embedding-based rough search ──────────────────────────────
        if ctx.config.llm.enabled:
            emb_samples = await _try_embedding_samples(ctx)
            if emb_samples and len(emb_samples) >= 5:
                # Extract rough_terms from sample names for FTS5 fallback
                emb_terms = _extract_terms_from_samples(emb_samples)
                return ctx.evolve(
                    rough_queries=emb_terms if emb_terms else rough_terms,
                    rough_samples=emb_samples,
                )

        # ── FTS5 fallback (original behaviour) ────────────────────────────
        conn = _open_db(ctx.db_path)
        try:
            return ctx.evolve(
                rough_queries=rough_terms,
                rough_samples=_fts5_rough_samples(conn, query, content_words, config_hash),
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Embedding-based rough search
# ---------------------------------------------------------------------------


async def _try_embedding_samples(ctx) -> list[dict] | None:
    """Try embedding search for rough samples. Returns None on failure."""
    try:
        from fw_context_mcp.indexer.db import (
            get_embeddings,
            search_similar_vec,
        )
        from fw_context_mcp.indexer.db import (
            open_db as _open_db,
        )
        from fw_context_mcp.llm.ollama import OllamaError, call_ollama_embed
    except Exception:
        log.warning("Embedding rough search unavailable — import failed", exc_info=True)
        return None

    conn = _open_db(ctx.db_path)
    try:
        with conn:
            has_vec0 = _table_exists(conn, "vec_symbols")
            has_blob = _table_has_rows(conn, "embeddings")

            if not has_vec0 and not has_blob:
                return None

            try:
                query_embs = call_ollama_embed([ctx.query], ctx.config.llm)
                query_vec = query_embs[0]
            except OllamaError:
                return None

            threshold = 0.5  # Broad for rough sampling — later phases refine

            if has_vec0:
                rows = search_similar_vec(
                    conn, query_vec, ctx.config_hash,
                    threshold=threshold, limit=20,
                )
                if rows:
                    sym_ids = [r["symbol_id"] for r in rows]
                    placeholders = ",".join("?" * len(sym_ids))
                    sym_rows = conn.execute(
                        f"""SELECT * FROM symbols
                            WHERE config_hash = ? AND id IN ({placeholders})
                            AND is_definition = 1
                            ORDER BY CASE WHEN kind = 'function' THEN 0
                                          WHEN kind = 'method' THEN 1
                                          WHEN kind = 'class' THEN 2
                                          WHEN kind = 'struct' THEN 3
                                          ELSE 4 END
                            LIMIT 20""",
                        (ctx.config_hash, *sym_ids),
                    ).fetchall()
                    if sym_rows:
                        return [dict(r) for r in sym_rows]
                # vec0 had no matches for this config_hash — fall through to BLOB

            # Legacy BLOB fallback
            if has_blob:
                stored = get_embeddings(conn, ctx.config_hash, ctx.config.llm.embed_model)
                if not stored:
                    return None
                import math
                scored: list[tuple[int, float]] = []
                for sym_id, emb_vec in stored.items():
                    dot = sum(x * y for x, y in zip(query_vec, emb_vec, strict=True))
                    norm_a = math.sqrt(sum(x * x for x in query_vec))
                    norm_b = math.sqrt(sum(x * x for x in emb_vec))
                    sim = (dot / (norm_a * norm_b)) if norm_a and norm_b else 0.0
                    if sim > threshold:
                        scored.append((sym_id, sim))
                scored.sort(key=lambda x: -x[1])
                top_ids = [s[0] for s in scored[:20]]
                if not top_ids:
                    return None
                placeholders = ",".join("?" * len(top_ids))
                sym_rows = conn.execute(
                    f"""SELECT * FROM symbols
                        WHERE config_hash = ? AND id IN ({placeholders})
                        AND is_definition = 1
                        LIMIT 20""",
                    (ctx.config_hash, *top_ids),
                ).fetchall()
                if sym_rows:
                    return [dict(r) for r in sym_rows]
                return None

            return None
    except Exception:
        log.warning("Embedding rough search failed", exc_info=True)
        return None
    finally:
        conn.close()


def _extract_terms_from_samples(samples: list[dict]) -> list[str]:
    """Extract content-bearing search terms from symbol names.

    Splits camelCase and snake_case names, removes single chars
    and common noise words, returns deduplicated list.
    """
    seen: set[str] = set()
    terms: list[str] = []
    _noise = {"get", "set", "is", "has", "do", "can", "new", "init", "del",
              "the", "for", "and", "not", "are", "was", "were", "all", "any",
              "app", "from", "ret", "end", "len", "val", "int", "void", "bool"}
    for s in samples[:12]:
        name = s.get("name", "").strip("_")
        # Step 1: split on underscores (snake_case)
        for part in name.split("_"):
            if not part:
                continue
            # Step 2: split camelCase within each part
            subtokens = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z0-9]+(?=[A-Z]|$)", part)
            for t in subtokens:
                t = t.strip("_").lower()
                if len(t) >= 3 and t not in _noise and t not in seen:
                    seen.add(t)
                    terms.append(t)
                    if len(terms) >= 8:
                        return terms
    return terms


# ---------------------------------------------------------------------------
# FTS5 fallback
# ---------------------------------------------------------------------------


def _fts5_rough_samples(conn, query: str, content_words: list[str], config_hash: str) -> list[dict]:
    """Original FTS5-based rough search (fallback when no embeddings)."""
    from fw_context_mcp.indexer.db import search_symbols

    rough_samples: list[dict] = []
    rough_seen_names: set[str] = set()

    def _is_noise(name: str) -> bool:
        if set(name) & {"(", ")", "~", "=", "<", ">", "[", "]"}:
            return True
        return len(name) <= 2

    # Phase 1a: phrase search (word pairs)
    if len(content_words) >= 2:
        pairs = content_words[1:]
        per_pair_budget = max(3, min(5, 20 // max(len(pairs), 1)))
        for w in pairs:
            phrase = f'"{content_words[0]} {w}"'
            try:
                rows = search_symbols(conn, phrase, config_hash, limit=per_pair_budget)
            except Exception:
                continue
            for r in rows:
                name = r["name"]
                if _is_noise(name) or name in rough_seen_names:
                    continue
                rough_seen_names.add(name)
                rough_samples.append(dict(r))
            if len(rough_samples) >= 20:
                break

    # Phase 1b: single-word search
    remaining = 20 - len(rough_samples)
    if remaining > 0 and content_words:
        per_word_budget = max(2, min(8, remaining // max(len(content_words), 1)))
        for word in content_words:
            try:
                rows = search_symbols(conn, word, config_hash, limit=per_word_budget)
            except Exception:
                continue
            for r in rows:
                name = r["name"]
                if _is_noise(name) or name in rough_seen_names:
                    continue
                rough_seen_names.add(name)
                rough_samples.append(dict(r))
            if len(rough_samples) >= 20:
                break

    return rough_samples


# ---------------------------------------------------------------------------
# Helpers (inlined from embedding.py to avoid circular imports)
# ---------------------------------------------------------------------------


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _table_has_rows(conn, name: str) -> bool:
    if not _table_exists(conn, name):
        return False
    row = conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
    return row is not None
