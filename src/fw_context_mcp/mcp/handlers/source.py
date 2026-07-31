"""Source MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...indexer import db as index_db
from ...indexer.db import (
    find_indirect_call_sites,
    find_refs,
    get_active_config,
    get_llm_analysis_for_symbol,
    get_overrides_for_method,
    lookup_macro,
)
from ...llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async
from ...utils import abs_path, read_file_lines
from ..shared.context import _open_db_safe, _resolve_context

log = logging.getLogger(__name__)
def _validate_path_in_root(resolved_path: str, root: Path) -> str | None:
    """Validate that *resolved_path* relative to *root* stays within the project.

    Returns an error message string on failure, or ``None`` on success.
    Used by ``read_file`` and ``get_file_map`` for consistent path-security checks.
    """
    try:
        Path(root, resolved_path).resolve().relative_to(root.resolve())
    except ValueError:
        return f"Path escapes project root: {resolved_path}"
    return None


# ── Truncation limits ──
_SOURCE_TRUNCATE_CHARS = 8000   # max chars for get_source / get_symbol_context body
_EXPLAIN_SNIPPET_CHARS = 4000   # max chars for explain_symbol source snippet
_CONTEXT_LINES_MAX = 200        # max context_lines parameter for explain_symbol
_MAX_SYMBOL_BODY_LINES = 1000   # hard cap on _read_symbol_body to prevent runaway reads

# ── moved from server.py ──
def _lookup_definition(
    conn,
    config_hash: str,
    name: str,
    *,
    preferred_kinds: tuple[str, ...] | None = ("class", "struct"),
    prefer_project: bool = False,
):
    """Find the best-matching symbol definition, trying exact then suffix match.

    Tries exact name match (preferring definitions), then qualified name match.
    Falls back to suffix-filtering by short name when ``::`` is present and
    the full name didn't match (e.g. ``Foo::bar`` → match ``bar``, then
    filter by ``qualified_name`` ending with ``Foo::bar``).

    When *preferred_kinds* is set, a ``CASE s.kind`` expression is prepended
    to ``ORDER BY`` so each listed kind sorts before any other kind.  Callers
    that need a specific kind (e.g. ``get_inheritance_chain`` needs
    ``("class", "struct")``) should pass it explicitly.  Pass ``None`` for
    bare ``ORDER BY s.line`` (no kind priority).

    When *prefer_project* is True, ``s.is_project DESC`` is inserted before
    the kind-priority clause so project-code symbols (``is_project=1``) sort
    before vendor/SDK symbols.  This prevents base-class definitions in SDK
    headers from shadowing project overrides when both share the same name.
    """
    _order_parts: list[str] = []
    if preferred_kinds:
        for k in preferred_kinds:
            if not isinstance(k, str) or "'" in k:
                raise ValueError(
                    f"Invalid preferred_kinds value: {k!r}. "
                    f"Must be a valid symbol kind string (no quotes)."
                )
        whens = " ".join(f"WHEN '{k}' THEN 0" for k in preferred_kinds)
        _order_parts.append(f"CASE s.kind {whens} ELSE 1 END")
    if prefer_project:
        _order_parts.append("s.is_project DESC")
    _order_prefix = ", ".join(_order_parts) + ", " if _order_parts else ""

    QUERY = f"""SELECT s.* FROM symbols s
       WHERE s.config_hash=? AND %s
       ORDER BY {_order_prefix}%s s.line"""

    result = _lookup_try_columns(
        conn, QUERY + " LIMIT 1", config_hash, name,
        ("s.name", "s.qualified_name"),
    )
    if result:
        return result

    if "::" in name:
        short_name = name.rsplit("::", 1)[-1]
        return _lookup_try_columns(
            conn, QUERY, config_hash, short_name,
            ("s.name", "s.qualified_name"),
            suffix_filter=name,
        )
    return None


def _lookup_try_columns(
    conn,
    query_template: str,
    config_hash: str,
    search_name: str,
    columns: tuple[str, ...],
    *,
    suffix_filter: str | None = None,
):
    """Try *search_name* against each column in *columns*.

    For each column, tries ``is_definition=1`` first, then without the filter
    (with ``is_definition DESC`` sort).  Returns the first matching row or
    ``None``.

    When *suffix_filter* is set, only rows whose ``qualified_name`` ends with
    that string are returned.
    """
    for column in columns:
        for is_def, replace in ((True, ""), (False, "s.is_definition DESC,")):
            where = f"{column}=? AND s.is_definition=1" if is_def else f"{column}=?"
            rows = conn.execute(
                query_template % (where, replace),
                (config_hash, search_name),
            ).fetchall()
            for row in rows:
                if suffix_filter is None or row["qualified_name"].endswith(suffix_filter):
                    return row
    return None

# ── moved from server.py ──
def _read_symbol_body(file_path: str, line_no: int, end_line: int = 0, max_lines: int = 400) -> str:
    """Read up to *max_lines* lines around *line_no* without loading the entire file.

    When *end_line* is provided (from libclang) it is used as the exact body
    boundary.  Otherwise brace-matching finds the closing ``}``.
    """
    try:
        p = Path(file_path)
        if not p.exists():
            return ""
        # Read only the needed window — avoid loading huge generated files
        start_idx = max(0, line_no - 1)
        read_start = max(0, start_idx - 5)  # small margin for brace matching
        read_end = end_line + 5 if end_line else start_idx + max_lines + 5
        read_end = min(read_end, start_idx + _MAX_SYMBOL_BODY_LINES)
        from itertools import islice

        with p.open(errors="replace") as fh:
            window = [line.rstrip("\n\r") for line in islice(fh, read_start, read_end)]
    except OSError:
        return ""

    if start_idx < read_start or not window:
        return ""
    local_start = start_idx - read_start
    if local_start < 0 or local_start >= len(window):
        return ""

    if end_line and end_line >= line_no:
        local_end = min(len(window) - 1, end_line - 1 - read_start)
        return "\n".join(f"{read_start + i + 1:4d}  {window[i]}" for i in range(local_start, local_end + 1))

    # Brace matching with basic string/comment awareness.
    # Tracks whether we're inside a string literal, char literal, or comment
    # to avoid miscounting braces in those contexts.
    #
    # LIMITATION: C++11 raw string literals (R"(...)" and R"delim(...)delim")
    # are NOT handled — braces and quotes inside them may cause miscounts.
    # Embedded C/C++ code rarely uses raw strings, so this is acceptable.
    NORMAL, STRING, CHAR, LINE_COMMENT, BLOCK_COMMENT = 0, 1, 2, 3, 4
    state = NORMAL
    depth = 0
    seen_open = False
    local_end = local_start
    for i in range(local_start, len(window)):
        line = window[i]
        j = 0
        while j < len(line):
            ch = line[j]
            if state == NORMAL:
                if ch == '"':
                    state = STRING
                elif ch == "'":
                    state = CHAR
                elif ch == '/' and j + 1 < len(line) and line[j + 1] == '/':
                    state = LINE_COMMENT
                    j += 1
                elif ch == '/' and j + 1 < len(line) and line[j + 1] == '*':
                    state = BLOCK_COMMENT
                    j += 1
                elif ch == '{':
                    depth += 1
                    seen_open = True
                elif ch == '}':
                    depth -= 1
            elif state == STRING:
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == '"':
                    state = NORMAL
            elif state == CHAR:
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == "'":
                    state = NORMAL
            elif state == LINE_COMMENT:
                # Line comment ends at end of line
                pass
            elif state == BLOCK_COMMENT:
                if ch == '*' and j + 1 < len(line) and line[j + 1] == '/':
                    state = NORMAL
                    j += 1
            j += 1
        # Line comments reset at end of line
        if state == LINE_COMMENT:
            state = NORMAL
        local_end = i
        if seen_open and depth <= 0 and state == NORMAL:
            break
    if not seen_open:
        local_end = min(len(window) - 1, local_start + 2)
    return "\n".join(f"{read_start + i + 1:4d}  {window[i]}" for i in range(local_start, local_end + 1))

# ── moved from server.py ──
async def explain_symbol(
    name: Annotated[str, Field(description="Symbol name to explain. E.g. 'uart_init', 'ModemMsg::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    context_lines: Annotated[int, Field(description="Lines of source context around the symbol definition.")] = 40,
) -> dict:
    """Explain what a C/C++ symbol does in plain English — libclang-aware
    analysis. Uses pre-computed LLM analysis when available (instant),
    falls back to on-demand LLM. Falls back to macro explanation when
    the name matches a ``#define``.

    Read-only. No side effects — uses pre-computed LLM analysis when available
    (instant, generated during ``fw-context index --analyze``), falls back to
    calling an LLM on-demand. Returns the symbol's purpose, inputs, outputs,
    and side effects.

    For raw source code use ``get_source``. For symbol metadata without
    explanation use ``lookup_symbol``. For body + callers + callees use
    ``get_symbol_context``.

    Args:
        name: Symbol name to explain. E.g. ``uart_init``, ``ModemMsg::send``.
        project_root: Project root directory. Auto-detected if omitted.
        context_lines: Lines of source context around the symbol definition
            (default 40, max 200). Only used when no pre-computed analysis exists.

    Returns:
        dict: {name, kind, file, line, signature, explanation, llm_analysis
        (if pre-computed)}, plus source/explain_prompt on fallback. Macro
        fallback returns ``kind="macro"``, ``signature`` (as ``#define NAME``),
        ``value`` (raw definition), and ``expanded_value``.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            row = _lookup_definition(conn, config_hash, name, prefer_project=True)
            if not row:
                # Macro fallback
                macros = lookup_macro(conn, config_hash, name, exact=True, limit=1)
                if macros:
                    m = macros[0]
                    return {
                        "name": m["name"],
                        "kind": "macro",
                        "file": abs_path(root, m["file_path"]),
                        "line": m["line"],
                        "signature": f"#define {m['name']}",
                        "source": f"#define {m['name']} {m['value']}",
                        **({"value": m["value"]}),
                        **({"expanded_value": m["expanded_value"]} if m["expanded_value"] else {}),
                    }
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            # Defense-in-depth: validate path is within project root
            try:
                from pathlib import Path as _Path
                _Path(file_path).resolve().relative_to(root.resolve())
            except ValueError:
                return {"error": f"Path {file_path} outside project root"}
            line_no = row["line"]
            signature = row["signature"] or ""
            kind = row["kind"]
            symbol_id = row["id"]

            # Check for pre-computed LLM analysis (instant, no Ollama call)
            llm_analysis = get_llm_analysis_for_symbol(conn, symbol_id)
    finally:
        pass  # connection managed by connection.py cache

    result: dict = {
        "name": name,
        "kind": kind,
        "file": file_path,
        "line": line_no,
        "signature": signature,
    }

    # Use pre-computed analysis if available (instant response)
    if llm_analysis:
        explanation = llm_analysis["summary"]
        if llm_analysis["inputs"]:
            explanation += f"\n\nInputs: {llm_analysis['inputs']}"
        if llm_analysis["outputs"]:
            explanation += f"\nOutputs: {llm_analysis['outputs']}"
        result["explanation"] = explanation
        result["llm_analysis"] = llm_analysis
        return result

    context_lines = min(context_lines, _CONTEXT_LINES_MAX)
    source_snippet = ""
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
        start = max(0, line_no - context_lines - 1)
        end = min(len(lines), line_no + context_lines)
        numbered = "\n".join(
            f"{i + start + 1:4d}  {lines[i + start]}" for i in range(end - start)
        )
        source_snippet = numbered
    except (IndexError, ValueError):
        pass
    prompt = (
        f"You are a C/C++ embedded firmware expert.\n"
        f"Explain what the following {kind} does. "
        f"Cover: (1) what it does and why, (2) key mechanism or logic, (3) when/how it fits in the system.\n\n"
        f"Symbol: {name}\n"
        f"File: {file_path}:{line_no}\n"
        f"Signature: {signature}\n"
    )
    if source_snippet:
        prompt += f"\nSource context:\n```cpp\n{source_snippet}\n```\n"
    if cfg.llm.enabled:
        try:
            result["explanation"] = await asyncio.wait_for(
                call_ollama_async(prompt, cfg.llm),
                timeout=cfg.llm.timeout,
            )
        except asyncio.TimeoutError:
            result["warning"] = (
                f"LLM request timed out after {cfg.llm.timeout:.0f}s. "
                "No local LLM — interpret the 'source' and "
                "'explain_prompt' fields below with your own LLM to provide the explanation."
            )
        except OllamaModelNotFoundError as e:
            result["warning"] = (
                f"{e}. No LLM model available — interpret the 'source' and "
                f"'explain_prompt' fields below with your own LLM to provide the explanation."
            )
        except OllamaError as e:
            result["warning"] = (
                f"LLM unavailable: {e}. No local LLM — interpret the 'source' and "
                f"'explain_prompt' fields below with your own LLM to provide the explanation."
            )
        else:
            return result
        result["source"] = source_snippet[:_EXPLAIN_SNIPPET_CHARS] if len(source_snippet) > _EXPLAIN_SNIPPET_CHARS else source_snippet
    result["explain_prompt"] = prompt
    return result

# ── moved from server.py ──
def get_source(
    name: Annotated[str, Field(description="Fully qualified symbol name. Returns exact function body via libclang extent.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Read a C/C++ function/method/enum/macro body using libclang exact
    extents — no guessing line numbers. Uses AST-precise {start, end}
    extents so you get exactly the function body. Generic file readers
    don't know where a function actually ends — libclang tracks exact
    {start, end} from the AST.

    For enums, includes a ``constants`` array listing all member constants
    with their values. For macros, returns kind="macro" with ``value``
    (raw definition) and ``expanded_value`` (preprocessor-resolved).

    For rich context (who calls this, what does it call) use
    ``get_symbol_context`` instead — it returns body, callers, and callees
    in a single call. For the full file, use a normal file read.

    Read-only. No side effects.

    Args:
        name: Fully qualified symbol name. Returns exact function body
            via libclang extent.
        project_root: Project root. Auto-detected if omitted.

    Returns:
        dict: {name, qualified_name, kind, file, line, signature,
        docstring, is_definition, is_template, is_virtual, is_pure_virtual,
        source (str — the function/enum/macro body, truncated at 8000 chars),
        warning (str, optional — when source file cannot be read)}.
        May also include ``template_usr``, ``parent_usr``, ``enum_value``,
        ``constants`` (list for enums), ``value`` (raw macro definition),
        ``expanded_value`` (preprocessor-resolved macro value) when applicable.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            row = _lookup_definition(conn, cfg_data["config_hash"], name, prefer_project=True)
            if not row:
                # Macro fallback
                macros = lookup_macro(conn, cfg_data["config_hash"], name, exact=True, limit=1)
                if macros:
                    m = macros[0]
                    return {
                        "name": m["name"],
                        "kind": "macro",
                        "file": abs_path(root, m["file_path"]),
                        "line": m["line"],
                        "signature": f"#define {m['name']}",
                        "source": f"#define {m['name']} {m['value']}",
                        **({"value": m["value"]}),
                        **({"expanded_value": m["expanded_value"]} if m["expanded_value"] else {}),
                    }
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            # Defense-in-depth: validate path is within project root
            try:
                from pathlib import Path as _Path
                _Path(file_path).resolve().relative_to(root.resolve())
            except ValueError:
                return {"error": f"Path {file_path} outside project root"}
            result: dict = {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "file": file_path,
                "line": row["line"],
                "signature": row["signature"] or "",
                "is_definition": bool(row["is_definition"]),
                "is_template": bool(row["is_template"]),
                "is_virtual": bool(row["is_virtual"]),
                "is_pure_virtual": bool(row["is_pure_virtual"]),
                "docstring": row["docstring"] or "",
            }
            if row["template_usr"]:
                result["template_usr"] = row["template_usr"]
            if row["parent_usr"]:
                result["parent_usr"] = row["parent_usr"]
            if row["enum_value"] is not None:
                result["enum_value"] = row["enum_value"]
            # For enums, collect all constants with their values
            if row["kind"] == "enum":
                qn = row["qualified_name"]
                const_rows = conn.execute(
                    """SELECT name, qualified_name, line, enum_value
                       FROM symbols
                       WHERE config_hash = ? AND kind = 'enum_constant'
                         AND (qualified_name LIKE ? OR qualified_name LIKE ?)
                       ORDER BY line""",
                    (cfg_data["config_hash"], f"{qn}::%", f"%{qn}::%"),
                ).fetchall()
                if const_rows:
                    result["constants"] = [
                        {
                            "name": c["name"],
                            **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
                        }
                        for c in const_rows
                    ]
            end_line = row["end_line"] or 0
            line_no = row["line"]
    finally:
        pass  # connection managed by connection.py cache
    source = _read_symbol_body(file_path, line_no, end_line=end_line)
    if not source:
        result["warning"] = f"Could not read source from {file_path}"
    else:
        result["source"] = source[:_SOURCE_TRUNCATE_CHARS] if len(source) > _SOURCE_TRUNCATE_CHARS else source
    return result

# ── moved from server.py ──
def get_file_map(
    file_path: Annotated[str, Field(description="Path to source file — relative to project root or just filename.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    signatures: Annotated[bool, Field(description="Include full function signatures in output.")] = False,
    max_per_kind: Annotated[int, Field(description="Max items per symbol kind group (default 30, 0 = unlimited).")] = 30,
) -> dict:
    """Fast structural map of all C/C++ symbols in a file grouped by kind —
    libclang-powered table of contents. Like a table of contents before
    NOTE: No path-traversal validation — the DB stores only relative paths
    from indexing, so absolute-path escaping is not a realistic attack vector.
    reading a chapter: see what functions, classes, and enums a file
    defines at a glance.

    Pass a path relative to the project root (``src/main.cpp``) or just the
    filename (``main.cpp``). Returns symbols keyed by kind (function, method,
    class, struct, enum, ...). Each kind has count (total) and items (first N,
    default 30). Set max_per_kind=0 for unlimited, signatures=true for full sigs.

    Enum constants (``enum_constant``) are grouped into ``subgroups`` by
    parent enum. Each subgroup has ``name``, ``count``, and ``constants``
    (list of ``{name, qualified_name, line, enum_value}``). The subgroup
    count reflects the real total even when ``max_per_kind`` limits the
    constants list.

    For detailed symbol information use ``get_symbol_context`` or
    ``lookup_symbol``.

    Read-only. No side effects. Use before reading a large file to
    orient yourself — see what functions, classes, and enums it defines.

    Args:
        file_path: Path relative to project root, or just the filename.
        project_root: Project directory. Auto-detected if omitted.
        signatures: Include full function signatures. Default: False.
        max_per_kind: Max items per kind group (default 30, 0 = unlimited).

    Returns:
        dict: {file, total_symbols, symbols: {kind: {count, items[],
        subgroups?[]}}}
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            # Resolve file_path: try exact match first, then suffix
            exact = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND file_path=?",
                (config_hash, file_path),
            ).fetchone()[0]
            if not exact:
                # Try to find the canonical path from the files table
                candidates = conn.execute(
                    "SELECT path FROM files WHERE config_hash=? AND path LIKE ? LIMIT 3",
                    (config_hash, f"%{file_path}"),
                ).fetchall()
                if not candidates:
                    return {"error": f"File not found in index: {file_path}. Check the path — use relative paths like 'src/main.cpp'."}
                # Pick the best match (shortest path that ends with file_path)
                resolved = min((c["path"] for c in candidates), key=len)
            else:
                resolved = file_path
            # Defense-in-depth: validate resolved path stays within project root
            err = _validate_path_in_root(resolved, root)
            if err:
                return {"error": err}
            result = index_db.get_file_map(
                conn, config_hash, resolved,
                signatures=signatures, max_per_kind=max_per_kind,
            )
    finally:
        pass  # connection managed by connection.py cache
    return result

# ── moved from server.py ──
def get_symbol_context(
    name: Annotated[str, Field(description="Symbol name. Returns body, signature, all direct callers and callees.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Rich one-shot context for a C/C++ symbol: body, signature, all direct
    callers and callees. Answers "what does this do and how does it fit in
    the system?" in a single response — libclang powers the call graph,
    not regex. Falls back to macro display when the symbol is not found.

    Prefer this over ``get_source`` when you also need callers, callees,
    indirect call sites, or LLM analysis — all returned in a single call.
    If you only need the raw function body (no metadata), ``get_source`` is
    slightly faster. For transitive call-graph exploration use
    ``find_all_callers_recursive`` or ``find_callees_recursive``.

    Returns ALL callers and callees including vendor/SDK code — the call
    graph naturally spans project and vendor boundaries in both directions
    (project → vendor API, vendor callback → project handler).

    Read-only. No side effects.

    Args:
        name: Symbol name. Returns body, signature, all direct callers
            and callees.
        project_root: Project root. Auto-detected if omitted.

    Returns:
        dict with: name, qualified_name, kind, file, line, signature,
        docstring (raw Doxygen comment text), is_definition, callers (list),
        callees (list), source (body text),
        indirect_call_sites (list, for field/variable symbols — where the
        function pointer is actually invoked).
        For field and variable symbols that have function pointer type,
        also includes ``resolution``: {assignments_found, call_sites_found,
        resolved, note} indicating whether assignments and call sites are
        linked (Phase 3).  ``resolved=False`` with a note when parts are
        missing — LLM can detect uncertainty.
        For enums also returns constants and enum_value.
        For macros returns ``kind="macro"``, ``value`` (raw definition), and
        ``expanded_value`` (preprocessor-resolved).
        When LLM analysis has been generated (``fw-context index --analyze``),
        includes ``llm_analysis``: {summary, inputs, outputs, model, analyzed_at}
        with a structured description of the symbol's purpose, parameters, and
        return values/side effects.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            row = _lookup_definition(conn, config_hash, name, prefer_project=True)
            if not row:
                # Macro fallback — macros have no callers/callees, return basic info
                macros = lookup_macro(conn, config_hash, name, exact=True, limit=1)
                if macros:
                    m = macros[0]
                    return {
                        "name": m["name"],
                        "kind": "macro",
                        "file": abs_path(root, m["file_path"]),
                        "line": m["line"],
                        "signature": f"#define {m['name']}",
                        "source": f"#define {m['name']} {m['value']}",
                        "value": m["value"],
                        "expanded_value": m["expanded_value"] or None,
                        "callers": [],
                        "callees": [],
                    }
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            # Defense-in-depth: validate path is within project root
            try:
                from pathlib import Path as _Path
                _Path(file_path).resolve().relative_to(root.resolve())
            except ValueError:
                return {"error": f"Path {file_path} outside project root"}
            symbol_usr = row["usr"]

            # Immediate callers (who calls this symbol, direct + indirect).
            # Returns ALL callers — the call graph naturally spans project
            # and vendor boundaries in both directions (project → vendor API,
            # vendor callback → project handler).  Filtering would distort
            # the call graph and break transitive closure.
            callers = find_refs(conn, config_hash, name, ref_kind=["call", "indirect"])
            callers_list = [
                {"name": c["caller_name"] or "?", "qualified_name": c["caller_qname"] or "",
                 "file": abs_path(root, c["caller_file"] or c["from_file"]),
                 "line": c["from_line"], "kind": c["caller_kind"] or "",
                 "ref_kind": c["ref_kind"]}
                for c in callers
            ]

            # Immediate callees (what this symbol calls, direct + indirect).
            callees_rows = conn.execute(
                """SELECT s.name, s.qualified_name, s.kind, s.file_path, r.ref_kind
                   FROM refs r
                   JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
                   WHERE r.from_usr = ? AND r.config_hash = ?
                     AND r.ref_kind IN ('call', 'indirect')""",
                (symbol_usr, config_hash),
            ).fetchall()
            callees_list = [
                {"name": c["name"], "qualified_name": c["qualified_name"] or "",
                 "kind": c["kind"], "file": abs_path(root, c["file_path"]),
                 "ref_kind": c["ref_kind"]}
                for c in callees_rows
            ]

            # Indirect call sites — for function pointer fields and variables
            indirect_calls_list: list[dict] = []
            if row["kind"] in ("field", "variable", "varglobal", "varlocal"):
                ics_rows = find_indirect_call_sites(
                    conn, config_hash, row["qualified_name"] or row["name"], limit=200
                )
                indirect_calls_list = [
                    {
                        "file": abs_path(root, r["from_file"]),
                        "line": r["from_line"],
                        "expr_text": r["expr_text"],
                        "fn_ptr_type": r["fn_ptr_type"],
                        "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                    }
                    for r in ics_rows
                ]
            elif row["kind"] in ("function", "method"):
                # Indirect calls made BY this function (via from_usr),
                # e.g. handlers[irq](arg), driver->onData(args), cb(args).
                ics_rows = conn.execute(
                    """SELECT ics.*, caller.name AS caller_name,
                              caller.qualified_name AS caller_qname,
                              caller.kind AS caller_kind
                       FROM indirect_call_sites ics
                       LEFT JOIN symbols caller
                         ON caller.config_hash = ics.config_hash
                        AND caller.usr = ics.from_usr
                       WHERE ics.config_hash = ? AND ics.from_usr = ?
                       ORDER BY ics.from_file, ics.from_line
                       LIMIT 200""",
                    (config_hash, symbol_usr),
                ).fetchall()
                indirect_calls_list = [
                    {
                        "file": abs_path(root, r["from_file"]),
                        "line": r["from_line"],
                        "expr_text": r["expr_text"],
                        "fn_ptr_type": r["fn_ptr_type"],
                        "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                    }
                    for r in ics_rows
                ]

            # Resolution info — for function pointer fields/variables
            resolution: dict | None = None
            if row["kind"] in ("field", "variable", "varglobal", "varlocal"):
                # Count assignments to this field
                assign_count = conn.execute(
                    "SELECT COUNT(*) FROM fp_assignments WHERE config_hash = ? AND lhs_usr = ?",
                    (config_hash, symbol_usr),
                ).fetchone()[0]
                ics_count = len(indirect_calls_list)
                resolved = assign_count > 0 and ics_count > 0
                if assign_count > 0 or ics_count > 0:
                    note_parts: list[str] = []
                    if assign_count > 0:
                        note_parts.append(f"{assign_count} function(s) assigned")
                    else:
                        note_parts.append("no assignments found")
                    if ics_count > 0:
                        note_parts.append(f"{ics_count} call site(s)")
                    else:
                        note_parts.append("no call sites found")
                    if resolved:
                        note_parts.append("fully resolved")
                    else:
                        note_parts.append(
                            "not fully resolved — "
                            "assignment may be in unindexed code or through type-erased API"
                            if assign_count == 0
                            else "call sites may be in unindexed code"
                        )
                    resolution = {
                        "assignments_found": assign_count,
                        "call_sites_found": ics_count,
                        "resolved": resolved,
                        "note": "; ".join(note_parts),
                    }

            # For enums, collect all constants with their values
            enum_constants: list[dict] = []
            if row["kind"] == "enum":
                qn = row["qualified_name"]
                const_rows = conn.execute(
                    """SELECT name, qualified_name, line, enum_value
                       FROM symbols
                       WHERE config_hash = ? AND kind = 'enum_constant'
                         AND (qualified_name LIKE ? OR qualified_name LIKE ?)
                       ORDER BY line""",
                    (config_hash, f"{qn}::%", f"%{qn}::%"),
                ).fetchall()
                enum_constants = [
                    {
                        "name": c["name"],
                        **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
                    }
                    for c in const_rows
                ]
            # Pre-computed LLM analysis (if available)
            llm_analysis = get_llm_analysis_for_symbol(conn, row["id"])
            # Override info for virtual methods
            overrides_info = None
            if row["kind"] in ("method", "destructor") and (row["is_virtual"] or row["is_pure_virtual"]):
                overrides_info = get_overrides_for_method(conn, config_hash, symbol_usr)
    finally:
        pass  # connection managed by connection.py cache

    source = _read_symbol_body(file_path, row["line"], end_line=row["end_line"] or 0)
    result: dict = {
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "file": file_path,
        "line": row["line"],
        "signature": row["signature"] or "",
        "is_definition": bool(row["is_definition"]),
        "is_template": bool(row["is_template"]),
        "is_virtual": bool(row["is_virtual"]),
        "is_pure_virtual": bool(row["is_pure_virtual"]),
        "callers": callers_list,
        "callees": callees_list,
        "indirect_call_sites": indirect_calls_list,
        "docstring": row["docstring"] or "",
    }
    if resolution:
        result["resolution"] = resolution
    if row["template_usr"]:
        result["template_usr"] = row["template_usr"]
    if row["parent_usr"]:
        result["parent_usr"] = row["parent_usr"]
    if row["enum_value"] is not None:
        result["enum_value"] = row["enum_value"]
    if enum_constants:
        result["constants"] = enum_constants
    if llm_analysis:
        result["llm_analysis"] = llm_analysis
    if overrides_info:
        result["overrides"] = overrides_info["overrides"]
        result["overridden_by"] = overrides_info["overridden_by"]
    if source:
        result["source"] = source[:_SOURCE_TRUNCATE_CHARS] if len(source) > _SOURCE_TRUNCATE_CHARS else source
    return result


# ── moved from server.py ──
def read_file(
    file_path: Annotated[str, Field(description="Path to source file — relative to project root or just filename.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Read a complete C/C++ source file with **ifdef-filtered** content —
    only code that actually compiles for the current build configuration.
    Inactive ``#ifdef`` branches are replaced with blank lines (preserving
    original line numbers).

    Use this to read a file without leaving the fw-context ecosystem.
    Unlike generic file readers, this tool returns build-accurate content:
    code gated behind ``#ifdef BOARD_V2`` stays visible only when
    ``BOARD_V2`` is actually defined for this build.  Line numbers match
    the original file — inactive branches appear as blank lines.

    For reading a single function body with libclang exact extents use
    ``get_source``.  For body + callers + callees in one call use
    ``get_symbol_context``.  For a structural overview without content use
    ``get_file_map``.  For searching patterns across files use
    ``search_content``.

    Read-only. No side effects.  Falls back to raw disk content (with a
    warning) when the indexed ``files.content`` column is empty — e.g. on
    a legacy index that predates this feature.  Run ``fw-context index``
    to populate the ifdef-filtered content.

    Args:
        file_path: Path relative to project root, or just the filename.
            E.g. ``src/main.cpp`` or ``main.cpp``.
        project_root: Project root. Auto-detected if omitted.

    Returns:
        dict: {file (str), language (str — ``"c"`` or ``"cpp"``),
        mtime (float), lines (int — total line count),
        content (str — the complete ifdef-filtered file text),
        warning (str, optional — when reading from raw disk instead of
        indexed content)}.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]

            # Resolve file_path: try exact match first, then suffix match
            exact = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=? AND path=?",
                (config_hash, file_path),
            ).fetchone()[0]
            if not exact:
                candidates = conn.execute(
                    "SELECT path FROM files WHERE config_hash=? AND path LIKE ? LIMIT 3",
                    (config_hash, f"%{file_path}"),
                ).fetchall()
                if not candidates:
                    return {
                        "error": f"File not found in index: {file_path}. "
                        f"Check the path — use relative paths like 'src/main.cpp'."
                    }
                resolved = min((c["path"] for c in candidates), key=len)
            else:
                resolved = file_path

            # Defense-in-depth: validate resolved path stays within project root
            err = _validate_path_in_root(resolved, root)
            if err:
                return {"error": err}

            row = conn.execute(
                "SELECT content, language, path, mtime FROM files WHERE config_hash=? AND path=?",
                (config_hash, resolved),
            ).fetchone()
    finally:
        pass  # connection managed by connection.py cache

    if not row:
        return {"error": f"File not found in index: {file_path}"}

    content = row["content"] or ""
    result: dict = {
        "file": abs_path(root, row["path"]),
        "language": row["language"],
        "mtime": row["mtime"],
    }

    if content:
        lines_list = content.splitlines()
        result["lines"] = len(lines_list)
        result["content"] = content
    else:
        # Fallback: raw disk read for legacy indexes without ifdef-filtered content
        disk_lines = read_file_lines(abs_path(root, resolved))
        if disk_lines is None:
            return {"error": f"Could not read file: {abs_path(root, resolved)}"}
        result["lines"] = len(disk_lines)
        result["content"] = "".join(disk_lines)
        result["warning"] = (
            "Raw disk content — ifdef-filtered content not available. "
            "Run 'fw-context index' to populate build-accurate content."
        )

    return result
