"""lookup_symbol MCP tool."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from fw_context_mcp.indexer.db import lookup_macro
from fw_context_mcp.mcp.handlers._search_fallbacks import _symbol_row_to_dict
from fw_context_mcp.mcp.shared.context import _db_path
from fw_context_mcp.mcp.shared.stale import _with_stale_recovery
from fw_context_mcp.utils import abs_path, resolve_project_root

LOOKUP_EXACT_SQL = """SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""

LOOKUP_PREFIX_SQL = r"""SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""

log = logging.getLogger(__name__)


def lookup_symbol(
    name: Annotated[str, Field(description="Symbol name. Exact match if exact=True, prefix LIKE match otherwise. E.g. 'uart_init' or 'uart_'.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
    exact: Annotated[bool, Field(description="True = exact name match, False = prefix LIKE match (default).")] = False,
    limit: Annotated[int, Field(description="Maximum results returned (capped at 100, default 50).")] = 50,
) -> list[dict]:
    """Look up a C/C++ symbol by name via libclang index — exact or prefix
    matching. Finds symbols text-based search can miss: build-conditional
    code, template instantiations, macro-expanded names. Prefer this when
    you know the exact symbol name or prefix. Falls back to macro lookup.

    Finds symbols text-based search can miss: build-conditional code, template
    instantiations, macro-expanded names. Macros are extracted via
    ``clang -dM -E`` during indexing so ``#ifdef``-conditional macros
    resolve correctly for the active build config. Prefer this over
    search_code when you know the exact symbol name or a prefix
    (``uart_`` finds all UART symbols). Use search_code for
    keyword/concept search.

    Read-only: yes. May auto-reindex stale files (non-blocking).

    Args:
        name: Symbol name (exact match) or prefix (set exact=False).
            E.g. 'uart_init' finds the exact function; 'uart_' finds
            all symbols starting with 'uart_'.
        project_root: Project directory. Auto-detected if omitted.
        exact: True = exact name match, False = prefix LIKE match (default).
        limit: Maximum results (default 50).

    Returns:
        list[dict]: Symbols with name, qualified_name, kind, file, line,
        signature, docstring, is_definition, is_template, is_virtual,
        is_pure_virtual fields. Enum constants include ``enum_value``
        with the integer value. Macro results include ``kind="macro"``,
        ``value`` (raw definition), and ``expanded_value`` (preprocessor-
        resolved value). May also include ``template_usr``,
        ``parent_usr``, ``summary``, ``inputs``, ``outputs`` when available.
        When no results found, may include ``_did_you_mean`` with suggested
        symbol names. Empty list if not found.

        **Note:** C++ constructors share their name with the enclosing
        class, so ``lookup_symbol("Foo")`` may return both ``class Foo``
        and ``constructor Foo::Foo()``.  Use the ``kind`` field to
        filter when you need a specific symbol type.
    """
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        limit = max(0, min(limit, 100))

        def _do_lookup(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            if exact:
                rows = c.execute(
LOOKUP_EXACT_SQL,
                    (config_hash, name, name, limit),
                ).fetchall()
            else:
                esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = c.execute(
LOOKUP_PREFIX_SQL,
                    (config_hash, f"{esc}%", f"{esc}%", limit),
                ).fetchall()

            # Fallback: "Foo::bar" without namespace — extract short name, suffix-filter
            if not rows and "::" in name:
                short_name = name.rsplit("::", 1)[-1]
                if exact:
                    rows = c.execute(
                        """SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, short_name, short_name, limit * 2),
                    ).fetchall()
                else:
                    esc2 = short_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    rows = c.execute(
                        r"""SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, f"{esc2}%", f"{esc2}%", limit * 2),
                    ).fetchall()
                rows = [r for r in rows if r["qualified_name"].endswith(name)][:limit]

            # Did-you-mean? suggestions when nothing matched
            _suggestions: list[str] = []
            if not rows:
                try:
                    from ...search.did_you_mean import suggest as suggest_names
                    _suggestions = suggest_names(c, config_hash, name, limit=5)
                except (ValueError, TypeError, RuntimeError, AttributeError):
                    pass  # suggestions are best-effort

            # Macro fallback: check the macros table
            if not rows:
                _macro_rows = lookup_macro(c, config_hash, name, exact=exact, limit=limit)
                if _macro_rows:
                    result = [
                        {
                            "name": m["name"],
                            "qualified_name": m["name"],
                            "kind": "macro",
                            "file": abs_path(root, m["file_path"]),
                            "line": m["line"],
                            "value": m["value"],
                            **({"expanded_value": m["expanded_value"]} if m["expanded_value"] else {}),
                        }
                        for m in _macro_rows
                    ]
                    if _suggestions:
                        result.append({"_did_you_mean": _suggestions})
                    return result

            fallback_used = False
            if not rows and _suggestions:
                for suggestion in _suggestions[:3]:
                    rows = c.execute(
                        """SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, suggestion, suggestion, limit),
                    ).fetchall()
                    if rows:
                        fallback_used = True
                        break

            result = [
                _symbol_row_to_dict(
                    r, root,
                    **({"_fallback": True} if fallback_used else {}),
                )
                for r in rows
            ]
            if _suggestions:
                result.append({"_did_you_mean": _suggestions})
            return result

        return _with_stale_recovery(root, db_path, _do_lookup)
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]

# ── moved from server.py ──
