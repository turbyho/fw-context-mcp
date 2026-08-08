"""Shared LLM response parsing for search phases.

Why a shared module?
    Both ``llm_query`` (Phase 2) and ``refine`` (Phase 3) call Ollama and
    need to extract JSON arrays of FTS5 search terms from the response.
    Centralising the parsing logic avoids duplicated regex and JSON
    extraction code, and ensures both phases apply the same sanitisation
    rules (safe token pattern, bogus-word filtering).

Why two extraction strategies (JSON then regex)?
    The LLM prompt asks for a JSON array, but small models sometimes wrap
    the array in explanatory text, markdown code fences, or numbered lists.
    The JSON path handles well-structured responses.  The regex fallback
    handles the remaining cases where the LLM output format drifts.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# Only allow alphanumeric, underscore, and trailing wildcard — LLM responses
# may contain markdown or FTS5 syntax that would alter query semantics.
_SAFE_FTS5_TOKEN = re.compile(r"^[\w*]+$")

# LLM models sometimes respond with these as top-level content when they
# fail to generate real terms — filter them to avoid empty-looking results.
_BOGUS = frozenset({"json", "[]"})


def parse_llm_search_terms(raw: str) -> list[str]:
    """Parse LLM response into FTS5 keyword search terms.

    What it does:
        Extracts search terms from an LLM response by first trying JSON
        array parsing (the intended LLM output format), then falling back
        to line-by-line regex extraction.

    Why two strategies?
        The primary path handles well-structured JSON responses.  The
        fallback handles LLM output that drifts from the prompt format:
        markdown lists, numbered items, or text with embedded query terms.
        This dual approach maximises the chance of extracting usable terms
        from any reasonable LLM response.
    """
    terms: list[str] = []
    # Primary path: extract JSON array from the response
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    terms.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        pass
    # Fallback path: regex extraction from each line
    if not terms:
        for line in raw.splitlines():
            line = re.sub(r"\s*\([^)]*\)\s*$", "", line)  # strip trailing parens
            cleaned = re.sub(r"^[\s\d\.\-\*]+", "", line).strip().strip("`'\"*")
            if cleaned and not cleaned.startswith("#") and _SAFE_FTS5_TOKEN.match(cleaned):
                terms.append(cleaned)
    # Filter bogus tokens that some LLMs emit as placeholder content
    return [t.strip() for t in terms
            if t and t.lower() not in _BOGUS]
