"""Variable MCP tools — see mcp/server.py for registration.

WHY a dedicated variable search tool instead of extending search_code:
variables need different metadata — enclosing function (for locals),
enclosing class (for static members), type signature, and references
(who reads/writes this variable?).  search_code returns symbol-level
metadata (name, file, line, docstring) but not the enclosing scope
or reference graph.  find_variables bridges this gap with batched
JOINs across the symbols, refs, and parent-usr relationships.

WHY parent_usr batch lookups: every local variable has a parent function
(class method, free function, or file scope).  Without batching, each
variable would incur a separate ``SELECT … FROM symbols WHERE usr=?``
query — O(n) SQL round-trips for n results.  The batch collects all
parent USRs into a single ``WHERE usr IN (?, ?, …)`` query.

WHY refs are capped at 30 per variable: global variables like errno
or HAL handles may have hundreds of references.  Returning all of
them would blow up the MCP response and provide diminishing value —
the assistant needs to see the pattern (who reads vs writes), not
every single site.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from ...utils import abs_path
from ..shared.paging import (
    call_text,
    clamp_offset,
    holds_past_end_info,
    page_hint,
    page_notice,
    past_end_info,
    selector_args,
)
from ..shared.stale import (
    _stale_files,
    annotate_stale,
    collect_result_paths,
    diagnose_empty_result,
)
from ._base import BaseHandler

log = logging.getLogger(__name__)

_VALID_KINDS = frozenset({"varglobal", "varlocal", "variable", "field"})
_VAR_KINDS = ("varglobal", "varlocal", "variable", "field")
_REFS_PER_VARIABLE = 30


def find_variables(
    name: Annotated[str, Field(description="Variable name or part of it. "
        "Substring match on name and qualified name (e.g. 'g_' finds g_debug_level, g_state).", min_length=1)],
    project_root: Annotated[str | None, Field(description="Project root. "
        "Auto-detected if omitted.")] = None,
    kind: Annotated[str | None, Field(description="Filter by kind: "
        "'varglobal', 'varlocal', 'field', legacy 'variable', or None for all.")] = None,
    limit: Annotated[int, Field(description="Maximum results "
        "(default 20, max 100).", ge=1)] = 20,
    offset: Annotated[int, Field(description="Skip this many variables. "
        "Reads the next page of a name that many variables share.", ge=0)] = 0,
    variant: Annotated[str | None, Field(description="Build variant (multi-build project). Omit to use default_variant. One query answers for ONE build.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image within the variant. Required when the variant holds several: each image is a separate program.")] = None,
) -> list[dict]:
    """Find C/C++ variables by name or part of it and trace who reads or
    writes them through the call graph.  libclang-powered: splits
    variables into global (``varglobal`` — file/namespace/class-scope)
    and local (``varlocal`` — inside a function body).

    Each result includes a type signature (``bool timeSet``,
    ``const IPAddress modbus_ip``), the enclosing function for locals
    (``"<file scope>"`` for globals), and a ``references`` list of the
    functions that read or write the variable — the same ``ref_kind``
    values as ``find_references`` (``"call"``, ``"ref"``, ``"member"``).
    The list is capped at 30 per variable, and holds only references of
    the selected build; ``references_total`` gives the whole count.  Only
    definitions are returned, globals first, then by name.

    Use when you need to understand shared state, find who modifies a
    global variable, trace side effects, or distinguish important globals
    from loop counters.  For general symbol search use ``search_code`` or
    ``lookup_symbol``.  For all references to a specific variable
    (including reads in expressions), use ``find_references``.

    This tool is the way to a LOCAL variable: ``search_code`` drops the
    ``varlocal`` kind, because a local matches every topic query aimed at
    the function around it.  ``search_code(..., kind="varlocal")`` reaches
    them as well.

    Legacy indexes with ``kind="variable"`` (pre-split) are detected and
    included in results — reindex to fully benefit from the split.

    Read-only. No side effects.

    Args:
        name: Variable name or part of it. Substring match on name and
            qualified name (e.g. ``g_`` finds ``g_debug_level``, ``g_state``).
        project_root: Project root directory. Auto-detected if omitted.
        kind: Optional kind filter — ``"varglobal"``, ``"varlocal"``,
            ``"field"``, or ``None`` (all). Default ``None``.  The legacy
            ``"variable"`` is also accepted, for an index made before the
            kind was split.
        limit: Maximum results (default 20, max 100).
        offset: Skip this many variables.  Reads the next page; the page
            notice names the offset to use.
        variant: Build variant (multi-build project). Omit to use
            default_variant. One query answers for ONE build.
        image: Sysbuild image within the variant. Required when the
            variant holds several: each image is a separate program.

    Returns:
        list of dicts, each with: name (str), qualified_name (str),
        kind (str — ``"varglobal"``, ``"varlocal"``, ``"field"``, or
        ``"variable"`` on an index made before the kind was split),
        file (str),
        line (int), signature (str — e.g. ``"const IPAddress modbus_ip"``),
        enclosing_function (str — function name for varlocal,
        ``"<file scope>"`` for varglobal), enclosing_class (str — class
        or struct name for fields and static members, empty otherwise),
        references (list[dict] — ``function``, ``file``, ``line``,
        ``ref_kind``; at most 30), references_total (int — every
        reference of the variable).  When the list is cut,
        ``references_hint`` names the ``find_references`` call that
        lists them all.

        The page notice ``{total, offset, shown, more, hint}`` comes
        before the rows of the answer.  ``total`` counts every variable that matches.  When
        ``more`` is true, the ``hint`` names the call that reads the next
        page.  A page after the last variable gives one ``info`` dict that
        names the total.

        No match gives ``[]``.  One dict with ``error`` means the query
        failed — check that key first.
        A ``warning`` dict that comes first means that indexed files
        changed after the last index run: the results can be out of date.
    """
    if not name.strip():
        return [{"error": "Variable name must be non-empty."}]
    if kind is not None and kind not in _VALID_KINDS:
        return [{"error": f"Invalid kind: {kind!r}. Expected 'varglobal', 'varlocal', 'field', 'variable', or None."}]
    limit = max(1, min(limit, 100))
    skip = clamp_offset(offset)

    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return [{"error": str(e)}]

    root = db.root

    def _do_find(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        if kind:
            kind_filter = "AND s.kind = ?"
        else:
            kind_filter = f"AND s.kind IN ({','.join('?' * len(_VAR_KINDS))})"

        esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list = [config_hash, f"%{esc_name}%", f"%{esc_name}%"]
        if kind:
            params.append(kind)
        else:
            params.extend(_VAR_KINDS)

        # One WHERE for the page and its count, thus ``total`` can never
        # describe a different answer than the rows.
        match = f"""s.config_hash = ?
                  AND (s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\')
                  {kind_filter}
                  AND s.is_definition = 1"""
        total = int(c.execute(f"SELECT COUNT(*) FROM symbols s WHERE {match}", params).fetchone()[0])
        # WHY ``s.usr`` ends the order: short names such as ``i``, ``ret``
        # and ``err`` tie on kind and name in every function, and SQLite
        # may give tied rows in any order.  ``usr`` is unique in one build.
        rows = c.execute(
            f"""SELECT s.* FROM symbols s
                WHERE {match}
                ORDER BY CASE s.kind WHEN 'varglobal' THEN 0 ELSE 1 END,
                         s.name, s.usr
                LIMIT ? OFFSET ?""",
            (*params, limit, skip),
        ).fetchall()

        if not rows:
            if skip and total:
                return [past_end_info("variable", skip, total)]
            return []

        rows_list = [dict(r) for r in rows]

        # Batch parent lookups
        parent_usrs = {r["parent_usr"] for r in rows_list if r.get("parent_usr")}
        parents: dict[str, dict] = {}
        if parent_usrs:
            placeholders = ",".join("?" * len(parent_usrs))
            parent_rows = c.execute(
                f"SELECT usr, name, qualified_name, kind FROM symbols "
                f"WHERE config_hash = ? AND usr IN ({placeholders})",
                (config_hash, *parent_usrs),
            ).fetchall()
            parents = {r["usr"]: dict(r) for r in parent_rows}

        # Batch refs lookups
        var_usrs = {r["usr"] for r in rows_list if r.get("usr")}
        refs_map: dict[str, list[dict]] = {}
        ref_totals: dict[str, int] = {}
        if var_usrs:
            placeholders = ",".join("?" * len(var_usrs))
            # The cap is per variable (ROW_NUMBER over to_usr): one cap over
            # all variables gave a later variable no reference at all.
            # ``ref_total`` counts every reference of the variable, thus the
            # result can say that the cap cut the list.  The order ends at
            # the refs rowid, as in find_references: two references on one
            # line tie on file and line.
            ref_rows = c.execute(
                f"""SELECT * FROM (
                        SELECT r.to_usr, r.from_file, r.from_line, r.ref_kind,
                               r.from_usr, c.name AS caller_name,
                               c.qualified_name AS caller_qname, c.kind AS caller_kind,
                               ROW_NUMBER() OVER (
                                   PARTITION BY r.to_usr
                                   ORDER BY r.from_file, r.from_line, r.rowid
                               ) AS rn,
                               COUNT(*) OVER (PARTITION BY r.to_usr) AS ref_total
                        FROM refs r
                        LEFT JOIN symbols c ON c.config_hash = r.config_hash
                            AND c.usr = r.from_usr AND c.is_definition = 1
                        WHERE r.config_hash = ? AND r.to_usr IN ({placeholders})
                    )
                    WHERE rn <= ?
                    ORDER BY from_file, from_line, rn""",
                (config_hash, *var_usrs, _REFS_PER_VARIABLE),
            ).fetchall()
            for ref in ref_rows:
                rdict = dict(ref)
                ref_totals[rdict["to_usr"]] = int(rdict["ref_total"])
                refs_map.setdefault(rdict["to_usr"], []).append({
                    "function": rdict.get("caller_qname") or rdict.get("caller_name") or "<unknown>",
                    "file": abs_path(root, rdict["from_file"]),
                    "line": rdict["from_line"],
                    "ref_kind": rdict["ref_kind"],
                })

        results = []
        for row in rows_list:
            parent_usr = row.get("parent_usr") or ""
            enclosing_function = "<file scope>"
            enclosing_class = ""

            if parent_usr:
                parent = parents.get(parent_usr)
                if parent:
                    pkind = parent["kind"]
                    if pkind in ("function", "method", "constructor", "destructor"):
                        enclosing_function = parent["qualified_name"] or parent["name"]
                    elif pkind in ("class", "struct", "union"):
                        enclosing_class = parent["qualified_name"] or parent["name"]

            var_usr = row["usr"]
            references = refs_map.get(var_usr, [])
            qualified_name = row["qualified_name"] or row["name"]
            entry = {
                "name": row["name"],
                "qualified_name": qualified_name,
                "kind": row["kind"],
                "file": abs_path(root, row["file_path"]),
                "line": row["line"],
                "signature": row["signature"] or "",
                "enclosing_function": enclosing_function,
                "enclosing_class": enclosing_class,
                "references": references,
                "references_total": ref_totals.get(var_usr, 0),
            }
            if entry["references_total"] > len(references):
                # find_references pages the whole list; this tool shows the
                # pattern of the first references only.  WHY no count in
                # the text: two static variables of one name in two files
                # share the qualified name, and find_references then answers
                # for both of them.
                entry["references_hint"] = (
                    f"{call_text('find_references', qualified_name, **selector_args(project_root, variant, image))} pages every reference."
                )
            results.append(entry)

        hint = page_hint(
            "find_variables", name, **({"kind": kind} if kind else {}),
            **selector_args(project_root, variant, image), next_offset=skip + len(results),
        )
        return [page_notice(total, skip, len(results), hint=hint), *results]

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        results = _do_find(conn, config_hash)
        file_paths = collect_result_paths(results, root)
        if file_paths:
            return results, _stale_files(conn, config_hash, file_paths, root), 0, []
        if holds_past_end_info(results):
            # The answer exists and is shorter than the offset: no empty
            # answer to diagnose.
            return results, [], 0, []
        # No variable found: diagnose the whole index instead, so an empty
        # answer over a changed tree does not read as proof of absence.
        dirty, new_sources = diagnose_empty_result(conn, config_hash, root)
        return results, [], dirty, new_sources

    results, stale, dirty, new_sources = db.executor.execute_sync(_query, db.config_hash)
    if stale or dirty:
        from fw_context_mcp.mcp.background import _ensure_daemon_running
        _ensure_daemon_running(root)
    return annotate_stale(
        results, stale, empty_dirty_count=dirty, empty_new_sources=new_sources
    )
