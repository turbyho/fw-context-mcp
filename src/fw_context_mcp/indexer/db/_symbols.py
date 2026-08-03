"""Symbol and macro storage / search routines for fw-context-mcp index."""

from __future__ import annotations

import logging
import re
import sqlite3

from fw_context_mcp.utils import is_db_exception

__all__ = [
    "_expand_query",
    "delete_macros_for_file",
    "find_macro_refs",
    "insert_macros_batch",
    "insert_symbols_batch",
    "lookup_macro",
    "search_symbols",
    "split_tokens",
]


log = logging.getLogger(__name__)

# Compiled regex patterns — module-level to avoid re-compilation on every call.
# _RE_UNNAMED strips anonymous struct/enum/union markers from libclang names.
_RE_UNNAMED = re.compile(r"\(unnamed\s+(struct|enum|union)\s+at\s+[^)]+\)")
# _RE_CAMEL splits camelCase: "onConnectionComplete" → "on Connection Complete"
_RE_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
# _RE_ACRONYM splits ACRONYMWords: "HTTPResponse" → "HTTP Response"
_RE_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
# _RE_SEPARATOR splits on any non-alphanumeric character (underscores, ::, etc.)
_RE_SEPARATOR = re.compile(r"[^a-zA-Z0-9]+")
# _RE_COL_FILTER detects column-filter syntax in FTS5 queries (single colon not part of ::)
_RE_COL_FILTER = re.compile(r"(?<!:):(?!:)")

# BM25 weight configuration — module-level constant to avoid recreating the
# weight list on every search_symbols() call (was previously inline).
#
# Each weight corresponds to a column in the symbols_fts FTS5 table.
# Higher weight → stronger influence on BM25 relevance ranking:
#
#   Column            Weight  Rationale
#   ────────────────  ──────  ─────────────────────────────────────────────
#   name               1.2    Slight boost — name matches are important but
#                              common words should not dominate.
#   qualified_name     0.75   De-emphasized — qualified name often repeats
#                              tokens already in 'name', avoid double-counting.
#   signature         10.0    Strongest signal — function signatures contain
#                              precise type/parameter info, exact match is a
#                              strong relevance indicator.
#   docstring          1.0    Neutral — docstrings are free-form text with
#                              variable quality; don't boost or penalize.
#   file_path          3.0    Moderate boost — matching the file name is a
#                              good signal for organizational relevance.
#   name_tokens        2.0    Slight boost — CamelCase/snake_case token
#                              decomposition provides extra match surface.
#   summary            1.0    Neutral — LLM-generated summaries (like docstrings).
#   inputs             5.0    Strong boost — LLM-analyzed input parameters
#                              are high-signal structured data.
#   outputs            1.0    Neutral — LLM-analyzed outputs, complementary
#                              to inputs but less discriminative.
#
# Extra columns beyond these 9 (e.g. 'source' body text when present) get
# weight 1.0 — they contribute but don't dominate the ranking.
_BASE_WEIGHTS = [1.2, 0.75, 10.0, 1.0, 3.0, 2.0, 1.0, 5.0, 1.0]
# Cache: PRAGMA table_info column count per connection id → avoids
# querying the schema on every search_symbols call.
_bm25_col_count_cache: dict[int, int] = {}

# Noise words that pollute FTS5 — strip before tokenizing (module-level, cached)
_NOISE_WORDS = frozenset(("at", "unnamed"))
# Guard against pathological inputs that cause ReDoS in regex processing (module-level, cached)
_MAX_NAME_LEN = 500


def split_tokens(name: str, qualified_name: str = "") -> str:
    """Normalize camelCase/snake_case names to space-separated lowercase tokens.

    Builds a searchable token graph in FTS5 where each camelCase component
    becomes an independent node, so ``connect*`` finds ``onConnectionComplete``,
    ``modem*`` finds ``ModemMsgManager``, etc.

    Examples:
        onConnectionComplete       → "on connection complete"
        modem_parser_oob_init      → "modem parser oob init"
        ZCfgDataManager            → "cfg data manager"
        HTTPResponse               → "http response"
        ZBLE::onConnectionComplete → "zble on connection complete"
        _last_ble_connected        → "last ble connected"
    """
    def _tokenize(s: str) -> list[str]:
        if len(s) > _MAX_NAME_LEN:
            log.debug("Truncated long name from %d to %d chars for tokenization", len(s), _MAX_NAME_LEN)
            s = s[:_MAX_NAME_LEN]
        # Strip anonymous struct/enum/union markers — these inject noise tokens
        # like "mbed", "include", "enum" that match thousands of irrelevant symbols.
        s = _RE_UNNAMED.sub("", s)
        s = _RE_CAMEL.sub(r"\1 \2", s)  # camelCase split
        s = _RE_ACRONYM.sub(r"\1 \2", s)  # HTTPResponse → HTTP Response
        parts = _RE_SEPARATOR.split(s)  # split on non-alnum
        return [p.lower() for p in parts if len(p) > 1 and p.lower() not in _NOISE_WORDS]

    tokens: list[str] = []
    seen: set[str] = set()
    for src in (name, qualified_name):
        if src:
            for tok in _tokenize(src):
                if tok not in seen:
                    seen.add(tok)
                    tokens.append(tok)
    # Semantic hints for well-known embedded conventions.
    # Cortex-M exception handlers use _Handler suffix (HardFault_Handler, NMI_Handler, ...)
    # and don't contain "irq"/"isr" in their names. Add searchable concept tokens.
    if name.endswith("_Handler"):
        for tag in ("interrupt", "exception", "isr"):
            if tag not in seen:
                seen.add(tag)
                tokens.append(tag)
    return " ".join(tokens)


def insert_symbols_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Insert symbol rows, promoting declaration→definition on USR conflict.

    Each row: (config_hash, file_id, file_path, name_tokens, usr, name,
               qualified_name, kind, line, col, end_line, is_definition,
               signature, docstring, enum_value, is_virtual, is_pure_virtual,
               parent_usr, is_template, template_usr, is_project, pagerank, source)

    Returns count of rows inserted or upgraded to definition.
    """

    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name, kind,
            line, col, end_line, is_definition, signature, docstring, enum_value,
            is_virtual, is_pure_virtual, parent_usr, is_template, template_usr, is_project, pagerank, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, usr) DO UPDATE SET
               file_id       = excluded.file_id,
               file_path     = excluded.file_path,
               name_tokens   = excluded.name_tokens,
               line          = excluded.line,
               col           = excluded.col,
               end_line      = excluded.end_line,
               is_definition = 1,
               signature     = excluded.signature,
               docstring     = excluded.docstring,
               enum_value    = excluded.enum_value,
               is_virtual    = excluded.is_virtual,
               is_pure_virtual = excluded.is_pure_virtual,
               parent_usr    = excluded.parent_usr,
               is_template   = excluded.is_template,
               template_usr  = excluded.template_usr,
               is_project    = excluded.is_project,
               pagerank      = excluded.pagerank,
               source        = excluded.source
           WHERE excluded.is_definition = 1 AND symbols.is_definition = 0""",
        rows,
    )
    return cur.rowcount  # approximate with ON CONFLICT DO UPDATE WHERE (used for logging only)


def insert_macros_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Insert macro rows, updating on (config_hash, file_id, line) conflict.

    Each row: (config_hash, file_id, name, value, expanded_value, line, is_function_like)

    Returns count of rows inserted or updated.
    """
    cur = conn.executemany(
        """INSERT INTO macros
           (config_hash, file_id, name, value, expanded_value, line, is_function_like)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, file_id, line) DO UPDATE SET
               name           = excluded.name,
               value          = excluded.value,
               expanded_value = CASE WHEN excluded.expanded_value != '' THEN excluded.expanded_value ELSE macros.expanded_value END,
               is_function_like = excluded.is_function_like""",
        rows,
    )
    return cur.rowcount  # approximate with ON CONFLICT DO UPDATE WHERE (used for logging only)


def delete_macros_for_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Delete all macros for a file (FTS ad trigger cleans up FTS index)."""
    conn.execute("DELETE FROM macros WHERE file_id=?", (file_id,))


def lookup_macro(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    exact: bool = False,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Look up macros by name — exact or prefix match.

    Returns rows from the ``macros`` table joined with ``files`` to get
    the file path.  Same three-tier name resolution as ``lookup_symbol``
    for symbols (exact name, exact name with definition preference,
    prefix LIKE).
    """
    limit = min(limit, 100)
    if exact:
        return conn.execute(
            """SELECT m.*, f.path AS file_path
               FROM macros m
               JOIN files f ON f.id = m.file_id
               WHERE m.config_hash = ? AND m.name = ?
               ORDER BY m.line
               LIMIT ?""",
            (config_hash, name, limit),
        ).fetchall()

    esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        r"""SELECT m.*, f.path AS file_path
           FROM macros m
           JOIN files f ON f.id = m.file_id
           WHERE m.config_hash = ? AND m.name LIKE ? ESCAPE '\'
           ORDER BY m.name, m.line
           LIMIT ?""",
        (config_hash, f"{esc}%", limit),
    ).fetchall()


def find_macro_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find file-level references to a macro using the files_fts index.

    Searches ifdef-filtered file content for occurrences of *name*.
    Returns file-level matches with highlighted snippets.
    """
    limit = min(limit, 100)
    table_row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files_fts'").fetchone()
    if table_row is None:
        return []

    expanded = _expand_query(name)
    try:
        return conn.execute(
            """SELECT f.path AS file_path, f.language,
                      snippet(files_fts, 1, '<b>', '</b>', '…', 80) AS _match_snippet
               FROM files_fts
               JOIN files f ON f.id = files_fts.rowid
               WHERE files_fts MATCH ? AND f.config_hash = ? AND f.content != ''
               ORDER BY rank
               LIMIT ?""",
            (expanded, config_hash, limit),
        ).fetchall()
    except Exception as exc:
        if not is_db_exception(exc):
            raise
        return []


def _expand_query(query: str, *, for_body_search: bool = False) -> str:
    """Add trailing wildcard to each bare word for broader prefix matching.

    Leaves existing wildcards (*) and FTS5 syntax (NEAR, ", parentheses,
    column filters with ``name_tokens : term*``) intact.  Single colons in
    column-filter syntax are detected via regex; C++ ``::`` passes through
    so its tokens get wildcard expansion.

    When *for_body_search* is True, the query is returned as-is — body
    search patterns like ``.attach(`` should not be wildcard-expanded.
    """
    if for_body_search:
        return query

    # Tokens that already are FTS5 syntax — don't touch them.
    # Single colon (not part of ::) covers column-filter expressions like
    # "name_tokens : term*" which would be corrupted by wildcard appending.
    _bare_syntax = ('"', "NEAR", "AND", "OR", "(", ")")
    _has_col_filter = _RE_COL_FILTER.search(query)
    if any(c in query for c in _bare_syntax) or _has_col_filter:
        return query

    parts = query.replace("::", " ").split()
    expanded = []
    for p in parts:
        if p.endswith("*"):
            expanded.append(p)
        else:
            expanded.append(f"{p}*")
    return " OR ".join(expanded)


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    limit: int = 20,
    kind: str | None = None,
    exclude_variables: bool = False,
    project_only: bool = False,
) -> list[sqlite3.Row]:
    """FTS5 search over symbols for a given build config.

    Bare words are expanded to trailing-wildcard prefix queries so that
    ``modem init`` matches ``modem_parser_oob_init``.
    When *kind* is given, the filter is applied in SQL (before LIMIT) so the
    caller reliably gets up to *limit* matching rows — filtering after the fact
    in Python would silently under-return.
    When *exclude_variables* is True, local/file-scope variables are excluded
    from results.  Set True in topic-search tools (``search_code``) to prevent
    low-signal entries from cluttering the top results; leave False in recall
    phases (hybrid / embedding search) where vector re-ranking handles relevance.
    When *project_only* is True, SDK/vendor paths are excluded
    (only src/, lib/, app/, include/ are retained).
    Note: file_path is read from the denormalized symbols.file_path column,
    not re-joined from files — the JOIN was removed to avoid the redundant
    round-trip and the shadowing hazard.
    """
    expanded = _expand_query(query)
    if kind:
        kind_filter = "AND s.kind = ?"
    elif exclude_variables:
        kind_filter = "AND s.kind NOT IN ('variable', 'varlocal')"
    else:
        kind_filter = ""
    if project_only:
        project_filter = "AND s.is_project = 1"
    else:
        project_filter = ""
    params: list = [expanded, config_hash]
    if kind:
        params.append(kind)
    params.append(limit)
    # Build bm25 weights — cache column count per connection to avoid
    # querying PRAGMA table_info on every search_symbols call.
    conn_id = id(conn)
    col_count = _bm25_col_count_cache.get(conn_id)
    if col_count is None:
        fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols_fts)").fetchall()]
        col_count = len(fts_cols)
        _bm25_col_count_cache[conn_id] = col_count
    n_extra = col_count - len(_BASE_WEIGHTS)
    weights = _BASE_WEIGHTS + [1.0] * max(0, n_extra)
    bm25_expr = f"bm25(symbols_fts, {', '.join(str(w) for w in weights)})"
    return conn.execute(
        f"""SELECT s.*
           FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH ? AND s.config_hash = ? {kind_filter} {project_filter}
            ORDER BY {bm25_expr}
           LIMIT ?""",
        params,
    ).fetchall()
