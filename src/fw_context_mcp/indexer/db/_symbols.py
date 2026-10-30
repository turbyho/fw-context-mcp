"""Symbol and macro storage / search routines for fw-context-mcp index.

Lookup strategy — three-tier resolution
───────────────────────────────────────
Every symbol/macro lookup follows a three-tier path designed to maximise
precision while tolerating the caller not knowing the exact qualified name:

  1. **Exact name match** — searches for the literal name the caller
     provides.  Fastest path; used when a tool already resolved the
     symbol name (e.g. from a hover event or a previous search result).
  2. **Exact qualified-name match** — when an unqualified name matches
     multiple symbols (e.g. ``send`` in both ``UART::send`` and
     ``SPI::send``), callers append ``ClassName::``.  This tier returns
     the single definitive match.
  3. **Prefix LIKE match** — when callers provide a partial name
     (``uart_`` finds ``uart_init``, ``uart_send``, ``uart_config``).
     Uses LIKE with ESCAPE so ``_`` and ``%`` in symbol names do not
     cause false matches.

Query building patterns
────────────────────────
**FTS5 for symbols** — ``search_symbols`` delegates to the
``symbols_fts`` virtual table.  Bare search terms are expanded to
trailing-wildcard prefix queries (``modem`` → ``modem*``) so a
single token matches ``modem_init``, ``modem_parser_oob_init``, etc.
Tokens are OR-joined to broaden recall.

**FTS5 for file content** — ``find_macro_refs`` delegates to
``files_fts``, searching ifdef-filtered file text.  The same
``_expand_query`` helper applies wildcard expansion so the caller
gets prefix matches by default.

**BM25 ranking** — per-column weights are tuned empirically for
embedded C/C++ codebases.  The signature column carries the highest
weight (10.0) because parameter types are a strong relevance signal.
Input parameter analysis from LLMs (weight 5.0) provides structured
data.  Qualified names are de-weighted (0.75) to prevent double-
counting tokens already present in the unqualified name.

**Sanitization** — a term may hold only ``[A-Za-z0-9_]`` plus one
trailing ``*`` to go to FTS5 bare; ``_fts5_safe_token`` puts anything
else into a quoted phrase (``#define``, ``.attach(``, a backslash from
a copy-pasted string literal).  ``_expand_token`` adds the prefix
wildcard on top for the name and content searches, and
``_sanitize_body_query`` leaves it out for the body search, which must
stay as precise as the caller wrote it.

Token decomposition
───────────────────
``split_tokens`` decomposes camelCase/snake_case/C++ qualified names
into lowercase tokens stored in the ``symbols.name_tokens`` column
and the ``symbols_fts`` FTS5 index.  This is the mechanism that lets
``connect*`` match ``onConnectionComplete`` and ``modem*`` match
``ModemManager``.  Anonymous struct/enum/union markers and single-
character noise tokens are stripped because they match thousands of
irrelevant symbols.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence

from fw_context_mcp.utils import is_db_exception

from ._chunking import chunked
from ._connection import ConnectionCache

__all__ = [
    "_expand_query",
    "count_macros",
    "delete_macros_for_files",
    "find_macro_refs",
    "insert_macros_batch",
    "insert_symbols_batch",
    "link_definition",
    "lookup_macro",
    "mark_stale_files",
    "search_symbols",
    "settle_stale_files",
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
# Cache: PRAGMA table_info column count per connection → avoids
# querying the schema on every search_symbols call.  See ConnectionCache
# for why it is not a dict keyed by id(conn).
_bm25_col_count_cache = ConnectionCache()

# Noise words that pollute FTS5 — strip before tokenizing (module-level, cached)
_NOISE_WORDS = frozenset(("at", "unnamed"))
# Guard against pathological inputs that cause ReDoS in regex processing (module-level, cached)
_MAX_NAME_LEN = 500


def split_tokens(name: str, qualified_name: str = "") -> str:
    """Normalize camelCase/snake_case names to space-separated lowercase tokens.

    Builds a searchable token graph in FTS5 where each camelCase component
    becomes an independent node, so ``connect*`` finds ``onConnectionComplete``,
    ``modem*`` finds ``ModemManager``, etc.

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


# The files whose rows the index run replaces or removes, and that it did
# not parse again yet (``mark_stale_files``), by ``files.id``.  A TEMP
# table: it belongs to the connection of one run, and an other connection
# never sees it.  WHY the id and not the path: a writer of a symbol row
# that spells the path in an other form cannot hide the row from the mark.
_STALE_FILES_DDL = "CREATE TEMP TABLE IF NOT EXISTS stale_files (file_id INTEGER PRIMARY KEY)"


def mark_stale_files(conn: sqlite3.Connection, config_hash: str, paths: Iterable[str]) -> None:
    """Mark the files *paths* of the build *config_hash* as stale: the run replaces or removes their rows.

    WHY: one row of ``symbols`` holds each USR, and the first definition
    wins.  When a definition moves to a file of an other unit (a body
    inline in a header, a function moved to an other `.cpp`), the parse of
    the new place can come before the delete of the old rows.  Without the
    mark, the upsert drops the new definition, and the delete then removes
    the symbol until a full run.  A row of a stale file gives way to any
    new row (``insert_symbols_batch``), as a row that the run did not
    insert yet.

    The runner marks, before the first parse, the files that the last run
    gave to the units that this run parses again and to the units that
    left the build, and the files that no unit owns.

    The first unit whose parse or store fails ends every mark
    (``settle_stale_files``): the unit keeps its rows, and the rows of
    the files that the run handed to it.  From there the upsert keeps the
    first definition, as an index run did before the marks.

    Limits:

    * A unit that the run does not parse gives no row again.  A deleted
      definition thus loses its symbol, also when an unchanged unit
      declares it (that build does not link).
    * After a failed unit, a definition that moved to a later unit, or to
      a file that the run hands to a later unit, can be lost as before.
      The next run that parses the unit with the moved definition repairs
      it.  An earlier unit can have replaced a row of the failed unit
      already; the next run that parses the failed unit repairs it.
    * A run that stops keeps each row that a new row replaced, for example
      a declaration in place of the definition of a changed unit that the
      run did not reach.  When that unit gets its earlier text back, the
      next run does not parse it, and the definition is lost until the
      unit changes again or a full run.
    """
    conn.execute(_STALE_FILES_DDL)
    conn.executemany(
        "INSERT OR IGNORE INTO stale_files (file_id) SELECT id FROM files WHERE config_hash = ? AND path = ?",
        [(config_hash, path) for path in paths],
    )


def settle_stale_files(conn: sqlite3.Connection, config_hash: str, paths: Iterable[str] | None = None) -> None:
    """End the stale mark of the files *paths* of the build *config_hash*, or of every file when *paths* is None.

    A unit settles the files whose rows it replaced: those rows are
    current, and the usual order of the upsert applies to them again.  A
    failed unit ends every mark (``mark_stale_files``).
    """
    conn.execute(_STALE_FILES_DDL)
    if paths is None:
        conn.execute("DELETE FROM stale_files")
    else:
        conn.executemany(
            "DELETE FROM stale_files WHERE file_id IN (SELECT id FROM files WHERE config_hash = ? AND path = ?)",
            [(config_hash, path) for path in paths],
        )


def _drop_old_data_of_stale_rows(conn: sqlite3.Connection, keys: Iterable[tuple[str, str]]) -> None:
    """Delete the old data of each row of a stale file that a key ``(config_hash, usr)`` names.

    The old data: the LLM analysis and its copy in ``symbols``
    (``summary``, ``inputs``, ``outputs``), the embeddings, the vectors,
    and the override edges whose derived end is the row.

    WHY: the upsert keeps the id of the row that it replaces, and moves the
    row to the file of the new row.  The delete of the stale file thus does
    not find the row.  Without this delete, the row keeps the data of the
    old text: a body that is gone, or the body of an other place.
    ``semantic_search`` then finds the row through the old vector, and
    ``search_code`` through the old analysis, which ``symbols_fts``
    indexes.  A method that now overrides nothing keeps the edge to its old
    base: no unit gives a row of the method that replaces the edge
    (``insert_overrides_batch``).

    The function removes the data as the delete of the file does:

    * The embeddings go to the stash first.  The embedding step takes a
      vector back when the text did not change (``take_stashed_embeddings``).
    * A unit writes its override edges after its symbols (``ops``).  The
      unit of the new row, or a later unit that holds the definition of
      the method, thus writes the current edges again.
    * The inheritance edges need no delete here: a unit deletes the edges
      of each class that it parses before it writes its symbols
      (``ops._delete_old_for_tu``), and only a definition of a class gets
      a row.
    """
    from ._embeddings import stash_embeddings_for_symbols

    found_rows: dict[int, tuple[str, str]] = {}
    for key in keys:
        found = conn.execute(
            "SELECT s.id FROM symbols s JOIN temp.stale_files r ON r.file_id = s.file_id"
            " WHERE s.config_hash = ? AND s.usr = ?",
            key,
        ).fetchone()
        if found is not None:
            found_rows[found[0]] = key
    if not found_rows:
        return
    conn.executemany("DELETE FROM overrides WHERE config_hash = ? AND derived_usr = ?", list(found_rows.values()))
    ids = list(found_rows)
    stash_embeddings_for_symbols(conn, ids)
    for chunk in chunked(ids):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM llm_analysis WHERE symbol_id IN ({marks})", chunk)
        conn.execute(f"UPDATE symbols SET summary = '', inputs = '', outputs = '' WHERE id IN ({marks})", chunk)
        conn.execute(f"DELETE FROM embeddings WHERE symbol_id IN ({marks})", chunk)
        try:
            conn.execute(f"DELETE FROM vec_symbols WHERE symbol_id IN ({marks})", chunk)
        except sqlite3.OperationalError as error:
            # sqlite-vec is not there: no table, or a table whose module
            # (vec0) this connection did not load (``_connection._load_sqlite_vec``).
            if "no such table" not in str(error) and "no such module" not in str(error):
                raise


def insert_symbols_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Insert symbol rows, promoting declaration→definition on USR conflict.

    Each row: (config_hash, file_id, file_path, name_tokens, usr, name,
               qualified_name, kind, line, col, end_line, is_definition,
               signature, docstring, enum_value, is_virtual, is_pure_virtual,
               parent_usr, is_template, template_usr, is_project, pagerank, source,
               is_weak[, link_name[, is_tentative]])

    A row without ``link_name`` has no link name: a writer of symbols that
    no linker resolves (a type, a test fixture) leaves it out.  A row
    without ``is_tentative`` is no tentative definition.

    Returns count of rows inserted or updated.

    Conflict resolution strategy
    ─────────────────────────────
    Libclang emits a USR per symbol per translation unit.  When a header
    declares ``int foo(int)`` and a .cpp file defines it, the indexer sees
    both as separate rows with the same USR.  The declaration row has
    ``is_definition=0``; the definition row has ``is_definition=1``.

    ``ON CONFLICT(config_hash, usr) DO UPDATE`` merges duplicates on the
    same USR.  The guard ``WHERE excluded.is_definition = 1 AND
    (symbols.is_definition = 0 OR (symbols.is_weak = 1 AND
    excluded.is_weak = 0))`` ensures:

      • A **declaration** arriving before a definition is silently
        overwritten when the definition row arrives later.
      • A **definition** arriving first stays untouched — the declaration
        row cannot downgrade it, unless its file is stale (below).
      • A **weak definition** gives way to a strong one of the same USR,
        as the linker does: the weak `HAL_GPIO_EXTI_Callback` of a HAL
        file and the strong one of the project are one USR, and the units
        come in the order of their paths, thus the stub can come first.
        The row keeps one definition, thus the stub is not in the index.
      • A **tentative definition** (`int g_x;` at file scope in C) gives
        way to a definition of one strength with an initializer
        (`int g_x = 5;`), as the linker merges a common symbol into it.
        Before, the row of the unit that came first won, by the order of
        the paths.
      • Two declarations for the same USR (e.g. from two headers), or two
        definitions of one strength, keep whichever arrived first.
      • A row of a **stale** file (``mark_stale_files``) gives way to any
        new row, a declaration too: the run replaces or removes the rows
        of that file, thus the row is not current.  The row keeps its id,
        and loses its old data first (``_drop_old_data_of_stale_rows``),
        as the delete of the file removes it.

    Without this guard, a declaration row arriving AFTER a current
    definition would overwrite ``is_definition`` back to 0 — breaking the
    call graph.
    """

    # The two last values are optional: (link_name, is_tentative).
    defaults = ("", 0)
    full = [(*row, *defaults[len(row) - 24:]) for row in rows]
    conn.execute(_STALE_FILES_DDL)
    if conn.execute("SELECT 1 FROM temp.stale_files LIMIT 1").fetchone() is not None:
        _drop_old_data_of_stale_rows(conn, [(row[0], row[4]) for row in full])
    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name, kind,
            line, col, end_line, is_definition, signature, docstring, enum_value,
            is_virtual, is_pure_virtual, parent_usr, is_template, template_usr, is_project, pagerank, source,
            is_weak, link_name, is_tentative)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, usr) DO UPDATE SET
               file_id       = excluded.file_id,
               file_path     = excluded.file_path,
               name_tokens   = excluded.name_tokens,
               line          = excluded.line,
               col           = excluded.col,
               end_line      = excluded.end_line,
               is_definition = excluded.is_definition,
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
               source        = excluded.source,
               is_weak       = excluded.is_weak,
               link_name     = excluded.link_name,
               is_tentative  = excluded.is_tentative
           WHERE (excluded.is_definition = 1 AND (
               symbols.is_definition = 0 OR (symbols.is_weak = 1 AND excluded.is_weak = 0)
               OR (symbols.is_tentative = 1 AND excluded.is_tentative = 0
                   AND excluded.is_weak <= symbols.is_weak)
           )) OR EXISTS (SELECT 1 FROM temp.stale_files r WHERE r.file_id = symbols.file_id)""",
        full,
    )
    return cur.rowcount  # approximate with ON CONFLICT DO UPDATE WHERE (used for logging only)


# The USR prefix of a symbol that a linker script defines.
_LINKER_SCRIPT_USR_PREFIX = "ld:"


def link_definition(
    conn: sqlite3.Connection, config_hash: str, link_name: str, *, declarations: bool = False,
) -> str | None:
    """Return the USR of the symbol that the linker gives *link_name*, or None.

    The candidates are the symbols whose link name is *link_name*
    (`symbols.link_name`): a `static` function, a C++ method and a local
    label have another link name or none, thus they are never one.  The
    ranks, as GNU ld takes them, in this order:

    1. A strong definition: in C, in C++, in assembly, or an assignment in
       a linker script.
    2. A weak definition of an object file (`.weak`, `__attribute__((weak))`
       when the index sees it).
    3. A `PROVIDE` of a linker script (stored weak): ld uses it only when no
       object defines the name.
    4. With *declarations*: a declaration, which stands for the symbol that
       the linker gets from elsewhere (a library).

    The language of a definition gives no rank: two definitions of one
    rank are a link error, or one of them is in a file that the link does
    not use, and the index does not hold which.  Then the answer is None,
    as when nothing matches: a wrong target is a claim about code that
    does not run.
    """
    if not link_name:
        return None
    rows = conn.execute(
        "SELECT usr, is_definition, is_weak FROM symbols "
        "WHERE config_hash=? AND link_name=?",
        (config_hash, link_name),
    ).fetchall()
    definitions = [row for row in rows if row["is_definition"]]
    weak = [row for row in definitions if row["is_weak"]]
    ranks = [
        [row for row in definitions if not row["is_weak"]],
        [row for row in weak if not str(row["usr"]).startswith(_LINKER_SCRIPT_USR_PREFIX)],
        [row for row in weak if str(row["usr"]).startswith(_LINKER_SCRIPT_USR_PREFIX)],
    ]
    if declarations:
        ranks.append([row for row in rows if not row["is_definition"]])
    for rank in ranks:
        found = {str(row["usr"]) for row in rank}
        if found:
            return found.pop() if len(found) == 1 else None
    return None


def insert_macros_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Insert macro rows, updating on (config_hash, file_id, line) conflict.

    Each row:
    (config_hash, file_id, name, value, params, expanded_value, line,
    is_function_like)

    Returns count of rows inserted or updated.

    The ``expanded_value`` merge uses ``CASE WHEN`` to preserve existing
    data: when a re-index overwrites the same file/line with a non-empty
    expanded_value, the new value replaces the old one.  When the incoming
    row has an empty expanded_value (e.g. the preprocessor could not
    resolve the macro), the existing value is kept.  Without this guard,
    a re-index of an unchanged file would lose resolved values obtained
    during the first pass.
    """
    cur = conn.executemany(
        """INSERT INTO macros
           (config_hash, file_id, name, value, params, expanded_value, line,
            is_function_like)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, file_id, line) DO UPDATE SET
               name           = excluded.name,
               value          = excluded.value,
               params         = excluded.params,
               expanded_value = CASE WHEN excluded.expanded_value != '' THEN excluded.expanded_value ELSE macros.expanded_value END,
               is_function_like = excluded.is_function_like""",
        rows,
    )
    return cur.rowcount  # approximate with ON CONFLICT DO UPDATE WHERE (used for logging only)



def delete_macros_for_files(conn: sqlite3.Connection, file_ids: Sequence[int]) -> None:
    """Delete all macros for *file_ids* (FTS ad trigger cleans up FTS index).

    WHY a plural form is needed: a translation unit owns more than its own
    file.  A ``#define`` in a header is stored under the HEADER's ``file_id``,
    so a delete keyed on the TU's file_id alone leaves those rows behind.  A
    macro removed from a header then survives, and a macro that only moved
    gains a SECOND row, because the UNIQUE key is
    ``(config_hash, file_id, line)`` — the ON CONFLICT upsert in
    ``insert_macros_batch`` only refreshes a macro that stayed on its line.

    No ``config_hash`` filter is needed: ``files`` is unique per
    ``(config_hash, path)``, so a file_id already identifies one config.
    """
    ids = list(dict.fromkeys(file_ids))  # dedupe, keep a stable order
    if not ids:
        return
    for chunk in chunked(ids):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM macros WHERE file_id IN ({placeholders})",
            chunk,
        )


_MACRO_EXACT_WHERE = "m.config_hash = ? AND m.name = ?"
_MACRO_PREFIX_WHERE = r"m.config_hash = ? AND m.name LIKE ? ESCAPE '\'"


def _macro_where(name: str, exact: bool) -> tuple[str, str, str]:
    r"""Give the WHERE text, the name parameter and the ORDER BY of a lookup.

    The count and the page must run on the SAME conditions.  Two copies of
    the text drift apart, and a ``total`` that describes another answer
    than the rows do is worse than no total at all.

    The ``ESCAPE '\'`` clause is required because a macro name legally
    holds underscores (common in embedded code — ``SYSTICK_HANDLER``), and
    ``_`` is the single-character wildcard of LIKE.  Without the escape it
    would match any character at that position.
    """
    if exact:
        return _MACRO_EXACT_WHERE, name, "ORDER BY m.line, m.file_id"
    esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _MACRO_PREFIX_WHERE, f"{esc}%", "ORDER BY m.name, m.line, m.file_id"


def count_macros(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    exact: bool = False,
) -> int:
    """Count the macros that ``lookup_macro`` answers with.

    Reports how many macros the name matches ALTOGETHER, thus a reader of
    one page knows whether more exist.
    """
    where, param, _order = _macro_where(name, exact)
    return conn.execute(
        f"SELECT COUNT(*) FROM macros m WHERE {where}",
        (config_hash, param),
    ).fetchone()[0]


def lookup_macro(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    exact: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Look up macros by name — exact or prefix match.

    Returns rows from the ``macros`` table joined with ``files`` to get
    the file path.  Same three-tier name resolution as ``lookup_symbol``
    for symbols (exact name, exact name with definition preference,
    prefix LIKE).

    The order ends at ``m.file_id``, which is unique for one macro name
    and line.  One name can be defined at the same line of two headers,
    and SQLite may return such tied rows in any order, thus an OFFSET
    over the name and the line alone could repeat one row and skip
    another.

    See ``_macro_where`` for the escaping of the LIKE pattern.
    """
    limit = min(limit, 100)
    where, param, order = _macro_where(name, exact)
    return conn.execute(
        f"""SELECT m.*, f.path AS file_path
              FROM macros m
              JOIN files f ON f.id = m.file_id
             WHERE {where}
             {order}
             LIMIT ? OFFSET ?""",
        (config_hash, param, limit, max(0, offset)),
    ).fetchall()


def find_macro_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int | None = 50,
) -> list[sqlite3.Row]:
    """Find file-level references to a macro using the files_fts index.

    Searches ifdef-filtered file content for occurrences of *name*.
    Returns file-level matches with highlighted snippets.

    ``limit=None`` reads EVERY match.  The caller needs it because the
    only reference that counts is one in active code, and a match inside
    a comment can be told apart from one outside it by the snippet alone
    — in Python, after the rows are here.  A SQL LIMIT therefore cuts a
    set that the answer has not been filtered out of yet, and neither the
    page nor its count would then be the truth.  One row per FILE bounds
    the read: 1922 files on one measured firmware index, and a common
    macro reached 316 of them.

    The order ends at ``f.path``.  ``rank`` ties — several files score
    alike — and SQLite may return tied rows in any order, thus a page
    walk over the rank alone could show one file twice and hide another.
    ``f.path`` is unique within one build.

    The ``files_fts`` existence check is not purely defensive — legacy
    indexes built before the files_fts table was introduced lack it
    entirely.  Rather than raising, the function returns an empty list so
    the caller can fall back to raw LIKE search or report an empty result
    set gracefully.
    """
    table_row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files_fts'").fetchone()
    if table_row is None:
        return []

    # A negative LIMIT is how SQLite spells "no bound", thus one query
    # text serves both callers.
    bound = -1 if limit is None else min(limit, 100)
    expanded = _expand_query(name)
    try:
        return conn.execute(
            """SELECT f.path AS file_path, f.language,
                      snippet(files_fts, 1, '<b>', '</b>', '…', 80) AS _match_snippet
               FROM files_fts
               JOIN files f ON f.id = files_fts.rowid
               WHERE files_fts MATCH ? AND f.config_hash = ? AND f.content != ''
               ORDER BY rank, f.path
               LIMIT ?""",
            (expanded, config_hash, bound),
        ).fetchall()
    except Exception as exc:
        if not is_db_exception(exc):
            raise
        return []


# A bare FTS5 term may hold only these characters, plus one trailing `*`.
# Everything else — punctuation of C code included — must go into a quoted
# phrase, else the FTS5 parser raises "fts5: syntax error".
#
# WHY a positive test and not a list of bad characters: the list was
# `#\[]{}^:+-`, and it named neither `.` nor `(`.  A search for the call
# pattern `.attach(` thus passed as a bare term, FTS5 rejected it, and every
# search tool answered with an empty list — a false negative that reads as
# "this code does not exist".  Measured on one project: `.attach(` gave 0
# results and `attach` gave 18.  A whitelist cannot miss the next character.
_RE_FTS5_SAFE_TERM = re.compile(r"^[A-Za-z0-9_]+\*?$")

# FTS5 operators.  They are syntax, not text to look for.
_FTS5_OPERATOR_TOKENS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _quote_fts5_term(term: str) -> str:
    """Wrap *term* in double quotes as an FTS5 phrase (doubling internal quotes)."""
    return '"' + term.replace('"', '""') + '"'


def _fts5_safe_token(token: str) -> str:
    """Return *token* in a form FTS5 accepts, without widening its meaning.

    A bareword stays as it is, an operator stays as it is, and anything else
    becomes a quoted phrase.  A trailing ``*`` survives outside the quotes,
    where FTS5 reads it as a prefix on the last token of the phrase
    (``"self test"*``).

    The quotes buy legality, not precision: the tokenizer drops punctuation
    inside a phrase as well, thus ``".attach("`` looks for the token
    ``attach``.  No FTS5 query can match a bracket — the caller sees which
    hit is the call from ``match_lines`` and ``_match_snippet``.
    """
    if not token:
        return token
    if token in _FTS5_OPERATOR_TOKENS or _RE_FTS5_SAFE_TERM.match(token):
        return token
    if token.endswith("*"):
        return _quote_fts5_term(token[:-1]) + "*"
    return _quote_fts5_term(token)


def _sanitize_body_query(query: str) -> str:
    """Repair a body query that FTS5 refused, without widening it.

    Call this only AFTER FTS5 has rejected the query as written.  Every
    query the engine accepts must reach it untouched: ``search_bodies``
    looks for a pattern in code, no wildcard is added, and a space stays an
    implicit AND.

    WHY the order matters, measured: a first version decided up front which
    tokens looked like syntax and quoted the rest.  It could not tell
    ``NEAR(attach rise)`` — an FTS5 operator — from ``.attach(`` — C code
    with a bracket.  The NEAR query dropped from 6 results to 0, and
    ``^attach``, which anchors a term to the start of a column, widened
    from 23 to 106.  The engine itself is the only exact judge of what it
    accepts, thus we ask it first and repair only what it turns down.

    The repair puts every token that is not a bareword into a quoted
    phrase.  A backslash goes first — it is never valid FTS5, and operators
    paste escaped strings from other tools (``extern \\"C\\"``).  A query
    that still holds a double quote is left as it is: an unbalanced quote
    is not something quoting more can fix.
    """
    if "\\" in query:
        log.debug("FTS5 sanitization: stripping backslashes from body query=%r", query)
        query = query.replace("\\", "")
    if '"' in query:
        return query
    return " ".join(_fts5_safe_token(tok) for tok in query.split())


def _sanitize_fts5_syntax(query: str) -> str:
    """Repair a query containing characters that are illegal in FTS5 syntax.

    A backslash is never valid FTS5 syntax (e.g. ``"extern \\"C\\""`` from a
    user copying a quoted string).  Strip backslashes and stray quote marks,
    then hand each token to :func:`_expand_token`, which quotes what FTS5
    would reject (e.g. ``#define``) and gives a bareword the usual
    trailing-wildcard expansion.
    """
    if "\\" in query:
        log.debug("FTS5 sanitization: stripping backslashes from query=%r", query)
    cleaned = query.replace("\\", "").replace('"', "")
    return " OR ".join(_expand_token(tok) for tok in cleaned.split())


def _expand_token(token: str) -> str:
    """Return *token* as a legal FTS5 term with prefix matching where it helps.

    A bareword gets a trailing ``*`` so ``modem`` reaches ``modem_init``.  A
    token that FTS5 would reject becomes a quoted phrase instead — a prefix
    on punctuation buys nothing, because the tokenizer already dropped it.
    """
    if token in _FTS5_OPERATOR_TOKENS:
        return token
    if _RE_FTS5_SAFE_TERM.match(token):
        return token if token.endswith("*") else f"{token}*"
    return _fts5_safe_token(token)


def _expand_query(query: str, *, for_body_search: bool = False) -> str:
    """Add trailing wildcard to each bare word for broader prefix matching.

    Leaves existing wildcards (*) and FTS5 syntax (NEAR, ", the operators,
    column filters with ``name_tokens : term*``) intact.  Single colons in
    column-filter syntax are detected via regex; C++ ``::`` passes through
    so its tokens get wildcard expansion.

    Queries containing a backslash (never valid FTS5) are repaired via
    :func:`_sanitize_fts5_syntax`.  A term that FTS5 cannot take as a
    bareword (``#define``, ``.attach(``, ``user-defined``) becomes a quoted
    phrase, thus it matches instead of raising ``fts5: syntax error``.

    When *for_body_search* is True, no wildcard is added and the terms stay
    AND-joined — ``search_bodies`` looks for a pattern in code, and a
    prefix on every word would widen each query.  The caller must still
    pass the result through :func:`_sanitize_body_query`, which repairs the
    punctuation of a code pattern without widening it.
    """
    if for_body_search:
        return query

    # Backslash is never valid FTS5 syntax — repair the whole query.
    if "\\" in query:
        return _sanitize_fts5_syntax(query)

    # Tokens that already are FTS5 syntax — don't touch them.  A single
    # colon (not part of ::) marks a column filter, e.g. `name_tokens : uart*`.
    #
    # A parenthesis is NOT in this list, although FTS5 groups with one: C
    # code holds far more brackets than a query holds groups, and while `(`
    # counted as syntax, the query `.attach(` went to FTS5 raw, was rejected,
    # and every search tool answered with an empty list.  A query that really
    # groups carries an operator as well (`(uart OR spi) AND init`), and the
    # operator keeps it on this path.
    #
    # What this costs, measured: `(connect write)` used to bypass expansion
    # and reach FTS5 as an implicit AND of two exact tokens — 18 rows.  It
    # is now expanded like any other pair of words, `connect* OR write*` —
    # 2442 rows.  The bypass was an accident of the old rule, not a feature:
    # the same words WITHOUT the brackets always expanded to OR, and FTS5
    # reads `(a b)` and `a b` alike.  A caller who wants AND writes the
    # operator, which is on the list above.
    _bare_syntax = ('"', "NEAR", "AND", "OR")
    _has_col_filter = _RE_COL_FILTER.search(query)
    if any(c in query for c in _bare_syntax) or _has_col_filter:
        return query

    # C++ :: must be replaced with a space BEFORE splitting — otherwise
    # "a::b_c::d" would become ["a::b_c::d"] (one token) instead of
    # ["a","b","c","d"].  FTS5's unicode61 tokenizer already splits on
    # spaces and non-alphanumeric chars; replacing :: with space lets
    # each namespace/class-component become an independent search term.
    parts = query.replace("::", " ").split()
    return " OR ".join(_expand_token(p) for p in parts)


def _symbol_search_filters(
    kind: str | None, exclude_variables: bool, project_only: bool,
) -> tuple[str, str]:
    """Give the two SQL filters of a symbol search.

    The count and the page must run on the SAME conditions.  Two copies of
    this text drift apart, and a ``total`` that describes another answer
    than the rows do is worse than no total at all.
    """
    if kind:
        kind_filter = "AND s.kind = ?"
    elif exclude_variables:
        # Covers both 'variable' (legacy unsplit kind) and 'varlocal'
        # (post-split kind for locals).  'varglobal' is deliberately NOT
        # excluded — global variables carry higher architectural weight
        # and their names are often meaningful search targets.
        kind_filter = "AND s.kind NOT IN ('variable', 'varlocal')"
    else:
        kind_filter = ""
    project_filter = "AND s.is_project = 1" if project_only else ""
    return kind_filter, project_filter


def count_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    kind: str | None = None,
    exclude_variables: bool = False,
    project_only: bool = False,
) -> int:
    """Count the symbols that ``search_symbols`` answers with.

    Reports how many symbols the query matches ALTOGETHER, thus a reader
    of one page knows whether more exist.  A query that FTS5 refuses
    counts as zero, which is what ``search_symbols`` answers with.
    """
    kind_filter, project_filter = _symbol_search_filters(
        kind, exclude_variables, project_only,
    )
    params: list = [_expand_query(query), config_hash]
    if kind:
        params.append(kind)
    try:
        return conn.execute(
            f"""SELECT COUNT(*)
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = ?
                 {kind_filter} {project_filter}""",
            params,
        ).fetchone()[0]
    except sqlite3.OperationalError as e:
        if getattr(e, "sqlite_errorcode", 0) == 1 and "fts5" in str(e).lower():
            log.debug("count_symbols: FTS5 syntax error (%s) — counting zero", e)
            return 0
        raise


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    limit: int = 20,
    kind: str | None = None,
    exclude_variables: bool = False,
    project_only: bool = False,
    offset: int = 0,
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
    *offset* walks past the rows of an earlier page.  The bm25 score ties —
    many symbols score alike — and SQLite may return tied rows in any
    order, thus ``s.usr`` ends the order.  It is unique within one build,
    so two pages of one walk cannot repeat or skip a row.
    Note: file_path is read from the denormalized symbols.file_path column,
    not re-joined from files — the JOIN was removed to avoid the redundant
    round-trip and the shadowing hazard.
    """
    expanded = _expand_query(query)
    kind_filter, project_filter = _symbol_search_filters(
        kind, exclude_variables, project_only,
    )
    params: list = [expanded, config_hash]
    if kind:
        params.append(kind)
    params += [limit, max(0, offset)]
    # Build bm25 weights — cache column count per connection to avoid
    # querying PRAGMA table_info on every search_symbols call.  The cache
    # drops the count when the database changes (a migration can add a
    # column) and never hands it to another connection.
    col_count = _bm25_col_count_cache.get(conn, "symbols_fts")
    if not isinstance(col_count, int):
        fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols_fts)").fetchall()]
        col_count = len(fts_cols)
        _bm25_col_count_cache.put(conn, "symbols_fts", col_count)
    n_extra = col_count - len(_BASE_WEIGHTS)
    weights = _BASE_WEIGHTS + [1.0] * max(0, n_extra)
    bm25_expr = f"bm25(symbols_fts, {', '.join(str(w) for w in weights)})"
    try:
        return conn.execute(
            f"""SELECT s.*
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = ? {kind_filter} {project_filter}
                ORDER BY {bm25_expr}, s.usr
               LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
    except sqlite3.OperationalError as e:
        if getattr(e, "sqlite_errorcode", 0) == 1 and "fts5" in str(e).lower():
            log.debug("search_symbols: FTS5 syntax error (%s) — returning empty", e)
            return []
        raise
