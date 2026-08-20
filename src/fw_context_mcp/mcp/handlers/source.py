"""MCP tool handlers that serve source-code and symbol-metadata queries.

Each handler translates a user-facing MCP tool call into one or more
index lookups against the libclang-powered SQLite symbol database and
returns structured, tool-ready results.

Handlers in this module:
  - explain_symbol    — human-readable explanation of a symbol's purpose
  - get_source        — exact function/enum body via libclang extents
  - get_symbol_context — body + callers + callees + indirect calls in one
  - get_file_map      — structural table of contents for a source file
  - read_file         — ifdef-filtered file content, line numbers preserved

Execution pattern (shared by all handlers):

1. Resolve the project database context (config hash + root path) via
   ``BaseHandler.resolve_db_context``.
2. Define a ``_query`` closure that runs **under** the executor lock on
   the single shared SQLite connection — no handler opens its own
   connection.  Timeout enforcement is delegated to ``_wrap_tool``
   (300 s + interrupt).
3. Execute ``_query`` via ``db.executor.execute_sync``, which serialises
   all DB access to prevent SQLite "database is locked" errors.
4. Post-process the raw query result: read source bodies from disk,
   format output keys, attach optional metadata (LLM analysis, override
   info, enum constants).

Private helper functions provide reusable building blocks:

  - _lookup_definition    — multi-tier best-match symbol lookup with
                            kind-priority and project-priority ordering
  - _lookup_try_columns   — column-level search strategy: try each column,
                            definition-first then any-match with
                            definition-descent sort
  - _read_symbol_body     — windowed file reading with brace-matching
                            fallback; avoids loading huge generated files
  - _try_macro_fallback   — #define macro lookup that returns a
                            standardised result dict callers can chain
  - _validate_path_in_root — path-security guard used by read_file and
                            get_file_map
  - _collect_*            — callers, callees, indirect calls, enum
                            constants, override metadata, and Phase 3
                            resolution info
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...indexer import db as index_db
from ...indexer.db import (
    find_indirect_call_sites,
    find_refs,
    get_llm_analysis_for_symbol,
    get_overrides_for_method,
    lookup_macro,
)
from ...llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async
from ...utils import abs_path, read_file_lines
from ..shared.context import _normalize_file_path_query
from ._base import BaseHandler

log = logging.getLogger(__name__)
def _validate_path_in_root(resolved_path: str, root: Path) -> str | None:
    """Check that *resolved_path* relative to *root* stays within the project.

    Returns an error message string on failure, or ``None`` on success.
    Uses ``Path.resolve().relative_to()`` which resolves symlinks and ``..``
    components — a path like ``../../etc/passwd`` is caught even if it
    appears to be under the project root.

    Used by ``read_file`` and ``get_file_map`` for consistent path-security
    checks before any disk read.
    """
    try:
        Path(root, resolved_path).resolve().relative_to(root.resolve())
    except ValueError:
        return f"Path escapes project root: {resolved_path}"
    return None


# ── Truncation limits ──
# These prevent blowing up MCP tool responses with multi-megabyte source
# bodies.  LLMs have token-budget constraints; returning the full body of a
# 2000-line function wastes tokens and slows tool-calling iteration.
# Context windows are shared across all tools in a conversation, so every
# byte saved here is a byte the user's LLM can use for actual reasoning.
_SOURCE_TRUNCATE_CHARS = 8000   # max chars for get_source / get_symbol_context body
_EXPLAIN_SNIPPET_CHARS = 4000   # max chars for explain_symbol source snippet
_CONTEXT_LINES_MAX = 200        # max context_lines parameter for explain_symbol
_MAX_SYMBOL_BODY_LINES = 1000   # fallback cap on _read_symbol_body; config overrides via IndexConfig.max_symbol_body_lines


def _get_max_body_lines() -> int:
    """Return the max body line cap from config, falling back to _MAX_SYMBOL_BODY_LINES."""
    try:
        from fw_context_mcp import config
        cfg = config.load()
        return cfg.index.max_symbol_body_lines
    except (ValueError, TypeError, RuntimeError, OSError):
        return _MAX_SYMBOL_BODY_LINES

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

    QUERY_WHERE = "SELECT s.* FROM symbols s WHERE s.config_hash=? AND %s"
    QUERY_ORDER = f"ORDER BY {_order_prefix}%s s.line"

    result = _lookup_try_columns(
        conn, f"{QUERY_WHERE} {QUERY_ORDER} LIMIT 1", config_hash, name,
        ("s.name", "s.qualified_name"),
    )
    if result:
        return result

    if "::" in name:
        short_name = name.rsplit("::", 1)[-1]
        result = _lookup_try_columns(
            conn, f"{QUERY_WHERE} {QUERY_ORDER}", config_hash, short_name,
            ("s.name", "s.qualified_name"),
            suffix_filter=name,
        )
        if result:
            return result
        # Plain-name fallback: e.g. "zbox::NoinitStruct" where the symbol
        # is global (qualified_name == name, no namespace prefix).
        result = _lookup_try_columns(
            conn, f"{QUERY_WHERE} AND s.qualified_name = s.name {QUERY_ORDER} LIMIT 1",
            config_hash, short_name,
            ("s.name",),
        )
        if result:
            return result
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
    """Try *search_name* against each column in *columns*, returning the first match.

    Strategy (per-column, two-pass):
    1. Exact-column match with ``is_definition=1`` — prefer the definition site
       over forward declarations.
    2. Exact-column match without the definition filter, sorted with
       ``is_definition DESC`` — gets any match when no definition exists.

    When *suffix_filter* is set, only rows whose ``qualified_name`` ends with
    that string survive.  This is used by ``_lookup_definition`` to
    disambiguate ``Foo::bar`` from an unrelated ``bar`` in another namespace.

    Returns ``None`` when no row matches across all columns.  This function is
    intentionally single-purpose — callers compose it with their own
    ``query_template`` for kind-priority ordering, project-priority sorting,
    and result limits.
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
        read_end = min(read_end, start_idx + _get_max_body_lines())
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

    # Brace matching with basic string/comment awareness —
    # delegated to the shared brace_matcher module.
    from fw_context_mcp.mcp.shared.brace_matcher import find_closing_brace

    local_end = find_closing_brace(window, local_start)
    return "\n".join(f"{read_start + i + 1:4d}  {window[i]}" for i in range(local_start, local_end + 1))


# ── shared macro fallback ──
def _try_macro_fallback(
    conn: sqlite3.Connection, config_hash: str, name: str, root: Path,
    *, include_empty_refs: bool = False,
) -> dict | None:
    """Look up *name* as a macro and return a standardised result dict.

    Returns ``None`` when no matching macro is found so callers can chain
    with ``if (m := _try_macro_fallback(...)): return m``.

    When *include_empty_refs* is True, empty ``callers`` and ``callees``
    lists are added so the result shape matches ``get_symbol_context``
    output (which always has these keys).  ``explain_symbol`` and
    ``get_source`` set this to False — they do not emit caller/callee keys.
    """
    macros = lookup_macro(conn, config_hash, name, exact=True, limit=1)
    if not macros:
        return None
    m = macros[0]
    result: dict = {
        "name": m["name"],
        "kind": "macro",
        "file": abs_path(root, m["file_path"]),
        "line": m["line"],
        "signature": f"#define {m['name']}",
        "source": f"#define {m['name']} {m['value']}",
        "value": m["value"],
    }
    if m["expanded_value"]:
        result["expanded_value"] = m["expanded_value"]
    if include_empty_refs:
        result["callers"] = []
        result["callees"] = []
    return result
# ── moved from server.py ──
async def explain_symbol(
    name: Annotated[str, Field(description="Symbol name to explain. E.g. 'uart_init', 'ModemMsg::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    context_lines: Annotated[int, Field(description="Lines of source context around the symbol definition.")] = 40,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        dict: {name, kind, file, line, signature, explanation, llm_analysis
        (if pre-computed)}, plus source/explain_prompt on fallback. Macro
        fallback returns ``kind="macro"``, ``signature`` (as ``#define NAME``),
        ``value`` (raw definition), and ``expanded_value``.

        A ``warning`` key means that the local LLM gave no explanation —
        the request timed out, or the model is not available.  The dict then
        holds ``source`` and ``explain_prompt``: read the source, and answer
        the prompt yourself.

        On failure the dict holds only ``error`` with the reason.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return {"error": str(e)}
    cfg = db.cfg
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        row = _lookup_definition(conn, config_hash, name, prefer_project=True)
        if not row:
            # Macro fallback
            if (m := _try_macro_fallback(conn, config_hash, name, root)):
                return m
            return {"error": f"Symbol not found: {name}"}
        file_path = abs_path(root, row["file_path"])
        # Defense-in-depth: validate path is within project root
        try:
            from pathlib import Path as _Path
            _Path(file_path).resolve().relative_to(root.resolve())
        except ValueError:
            return {"error": f"Path {file_path} outside project root"}
        # Check for pre-computed LLM analysis (instant, no Ollama call)
        llm_analysis = get_llm_analysis_for_symbol(conn, row["id"])
        return row, file_path, llm_analysis

    query_result = db.executor.execute_sync(_query, db.config_hash)
    if isinstance(query_result, dict):
        return query_result
    row, file_path, llm_analysis = query_result
    line_no = row["line"]
    signature = row["signature"] or ""
    kind = row["kind"]

    result: dict = {
        "name": name,
        "kind": kind,
        "file": file_path,
        "line": line_no,
        "signature": signature,
    }

    # Pre-computed LLM analysis (from `fw-context index --analyze`) is
    # instant — no Ollama network call.  Return immediately when available
    # so the caller gets a response in <1 ms instead of 10–30 s.
    if llm_analysis:
        explanation = llm_analysis["summary"]
        if llm_analysis["inputs"]:
            explanation += f"\n\nInputs: {llm_analysis['inputs']}"
        if llm_analysis["outputs"]:
            explanation += f"\nOutputs: {llm_analysis['outputs']}"
        result["explanation"] = explanation
        result["llm_analysis"] = llm_analysis
        return result

    # No pre-computed analysis — fall through to the on-demand path.
    # Build a source-context window around the symbol so the LLM can
    # see what the code actually does, not just the signature.
    context_lines = max(1, min(context_lines, _CONTEXT_LINES_MAX))
    source_snippet = ""
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
        start = max(0, line_no - context_lines - 1)
        end = min(len(lines), line_no + context_lines)
        numbered = "\n".join(
            f"{i + start + 1:4d}  {lines[i + start]}" for i in range(end - start)
        )
        source_snippet = numbered
    except (IndexError, ValueError, OSError):
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
        # Three distinct error types form a progressive-degradation strategy:
        # - TimeoutError  → user knows the request completed (just too slow)
        # - ModelNotFound → actionable: install the model or switch to cloud
        # - OllamaError   → generic failure (server down, OOM, etc.)
        # Each produces a warning + raw data so the user's LLM can still
        # interpret the symbol from source + prompt without our server.
        try:
            result["explanation"] = await asyncio.wait_for(
                call_ollama_async(prompt, cfg.llm),
                timeout=cfg.llm.timeout,
            )
        except TimeoutError:
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
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        dict: {name, qualified_name, kind, file, line, signature,
        docstring, is_definition, is_template, is_virtual, is_pure_virtual,
        source (str — the function/enum/macro body, truncated at 8000 chars),
        warning (str, optional — when source file cannot be read)}.
        May also include ``template_usr``, ``parent_usr``, ``enum_value``,
        ``constants`` (list for enums), ``value`` (raw macro definition),
        ``expanded_value`` (preprocessor-resolved macro value) when applicable.

        On failure the dict holds only ``error`` with the reason.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return {"error": str(e)}
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        row = _lookup_definition(conn, config_hash, name, prefer_project=True)
        if not row:
            # Macro fallback
            if (m := _try_macro_fallback(conn, config_hash, name, root)):
                return m
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
        # For enums, collect all member constants.
        # Two LIKE patterns cover both unqualified (EnumName::CONST) and
        # namespaced (prefix::EnumName::CONST, e.g. inner enum in a class).
        # SQLite GLOB/REGEXP would be cleaner but LIKE is indexed for FTS5
        # prefix scans and avoids a second table lookup per enum.
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
            if const_rows:
                result["constants"] = [
                    {
                        "name": c["name"],
                        **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
                    }
                    for c in const_rows
                ]
        return result, file_path, row["line"], row["end_line"] or 0

    query_result = db.executor.execute_sync(_query, db.config_hash)
    if isinstance(query_result, dict):
        # Early-return path from the closure: macro fallback or error dict.
        return query_result
    result, file_path, line_no, end_line = query_result
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
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> dict:
    """Fast structural map of all C/C++ symbols in a file grouped by kind —
    libclang-powered table of contents. Like a table of contents before
    reading a chapter: see what functions, classes, and enums a file
    defines at a glance.

    Paths are validated against the project root before read —
    :func:`_validate_path_in_root` ensures resolved paths stay within bounds.

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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        dict: {file, total_symbols, symbols: {kind: {count, items[],
        subgroups?[]}}}

        On failure the dict holds only ``error`` with the reason.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return {"error": str(e)}
    root = db.root
    file_path = _normalize_file_path_query(file_path)

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        # Path resolution: exact match first, then suffix LIKE fallback.
        # Users often pass just "main.cpp" or "src/main.cpp" — the suffix
        # match finds the canonical path without requiring the full relative
        # prefix.  When multiple candidates exist, choose the shortest path
        # because it is the least-nested (most canonical) match.
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
        return index_db.get_file_map(
            conn, config_hash, resolved,
            signatures=signatures, max_per_kind=max_per_kind,
        )

    return db.executor.execute_sync(_query, db.config_hash)

# ── moved from server.py ──
# ── get_symbol_context collectors ───────────────────────────────────


def _collect_callers(
    conn: sqlite3.Connection, config_hash: str, name: str, root: Path
) -> list[dict]:
    """Return immediate callers (direct + indirect) for a symbol."""
    callers = find_refs(conn, config_hash, name, ref_kind=["call", "indirect"])
    return [
        {"name": c["caller_name"] or "?", "qualified_name": c["caller_qname"] or "",
         "file": abs_path(root, c["caller_file"] or c["from_file"]),
         "line": c["from_line"], "kind": c["caller_kind"] or "",
         "ref_kind": c["ref_kind"]}
        for c in callers
    ]


def _collect_callees(
    conn: sqlite3.Connection, config_hash: str, symbol_usr: str, root: Path
) -> list[dict]:
    """Return immediate callees (direct + indirect calls made by this symbol)."""
    callees_rows = conn.execute(
        """SELECT s.name, s.qualified_name, s.kind, s.file_path, r.ref_kind
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.from_usr = ? AND r.config_hash = ?
             AND r.ref_kind IN ('call', 'indirect')""",
        (symbol_usr, config_hash),
    ).fetchall()
    return [
        {"name": c["name"], "qualified_name": c["qualified_name"] or "",
         "kind": c["kind"], "file": abs_path(root, c["file_path"]),
         "ref_kind": c["ref_kind"]}
        for c in callees_rows
    ]


def _collect_indirect_calls(
    conn: sqlite3.Connection,
    config_hash: str,
    row: sqlite3.Row,
    symbol_usr: str,
    root: Path,
) -> list[dict]:
    """Return indirect call sites for a symbol — dual-purpose by symbol kind.

    For **field/variable** kinds (function-pointer fields): returns call sites
    where this field is actually invoked (e.g. ``driver.onData(buf, len)``).
    Uses ``find_indirect_call_sites`` which queries the Phase 3 link table.

    For **function/method** kinds: returns indirect call sites made BY this
    function — calls through function pointers that originate inside this
    function's body (e.g. ``stored_callback(42)`` inside ``dispatch_event``).

    Returns an empty list for all other kinds (enum, class, struct, etc.).
    """
    kind = row["kind"]
    if kind in ("field", "variable", "varglobal", "varlocal"):
        ics_rows = find_indirect_call_sites(
            conn, config_hash, row["qualified_name"] or row["name"], limit=200
        )
        return [
            {
                "file": abs_path(root, r["from_file"]),
                "line": r["from_line"],
                "expr_text": r["expr_text"],
                "fn_ptr_type": r["fn_ptr_type"],
                "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
            }
            for r in ics_rows
        ]
    if kind in ("function", "method"):
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
        return [
            {
                "file": abs_path(root, r["from_file"]),
                "line": r["from_line"],
                "expr_text": r["expr_text"],
                "fn_ptr_type": r["fn_ptr_type"],
                "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
            }
            for r in ics_rows
        ]
    return []


def _collect_resolution_info(
    conn: sqlite3.Connection,
    config_hash: str,
    row: sqlite3.Row,
    symbol_usr: str,
    indirect_calls_list: list[dict],
) -> dict | None:
    """Build Phase 3 resolution metadata for function-pointer symbols.

    Phase 3 links function-pointer *assignments* (who is attached to this
    field?) with *call sites* (where is this field actually invoked?).  When
    both sides are populated the resolution is "fully resolved" — the LLM
    (and the user) can trace the data flow from registration through
    invocation.  When one side is missing, the `note` explains why
    (assignments in unindexed code, type-erased API, etc.).

    Returns ``None`` when the symbol is not a function-pointer-bearing
    field/variable (kinds other than field/variable/varglobal/varlocal)
    OR when neither assignments nor call sites exist — no need to emit
    empty resolution metadata.
    """
    kind = row["kind"]
    if kind not in ("field", "variable", "varglobal", "varlocal"):
        return None
    assign_count = conn.execute(
        "SELECT COUNT(*) FROM fp_assignments WHERE config_hash = ? AND lhs_usr = ?",
        (config_hash, symbol_usr),
    ).fetchone()[0]
    ics_count = len(indirect_calls_list)
    if assign_count == 0 and ics_count == 0:
        return None
    resolved = assign_count > 0 and ics_count > 0
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
            + ("assignment may be in unindexed code or through type-erased API"
               if assign_count == 0
               else "call sites may be in unindexed code")
        )
    return {
        "assignments_found": assign_count,
        "call_sites_found": ics_count,
        "resolved": resolved,
        "note": "; ".join(note_parts),
    }


def _collect_enum_constants(
    conn: sqlite3.Connection, config_hash: str, row: sqlite3.Row
) -> list[dict]:
    """Return enum member constants (name + value) for an enum symbol.

    Uses two LIKE patterns to match both unqualified constants
    (``EnumName::CONST``) and namespaced ones (``prefix::EnumName::CONST``).
    LIKE is preferred over GLOB/REGEXP because SQLite can optimise it with
    the FTS5-backed index prefix scan, a pattern repeated in ``get_source``.
    """
    if row["kind"] != "enum":
        return []
    qn = row["qualified_name"]
    const_rows = conn.execute(
        """SELECT name, qualified_name, line, enum_value
           FROM symbols
           WHERE config_hash = ? AND kind = 'enum_constant'
             AND (qualified_name LIKE ? OR qualified_name LIKE ?)
           ORDER BY line""",
        (config_hash, f"{qn}::%", f"%{qn}::%"),
    ).fetchall()
    return [
        {
            "name": c["name"],
            **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
        }
        for c in const_rows
    ]


def _collect_override_info(
    conn: sqlite3.Connection, config_hash: str, row: sqlite3.Row
) -> dict | None:
    """Return virtual-method override metadata when applicable.

    Only methods and destructors marked ``is_virtual`` or ``is_pure_virtual``
    trigger this lookup — non-virtual methods return ``None`` immediately.
    The override data (``overrides`` → base-class method, ``overridden_by``
    → derived-class methods) is stored in the ``overrides`` table populated
    during ``fw-context index`` post-processing of the inheritance graph.
    """
    kind = row["kind"]
    if kind not in ("method", "destructor"):
        return None
    if not (row["is_virtual"] or row["is_pure_virtual"]):
        return None
    return get_overrides_for_method(conn, config_hash, row["usr"])


# ── moved from server.py ──
def get_symbol_context(
    name: Annotated[str, Field(description="Symbol name. Returns body, signature, all direct callers and callees.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

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

        The dict also carries the libclang flags of the symbol:
        is_virtual, is_pure_virtual, is_template, parent_usr, and
        template_usr.  For a virtual method it adds ``overrides`` (the base
        methods that this method overrides) and ``overridden_by`` (the
        derived methods that override it) — use these two before you change
        a virtual method.

        On failure the dict holds only ``error`` with the reason.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return {"error": str(e)}
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        row = _lookup_definition(conn, config_hash, name, prefer_project=True)
        if not row:
            # Macro fallback — macros have no callers/callees, return basic info
            if (m := _try_macro_fallback(conn, config_hash, name, root, include_empty_refs=True)):
                return m
            return {"error": f"Symbol not found: {name}"}
        file_path = abs_path(root, row["file_path"])
        # Defense-in-depth: validate path is within project root
        try:
            from pathlib import Path as _Path
            _Path(file_path).resolve().relative_to(root.resolve())
        except ValueError:
            return {"error": f"Path {file_path} outside project root"}
        symbol_usr = row["usr"]

        # Assemble six independent data sources inside a single DB transaction
        # (via execute_sync) so the caller gets a complete picture in one
        # response — avoids N+1 MCP round-trips for callers, callees, etc.
        # Each collector is a separate function for readability; the DB lock
        # is held for the entire _query closure so the snapshot is consistent.

        # Immediate callers (who calls this symbol, direct + indirect).
        callers_list = _collect_callers(conn, config_hash, name, root)

        # Immediate callees (what this symbol calls, direct + indirect).
        callees_list = _collect_callees(conn, config_hash, symbol_usr, root)

        # Indirect call sites — for function pointer fields and variables,
        # and indirect calls made BY functions/methods.
        indirect_calls_list = _collect_indirect_calls(conn, config_hash, row, symbol_usr, root)

        # Resolution info — for function pointer fields/variables
        resolution = _collect_resolution_info(conn, config_hash, row, symbol_usr, indirect_calls_list)

        # For enums, collect all constants with their values
        enum_constants = _collect_enum_constants(conn, config_hash, row)

        # Pre-computed LLM analysis (if available)
        llm_analysis = get_llm_analysis_for_symbol(conn, row["id"])

        # Override info for virtual methods
        overrides_info = _collect_override_info(conn, config_hash, row)

        return (
            row, file_path, callers_list, callees_list, indirect_calls_list,
            resolution, enum_constants, llm_analysis, overrides_info,
        )

    query_result = db.executor.execute_sync(_query, db.config_hash)
    if isinstance(query_result, dict):
        # Early-return path from the closure: macro fallback or error dict.
        return query_result
    (
        row, file_path, callers_list, callees_list, indirect_calls_list,
        resolution, enum_constants, llm_analysis, overrides_info,
    ) = query_result

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
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        dict: {file (str), language (str — ``"c"`` or ``"cpp"``),
        mtime (float), lines (int — total line count),
        content (str — the complete ifdef-filtered file text),
        warning (str, optional — when reading from raw disk instead of
        indexed content)}.

        On failure the dict holds only ``error`` with the reason.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return {"error": str(e)}
    root = db.root
    file_path = _normalize_file_path_query(file_path)

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.

        # Path resolution: exact match first, then suffix LIKE fallback
        # (same strategy as get_file_map — users often pass bare filenames).
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
        return resolved, row

    query_result = db.executor.execute_sync(_query, db.config_hash)
    if isinstance(query_result, dict):
        # Early-return path from the closure: error dict.
        return query_result
    resolved, row = query_result

    if not row:
        return {"error": f"File not found in index: {file_path}"}

    content = row["content"] or ""
    result: dict = {
        "file": abs_path(root, row["path"]),
        "language": row["language"],
        "mtime": row["mtime"],
    }

    if content:
        # Normal path: ifdef-filtered content is available from the index.
        # This is the preferred code path — inactive #ifdef branches are
        # already stripped, line numbers are preserved as blank lines.
        lines_list = content.splitlines()
        result["lines"] = len(lines_list)
        result["content"] = content
    else:
        # Legacy index fallback: older indexes (pre-ifdef-filtering) have an
        # empty content column.  Read from raw disk instead and emit a
        # warning so the user knows this is NOT build-accurate content.
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
