"""Shared indexing operations — parse TU, store symbols, refs, macros, content.

Three code paths share this module:
  1. Bulk index (``runner.py``) — full Cold reindex of all TUs
  2. Single-file reindex (``reindex_file`` / ``reindex_file_impl``) —
     incremental update after a source edit
  3. Auto-reindex (file watcher daemon) — background update on save

Pipeline, per TU (``store_symbols_for_unit``):
  1. Parse with libclang (or use caller-supplied *pre_parsed* to avoid
     double parsing when the caller already parsed outside the write lock)
  2. Delete old symbols, refs, inheritance, indirect call sites,
     function-pointer assignments, and macro rows for this TU
  3. Insert new symbol rows — compute ``is_project`` from vendor/project
     LIKE patterns, extract function bodies from disk via cached reads
  4. Detect symbols that moved between files without content changes —
     preserve LLM analysis by updating file_id instead of re-inserting
  5. Insert refs, indirect call sites, function-pointer assignments,
     pending dispatch edges, and inheritance rows
  6. Store macro definitions (per-TU replacement)
  7. Fill ``files.content`` with ``#ifdef``-filtered text — tokenize the
     TU to find which lines are active for the build configuration

Post-processing (after all TUs):
  * ``backfill_cross_tu_refs`` — resolve method calls whose callee lives
    in a different TU (invisible to per-TU ``_qn_to_usr``)

Design decisions:
  - All database writes happen inside a caller-managed transaction —
    this module never commits.  The caller can group multiple TUs into
    a single transaction for speed, or commit per-TU for incremental work.
  - Pre-parsed data (``pre_parsed`` kwarg) lets the caller run expensive
    libclang parsing outside the write lock, then hand the results to
    ``store_symbols_for_unit`` for fast DB insertion.
  - The ``_body_cache`` avoids re-reading the same header file hundreds
    of times during a single TU — OrderedDict with manual eviction caps
    memory at ~200 files.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from functools import cache

try:
    from clang.cindex import TranslationUnitLoadError
except ImportError:
    TranslationUnitLoadError = RuntimeError  # clang not available — use fallback
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import: fw_context_mcp.indexer.symbols pulls in libclang at
    # import time, and ops is imported on paths that never parse.
    from fw_context_mcp.indexer.symbols import ExtractionResult

from fw_context_mcp.indexer.config_hash import compute_tu_content_hash
from fw_context_mcp.indexer.db import (
    _resolve_target_usr,
    get_file_mtimes,
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_inheritance_batch,
    insert_macros_batch,
    insert_refs_batch,
    insert_symbols_batch,
    replace_file_data,
    split_tokens,
    upsert_file,
)
from fw_context_mcp.utils import (
    CPP_EXTENSIONS,
    abs_path,
    compute_content_hash,
    compute_source_hash,
    read_file_lines,
)

from .db._chunking import chunked
from .db._files import FileIdLookup
from .manifest import _is_generated_header

log = logging.getLogger(__name__)

# NOTE(turbyho, 2026-07-31): Each header is opened/read/closed individually — 500+ headers = 500+
# open/read/close cycles per TU.  Consider batch I/O for read_file_lines.
#
# OrderedDict with manual eviction is used instead of functools.lru_cache
# because the cached values are large list[str] objects and we need an
# explicit, small cap — lru_cache defaults to 128 but drops old entries
# even when they are still useful within one TU.  A per-TU OrderedDict
# with `popitem(last=False)` acts like FIFO: oldest entries (from already-
# processed headers) are evicted first, while recently-used entries
# (headers shared across many symbols in the same file) stay resident.
_body_cache: OrderedDict[str, list[str] | None] = OrderedDict()
_BODY_CACHE_MAX_ENTRIES = 200


def _cached_read_lines(abs_path: str) -> list[str] | None:
    """Read all lines of *abs_path*, caching the result in an LRU-like cache.

    Returns ``None`` when the file cannot be read (OSError, missing file).
    The cache is per-TU — ``_clear_body_cache()`` is called at the start
    of each TU so a later TU re-reads files that changed on disk.

    Performance: a header included by 200 symbols in one TU would trigger
    200 file reads without this cache.  With the cache, the first read
    costs one I/O call; the next 199 are dictionary lookups.
    """
    if abs_path not in _body_cache:
        if len(_body_cache) >= _BODY_CACHE_MAX_ENTRIES:
            _body_cache.popitem(last=False)
        _body_cache[abs_path] = read_file_lines(abs_path)
    else:
        _body_cache.move_to_end(abs_path)
    return _body_cache[abs_path]


def _clear_body_cache() -> None:
    """Drop all cached file contents at the start of a new TU.

    Header files may have been modified between TU parses (unlikely during
    a single bulk index, but possible during auto-reindex).  Clearing the
    cache ensures the next TU reads fresh data from disk.
    """
    _body_cache.clear()


def _read_body(lines: list[str], start_line: int, end_line: int) -> str:
    """Extract symbol body from pre-read file lines using libclang extents.

    *start_line* and *end_line* are 1-based.
    Returns the joined body text or an empty string when the range is invalid.
    """
    if end_line > start_line and end_line <= len(lines):
        return "".join(lines[start_line - 1 : end_line])
    return ""


def _compute_content_hash(
    lines: list[str],
    start_line: int,
    end_line: int,
    signature: str,
    qualified_name: str,
    docstring: str,
) -> str:
    """Stable hash of a symbol's body + identity for change detection.

    Uses the actual body text (read from disk via libclang extents) so that
    even a refactor preserving line count is detected.  Whitespace is stripped
    so formatting-only changes are ignored.
    """
    body = _read_body(lines, start_line, end_line)
    return compute_content_hash(body, qualified_name, signature, docstring)


def _normalize_file_path(file_path: str, project_root: Path) -> str:
    """Convert a file path to project-relative when inside *project_root*.

    Files outside *project_root* (SDK, framework) keep an absolute path, but
    a RESOLVED one.  The fallback used to return *file_path* untouched, and
    that produced two ``files`` rows for one file: libclang reports a system
    header reached through an include path as
    ``/usr/lib64/gcc/…/16/../../../../include/c++/16/algorithm``, which this
    function stored verbatim, while ``_build_filtered_file_content`` stored
    ``str(Path(p).resolve())`` — ``/usr/include/c++/16/algorithm``.  The
    symbols hung off one row and the content off the other, and nothing
    matched them up.  Measured on HA_Boiler: 145 duplicate rows, and the
    whole C++ standard library became invisible once the coverage purge
    removed the spelling the manifest did not use.

    One spelling per file is the invariant every path-keyed lookup relies on,
    so this is the only place that decides it.
    """
    resolved = Path(file_path).resolve()
    try:
        return str(resolved.relative_to(project_root.resolve()))
    except ValueError:
        return str(resolved)


def _build_filtered_file_content(
    conn, unit, config_hash: str, project_root: Path, *, build_dir_patterns: list[str] | None = None, existing_tu=None,
    skip_files: frozenset[str] | None = None,
    refresh_paths: set[str] | None = None,
) -> tuple[int, list[dict]]:
    """Tokenize TU, extract active lines per file, store ifdef-filtered content.

    Parses *unit* (a ``CompilationUnit`` data class) with libclang, then
    tokenizes to find which source lines are active (not ``#ifdef``-dead).
    Processes files whose ``content`` column is still empty, plus the files
    named in *refresh_paths* — the set this parse owns, whose text just
    changed.  Everything else keeps its stored content: re-filtering a file
    that did not change would cost an AST walk for an identical result.

    WHY *refresh_paths* is needed: ``files.content`` backs ``read_file`` and
    ``search_content``.  Without it a file's indexed text is frozen at the
    first index — a re-parsed TU would report fresh symbols while the same
    tools served the pre-change source.

    Also collects included header paths and their SHA-256 hashes — used by
    the manifest update phase to avoid a second tokenization pass.

    Inactive lines are replaced with ``\\n`` so line numbers stay
    consistent with the original source.

    Files inside *project_root* are stored with a relative path; files
    outside (framework headers from PlatformIO, ESP-IDF, Zephyr modules)
    are stored with their absolute path so ``search_content`` can find
    them.

    Returns:
        ``(filled_count, headers)`` where *filled_count* is the number of
        files whose ifdef-filtered content was newly stored, and *headers*
        is a list of ``{path, hash, generated}`` dicts for included header
        files.

    Important:
        The caller **MUST** hold an active transaction.  This function
        performs ``INSERT … ON CONFLICT UPDATE`` on the ``files`` table
        and does not manage its own transaction boundary.  Partial failure
        during content fill leaves ``files.content`` empty for some files,
        and the early-return guard (``content=''`` check) skips them on
        subsequent calls — the content is never filled for those files.

    Side effects:
        - Modifies ``files.content`` and ``files.mtime`` in the database.
        - Reads source files from disk via libclang.
    """
    import time as _time

    _t0 = _time.monotonic()

    # ── Fast-path: skip tokenization + AST walk when all files already have content ──
    # The files.content column is guaranteed to exist — it was added by
    # _MIGRATION_ADD_COLUMNS during open_db().  We avoid _ensure_column()
    # here because it's DDL that auto-commits in SQLite, which breaks
    # any transaction the caller may be holding.
    remaining = conn.execute(
        "SELECT COUNT(*) FROM files WHERE config_hash=? AND content=''",
        (config_hash,),
    ).fetchone()[0]

    from clang import cindex as cx

    from fw_context_mcp.indexer.symbols import _get_index

    if existing_tu is not None:
        tu = existing_tu  # reuse TU from extract_all — avoid double parse
    else:
        try:
            tu = _get_index().parse(
                str(unit.file),
                args=unit.clang_args,
                options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except (RuntimeError, cx.TranslationUnitLoadError):
            log.debug("_build_filtered_file_content: parse failed for %s", unit.file.name)
            return 0, []

    from fw_context_mcp.indexer.manifest import _is_generated_header

    # ── Collect included header paths + SHA-256 hashes (always needed for manifest) ──
    headers: list[dict] = []
    seen_headers: set[str] = set()
    # (resolved absolute path, content hash) for every header this parse
    # read.  The blank-out pass at the end of this function needs both: the
    # absolute path to read the file and to key the files row, and the hash
    # to tell a changed header from an unchanged one.  `headers` alone
    # cannot serve it — it stores the project-relative spelling.
    header_files: list[tuple[str, str]] = []

    for inc in tu.get_includes():
        abs_path = str(inc.include.name)
        if abs_path in seen_headers:
            continue
        seen_headers.add(abs_path)

        resolved = Path(abs_path).resolve()
        # No extension filter — see _collect_headers_from_tokens() for why.
        # Anything reached by an #include belongs in the manifest, or it can
        # neither be kept nor invalidated.
        try:
            rel = str(resolved.relative_to(project_root))
        except ValueError:
            rel = str(resolved)

        h = compute_source_hash(resolved)
        generated = _is_generated_header(rel, build_dir_patterns)
        headers.append({"path": rel, "hash": h, "generated": generated})
        header_files.append((str(resolved), h))

    # ── Fast-path return: all files already have filtered content ──
    # Skip tokenization and AST walk — they're only needed for content-fill,
    # not for header collection (which only needs get_includes, already done).
    filled = 0
    # Asked before the fast-path return, because the answer decides whether
    # that return may be taken at all.  One indexed lookup per header and no
    # file read — the hashes arrived with header_files above, which this
    # function computed for the manifest either way.
    changed_headers = _changed_header_rows(conn, config_hash, project_root, header_files)
    if remaining == 0 and not refresh_paths and not changed_headers:
        return 0, headers
    # A changed header sends this parse down the full path even when every
    # file already has content.  The blank-out pass at the end needs to know
    # which files carried an active line, and only the tokenization below
    # can tell it — without that set it cannot separate a header an edit
    # emptied from one that still holds code, and it blanked both.

    # ── Build ifdef-filtered content for files that still need it ──
    tokens = list(tu.cursor.get_tokens())

    # ── Collect active lines from the main source file via tokenization ──
    active: dict[str, set[int]] = {}
    for tok in tokens:
        loc = tok.location
        if loc.file is not None:
            fname = str(loc.file.name)
            active.setdefault(fname, set()).add(loc.line)

    # ── Collect active lines from all header files in a single AST traversal ──
    # When *skip_files* is provided, subtrees of already-processed headers
    # are skipped — content for those files was already stored during the
    # first TU that included them.  Without this integration,
    # _collect_all_active_lines would walk headers that extract_all skipped,
    # producing content rows with no matching symbol rows (data inconsistency).
    def _collect_all_active_lines(root_cursor) -> None:
        """Walk the AST and add extent line ranges keyed by source file."""
        if skip_files:
            from fw_context_mcp.indexer.symbols import _iter_cursor_skip

            # Per-TU resolve function with its own lru_cache.
            # Each _build_filtered_file_content call uses a fresh cache
            # because *unit.directory* differs per TU.  Within a single
            # content-fill walk (~100K cursors, ~1K unique files), the
            # cache hits ~99 % of resolve calls.
            @cache
            def _res(path: str) -> Path:
                p = Path(path)
                return (unit.directory / p).resolve() if not p.is_absolute() else p.resolve()

            walker = _iter_cursor_skip(root_cursor, skip_files, _res)
        else:
            walker = root_cursor.walk_preorder()

        for cursor in walker:
            if cursor.location.file:
                fname = str(cursor.location.file.name)
                extent = cursor.extent
                if extent.start.file and extent.end.file:
                    for line in range(extent.start.line, extent.end.line + 1):
                        active.setdefault(fname, set()).add(line)

    _collect_all_active_lines(tu.cursor)

    # Paths this loop wrote.  The blank-out pass below must not touch them
    # again: this loop already put the current text there.
    written: set[str] = set()
    # Every file this parse saw an active line in, whether or not the loop
    # below writes it.  `written` cannot serve as that set: the loop also
    # skips a file it deliberately leaves alone — one that already has text
    # and that this parse does not own — and such a file carried active
    # lines all the same.  The blank-out pass took the absence from
    # `written` as "no active line" and erased the text of a header that
    # still held code.
    active_paths: set[str] = {
        _normalize_file_path(path, project_root)
        for path, lines in active.items()
        if lines
    }

    for abs_path, active_lines in active.items():
        if not active_lines:
            continue

        db_path = _normalize_file_path(abs_path, project_root)
        resolved = Path(abs_path).resolve()

        # Already processed — and not owned by this parse, so its stored
        # content still matches the disk.
        row = conn.execute(
            "SELECT content FROM files WHERE config_hash=? AND path=?",
            (config_hash, db_path),
        ).fetchone()
        if row and row[0] and not (refresh_paths and db_path in refresh_paths):
            continue  # already have filtered content

        # Read original file
        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                original = f.readlines()
        except OSError:
            continue

        if not original:
            continue

        # Replace inactive lines with \n to preserve line numbers
        max_line = max(active_lines)
        filtered = [
            original[i - 1] if (i in active_lines) else "\n" for i in range(1, min(len(original), max_line) + 1)
        ]
        content = "".join(filtered)

        # Insert or update — file rows may not exist for headers-only files.
        lang = "cpp" if Path(db_path).suffix in CPP_EXTENSIONS else "c"
        # Grab real mtime so _count_modified_files won't flag this as stale.
        try:
            file_mtime = resolved.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        # The stored mtime moves forward together with the content: both come
        # from the text this parse just read, so the pair stays consistent.
        # Updating mtime without the content would hide a real change.
        # `generated` is written here too, and not only in
        # _store_symbol_rows.  That function iterates over SYMBOLS, so a
        # header that declares nothing never reaches it — this INSERT is the
        # only creator of its row.  Measured on HA_Boiler: 7 of 56 generated
        # headers, among them umbrella and version headers, came in this way
        # and kept generated=0 while the manifest said True.
        #
        # MAX for the same reason upsert_file() uses it: `generated` is a
        # property of the PATH, so it goes from 0 to 1 and never back.
        conn.execute(
            "INSERT INTO files (config_hash, path, language, content, mtime, generated) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (config_hash, path) DO UPDATE SET "
            "content = excluded.content, mtime = MAX(files.mtime, excluded.mtime), "
            "generated = MAX(files.generated, excluded.generated)",
            (
                config_hash, db_path, lang, content, file_mtime,
                int(_is_generated_header(db_path, build_dir_patterns)),
            ),
        )
        filled += 1
        written.add(db_path)

    filled += _blank_out_inactive_files(
        conn, config_hash, project_root, changed_headers, written, active_paths,
        build_dir_patterns,
    )

    if filled:
        log.info("content fill: %d files from TU %s in %.1fs", filled, unit.file.name, _time.monotonic() - _t0)
    return filled, headers


def _changed_header_rows(
    conn,
    config_hash: str,
    project_root: Path,
    header_files: list[tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Return ``{db_path: (abs_path, current_hash)}`` for headers gone stale.

    A header qualifies when the index holds text for it AND a ``source_hash``
    that disagrees with the file on disk.  Without a stored hash there is
    nothing to compare, and rewriting on that basis alone would touch every
    header on every parse.  Without stored text there is nothing that could
    be serving a stale answer — the content loop owns the fill for those.

    One indexed lookup per header and no file read: the current hash arrived
    with *header_files*, which ``_build_filtered_file_content`` computes for
    the manifest on every path, including the fast one.

    The absolute path rides along because the blank-out pass has to read the
    file: rebuilding it from *db_path* would depend on whether *project_root*
    arrived resolved, and _normalize_file_path resolves before it compares.
    The path that produced the key is the one that opens the file.

    Two callers, one question: the fast-path return may not be taken while
    this is non-empty, and the blank-out pass works from it.  Asking twice
    would read the same rows twice.
    """
    changed: dict[str, tuple[str, str]] = {}
    for header_path, current_hash in header_files:
        db_path = _normalize_file_path(header_path, project_root)
        row = conn.execute(
            "SELECT content, source_hash FROM files WHERE config_hash=? AND path=?",
            (config_hash, db_path),
        ).fetchone()
        if row is None or not row[0]:
            continue
        stored_hash = row[1]
        if not stored_hash or stored_hash == current_hash:
            continue
        changed[db_path] = (header_path, current_hash)
    return changed


def _blank_out_inactive_files(
    conn,
    config_hash: str,
    project_root: Path,
    changed_headers: dict[str, tuple[str, str]],
    written: set[str],
    active_paths: set[str],
    build_dir_patterns: list[str] | None,
) -> int:
    """Refresh a header the parse read that produced no active line.

    ``active`` holds only files that carry a token or a cursor extent, and
    the content loop skips an entry whose line set is empty.  A header with
    nothing to parse reaches neither: an empty one, or one an edit reduced
    to comments, pragmas and include guards.  Measured before this pass
    existed, files.content then kept the text from BEFORE the edit,
    read_file served that text, and search_content still matched words the
    file no longer held.

    The right text is the all-blank form.  The filter keeps line numbers
    and blanks every inactive line; here every line is inactive, thus the
    content becomes one newline per line of the file.  Storing "" instead
    would be wrong: an empty content column means "not filled yet" to the
    ``remaining`` count and to the loop's own skip test, so the file would
    be re-read on every parse for ever.

    TWO exclusions, and both are necessary:

    * *written* — the content loop already put the current text there.
    * *active_paths* — this parse saw an active line in the file, thus it
      is not a header that produced none, whatever the loop then did with
      it.  This is the check the function used to lack: it took the absence
      of a path from *written* as proof of no active line, and the loop
      leaves a file out of *written* for a second reason — it already has
      text and this parse does not own it.  A header that changed on disk
      and still held code was blanked, and the blank was not recoverable:
      it is not the empty string, so the fill test on the next parse skips
      the file for ever.

    *changed_headers* already holds only headers whose stored hash
    disagrees with the file, so an unchanged one costs nothing here.
    """
    # Local, like the one in _build_filtered_file_content: manifest imports
    # from this module, thus a module-level import would close a cycle.
    from fw_context_mcp.indexer.manifest import _is_generated_header

    refreshed = 0
    for db_path, (header_path, current_hash) in changed_headers.items():
        if db_path in written or db_path in active_paths:
            continue

        resolved = Path(header_path)
        try:
            with open(resolved, encoding="utf-8", errors="replace") as handle:
                line_count = sum(1 for _ in handle)
            file_mtime = resolved.stat().st_mtime
        except OSError:
            continue
        if not line_count:
            continue

        lang = "cpp" if Path(db_path).suffix in CPP_EXTENSIONS else "c"
        conn.execute(
            "INSERT INTO files (config_hash, path, language, content, mtime, generated, source_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (config_hash, path) DO UPDATE SET "
            "content = excluded.content, mtime = MAX(files.mtime, excluded.mtime), "
            "generated = MAX(files.generated, excluded.generated), "
            "source_hash = excluded.source_hash",
            (
                config_hash, db_path, lang, "\n" * line_count, file_mtime,
                int(_is_generated_header(db_path, build_dir_patterns)),
                current_hash,
            ),
        )
        refreshed += 1
    return refreshed


def _store_symbol_rows(
    conn: sqlite3.Connection,
    config_hash: str,
    syms: list,
    file_id_cache: dict[str, int],
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
    build_dir_patterns: list[str] | None = None,
) -> tuple[int, dict[int, int]]:
    """Build and batch-insert symbol rows for one TU.

    Returns ``(syms_added, file_proj)`` where *file_proj* maps
    ``file_id → max(is_project)`` across all files touched by this TU.

    The caller uses *file_proj* to update ``files.is_project`` —
    a file that hosts both project and vendor symbols is treated as
    project code (``is_project=1`` wins because ``project_patterns``
    take priority).

    Design decisions:
      - File IDs are resolved per unique path (not per symbol) to avoid
        duplicate ``files`` rows.  The *file_id_cache* dict is shared
        across the whole TU so headers included by many symbols only
        get one file row each.
      - ``is_project`` is computed from LIKE patterns rather than from
        a directory prefix check because build systems (PlatformIO,
        ESP-IDF) spread project code across multiple non-trivial
        directory trees — a simple ``startswith(project_root)`` would
        misclassify framework headers copied into the build directory.
      - Bodies are only extracted for definitions (``is_definition``
        with ``end_line > start_line``).  Declarations and forward
        declarations store ``body=''`` to save disk space.
      - ``files.generated`` is written from *build_dir_patterns*, the same
        source the manifest uses for ``headers[path].generated``.  The two
        must agree; a disagreement means somebody wrote the manifest with
        different patterns from the ones the index used.

    *build_dir_patterns* does NOT take part in ``is_project``.  Generated
    code is project code, and the two columns answer different questions:
    ``is_project`` asks who owns the file, ``generated`` asks whether a tool
    wrote it.
    """
    from .sdk_detect import _path_matches

    rows = []
    file_proj: dict[int, int] = {}
    for s in syms:
        sym_file = s.file
        normalized_sym_file = _normalize_file_path(sym_file, project_root)
        if normalized_sym_file not in file_id_cache:
            lang = "cpp" if Path(sym_file).suffix in CPP_EXTENSIONS else "c"
            try:
                sym_mtime = Path(sym_file).stat().st_mtime
            except OSError:
                sym_mtime = 0.0
            # The hash goes in for every file, not only for a translation
            # unit.  The staleness check uses it to tell a real change from a
            # file that git only rewrote, and a header is what git rewrites
            # most.
            #
            # The cost is per (TU, file) pair, NOT per file: file_id_cache is
            # built fresh for every translation unit, and compute_source_hash
            # has no cache of its own.  A header that 200 units include is
            # read 200 times.  Measured on zbox-ecb-fw: 1,037 unique headers
            # but 86,686 references, an amplification of 84x.  It still costs
            # only about 1 s over a whole index run, because the files are
            # small and the page cache serves the repeats — the amplification
            # is what matters to anyone sizing a change here, not the 1 s.
            file_id_cache[normalized_sym_file] = upsert_file(
                conn, config_hash, normalized_sym_file, lang,
                generated=_is_generated_header(normalized_sym_file, build_dir_patterns),
                mtime=sym_mtime,
                source_hash=compute_source_hash(Path(sym_file)),
            )
        rel_path = normalized_sym_file
        # ── Compute is_project ──
        # project_patterns are checked FIRST because a path that matches
        # both project_patterns AND vendor_patterns should be treated as
        # project code (e.g. a project file inside a directory that also
        # happens to match a generic vendor pattern).
        # Files outside the project root (framework headers from absolute
        # system paths) cannot match project_patterns — they default to
        # is_project=0 unless a broad project_pattern catches them.
        try:
            resolved_sym = Path(sym_file).resolve()
            rel = str(resolved_sym.relative_to(project_root))
            if any(_path_matches(rel, p) for p in project_patterns):
                is_proj = 1
            elif any(_path_matches(rel, p) for p in vendor_patterns):
                is_proj = 0
            else:
                is_proj = 1
        except ValueError:
            abs_path = str(resolved_sym)
            if any(_path_matches(abs_path, p) for p in project_patterns):
                is_proj = 1
            else:
                is_proj = 0
        # ── Extract function body for definitions ──
        # Bodies are stored in the DB to power search_bodies (FTS5 over
        # function implementations).  Declarations (is_definition=False)
        # store an empty body — only definitions have executable code.
        # _cached_read_lines reuses file content across symbols in the
        # same file, avoiding repeated disk I/O.
        body = ""
        if s.is_definition and s.end_line > s.line:
            file_lines = _cached_read_lines(s.file)
            if file_lines is not None:
                body = _read_body(file_lines, s.line, s.end_line)
        fid = file_id_cache[normalized_sym_file]
        # Track the highest is_project value per file — files hosting any
        # project symbol get is_project=1 even if they also contain
        # vendor symbols (e.g. a glue header that includes both).
        file_proj[fid] = max(file_proj.get(fid, 0), is_proj)
        rows.append(
            (
                config_hash,
                file_id_cache[normalized_sym_file],
                rel_path,
                split_tokens(s.name, s.qualified_name),
                s.usr,
                s.name,
                s.qualified_name,
                s.kind,
                s.line,
                s.column,
                s.end_line,
                int(s.is_definition),
                s.signature,
                s.docstring,
                s.enum_value,
                int(s.is_virtual),
                int(s.is_pure_virtual),
                s.parent_usr,
                int(s.is_template),
                s.template_usr,
                is_proj,
                0.0,
                body,
                # is_weak: always 0 on this path.  libclang's Python
                # binding exposes no weak attribute, so a C definition
                # marked `__attribute__((weak))` is indistinguishable
                # from a strong one here.  The column exists for the
                # assembly path, where `.weak` is a directive the reader
                # sees plainly, and that is where the vector table needed
                # it.  A C definition therefore counts as strong, which
                # is what the code assumed before the column existed.
                0,
            )
        )
    # Batch insert — a single SQL statement for all symbols in the TU
    # is orders of magnitude faster than individual INSERT per symbol.
    if rows:
        insert_symbols_batch(conn, rows)
    return len(rows), file_proj




def _detect_moved_symbols(
    conn: sqlite3.Connection,
    config_hash: str,
    syms: list,
    old_usrs: set[str],
    file_id_cache: dict[str, int],
    project_root: Path,
) -> None:
    """Detect symbols that moved between files without content changes.

    When a developer moves a function definition from ``a.cpp`` to ``b.cpp``
    without editing the body, the symbol gets a new USR (because USR encodes
    file path in some libclang configurations) or gets caught here when the
    old USR still exists in the DB but the symbol's file changed.

    Strategy: for each new symbol whose USR also exists in the previous
    index (different file_id), compare content hashes.  If the hash is
    identical, update the OLD row's file_id/line/column in place and
    delete the DUPLICATE row that the current batch insert just created.

    This preserves LLM analysis (``llm_analysis`` table is tied to
    ``symbol_id``) across file moves — without this step, a moved function
    would lose its pre-computed analysis and require re-analysis.

    Content hash comparison uses the actual body text (via libclang extents)
    so that pure-formatting changes that don't affect the hash are still
    treated as "same body" even if whitespace differs.
    """
    moved = 0
    for s in syms:
        # Skip symbols that are pure-new (no old USR match) — they
        # cannot be moved because there is no previous location.
        if s.usr in old_usrs:
            continue
        normalized_sym_file = _normalize_file_path(s.file, project_root)
        # Fetch the old row with its LLM analysis — we only preserve
        # moves for symbols that actually have analysis (summary IS NOT NULL).
        # Symbols without analysis are cheap to re-insert; the duplicate
        # cleanup would add overhead without benefit.
        old_row = conn.execute(
            """SELECT s.id, s.file_id, s.qualified_name, s.signature, s.line, s.end_line,
                      s.docstring, f.path as file_path,
                      a.summary, a.inputs, a.outputs, a.model, a.analyzed_at
               FROM symbols s
               LEFT JOIN llm_analysis a ON a.symbol_id = s.id
               JOIN files f ON f.id = s.file_id
               WHERE s.usr = ? AND s.config_hash = ?""",
            (s.usr, config_hash),
        ).fetchone()
        if old_row is None:
            continue
        # Only preserve moves for analyzed symbols — unanalyzed symbols
        # are cheaper to re-insert from scratch than to verify content
        # equality across file reads.
        if old_row["summary"] is None:
            continue
        # Same file — not a move, nothing to do.
        if old_row["file_id"] == file_id_cache[normalized_sym_file]:
            continue

        lines = _cached_read_lines(s.file)
        if lines is None:
            continue
        old_lines = _cached_read_lines(abs_path(project_root, old_row["file_path"]))
        if old_lines is None:
            continue
        # Compare content hashes — same body + signature = same symbol.
        # If hashes differ, the symbol was genuinely modified (not just
        # moved), so we keep the new insert (old row will be cleaned up
        # by _delete_old_for_tu on its original TU).
        old_ch = _compute_content_hash(
            old_lines,
            old_row["line"],
            old_row["end_line"],
            old_row["signature"],
            old_row["qualified_name"],
            old_row["docstring"],
        )
        new_ch = _compute_content_hash(
            lines, s.line, s.end_line, s.signature, s.qualified_name, s.docstring,
        )
        if old_ch != new_ch:
            continue

        # ── Update old row in place, delete the duplicate ──
        # The current batch insert already created a NEW row for this
        # symbol.  We update the OLD row's location fields to point at
        # the new file, then delete the duplicate.  LLM analysis and
        # embedding rows remain linked to the old row's id.
        try:
            new_rel = str(Path(s.file).resolve().relative_to(project_root))
        except ValueError:
            new_rel = s.file
        conn.execute(
            """UPDATE symbols SET file_id = ?, file_path = ?,
               line = ?, col = ?, end_line = ?
               WHERE id = ?""",
            (file_id_cache[normalized_sym_file], new_rel, s.line, s.column, s.end_line, old_row["id"]),
        )
        # Delete the duplicate row created by this batch insert.
        # We match by (config_hash, usr, id != old_row_id) — the duplicate
        # is guaranteed to exist because we just inserted it.
        dup_id = conn.execute(
            "SELECT id FROM symbols WHERE config_hash = ? AND usr = ? AND id != ?",
            (config_hash, s.usr, old_row["id"]),
        ).fetchone()
        if dup_id:
            conn.execute("DELETE FROM symbols WHERE id = ?", (dup_id[0],))
        moved += 1
    if moved:
        log.debug("Detected %d moved symbols", moved)


def _owned_file_ids(
    known: FileIdLookup,
    normalized_tu_path: str,
    owned_paths: set[str] | None,
) -> list[int]:
    """Return the previous file_ids of the files this parse owns.

    Paths with no previous row (first index of that file) are skipped —
    there is nothing to delete or compare for them.  Falls back to the TU's
    own file when *owned_paths* is None, which reproduces the behaviour from
    before header ownership was tracked.
    """
    paths = owned_paths if owned_paths is not None else {normalized_tu_path}
    return sorted({known[p][0] for p in paths if p in known})


def _save_old_state(
    conn: sqlite3.Connection,
    normalized_tu_path: str,
    known: FileIdLookup,
    *,
    owned_paths: set[str] | None = None,
) -> set[str]:
    """Return USRs of symbols that existed for the files this parse owns.

    Used by :func:`_detect_moved_symbols` to detect symbols that moved
    between files.

    The set spans the TU plus the headers it claimed this run (*owned_paths*)
    — exactly the rows :func:`_delete_old_for_tu` is about to delete.  A
    symbol that stays in the same owned header must not look "new" to move
    detection, or it would be treated as an incoming move from its own file.
    Symbols owned by OTHER TUs are still excluded, even when they share a USR
    because both include the same header.
    """
    file_ids = _owned_file_ids(known, normalized_tu_path, owned_paths)
    if not file_ids:
        return set()
    usrs: set[str] = set()
    for chunk in chunked(file_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT usr FROM symbols WHERE file_id IN ({placeholders})",
            chunk,
        ).fetchall()
        usrs.update(r["usr"] for r in rows)
    return usrs


def _delete_old_for_tu(
    conn: sqlite3.Connection,
    config_hash: str,
    normalized_tu_path: str,
    known: FileIdLookup,
    syms: list,
    *,
    owned_paths: set[str] | None = None,
) -> None:
    """Delete stale data for the files this parse owns, before inserting.

    Performs four cleanup actions:
      1. Deletes inheritance edges for classes being re-parsed (class
         hierarchy may have changed: removed bases, added bases).
      2. Deletes ``embeddings``, ``llm_analysis``, and ``vec_symbols``
         rows for symbols about to be deleted — prevents orphan rows.
      3. Deletes all inheritance records tied to the owned file_ids.
      4. Deletes all symbols tied to the owned file_ids (the main cleanup).

    *owned_paths* covers the TU plus the headers it claimed this run.  It
    must include the headers: their symbol rows carry the HEADER's file_id,
    so deleting only the TU's file_id would leave them in place.  The fresh
    rows would then hit ``ON CONFLICT(config_hash, usr)`` in
    ``insert_symbols_batch``, whose guard only promotes a declaration to a
    definition — every other update is dropped.  The visible effect is a
    header whose symbols keep the line numbers, signatures, and even the
    membership of the very first index.

    Inheritance is cleaned first because the ``inheritance`` table has
    no FK to ``symbols`` (the FK constraint was dropped to allow deferred
    batch insertion).  Cleaning it before the symbols ensures no dangling
    inheritance edges survive.

    The ``vec_symbols`` DELETE is wrapped in a try/except because
    ``sqlite-vec`` is an optional extension — the table may not exist
    in environments where ``sqlite-vec`` is not loaded.
    """
    # Step 1: Remove inheritance edges for classes being re-parsed.
    #         If a class was removed entirely, its inheritance edges
    #         would also be caught by step 3 (DELETE by file_id).
    #         But if a class MOVED to another file, this prevents
    #         stale inheritance edges pointing at the old file.
    _class_kinds = frozenset({"class", "struct"})
    _class_usrs = {s.usr for s in syms if s.kind in _class_kinds}
    if _class_usrs:
        for usr in _class_usrs:
            conn.execute(
                "DELETE FROM inheritance WHERE config_hash = ? AND derived_usr = ?",
                (config_hash, usr),
            )
    # Step 2: hand the owned files to the one primitive that knows what a
    #         file owns — symbols, macros, inheritance, embeddings, analysis,
    #         vectors, and the refs / call sites / assignments originating in
    #         it.  This used to be three cleanups keyed three different ways
    #         (file_id here, paths in the refs block, file_id again for
    #         macros), each deriving "which files does this parse own" for
    #         itself, and each getting it wrong for rows owned by a header.
    replace_file_data(
        conn, config_hash, _owned_file_ids(known, normalized_tu_path, owned_paths)
    )


def _store_macros_for_unit(
    conn: sqlite3.Connection,
    config_hash: str,
    macros: list,
    file_path: str,
    normalized_tu_path: str,
    current_mtime: float,
    tu_file_id: int,
    file_id_cache: dict[str, int],
    project_root: Path,
) -> None:
    """Insert macro definitions for one translation unit.

    Upserts file records for header files that contain macros, then inserts
    the fresh set.  Nothing is deleted here: ``replace_file_data()`` already
    cleared the macros of every file this parse owns, including the headers,
    and it did so whether or not this parse found any macros to replace them
    with.  A ``#define`` in a header is stored under the HEADER's file_id, so
    a delete keyed on ``tu_file_id`` alone used to leave those rows behind.

    Macro storage is per-owned-file replacement, not per-macro upsert.  This
    is necessary because the same macro name may be defined with different
    values in different TUs (``#ifdef``-conditional definitions) — replacing
    a whole file's rows ensures each TU's view of the macro is consistent
    with its own preprocessing output.

    The mtime for the TU's own file uses the current stat result; header
    files get mtime=0.0 if stat fails (the upsert_file call will look up
    the existing mtime from the DB via its ON CONFLICT handler).
    """
    # No delete here: replace_file_data() already cleared the macros of every
    # owned file, whether or not this parse found any to replace them with.
    if not macros:
        return
    macro_rows: list[tuple] = []
    for m in macros:
        m_raw = str(m.file) if m.file else file_path
        m_path = _normalize_file_path(m_raw, project_root)
        if m_path not in file_id_cache:
            lang = "cpp" if Path(m_raw).suffix in CPP_EXTENSIONS else "c"
            m_mtime = current_mtime if m_path == normalized_tu_path else 0.0
            try:
                m_mtime = Path(m_raw).stat().st_mtime
            except OSError:
                pass
            # source_hash for the same reason as in _store_symbol_rows: a
            # file whose mtime moves must carry the hash of the text that
            # moved it, or the staleness check reports it as changed for
            # ever.  Same per-(TU, file) cost as there.
            file_id_cache[m_path] = upsert_file(
                conn, config_hash, m_path, lang, mtime=m_mtime,
                source_hash=compute_source_hash(Path(m_raw)),
            )
        m_file_id = file_id_cache[m_path]
        macro_rows.append(
            (
                config_hash,
                m_file_id,
                m.name,
                m.value,
                m.expanded_value,
                m.line,
                int(m.is_function_like),
            )
        )
    if macro_rows:
        insert_macros_batch(conn, macro_rows)

def store_symbols_for_unit(
    conn,
    unit,
    config_hash: str,
    project_root: Path,
    vendor_patterns: list[str] | None = None,
    project_patterns: list[str] | None = None,
    index_refs: bool = False,
    pre_parsed: ExtractionResult | None = None,
    existing_files: dict[str, tuple[int, float]] | None = None,
    hashes=None,
    build_dir_patterns: list[str] | None = None,
    skip_files: frozenset[str] | None = None,
) -> tuple[int, int, list[dict]]:
    """Parse one translation unit and store its symbols + refs in the DB.

    Handles:
    - Running libclang ``extract_all`` (unless *pre_parsed* is provided)
    - Managing file_id cache and mtimes
    - Deleting old symbols for the TU
    - Building and inserting symbol rows with ``is_project`` computed from
      *vendor_patterns* and *project_patterns*
    - Building and inserting reference rows
    - Updating ``files.is_project`` for all files touched by this TU

    Returns ``(symbols_added, refs_added, headers)`` where *headers* is a
    list of ``{path, hash, generated}`` dicts for included header files,
    collected during the ifdef-filtered content pass.

    *conn* must be open; the caller is responsible for transactions.

    *vendor_patterns* and *project_patterns* are LIKE patterns (with ``%``
    wildcard) used to compute ``is_project`` for each symbol and file.
    Patterns must already be normalized (``third_party`` → ``third_party/%``).
    ``project_patterns`` take priority over ``vendor_patterns``.

    *pre_parsed* is an optional ``ExtractionResult`` from a prior
    ``extract_all`` call.  When provided, the libclang parse step is skipped
    entirely, which lets callers parse outside the write lock.  It must be the
    dataclass: its ``newly_seen_files`` is what tells the cleanup which files
    this parse owns, and a caller that cannot supply that has no business
    skipping the parse.

    *existing_files* is an optional ``{path: (file_id, mtime)}`` dict
    from ``get_file_mtimes()``.  When provided (bulk indexing path), it
    avoids a redundant full-scan of the files table inside the write lock.
    When ``None`` (reindex_file / watch paths), the lookup falls back to
    calling ``get_file_mtimes()``.
    """
    from fw_context_mcp.indexer.symbols import extract_all

    if vendor_patterns is None:
        vendor_patterns = []
    if project_patterns is None:
        project_patterns = []

    file_path = str(unit.file.resolve())
    normalized_tu_path = _normalize_file_path(file_path, project_root)
    try:
        current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    # Parse, or use the caller's ExtractionResult.
    #
    # This used to accept three positional tuple shapes as well, for callers
    # that predated the dataclass.  Nothing in the package passed one; the only
    # users were tests, and the 5-tuple shape had become actively wrong — it
    # carried no macros, so it read as "this parse found none" and the cleanup
    # cleared the stored ones with nothing to put back.
    tu = None
    newly_seen: set[str] = set()
    if pre_parsed is not None:
        result = pre_parsed
        tu = result.tu
        syms = result.symbols
        refs = result.references
        inheritance = result.inheritance
        indirect_call_sites = result.indirect_call_sites
        fp_assignments = result.fp_assignments
        macros = result.macros
        pending_dispatches = result.pending_dispatches
        # Every file whose cursors this parse walked — the set this TU owns in
        # this run.  Headers already claimed by an earlier TU are absent
        # (skip_files excluded their subtrees).  Drives stale-row deletion and
        # the content refresh below.
        newly_seen = set(result.newly_seen_files or ())
    else:
        # ── Run libclang parse inside the write lock ──
        # Two types of exceptions are handled differently:
        #   - sqlite3.Error: always fatal — the DB is likely corrupt,
        #     and continuing would produce an incomplete index.
        #   - RuntimeError / TranslationUnitLoadError with "unable to open
        #     database file": a known error from clang's module cache
        #     when the DB exceeds OS limits — fatal for the same reason.
        #   - Other RuntimeError / TranslationUnitLoadError: transient
        #     parse failures (corrupt PCH, missing include path, OOM
        #     during parse) — log and skip this TU; the indexer continues
        #     with the remaining TUs.
        try:
            result = extract_all(
                unit,
                with_refs=index_refs,
                return_tu=True,
                skip_files=set(skip_files) if skip_files else None,
            )
            tu = result.tu
            syms = result.symbols
            refs = result.references
            inheritance = result.inheritance
            indirect_call_sites = result.indirect_call_sites
            fp_assignments = result.fp_assignments
            macros = result.macros
            pending_dispatches = getattr(result, 'pending_dispatches', [])
            # Every file whose cursors this parse walked — the set this TU
            # owns in this run.  Headers already claimed by an earlier TU are
            # absent (skip_files excluded their subtrees).  Drives stale-row
            # deletion and the content refresh below.
            newly_seen = set(getattr(result, "newly_seen_files", ()) or ())
        except sqlite3.Error:
            log.error("Fatal DB error parsing %s — stopping indexer", unit.file.name)
            raise
        except (RuntimeError, TranslationUnitLoadError) as exc:
            msg = str(exc)
            if "unable to open database file" in msg:
                log.error("Fatal DB error parsing %s: %s — stopping indexer", unit.file.name, exc)
                raise
            log.warning("skip TU %s: %s", unit.file.name, exc)
            return 0, 0, []

    # ── Resolve known files for this TU ──
    # When the caller provides *existing_files* (bulk indexing path), use it
    # directly — avoids a redundant full-scan of the files table inside the
    # write lock (O(N) per TU → O(N²) total).  Falls back to get_file_mtimes()
    # for reindex_file / watch paths where the dict is not pre-built.
    if existing_files is not None:
        known = existing_files
    else:
        known = get_file_mtimes(conn, config_hash)

    # ── Clear body cache per TU — fresh reads for each translation unit ──
    _clear_body_cache()

    # ── Files this parse owns: the TU plus the headers it claimed ──
    # Symbol rows for a header live under the HEADER's file_id, not the TU's.
    # Deleting by the TU's file_id alone leaves them behind, and the fresh
    # rows then lose the ON CONFLICT(config_hash, usr) race in
    # insert_symbols_batch — a header definition would keep its first-index
    # line numbers forever and a deleted declaration would never disappear.
    owned_paths = {_normalize_file_path(p, project_root) for p in newly_seen}
    owned_paths.add(normalized_tu_path)

    # ── Phase 1: Save USRs of old symbols ──
    old_usrs = _save_old_state(conn, normalized_tu_path, known, owned_paths=owned_paths)

    # ── Phase 2: Delete old symbols ──
    _delete_old_for_tu(conn, config_hash, normalized_tu_path, known, syms, owned_paths=owned_paths)

    # Upsert the TU file record and capture its id.
    # When the caller provides hashes (bulk-indexing path with pre-computed
    # content/stability hashes), we store them in the files row.  These
    # hashes power the manifest verification step, which detects whether
    # a TU needs re-parsing by comparing current hashes to stored hashes.
    # Without hashes (reindex_file path), we still compute source_hash —
    # see the else branch for why it cannot be left out.
    if hashes is not None:
        source_hash, flags_hash, manifest_entry_hash = hashes
        content_hash_val = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
        tu_file_id = upsert_file(
            conn, config_hash, normalized_tu_path, unit.language,
            mtime=current_mtime, content_hash=content_hash_val,
            source_hash=source_hash, flags_hash=flags_hash,
        )
    else:
        # This call moves mtime forward, thus it MUST move source_hash too.
        # upsert_file keeps a stored hash when the caller passes an empty
        # one, which is right for a caller that knows nothing about the
        # content — but this caller just re-parsed the file.  Left out, the
        # row holds the hash of the text before the edit next to the mtime
        # of the text after it, and every staleness check in
        # mcp/shared/stale.py then reports the file as changed forever,
        # although its symbols are current.
        #
        # content_hash and flags_hash stay as they are: they feed the
        # manifest shortcut in _check_and_parse_unit, which compares them
        # against freshly computed values and reparses on a mismatch.  A
        # stale value there costs one parse, never a wrong answer.
        tu_file_id = upsert_file(
            conn, config_hash, normalized_tu_path, unit.language,
            mtime=current_mtime,
            source_hash=compute_source_hash(unit.file.resolve()),
        )

    syms_added = 0
    refs_added = 0
    file_id_cache: dict[str, int] = {normalized_tu_path: tu_file_id}

    if syms:
        syms_added, file_proj = _store_symbol_rows(
            conn, config_hash, syms, file_id_cache, project_root,
            vendor_patterns, project_patterns, build_dir_patterns,
        )
        # Update files.is_project for all files touched by this TU.
        # Using ``is_project < ip`` ensures a file that was previously
        # vendor-only (is_project=0) gets upgraded to project (is_project=1)
        # when any symbol in it matches a project_pattern — but a project
        # file is never downgraded to vendor.
        for fid, ip in file_proj.items():
            conn.execute(
                "UPDATE files SET is_project = ? WHERE id = ? AND is_project < ?",
                (ip, fid, ip),
            )
        _detect_moved_symbols(conn, config_hash, syms, old_usrs, file_id_cache, project_root)

    # Every path written to or matched against the database goes through the
    # one normaliser.  There used to be a second, local one here that omitted
    # project_root.resolve(); the two agreed for an ordinary checkout and
    # diverged for a symlinked project root, which would have made
    # refs.from_file unmatchable against files.path.
    def _rel(p: str) -> str:
        return _normalize_file_path(p, project_root)

    # No per-table delete set is derived here any more.  The stale rows of
    # every file this parse owns — the reference tables included — were
    # cleared by replace_file_data() in phase 2, from the file ids alone.

    # References
    if index_refs and refs:
        # The last column is refs.slot_index.  It carries a value only for an
        # indirect reference that came from an element of a positional
        # one-dimensional array initializer; it is None everywhere else.
        ref_rows = [(config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind, r.slot_index) for r in refs]
        insert_refs_batch(conn, ref_rows)
        refs_added = len(ref_rows)

    # Indirect call sites (function pointer invocations)
    if index_refs and indirect_call_sites:
        ics_rows = [
            (
                config_hash,
                _rel(ics.from_file),
                ics.from_line,
                ics.from_usr,
                ics.expr_text,
                ics.target_usr,
                ics.target_name,
                ics.fn_ptr_type,
            )
            for ics in indirect_call_sites
        ]
        insert_indirect_call_sites_batch(conn, ics_rows)

    # Function pointer assignments (Phase 3 — links assignments to call sites)
    if index_refs and fp_assignments:
        fpa_rows = [
            (
                config_hash,
                _rel(fpa.from_file),
                fpa.from_line,
                fpa.lhs_usr,
                fpa.lhs_name,
                fpa.rhs_usr,
                fpa.rhs_name,
                fpa.fn_ptr_type,
                fpa.method,
                fpa.from_usr,
            )
            for fpa in fp_assignments
        ]
        insert_fp_assignments_batch(conn, fpa_rows)

    # Pending dispatch edges (for deferred resolution in post-processing)
    # Dispatch edges (event-loop callbacks, thread starts, timer callbacks)
    # cannot be resolved during per-TU processing because the target symbol
    # may live in a different TU that hasn't been parsed yet.  Instead, we
    # store them in a temp table and resolve them after ALL TUs are indexed
    # (see the dispatch bridging phase in runner.py).
    if index_refs and pending_dispatches:
        conn.execute(
            """CREATE TEMP TABLE IF NOT EXISTS _pending_dispatch (
                config_hash TEXT,
                callee_qn TEXT,
                target_qn_partial TEXT,
                target_name TEXT,
                target_usr TEXT,
                file TEXT,
                line INTEGER,
                caller_usr TEXT
            )"""
        )
        pd_rows = [
            (
                config_hash,
                pd.callee_qn,
                pd.target_qn_partial,
                pd.target_name,
                pd.target_usr or "",
                _rel(pd.file),
                pd.line,
                pd.caller_usr or "",
            )
            for pd in pending_dispatches
        ]
        conn.executemany(
            "INSERT INTO _pending_dispatch VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            pd_rows,
        )

    # Inheritance
    if inheritance:
        inheritance_rows = [(config_hash, i.derived_usr, i.base_usr, i.access, int(i.is_virtual)) for i in inheritance]
        insert_inheritance_batch(conn, inheritance_rows)

    # Macros
    _t_macros = time.monotonic()
    _store_macros_for_unit(
        conn, config_hash, macros, file_path, normalized_tu_path,
        current_mtime, tu_file_id, file_id_cache, project_root,
    )
    _t_macros = time.monotonic() - _t_macros

    # Fill files.content with ifdef-filtered content (tokenization pass)
    _t_content = time.monotonic()
    _, headers = _build_filtered_file_content(
        conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns, existing_tu=tu,
        skip_files=skip_files, refresh_paths=owned_paths,
    )
    _t_content = time.monotonic() - _t_content

    if _t_macros > 0.1 or _t_content > 0.1:
        log.debug(
            "store_symbols_for_unit %s: macros=%.2fs content_fill=%.2fs", Path(file_path).name, _t_macros, _t_content
        )

    return syms_added, refs_added, headers


# ---------------------------------------------------------------------------
# Cross-TU reference backfill — post-processing phase
# ---------------------------------------------------------------------------

# Regex matching obj.method( and obj->method( patterns in source lines.
# Used by backfill_cross_tu_refs to find call sites that per-TU fallback
# could not resolve (because the callee was defined in another TU and not
# declared in any included header).
#
# Captures two groups:
#   group(1) — the object/receiver name (unused, but validated by regex)
#   group(2) — the method name (used to resolve against the symbols table)
_CALL_PATTERN_RE = re.compile(r"(\w+)(?:\.|->)(\w+)\s*\(")


def backfill_cross_tu_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    project_root: Path,
) -> int:
    """Post-processing: backfill cross-TU call references.

    Scans project source files for ``obj.method(`` and ``obj->method(``
    patterns on lines inside known function bodies that have no existing
    call/indirect reference.  Resolves the callee via the global symbols
    table (``_resolve_target_usr``) — which has visibility across ALL TUs
    after the TU loop completes.

    This fills the gap left by per-TU ``_qn_to_usr`` which only contains
    symbols from the current translation unit.  Cross-TU method calls
    (e.g. a private method defined in another ``.cpp`` file) are resolved
    here using the complete symbols table.

    Returns:
        Number of new references inserted.
    """
    total_added = 0

    # 1. Get all project source files (.c/.cpp) with indexed content.
    #    Only project files are scanned — vendor SDK code is not expected
    #    to have cross-TU method calls that libclang misses.  Files without
    #    content (content='') are skipped because we need the ifdef-filtered
    #    source text to scan for method call patterns.
    source_files = conn.execute(
        """SELECT f.path, f.content, f.language
           FROM files f
           WHERE f.config_hash = ? AND f.is_project = 1
             AND f.content != ''
             AND (f.path LIKE '%.cpp' OR f.path LIKE '%.c'
                  OR f.path LIKE '%.cc' OR f.path LIKE '%.cxx')""",
        (config_hash,),
    ).fetchall()

    for frow in source_files:
        file_path_rel = frow["path"]
        content = frow["content"]
        if not content:
            continue

        lines = content.splitlines()
        n_lines = len(lines)

        # 2. Get function/method definitions in this file → fn_spans.
        #    Only definitions (is_definition=1) with valid end_line.
        fn_rows = conn.execute(
            """SELECT usr, line, end_line
               FROM symbols
               WHERE config_hash = ? AND file_path = ?
                 AND is_definition = 1
                 AND kind IN ('function', 'method', 'constructor', 'destructor')
                 AND end_line > line""",
            (config_hash, file_path_rel),
        ).fetchall()

        if not fn_rows:
            continue  # No function bodies in this file

        # Build function spans: [(usr, start_line, end_line)]
        fn_spans: list[tuple[str, int, int]] = [
            (r["usr"], r["line"], r["end_line"]) for r in fn_rows
        ]

        # 3. Get lines that already have call/indirect refs in this file.
        existing_ref_lines = {
            r["from_line"]
            for r in conn.execute(
                """SELECT DISTINCT from_line FROM refs
                   WHERE config_hash = ? AND from_file = ?
                     AND ref_kind IN ('call', 'indirect')""",
                (config_hash, file_path_rel),
            ).fetchall()
        }

        # 4. For each function span, scan lines without existing refs.
        new_refs: list[tuple] = []
        for fn_usr, fn_start, fn_end in fn_spans:
            # Clamp to actual line count — fn_end may extend past
            # the file length if the source was truncated or if
            # libclang reported an extent beyond EOF.
            span_end = min(fn_end, n_lines)
            for lineno in range(fn_start, span_end + 1):
                if lineno in existing_ref_lines:
                    continue
                line_text = lines[lineno - 1] if 0 < lineno <= n_lines else ""
                if not line_text:
                    continue
                # Scan for obj.method( and obj->method( patterns.
                # Each match yields a potential cross-TU call that
                # libclang's per-TU cursor visitor could not resolve.
                for m in _CALL_PATTERN_RE.finditer(line_text):
                    method_name = m.group(2)
                    # Skip common false positives — C++ keywords and
                    # builtins that match the regex but are not method
                    # calls (e.g. `if (` in minified code).
                    if method_name in ("if", "for", "while", "switch", "sizeof", "return"):
                        continue
                    target_usr = _resolve_target_usr(conn, config_hash, method_name)
                    # Avoid self-references — a function calling itself
                    # (recursion) would already have a per-TU ref.
                    if target_usr and target_usr != fn_usr:
                        new_refs.append(
                            (config_hash, target_usr, file_path_rel, lineno,
                             fn_usr, "call", None)
                        )
                        # Mark this line as having a ref to avoid
                        # inserting duplicate references when multiple
                        # method calls appear on the same line.
                        existing_ref_lines.add(lineno)

        if new_refs:
            insert_refs_batch(conn, new_refs)
            total_added += len(new_refs)

    return total_added
