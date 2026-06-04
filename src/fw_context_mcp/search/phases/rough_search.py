"""Phase 1: Rough FTS5 search to gather sample symbols for LLM context."""

from __future__ import annotations

import re

from fw_context_mcp.search.phases.base import Phase

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
    """Gather 12-20 sample symbols via FTS5 word-pair + single-word search.

    These samples are shown to the LLM in ``LLMQueryPhase`` so it can learn
    the project's naming conventions (snake_case vs camelCase, prefixes, etc.)
    before generating search terms.
    """

    name = "rough_search"

    async def run(self, ctx):
        from fw_context_mcp.indexer.db import open_db, search_symbols

        query = ctx.query
        config_hash = ctx.config_hash
        raw_words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
        content_words = [w for w in raw_words if w not in _STOP_WORDS and len(w) > 1]
        rough_terms = content_words if content_words else [query]

        rough_samples: list[dict] = []
        rough_seen_names: set[str] = set()

        def _is_noise(name: str) -> bool:
            if set(name) & {"(", ")", "~", "=", "<", ">", "[", "]"}:
                return True
            return len(name) <= 2

        conn = open_db(ctx.db_path)
        with conn:
            # Phase 1a: phrase search (word pairs) — allocates ~1 pair per content word
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

            # Phase 1b: single-word search — remaining slots
            remaining = 20 - len(rough_samples)
            if remaining > 0 and rough_terms:
                per_word_budget = max(2, min(8, remaining // max(len(rough_terms), 1)))
                for word in rough_terms:
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

        return ctx.evolve(rough_queries=rough_terms, rough_samples=rough_samples)
