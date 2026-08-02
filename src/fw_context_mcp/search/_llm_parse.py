"""Shared LLM response parsing for search phases.

Used by both ``llm_query`` and ``refine`` phases to extract JSON arrays
from LLM responses — avoids duplicated code.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

_SAFE_FTS5_TOKEN = re.compile(r"^[\w*]+$")
_BOGUS = frozenset({"json", "[]"})


def parse_llm_search_terms(raw: str) -> list[str]:
    """Parse LLM response into FTS5 keyword search terms.

    Tries JSON array first, falls back to line-by-line regex.
    """
    terms: list[str] = []
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
    if not terms:
        for line in raw.splitlines():
            line = re.sub(r"\s*\([^)]*\)\s*$", "", line)
            cleaned = re.sub(r"^[\s\d\.\-\*]+", "", line).strip().strip("`'\"*")
            if cleaned and not cleaned.startswith("#") and _SAFE_FTS5_TOKEN.match(cleaned):
                terms.append(cleaned)
    return [t.strip() for t in terms
            if t and t.lower() not in _BOGUS]
