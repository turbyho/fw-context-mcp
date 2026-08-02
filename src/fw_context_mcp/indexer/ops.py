"""Shared index operations used by runner.py and MCP server tools.

Extracts the duplicated "parse TU → store symbols" loop into a single
function so the indexer, reindex_file, and auto-reindex all use the
same code path.
"""

from __future__ import annotations

import logging
import sqlite3
import time

try:
    from clang.cindex import TranslationUnitLoadError
except ImportError:
    TranslationUnitLoadError = RuntimeError  # clang not available — use fallback
from collections import OrderedDict
from pathlib import Path

from fw_context_mcp.indexer.config_hash import compute_tu_content_hash
from fw_context_mcp.indexer.db import (
    delete_fp_assignments_for_file,
    delete_indirect_call_sites_for_file,
    delete_inheritance_for_file,
    delete_macros_for_file,
    delete_refs_for_file,
    delete_symbols_for_file,
    get_file_mtimes,
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_inheritance_batch,
    insert_macros_batch,
    insert_refs_batch,
    insert_symbols_batch,
    split_tokens,
    upsert_file,
    upsert_llm_analysis_batch,
)
from fw_context_mcp.utils import SAFE_EXCEPT, abs_path, compute_content_hash, compute_source_hash, read_file_lines

log = logging.getLogger(__name__)

# NOTE(turbyho, 2026-07-31): Each header is opened/read/closed individually — 500+ headers = 500+
# open/read/close cycles per TU.  Consider batch I/O for read_file_lines.
_body_cache: OrderedDict[str, list[str] | None] = OrderedDict()
_BODY_CACHE_MAX_ENTRIES = 200


def _cached_read_lines(abs_path: str) -> list[str] | None:
    if abs_path not in _body_cache:
        if len(_body_cache) >= _BODY_CACHE_MAX_ENTRIES:
            _body_cache.popitem(last=False)
        _body_cache[abs_path] = read_file_lines(abs_path)
    else:
        _body_cache.move_to_end(abs_path)
    return _body_cache[abs_path]


def _clear_body_cache() -> None:
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

    Files outside *project_root* (SDK, framework) keep their absolute path.
    This is the canonical path normalization for the ``files.path`` column —
    consistent with :func:`_build_filtered_file_content`.
    """
    resolved_root = project_root.resolve()
    try:
        return str(Path(file_path).resolve().relative_to(resolved_root))
    except ValueError:
        return file_path


def _build_filtered_file_content(
    conn, unit, config_hash: str, project_root: Path, *, build_dir_patterns: list[str] | None = None, existing_tu=None
) -> tuple[int, list[dict]]:
    """Tokenize TU, extract active lines per file, store ifdef-filtered content.

    Parses *unit* (a ``CompilationUnit`` data class) with libclang, then
    tokenizes to find which source lines are active (not ``#ifdef``-dead).
    Only processes files whose ``content`` column is still empty — once set,
    content is trusted until the next full reindex.

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

    from fw_context_mcp.indexer.manifest import HEADER_EXTS as _HEADER_EXTS
    from fw_context_mcp.indexer.manifest import _is_generated_header

    # ── Collect included header paths + SHA-256 hashes (always needed for manifest) ──
    headers: list[dict] = []
    seen_headers: set[str] = set()

    for inc in tu.get_includes():
        abs_path = str(inc.include.name)
        if abs_path in seen_headers:
            continue
        seen_headers.add(abs_path)

        resolved = Path(abs_path).resolve()
        if resolved.suffix.lower() not in _HEADER_EXTS:
            continue

        try:
            rel = str(resolved.relative_to(project_root))
        except ValueError:
            rel = str(resolved)

        h = compute_source_hash(resolved)
        generated = _is_generated_header(rel, build_dir_patterns)
        headers.append({"path": rel, "hash": h, "generated": generated})

    # ── Fast-path return: all files already have filtered content ──
    # Skip tokenization and AST walk — they're only needed for content-fill,
    # not for header collection (which only needs get_includes, already done).
    filled = 0
    if remaining == 0:
        return 0, headers

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
    def _collect_all_active_lines(root_cursor) -> None:
        """Walk the AST iteratively and add extent line ranges keyed by source file."""
        stack: list = [root_cursor]
        while stack:
            cursor = stack.pop()
            if cursor.location.file:
                fname = str(cursor.location.file.name)
                extent = cursor.extent
                if extent.start.file and extent.end.file:
                    for line in range(extent.start.line, extent.end.line + 1):
                        active.setdefault(fname, set()).add(line)
            for child in cursor.get_children():
                stack.append(child)

    _collect_all_active_lines(tu.cursor)

    for abs_path, active_lines in active.items():
        if not active_lines:
            continue

        db_path = _normalize_file_path(abs_path, project_root)
        resolved = Path(abs_path).resolve()

        # Already processed?
        row = conn.execute(
            "SELECT content FROM files WHERE config_hash=? AND path=?",
            (config_hash, db_path),
        ).fetchone()
        if row and row[0]:
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
        lang = "cpp" if db_path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hxx")) else "c"
        # Grab real mtime so _count_modified_files won't flag this as stale.
        try:
            file_mtime = resolved.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        conn.execute(
            "INSERT INTO files (config_hash, path, language, content, mtime) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (config_hash, path) DO UPDATE SET content = excluded.content",
            (config_hash, db_path, lang, content, file_mtime),
        )
        filled += 1

    if filled:
        log.info("content fill: %d files from TU %s in %.1fs", filled, unit.file.name, _time.monotonic() - _t0)
    return filled, headers


def _store_symbol_rows(
    conn: sqlite3.Connection,
    config_hash: str,
    syms: list,
    file_id_cache: dict[str, int],
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
) -> tuple[int, dict[int, int]]:
    """Build symbol rows for every symbol in *syms*, insert them in a batch,
    and return ``(syms_added, file_proj)``.

    *file_proj* maps ``file_id → max(is_project)`` so the caller can update
    ``files.is_project`` across all files touched by this TU.
    """
    from .sdk_detect import _path_matches

    rows = []
    file_proj: dict[int, int] = {}
    for s in syms:
        sym_file = s.file
        normalized_sym_file = _normalize_file_path(sym_file, project_root)
        if normalized_sym_file not in file_id_cache:
            lang = "cpp" if Path(sym_file).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
            try:
                sym_mtime = Path(sym_file).stat().st_mtime
            except OSError:
                sym_mtime = 0.0
            file_id_cache[normalized_sym_file] = upsert_file(
                conn, config_hash, normalized_sym_file, lang, mtime=sym_mtime,
            )
        rel_path = normalized_sym_file
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
        body = ""
        if s.is_definition and s.end_line > s.line:
            file_lines = _cached_read_lines(s.file)
            if file_lines is not None:
                body = _read_body(file_lines, s.line, s.end_line)
        fid = file_id_cache[normalized_sym_file]
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
            )
        )
    if rows:
        insert_symbols_batch(conn, rows)
    return len(rows), file_proj


def _restore_llm_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
    syms: list,
    saved_analyses: dict[str, dict],
    *,
    cache_client=None,
) -> None:
    """Restore LLM analysis for symbols whose body didn't change.

    Uses per-build saved analysis (exact USR match) as the primary source,
    then falls back to the global local cache, then the remote cache server.
    """
    from fw_context_mcp.cache_client import (
        get_local_cache_db,
        local_cache_lookup,
        local_cache_upsert,
    )

    local_db = get_local_cache_db(readonly=True)
    restored = 0
    try:
        for s in syms:
            lines = _cached_read_lines(s.file)
            if lines is None:
                continue
            body = _read_body(lines, s.line, s.end_line)
            new_ch = compute_content_hash(body, s.qualified_name, s.signature, s.docstring)

            cached: dict | None = None
            saved = saved_analyses.get(s.usr)
            if saved is not None and saved.get("content_hash") == new_ch:
                cached = saved
            else:
                cached = local_cache_lookup(local_db, [new_ch]).get(new_ch)

            # Tier C: remote cache server
            if not cached and cache_client is not None:
                try:
                    remote_hits = cache_client.batch_get([new_ch])
                    cached = remote_hits.get(new_ch)
                    if cached:
                        # Store in local global cache for next time
                        writable_db = get_local_cache_db(readonly=False)
                        try:
                            local_cache_upsert(writable_db, [{"hash": new_ch, **cached}])
                        finally:
                            writable_db.close()
                except SAFE_EXCEPT:
                    pass  # remote down → graceful fallback

            if not cached:
                continue
            new_id = conn.execute(
                "SELECT id FROM symbols WHERE config_hash = ? AND usr = ?",
                (config_hash, s.usr),
            ).fetchone()
            if not new_id:
                continue
            upsert_llm_analysis_batch(
                conn,
                [
                    (
                        new_id[0],
                        cached["summary"],
                        cached["inputs"],
                        cached["outputs"],
                        cached["model"],
                        new_ch,
                    )
                ],
            )
            restored += 1
        if restored:
            log.debug("Restored LLM analysis for %d symbols from cache", restored)
    finally:
        local_db.close()


def _detect_moved_symbols(
    conn: sqlite3.Connection,
    config_hash: str,
    syms: list,
    old_usrs: set[str],
    file_id_cache: dict[str, int],
    project_root: Path,
) -> None:
    """Detect symbols that moved between files without content changes.

    When a symbol has the same USR, same content hash, but a different file_id
    compared to the previous index, update its file_id in place and delete
    the duplicate row created by the current batch insert.
    """
    moved = 0
    for s in syms:
        if s.usr in old_usrs:
            continue
        normalized_sym_file = _normalize_file_path(s.file, project_root)
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
        if old_row["summary"] is None:
            continue
        if old_row["file_id"] == file_id_cache[normalized_sym_file]:
            continue

        lines = _cached_read_lines(s.file)
        if lines is None:
            continue
        old_lines = _cached_read_lines(abs_path(project_root, old_row["file_path"]))
        if old_lines is None:
            continue
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
        dup_id = conn.execute(
            "SELECT id FROM symbols WHERE config_hash = ? AND usr = ? AND id != ?",
            (config_hash, s.usr, old_row["id"]),
        ).fetchone()
        if dup_id:
            conn.execute("DELETE FROM symbols WHERE id = ?", (dup_id[0],))
        moved += 1
    if moved:
        log.debug("Detected %d moved symbols", moved)


def _save_old_state(
    conn: sqlite3.Connection,
    config_hash: str,
    normalized_tu_path: str,
    known: dict[str, tuple[int, float]],
) -> tuple[set[str], dict[str, dict]]:
    """Save USRs and LLM analysis of symbols that existed in a previous build.

    Returns (old_usrs, saved_analyses) for use by _restore_llm_analysis
    and _detect_moved_symbols later in the pipeline.
    """
    old_usrs: set[str] = set()
    saved_analyses: dict[str, dict] = {}
    if normalized_tu_path not in known:
        return old_usrs, saved_analyses
    file_id_old = known[normalized_tu_path][0]
    old_rows = conn.execute(
        """SELECT s.usr, a.summary, a.inputs, a.outputs, a.model, a.content_hash,
                  s.source, s.qualified_name, s.signature, s.docstring
           FROM symbols s
           LEFT JOIN llm_analysis a ON a.symbol_id = s.id
           WHERE s.file_id = ?""",
        (file_id_old,),
    ).fetchall()
    for r in old_rows:
        old_usrs.add(r["usr"])
        if r["summary"]:
            ch = r["content_hash"] or ""
            if not ch and r["source"]:
                # content_hash was NULL — compute from the OLD body stored
                # in the symbols table so we can detect whether the source
                # file changed between the previous index and now.
                ch = compute_content_hash(
                    r["source"], r["qualified_name"], r["signature"], r["docstring"]
                )
            saved_analyses[r["usr"]] = {
                "summary": r["summary"],
                "inputs": r["inputs"],
                "outputs": r["outputs"],
                "model": r["model"],
                "content_hash": ch,
            }
    return old_usrs, saved_analyses


def _delete_old_for_tu(
    conn: sqlite3.Connection,
    config_hash: str,
    normalized_tu_path: str,
    known: dict[str, tuple[int, float]],
    syms: list,
) -> None:
    """Delete stale inheritance and symbol rows for a TU before re-insertion.

    Removes inheritance edges for classes being re-parsed, then drops all
    symbols and inheritance records tied to the TU's file_id.
    """
    _class_kinds = frozenset({"class", "struct"})
    _class_usrs = {s.usr for s in syms if s.kind in _class_kinds}
    if _class_usrs:
        for usr in _class_usrs:
            conn.execute(
                "DELETE FROM inheritance WHERE config_hash = ? AND derived_usr = ?",
                (config_hash, usr),
            )
    if normalized_tu_path in known:
        file_id_old = known[normalized_tu_path][0]
        delete_inheritance_for_file(conn, config_hash, file_id_old)
        delete_symbols_for_file(conn, file_id_old)


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
    """Insert or update macro definitions for one translation unit.

    Upserts file records for header files that contain macros, then
    replaces all macros previously stored for this TU with the fresh set.
    """
    if not macros:
        return
    macro_rows: list[tuple] = []
    for m in macros:
        m_raw = str(m.file) if m.file else file_path
        m_path = _normalize_file_path(m_raw, project_root)
        if m_path not in file_id_cache:
            lang = "cpp" if Path(m_raw).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
            m_mtime = current_mtime if m_path == normalized_tu_path else 0.0
            try:
                m_mtime = Path(m_raw).stat().st_mtime
            except OSError:
                pass
            file_id_cache[m_path] = upsert_file(conn, config_hash, m_path, lang, mtime=m_mtime)
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
    delete_macros_for_file(conn, tu_file_id)
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
    pre_parsed=None,
    existing_files: dict[str, tuple[int, float]] | None = None,
    hashes=None,
    build_dir_patterns: list[str] | None = None,
    cache_client=None,
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

    *pre_parsed* is an optional tuple ``(syms, refs, inheritance,
    indirect_call_sites, fp_assignments)`` from a prior ``extract_all``
    call.  When provided, the libclang parse step is skipped entirely.
    This allows callers to run expensive parsing outside the write lock.

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

    # Parse (or use caller-supplied pre-parsed data)
    tu = None
    if pre_parsed is not None:
        # ExtractionResult dataclass — use named fields instead of positional unpacking
        if hasattr(pre_parsed, 'tu'):
            result = pre_parsed
            tu = result.tu
            syms = result.symbols
            refs = result.references
            inheritance = result.inheritance
            indirect_call_sites = result.indirect_call_sites
            fp_assignments = result.fp_assignments
            macros = result.macros
        elif len(pre_parsed) == 7:
            tu, syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = pre_parsed
        elif len(pre_parsed) == 6:
            syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = pre_parsed
        else:
            syms, refs, inheritance, indirect_call_sites, fp_assignments = pre_parsed
            macros = []
    else:
        try:
            result = extract_all(
                unit,
                with_refs=index_refs,
                return_tu=True,
            )
            tu = result.tu
            syms = result.symbols
            refs = result.references
            inheritance = result.inheritance
            indirect_call_sites = result.indirect_call_sites
            fp_assignments = result.fp_assignments
            macros = result.macros
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

    # ── Phase 1: Save USRs + analysis of old symbols ──
    old_usrs, saved_analyses = _save_old_state(conn, config_hash, normalized_tu_path, known)

    # ── Phase 2: Delete old symbols ──
    _delete_old_for_tu(conn, config_hash, normalized_tu_path, known, syms)

    # Upsert the TU file record and capture its id
    if hashes is not None:
        source_hash, flags_hash, manifest_entry_hash = hashes
        content_hash_val = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
        tu_file_id = upsert_file(
            conn, config_hash, normalized_tu_path, unit.language,
            mtime=current_mtime, content_hash=content_hash_val,
            source_hash=source_hash, flags_hash=flags_hash,
        )
    else:
        tu_file_id = upsert_file(conn, config_hash, normalized_tu_path, unit.language, mtime=current_mtime)

    syms_added = 0
    refs_added = 0
    file_id_cache: dict[str, int] = {normalized_tu_path: tu_file_id}

    if syms:
        syms_added, file_proj = _store_symbol_rows(
            conn, config_hash, syms, file_id_cache, project_root,
            vendor_patterns, project_patterns,
        )
        for fid, ip in file_proj.items():
            conn.execute(
                "UPDATE files SET is_project = ? WHERE id = ? AND is_project < ?",
                (ip, fid, ip),
            )
        _restore_llm_analysis(conn, config_hash, syms, saved_analyses, cache_client=cache_client)
        _detect_moved_symbols(conn, config_hash, syms, old_usrs, file_id_cache, project_root)

    # Path-relative helper used by refs and indirect_call_sites blocks
    def _rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(project_root))
        except ValueError:
            return p

    tu_rel = _rel(file_path)

    # References
    if index_refs and refs:
        delete_refs_for_file(conn, config_hash, tu_rel)
        ref_rows = [(config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind) for r in refs]
        insert_refs_batch(conn, ref_rows)
        refs_added = len(ref_rows)

    # Indirect call sites (function pointer invocations)
    if index_refs and indirect_call_sites:
        delete_indirect_call_sites_for_file(conn, config_hash, tu_rel)
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
        delete_fp_assignments_for_file(conn, config_hash, tu_rel)
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
        conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns, existing_tu=tu
    )
    _t_content = time.monotonic() - _t_content

    if _t_macros > 0.1 or _t_content > 0.1:
        log.debug(
            "store_symbols_for_unit %s: macros=%.2fs content_fill=%.2fs", Path(file_path).name, _t_macros, _t_content
        )

    return syms_added, refs_added, headers
