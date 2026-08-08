"""Phase: Rough search to gather sample symbols for LLM context.

Why rough search before LLM query?
    The LLM generates better FTS5 terms when it can see the project's
    actual naming conventions.  Rough search provides 12-20 sample symbols
    that represent the codebase's style (snake_case vs camelCase, common
    prefixes, abbreviations).  The LLM uses these samples to generate
    terms that match the project's conventions.

Why embedding-first?
    When embeddings are available, semantic similarity finds conceptually
    relevant samples even when query words don't appear literally in symbol
    names.  For "parcel locker", FTS5 would find zero samples (no symbol
    contains "parcel" or "locker"), but embeddings find ``get_door_state``
    and ``set_shipment`` — semantically related.  These samples teach the
    LLM the naming style of the correct subsystem.

Why extract terms from sample names?
    When embedding search is unavailable (no embeddings, no Ollama), the
    phase falls back to FTS5 word-pair search.  The ``rough_terms``
    extracted from sample names provide FTS5 fallback queries for
    downstream phases — these are the user's raw query words, which is
    better than nothing.

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

from fw_context_mcp.search.phases.embedding_helpers import (
    brute_force_search,
    round_robin_by_kind,
    table_exists,
    table_has_rows,
)

log = logging.getLogger(__name__)

_table_exists = table_exists  # backward-compat alias
_table_has_rows = table_has_rows

# Words that add no search signal — filtering these prevents FTS5
# queries like "is the modem connected" from searching for "is" and "the"
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

    name = "rough_search"  #: Phase identifier used in pipeline configuration.

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Gather 12-20 sample symbols via embedding or FTS5 search.

        Tries embedding-based sampling first (semantic similarity). Falls
        back to FTS5 word-pair + single-word search. Extracts keyword
        terms from sample names for downstream FTS5 fallback use.
        """
        query = ctx.query
        config_hash = ctx.config_hash

        # Extract content words for FTS5 fallback — from both translated
        # and original query so local-language code names also match
        raw_words = re.findall(r"\w+", query.lower())
        content_words = [w for w in raw_words if w not in _STOP_WORDS and len(w) > 1]
        rough_terms: list[str] = content_words if content_words else [query]

        # Merge original-language terms when query was translated.
        # Example: Czech "moderni pripojeni" translated to "modem connect"
        # — "moderni" and "pripojeni" don't exist in translated form but
        # might be actual function name tokens in the codebase.
        if ctx.translated_from:
            orig_raw = re.findall(r"[a-zA-Z0-9_]+", ctx.translated_from.lower())
            orig_words = [w for w in orig_raw if w not in _STOP_WORDS and len(w) > 1]
            for w in orig_words:
                if w not in rough_terms:
                    rough_terms.append(w)

        # ── Try embedding-based rough search ──────────────────────────────
        if ctx.config.llm is not None and ctx.config.llm.enabled:
            emb_samples = await _try_embedding_samples(ctx)
            if emb_samples and len(emb_samples) >= 5:
                # Extract rough_terms from sample names for FTS5 fallback.
                # These terms come from actual symbol names and are more
                # likely to match than raw query words.
                emb_terms = _extract_terms_from_samples(emb_samples)
                return ctx.evolve(
                    rough_queries=emb_terms if emb_terms else rough_terms,
                    rough_samples=emb_samples,
                )

        # ── FTS5 fallback (original behaviour) ────────────────────────────
        def _query(conn, _config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _fts5_rough_samples(conn, query, content_words, config_hash)

        samples = ctx.executor.execute_sync(_query, config_hash)
        return ctx.evolve(
            rough_queries=rough_terms,
            rough_samples=samples,
        )


# ---------------------------------------------------------------------------
# Embedding-based rough search
# ---------------------------------------------------------------------------


async def _try_embedding_samples(ctx) -> list[dict] | None:
    """Try embedding search for rough samples. Returns None on failure.

    Why return None instead of raising?
        Embedding search is a best-effort optimisation.  If it fails
        (no embeddings, Ollama down, missing dependencies), the caller
        falls back to FTS5.  Raising would require error handling in
        the phase runner when the fallback is already built in.
    """
    try:
        from fw_context_mcp.indexer.db import (
            get_embeddings,
            search_similar_vec,
        )
        from fw_context_mcp.llm.embedder_factory import get_embedder
        from fw_context_mcp.llm.ollama import OllamaError
    except (ValueError, TypeError, RuntimeError, AttributeError):
        log.warning("Embedding rough search unavailable — import failed", exc_info=True)
        return None

    def _query(conn, _config_hash):
        # Runs under the executor lock on the single shared connection;
        # the phase must not open its own connection.
        has_vec0 = _table_exists(conn, "vec_symbols")
        has_blob = _table_has_rows(conn, "embeddings")

        if not has_vec0 and not has_blob:
            return None

        try:
            embedder = get_embedder(ctx.config.llm)
            query_embs = embedder.embed_queries([ctx.query])
            query_vec = query_embs[0]
        except OllamaError:
            return None

        # Broad threshold for rough sampling — we want 12-20 candidates
        # to show the LLM, not precise top-5 hits.  Later phases refine.
        threshold = 0.5

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
                        AND is_definition = 1""",
                    (ctx.config_hash, *sym_ids),
                ).fetchall()
                if sym_rows:
                    # Round-robin for kind diversity — the LLM should see
                    # functions, methods, structs, not just one kind
                    result = round_robin_by_kind(sym_rows, limit=20)
                    return [dict(r) for r in result]
            # vec0 had no matches for this config_hash — fall through to BLOB

        # Legacy BLOB fallback
        if has_blob:
            stored = get_embeddings(conn, ctx.config_hash, ctx.config.llm.embed_key())
            if not stored:
                return None
            scored = brute_force_search(query_vec, stored, threshold=threshold)
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

    try:
        return ctx.executor.execute_sync(_query, ctx.config_hash)
    except (ValueError, TypeError, RuntimeError, AttributeError):
        log.warning("Embedding rough search failed", exc_info=True)
        return None


def _extract_terms_from_samples(samples: list[dict]) -> list[str]:
    """Extract content-bearing search terms from symbol names.

    Why extract terms from samples?
        When embeddings find conceptually related samples (e.g. "modem init"
        → ``network_registration``, ``modem_attach``, ``lte_configure``),
        extracting tokenised terms from these names gives FTS5 fallback
        queries that are much better than raw query words.  The tokens
        ``network``, ``registration``, ``modem``, ``attach``, ``lte``,
        ``configure`` capture the subsystem terminology.

    Why filter noise words?
        Common prefixes like ``get``, ``set``, ``init``, ``is`` appear
        in almost every function name.  Including them in FTS5 queries
        would match everything — they carry no signal about the specific
        subsystem being queried.

    Splits camelCase and snake_case names, removes single chars
    and common noise words, returns deduplicated list.
    """
    seen: set[str] = set()
    terms: list[str] = []
    _noise = {"get", "set", "is", "has", "do", "new", "init", "del",
              "the", "for", "and", "not", "are", "was", "were", "all", "any",
              "app", "from", "ret", "end", "int", "void", "bool"}
    for s in samples[:12]:
        name = s.get("name", "").strip("_")
        for part in name.split("_"):
            if not part:
                continue
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
    """Original FTS5-based rough search (fallback when no embeddings).

    Why phrase search before single-word?
        Word pairs like "uart configure" are more precise than individual
        words.  Searching for ``"uart configure"`` (exact phrase in FTS5)
        returns fewer but more relevant samples.  If phrase search fills
        all 20 slots, single-word never runs — we already have enough.

    Why budget per word?
        Without budget, a common word like "modem" (~500 matches) would
        fill all 20 sample slots, crowding out results for the second
        word.  Per-word budget (e.g. max 8 per word) ensures both terms
        contribute roughly equally to the sample set.
    """
    from fw_context_mcp.indexer.db import search_symbols

    rough_samples: list[dict] = []
    rough_seen_names: set[str] = set()

    def _is_noise(name: str) -> bool:
        if name.startswith("operator"):
            return False  # valid C++ operator overloads
        # Names containing these characters are likely mangled or
        # compiler-generated — not useful as LLM samples
        if set(name) & {"(", ")", "~", "=", "<", ">", "[", "]"}:
            return True
        return len(name) <= 2

    # Phase 1a: phrase search (word pairs) — more precise
    if len(content_words) >= 2:
        pairs = content_words[1:]
        per_pair_budget = max(3, min(5, 20 // max(len(pairs), 1)))
        for w in pairs:
            phrase = f'"{content_words[0]} {w}"'
            try:
                rows = search_symbols(conn, phrase, config_hash, limit=per_pair_budget)
            except (ValueError, TypeError, RuntimeError, AttributeError):
                continue
            for r in rows:
                name = r["name"]
                if _is_noise(name) or name in rough_seen_names:
                    continue
                rough_seen_names.add(name)
                rough_samples.append(dict(r))
            if len(rough_samples) >= 20:
                break

    # Phase 1b: single-word search — broader, fills remaining slots
    remaining = 20 - len(rough_samples)
    if remaining > 0 and content_words:
        per_word_budget = max(2, min(8, remaining // max(len(content_words), 1)))
        for word in content_words:
            try:
                rows = search_symbols(conn, word, config_hash, limit=per_word_budget)
            except (ValueError, TypeError, RuntimeError, AttributeError):
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


