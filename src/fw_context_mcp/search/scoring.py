"""Scoring functions shared across search phases.

Why centralised scoring?
    ``SEARCH_CODE``, ``SMART_SEARCH``, and any future search tools must
    apply the same scoring rules (stem matching, kind weighting, project
    bonus).  Centralising prevents drift — a scoring change in one tool
    automatically applies everywhere.

Why stem matching instead of substring matching?
    Substring matching produces false positives: stem ``"tim"`` matches
    ``"optimization"`` even though the symbols are unrelated.  Stem
    matching requires the stem to appear at token boundaries (underscore,
    case change, space in name_tokens) — ``"tim"`` matches ``"timer_init"``
    but not ``"optimization"``.

Centralised so that search_code, smart_search, and any future search tools
apply the same scoring rules.
"""

from __future__ import annotations

import re

# Regex cache for _stem_matches — bounded LRU via functools.
# Stems repeat across query cycles ("modem", "init", "uart") so
# caching the compiled regex avoids re-compilation per result.
from functools import lru_cache


@lru_cache(maxsize=10000)
def _compile_stem_pattern(stem: str) -> re.Pattern:
    return re.compile(rf'(?:^|_|[a-z])({re.escape(stem)})(?:$|_|[A-Z])')

# Bonus for definition-level symbol kinds (functions, methods, classes, etc.)
# vs lightweight kinds (variables, fields, namespaces).
#
# Why: a function named "uart_configure" is more likely to be the target
# of a search than a local variable also named "uart_configure".  The
# kind weight biases the score toward structural symbols.
KIND_WEIGHT: dict[str, int] = {
    "function": 2,
    "method": 2,
    "constructor": 2,
    "destructor": 2,
    "class": 2,
    "struct": 2,
    "enum": 2,
    "typedef": 2,
    "enum_constant": 1,     # less structural than the enum itself
    "namespace": 1,         # rarely the search target
    "varglobal": 2,         # globals are important (config flags, state)
    "varlocal": 0,          # noise — local variables are rarely searched
    "variable": 0,
    "field": 0,
}


def _stem_matches(stem: str, name: str, name_tokens: str) -> bool:
    """Check if *stem* matches at token boundaries — not as substring.

    Why not ``stem in name``?
        ``"tim" in "optimization"`` → True, but ``"tim"`` and ``"optimization"``
        are unrelated.  Token-boundary matching prevents this.

    Why multiple strategies?
        - ``name_tokens`` is the pre-computed camelCase/snake_case split
          (e.g. "timer init") — fastest check, covers most cases
        - ``_stem_`` boundaries handle snake_case where the tokenizer
          may have left the underscore as-is
        - ``__stem__`` handles GNU/C double-underscore extensions (``__wrap_malloc``)
        - The regex handles camelCase/PascalCase boundaries (``TimerInit`` →
          ``Timer`` matches stem ``"timer"``)

    Matches when *stem* appears as a whole token in *name_tokens*
    (space-separated) or at underscore/case boundaries in *name*.
    Avoids false positives like stem ``"tim"`` matching ``"optimization"``.
    """
    # Fast path: exact match in space-separated tokens
    if stem in name_tokens.split():
        return True
    if stem == name:
        return True
    # Check for stem at underscore boundaries: _stem_ or stem_ or _stem
    if f"_{stem}_" in name or name.startswith(f"{stem}_") or name.endswith(f"_{stem}"):
        return True
    # Check for stem at double-underscore boundaries (GNU/C extensions: __wrap_malloc)
    if f"__{stem}__" in name:
        return True
    # Check for stem at case boundaries (camelCase/PascalCase) — cached via lru_cache
    return bool(_compile_stem_pattern(stem).search(name))


def score_result(
    row: dict,
    query_stems: list[str],
) -> int:
    """Score a single result row by how well it matches the query stems.

    Why stem-based scoring?
        FTS5 returns results ranked by its own relevance formula (BM25).
        But FTS5 ranks token frequency, not concept match.  Stem-based
        scoring checks whether each query term actually appears at a
        meaningful boundary in the symbol name — a stronger signal.

    Why the is_project bonus?
        Across all experiments, project-owned symbols consistently rank
        higher in user relevance judgments than vendor SDK symbols with
        the same stem score.  The +1 bonus is small enough not to override
        a strong stem match, but large enough to break ties toward
        project code.

    Points:
        name / name_tokens match  → +3  (strongest signal — exact concept match)
        qualified_name match      → +2  (namespace/class context relevant)
        file_path match           → +1  (weak signal — may be coincidence)
        project-local code        → +1  (prefer user code over vendor SDK)
        kind weight               → KIND_WEIGHT bonus

    ``query_stems`` are lowercased query terms with trailing ``*`` stripped
    so ``modem_init*`` becomes ``modem_init``.
    """
    name = (row.get("name") or "").lower()
    ntoks = (row.get("name_tokens") or "").lower()
    qname = (row.get("qualified_name") or "").lower()
    fpath = (row.get("file_path") or "").lower()
    kind = row.get("kind") or ""

    s = 0
    for stem in query_stems:
        # Skip single-char stems — they match too broadly and produce
        # noise (e.g. stem "c" matches every symbol containing "c")
        if len(stem) < 2:
            continue
        # Token-boundary match: stem must match a whole token in name_tokens
        # (space-separated), or appear at underscore/case boundaries in name.
        if _stem_matches(stem, name, ntoks):
            s += 3
        elif stem in qname:
            s += 2
        elif stem in fpath:
            s += 1

    # Bonus for symbols under project source roots.
    # Configurable via config.toml [index] project_paths — only symbols
    # whose file_path matches a project root get is_project=1.
    if row.get("is_project") == 1:
        s += 1

    s += KIND_WEIGHT.get(kind, 0)
    return s


def stems_from_queries(queries: list[str]) -> list[str]:
    """Extract lowercased stems from query strings (strip trailing wildcards).

    Why strip wildcards?
        FTS5 queries use ``*`` as prefix wildcard (``modem_init*``).  The
        stem for scoring is ``modem_init`` — the ``*`` is an FTS5 artefact,
        not part of the concept.
    """
    return [q.rstrip("*").lower() for q in queries if q]
