"""lookup_symbol MCP tool.

WHY lookup_symbol exists separately from search_code: search_code uses
FTS5 full-text search — it tokenizes names into words and matches by
concept.  This is lossy for exact names: ``"kb_open_disp"`` tokenizes
as ``"kb" + "open" + "disp"``, which matches unrelated symbols containing
those tokens.  lookup_symbol uses SQL LIKE with escaped wildcards for
EXACT or PREFIX matching — no tokenization, no false positives.

WHY there is a ``::`` short-name fallback: users often know partial
qualified names (``"Foo::bar"``) but not the full namespace prefix.
The fallback extracts the short name after the last ``::``, does a LIKE
search, then filters by qualified_name suffix — resolving ``"bar"``
to ``"some::ns::Foo::bar"`` without requiring the user to know the
namespace chain.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from fw_context_mcp.indexer.db import count_macros, lookup_macro
from fw_context_mcp.mcp.handlers._search_fallbacks import _symbol_row_to_dict
from fw_context_mcp.mcp.shared.context import _db_path
from fw_context_mcp.mcp.shared.paging import clamp_offset, page_notice
from fw_context_mcp.utils import abs_path, resolve_project_root

# ``class`` comes from the parent symbol, and it is empty for a free
# function.  It is what tells two same-name methods apart at a glance: a
# common name such as ``read`` or ``write`` lives in many classes, and a
# reader should not have to cut a qualified name at the last ``::``.
_OWNER_SQL = """CASE WHEN p.kind IN ('class', 'struct', 'union')
                     THEN p.name ELSE '' END AS owner"""
_OWNER_JOIN = """LEFT JOIN symbols p
                   ON p.usr = s.parent_usr AND p.config_hash = s.config_hash"""

# OFFSET pages through a name that matches more symbols than one answer
# should carry.  Without it the tools could offer a short list and a reader
# had no way to reach the rest.
#
# The order stays as it was — a definition first, then the line — because
# an OFFSET is only meaningful over a STABLE order.  Relevance belongs to
# the ``candidates`` list of the body tools, which ranks by reference
# count; this listing is the complete one, and it must not shuffle between
# two pages of the same walk.
#
# ``s.file_path`` and ``s.usr`` end the order.  A definition flag and a
# line number tie constantly — line 1 of two headers, or two template
# instantiations of one name — and SQLite may return tied rows in any
# order, thus an OFFSET over those two columns alone could show one symbol
# twice and hide another.  ``s.usr`` is unique within one build.
_LOOKUP_ORDER = "ORDER BY s.is_definition DESC, s.line, s.file_path, s.usr"

_LOOKUP_EXACT_WHERE = "s.config_hash=? AND (s.name=? OR s.qualified_name=?)"
_LOOKUP_PREFIX_WHERE = (
    r"s.config_hash=? AND (s.name LIKE ? ESCAPE '\' "
    r"OR s.qualified_name LIKE ? ESCAPE '\')"
)

LOOKUP_EXACT_SQL = f"""SELECT s.*, {_OWNER_SQL} FROM symbols s
   {_OWNER_JOIN}
   WHERE {_LOOKUP_EXACT_WHERE}
   {_LOOKUP_ORDER}
   LIMIT ? OFFSET ?"""

LOOKUP_PREFIX_SQL = f"""SELECT s.*, {_OWNER_SQL} FROM symbols s
   {_OWNER_JOIN}
   WHERE {_LOOKUP_PREFIX_WHERE}
   {_LOOKUP_ORDER}
   LIMIT ? OFFSET ?"""


def _escape_like(text: str) -> str:
    r"""Make ``_`` and ``%`` literal for a LIKE pattern.

    An embedded name carries underscores by habit (``uart_init``), and
    ``_`` is the single-character wildcard of LIKE.  Without the escape
    ``uart_init`` would also match ``uartXinit``.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _page_hint(name: str, next_offset: int) -> str:
    """Spell out the call that reads the next page.

    Every query of this tool now selects the owner column, thus each row
    carries its class and each page carries the same fields.
    """
    return f"lookup_symbol('{name}', offset={next_offset}) reads the next page."


log = logging.getLogger(__name__)


def lookup_symbol(
    name: Annotated[str, Field(description="Symbol name. Exact match if exact=True, prefix LIKE match otherwise. E.g. 'uart_init' or 'uart_'.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
    exact: Annotated[bool, Field(description="True = exact name match, False = prefix LIKE match (default).")] = False,
    limit: Annotated[int, Field(description="Maximum results returned (capped at 100, default 50).")] = 50,
    offset: Annotated[int, Field(description="Skip this many results. Pages through a name that many classes share, such as 'read' or 'write'.")] = 0,
    variant: Annotated[str | None, Field(description="Build variant (multi-build project). Omit to use default_variant. One query answers for ONE build.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image within the variant. Required when the variant holds several: each image is a separate program.")] = None,
) -> list[dict]:
    """Look up a C/C++ symbol by name via libclang index — exact or prefix
    matching. Finds symbols text-based search can miss: build-conditional
    code, template instantiations, macro-expanded names. Macros are
    extracted via ``clang -dM -E`` during indexing so ``#ifdef``-conditional
    macros resolve correctly for the active build config. Prefer this over
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
        limit: Maximum results of one page (default 50).
        offset: Skip this many results (default 0).  A common method name
            lives in many classes — ``read`` and ``write`` match dozens of
            symbols — and this walks past the ones already seen.  The page
            notice names the offset to use.  The order is stable (a
            definition first, then the line, then the file and the USR),
            thus two pages never overlap and never skip a symbol.
        variant: Build variant (multi-build project). Omit to use
            default_variant. One query answers for ONE build.
        image: Sysbuild image within the variant. Required when the
            variant holds several: each image is a separate program.

    Returns:
        list[dict]: The first is the page notice — ``total``, ``offset``,
        ``shown``, ``more`` — where ``total`` counts every symbol the name
        matches.  Read it before you conclude that a page holds them all.
        Each symbol that follows has name, qualified_name, kind, file,
        line, signature, docstring, is_definition, is_template, is_virtual,
        is_pure_virtual fields, and ``class`` — the class, struct or union
        that declares the symbol, absent for a free function.  ``class`` is
        what tells two same-name methods apart at a glance.
        Enum constants include ``enum_value``
        with the integer value. Macro results include ``kind="macro"``,
        ``value`` (raw definition), and ``expanded_value`` (preprocessor-
        resolved value). May also include ``template_usr``,
        ``parent_usr``, and ``llm_analysis`` (``{summary, inputs,
        outputs}``) when available.  A model wrote the text in
        ``llm_analysis``, and the code did not — use it to find a symbol,
        and quote ``signature``, ``docstring``, or ``get_source`` instead.
        When no results found, may include ``_did_you_mean`` with suggested
        symbol names. When no symbol matches, the list is empty — there is
        then no page notice, because there is no page.  An ``info`` entry
        comes back for one case only: an offset past the end of an answer
        that does hold rows.

        **Note:** C++ constructors share their name with the enclosing
        class, so ``lookup_symbol("Foo")`` may return both ``class Foo``
        and ``constructor Foo::Foo()``.  Use the ``kind`` field to
        filter when you need a specific symbol type.

        A symbol that comes from the relaxed prefix fallback carries
        ``_fallback: True`` — the name is not an exact match of *name*.

        A list with one dict that holds an ``error`` key means that the
        project has no index, or that the lookup failed.  Read that key
        before you read the result fields.
    """
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        limit = max(0, min(limit, 100))
        skip = clamp_offset(offset)

        def _page(c: sqlite3.Connection, where: str, params: tuple) -> tuple[list, int]:
            """Read one page of the symbols that *where* selects, and count them.

            The page and the count run on the SAME text, thus ``total`` can
            never describe another answer than the rows do.
            """
            rows = c.execute(
                f"""SELECT s.*, {_OWNER_SQL} FROM symbols s
                    {_OWNER_JOIN}
                    WHERE {where}
                    {_LOOKUP_ORDER}
                    LIMIT ? OFFSET ?""",
                (*params, limit, skip),
            ).fetchall()
            total = c.execute(
                f"SELECT COUNT(*) FROM symbols s WHERE {where}", params,
            ).fetchone()[0]
            return rows, total

        def _do_lookup(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            # ── Tier 1: Exact or prefix LIKE, no tokenization ──
            # The ESCAPE '\' prevents SQL wildcards in user input from
            # being interpreted — e.g. "UART_DRIVER" must match the
            # literal underscore, not "UART+any_char+DRIVER".
            if exact:
                rows, total = _page(c, _LOOKUP_EXACT_WHERE, (config_hash, name, name))
            else:
                esc = _escape_like(name)
                rows, total = _page(
                    c, _LOOKUP_PREFIX_WHERE, (config_hash, f"{esc}%", f"{esc}%"),
                )

            # Fallback: "Foo::bar" without namespace — extract short name, suffix-filter
            # WHY: users often type qualified names partially — e.g. "bar" when
            # they mean "ns::Foo::bar".  The short-name LIKE search finds broad
            # candidates; the suffix filter then narrows to exact matches.
            #
            # The gate is the COUNT and not the rows.  An offset past the end
            # of a name that DID match leaves the page empty, and a fallback
            # would then answer about another symbol under the first name.
            #
            # The suffix test is a LIKE and no longer a filter in Python.  A
            # filter after the LIMIT cuts an unknown number of rows, thus
            # neither the page nor the count would be the truth.
            if total == 0 and "::" in name:
                short_name = name.rsplit("::", 1)[-1]
                suffix = f"%{_escape_like(name)}"
                if exact:
                    rows, total = _page(
                        c,
                        _LOOKUP_EXACT_WHERE + r" AND s.qualified_name LIKE ? ESCAPE '\'",
                        (config_hash, short_name, short_name, suffix),
                    )
                else:
                    esc2 = _escape_like(short_name)
                    rows, total = _page(
                        c,
                        _LOOKUP_PREFIX_WHERE + r" AND s.qualified_name LIKE ? ESCAPE '\'",
                        (config_hash, f"{esc2}%", f"{esc2}%", suffix),
                    )

            # Did-you-mean? suggestions when nothing matched
            _suggestions: list[str] = []
            if total == 0:
                try:
                    from ...search.did_you_mean import suggest as suggest_names
                    _suggestions = suggest_names(c, config_hash, name, limit=5)
                except (ValueError, TypeError, RuntimeError, AttributeError):
                    pass  # suggestions are best-effort

            # Macro fallback: check the macros table
            if total == 0:
                macro_total = count_macros(c, config_hash, name, exact=exact)
                _macro_rows = lookup_macro(
                    c, config_hash, name, exact=exact, limit=limit, offset=skip,
                )
                if macro_total:
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
                    result.insert(0, page_notice(
                        macro_total, skip, len(result),
                        hint=_page_hint(name, skip + len(result)),
                    ))
                    if _suggestions:
                        result.append({"_did_you_mean": _suggestions})
                    return result

            fallback_used = False
            if total == 0 and _suggestions:
                for suggestion in _suggestions[:3]:
                    rows, total = _page(
                        c, _LOOKUP_EXACT_WHERE, (config_hash, suggestion, suggestion),
                    )
                    if total:
                        fallback_used = True
                        break

            result = [
                _symbol_row_to_dict(
                    r, root,
                    # ``class`` is empty for a free function.
                    **({"class": r["owner"]} if r["owner"] else {}),
                    **({"_fallback": True} if fallback_used else {}),
                )
                for r in rows
            ]
            if result:
                result.insert(0, page_notice(
                    total, skip, len(result),
                    hint=_page_hint(name, skip + len(result)),
                ))
            elif skip and total:
                result.append({"info": (
                    f"No symbol at offset {skip}; the answer holds {total}."
                )})
            if _suggestions:
                result.append({"_did_you_mean": _suggestions})
            return result

        from ..shared.variants import run_scoped_query

        return run_scoped_query(root, db_path, _do_lookup, variant or "", image or "")
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]

# ── moved from server.py ──
