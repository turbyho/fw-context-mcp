"""Search fallback strategies and shared formatting utilities.

All fallback strategies and formatting helpers are defined in
:mod:`fw_context_mcp.search.shared_fallbacks` and re-exported here
for backward compatibility.  Only ``_search_code_fts5_kind`` is
handler-specific (it runs a primary FTS5 search with optional kind
filter before the fallback chain).

WHY this re-export layer exists: the search fallback chain was
originally in this module.  When it moved to ``search.shared_fallbacks``
to be shareable with CLI tools, existing callers in the handlers
package would have needed import-path changes across many files.
The re-export avoids a noisy refactor — existing handlers import
from ``_search_fallbacks`` and get the same symbols, now sourced
from the canonical location.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fw_context_mcp.indexer.db import search_symbols
from fw_context_mcp.search.shared_fallbacks import (  # noqa: F401 — re-export
    _SEARCH_CODE_FALLBACKS,
    _fmt_symbol_rows,
    _search_code_docstring,
    _search_code_individual_terms,
    _search_code_macros_fts,
    _search_code_name_tokens,
    _symbol_row_to_dict,
)


def _search_code_fts5_kind(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict], str] | None:
    """Primary FTS5 search with optional kind + kind-less fallback.

    Returns ``(rows, method_name)`` on success, ``None`` when no
    results are found.

    WHY ``exclude_variables=True``: FTS5 indexes the qualified name, thus a
    local variable matches through the name of the function that holds it —
    a search for ``sensor`` answered with ``V``, ``ret`` and ``tmp_value``
    from inside ``read_sensor_value``, 4 of 20 results on one measured
    query.  A local is never the answer to "which symbol is about X".
    ``search_symbols`` gives an explicit *kind* precedence over this filter,
    thus ``search_code(..., kind="varlocal")`` still reaches them.
    """
    rows = search_symbols(
        c, query, config_hash, limit=limit, kind=kind,
        exclude_variables=True, project_only=project_only,
    )
    method = "fts5+kind"
    if not rows and kind:
        rows = search_symbols(
            c, query, config_hash, limit=limit, kind=None,
            exclude_variables=True, project_only=project_only,
        )
        if rows:
            method = "fts5"
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, method)

