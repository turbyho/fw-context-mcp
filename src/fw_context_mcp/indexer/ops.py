"""Shared index operations used by runner.py and MCP server tools.

Extracts the duplicated "parse TU → store symbols" loop into a single
function so the indexer, reindex_file, and auto-reindex all use the
same code path.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fw_context_mcp.indexer.db import (
    _ensure_column,
    delete_fp_assignments_for_file,
    delete_indirect_call_sites_for_file,
    delete_inheritance_for_file,
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
from fw_context_mcp.utils import compute_content_hash, read_file_lines

log = logging.getLogger(__name__)


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


def _build_filtered_file_content(
    conn, unit, config_hash: str, project_root: Path
) -> int:
    """Tokenize TU, extract active lines per file, store ifdef-filtered content.

    Parses *unit* (a ``CompilationUnit`` data class) with libclang, then
    tokenizes to find which source lines are active (not ``#ifdef``-dead).
    Only processes files whose ``content`` column is still empty — once set,
    content is trusted until the next full reindex.

    Inactive lines are replaced with ``\\n`` so line numbers stay
    consistent with the original source.

    Returns the number of files whose content was filled.
    """
    # Fast path: skip if no files need content filling
    _ensure_column(conn, "files", "content", "TEXT NOT NULL DEFAULT ''")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM files WHERE config_hash=? AND content=''",
        (config_hash,),
    ).fetchone()[0]
    if remaining == 0:
        return 0

    import time as _time
    _t0 = _time.monotonic()

    from clang import cindex as cx

    from fw_context_mcp.indexer.symbols import _get_index

    try:
        tu = _get_index().parse(
            str(unit.file),
            args=unit.clang_args,
            options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
        tokens = list(tu.cursor.get_tokens())
    except Exception:
        log.debug("_build_filtered_file_content: parse/tokenization failed for %s", unit.file.name)
        return 0

    # Collect active lines -> (abs_path, set of line numbers)
    active: dict[str, set[int]] = {}
    for tok in tokens:
        loc = tok.location
        if loc.file is not None:
            fname = str(loc.file.name)
            active.setdefault(fname, set()).add(loc.line)

    filled = 0
    for abs_path, active_lines in active.items():
        if not active_lines:
            continue

        try:
            rel_path = str(Path(abs_path).resolve().relative_to(project_root))
        except ValueError:
            continue  # file outside project tree

        # Already processed?
        row = conn.execute(
            "SELECT content FROM files WHERE config_hash=? AND path=?",
            (config_hash, rel_path),
        ).fetchone()
        if row and row[0]:
            continue  # already have filtered content

        # Read original file
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                original = f.readlines()
        except Exception:
            continue

        if not original:
            continue

        # Replace inactive lines with \n to preserve line numbers
        max_line = max(active_lines)
        filtered = [
            original[i - 1] if (i in active_lines) else "\n"
            for i in range(1, min(len(original), max_line) + 1)
        ]
        content = "".join(filtered)

        # Insert or update — file rows may not exist for headers-only files.
        lang = "cpp" if rel_path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hxx")) else "c"
        # Grab real mtime so _count_modified_files won't flag this as stale.
        try:
            file_mtime = Path(abs_path).stat().st_mtime
        except OSError:
            file_mtime = 0.0
        conn.execute(
            "INSERT INTO files (config_hash, path, language, content, mtime) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (config_hash, path) DO UPDATE SET content = excluded.content",
            (config_hash, rel_path, lang, content, file_mtime),
        )
        filled += 1

    if filled:
        log.info("content fill: %d files from TU %s in %.1fs", filled, unit.file.name, _time.monotonic() - _t0)
    return filled


def store_symbols_for_unit(
    conn,
    unit,
    config_hash: str,
    project_root: Path,
    source_roots: list[Path],
    exclude_paths: list[Path] | None = None,
    index_refs: bool = False,
    pre_parsed=None,
    existing_files: dict[str, tuple[int, float]] | None = None,
) -> tuple[int, int]:
    """Parse one translation unit and store its symbols + refs in the DB.

    Handles:
    - Running libclang ``extract_all`` (unless *pre_parsed* is provided)
    - Managing file_id cache and mtimes
    - Deleting old symbols for the TU
    - Building and inserting symbol rows
    - Building and inserting reference rows

    Returns ``(symbols_added, refs_added)``.

    *conn* must be open; the caller is responsible for transactions.

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

    if exclude_paths is None:
        exclude_paths = []

    file_path = str(unit.file.resolve())
    try:
        current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    # Parse (or use caller-supplied pre-parsed data)
    if pre_parsed is not None:
        if len(pre_parsed) == 6:
            syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = pre_parsed
        else:
            syms, refs, inheritance, indirect_call_sites, fp_assignments = pre_parsed
            macros = []
    else:
        try:
            syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = extract_all(
                unit,
                source_roots=source_roots,
                exclude_paths=exclude_paths,
                with_refs=index_refs,
            )
        except Exception as exc:
            log.warning("skip TU %s: %s", unit.file.name, exc)
            return 0, 0

    # ── Resolve known files for this TU ──
    # When the caller provides *existing_files* (bulk indexing path), use it
    # directly — avoids a redundant full-scan of the files table inside the
    # write lock (O(N) per TU → O(N²) total).  Falls back to get_file_mtimes()
    # for reindex_file / watch paths where the dict is not pre-built.
    if existing_files is not None:
        known = existing_files
    else:
        known = get_file_mtimes(conn, config_hash)

    # ── File-content cache for body hashing ──
    # Each source file is read at most once per TU; all symbols from
    # the same file share the cached lines.
    _body_cache: dict[str, list[str] | None] = {}

    def _cached_lines(abs_path: str) -> list[str] | None:
        if abs_path not in _body_cache:
            _body_cache[abs_path] = read_file_lines(abs_path)
        return _body_cache[abs_path]


    # ── Phase 1: Save USRs + analysis of old symbols ──
    # Phase 3 restores per-build LLM analysis for symbols whose body
    # didn't change; Phase 4 detects file-moves.  ON DELETE CASCADE
    # removes llm_analysis when old symbols are deleted, so we save
    # it beforehand and restore it by USR match (preferred over the
    # global cache which may contain stale entries from other projects).
    old_usrs: set[str] = set()
    saved_analyses: dict[str, dict] = {}  # usr → {summary, inputs, output, model, content_hash}
    if file_path in known:
        file_id_old, _ = known[file_path]
        old_rows = conn.execute(
            """SELECT s.usr, a.summary, a.inputs, a.outputs, a.model, a.content_hash
               FROM symbols s
               LEFT JOIN llm_analysis a ON a.symbol_id = s.id
               WHERE s.file_id = ?""", (file_id_old,),
        ).fetchall()
        for r in old_rows:
            old_usrs.add(r["usr"])
            if r["summary"]:
                saved_analyses[r["usr"]] = {
                    "summary": r["summary"],
                    "inputs": r["inputs"],
                    "outputs": r["outputs"],
                    "model": r["model"],
                    "content_hash": r["content_hash"],
                }

    # ── Phase 2: Delete old symbols (existing logic) ──
    if file_path in known:
        file_id_old, _ = known[file_path]
        delete_inheritance_for_file(conn, config_hash, file_id_old)
        delete_symbols_for_file(conn, file_id_old)
        # ON DELETE CASCADE → llm_analysis, embeddings removed

    # Upsert the TU file record
    upsert_file(conn, config_hash, file_path, unit.language, mtime=current_mtime)

    syms_added = 0
    refs_added = 0

    if syms:
        file_id_cache: dict[str, int] = {}
        # Pre-compute source_roots as strings for is_project checks
        source_root_strs = [str(r) for r in source_roots] if source_roots else []
        rows = []
        for s in syms:
            sym_file = s.file
            if sym_file not in file_id_cache:
                lang = "cpp" if Path(sym_file).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
                if sym_file == file_path:
                    sym_mtime = current_mtime
                else:
                    try:
                        sym_mtime = Path(sym_file).stat().st_mtime
                    except OSError:
                        sym_mtime = 0.0
                file_id_cache[sym_file] = upsert_file(
                    conn, config_hash, sym_file, lang, mtime=sym_mtime,
                )
            try:
                rel_path = str(Path(sym_file).resolve().relative_to(project_root))
            except ValueError:
                rel_path = sym_file
            # Determine is_project using the same logic as _is_project_local
            is_proj = 0
            if source_root_strs:
                rp = rel_path.rstrip("/")
                for root_s in source_root_strs:
                    root_n = root_s.rstrip("/")
                    if rp.startswith(root_n + "/") or rp == root_n:
                        is_proj = 1
                        break
            # Read source body for definitions (for FTS5 search_bodies).
            body = ""
            if s.is_definition and s.end_line > s.line:
                file_lines = _cached_lines(s.file)
                if file_lines is not None:
                    body = _read_body(file_lines, s.line, s.end_line)
            rows.append((
                config_hash,
                file_id_cache[sym_file],
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
                0.0,  # pagerank (computed later by _build_pagerank)
                body,
            ))
        insert_symbols_batch(conn, rows)
        syms_added = len(rows)

        # ── Phase 3: Restore LLM analysis — per-build first, then global cache ──
        restored = 0
        if syms:
            from fw_context_mcp.cache_client import get_local_cache_db, local_cache_lookup

            local_db = get_local_cache_db(readonly=True)
            for s in syms:
                lines = _cached_lines(s.file)
                if lines is None:
                    continue
                body = _read_body(lines, s.line, s.end_line)
                new_ch = compute_content_hash(body, s.qualified_name, s.signature, s.docstring)

                cached: dict | None = None
                # Prefer per-build saved analysis — exact USR match.
                # When content_hash is present, also verify it matches
                # the new body.  When empty (analysis from older index
                # before content_hash was populated), accept the saved
                # analysis as authoritative for the same USR.
                saved = saved_analyses.get(s.usr)
                if saved is not None and (
                    saved.get("content_hash") == new_ch
                    or not saved.get("content_hash")
                ):
                    cached = saved
                else:
                    # Fall back to local global cache
                    cached = local_cache_lookup(local_db, [new_ch]).get(new_ch)

                if not cached:
                    continue
                new_id = conn.execute(
                    "SELECT id FROM symbols WHERE config_hash = ? AND usr = ?",
                    (config_hash, s.usr),
                ).fetchone()
                if not new_id:
                    continue
                upsert_llm_analysis_batch(conn, [(
                    new_id[0], cached["summary"], cached["inputs"],
                    cached["outputs"], cached["model"], new_ch,
                )])
                restored += 1
            local_db.close()
        if restored:
            log.debug(
                "Restored LLM analysis for %d symbols from cache in %s",
                restored, Path(file_path).name,
            )

        # ── Phase 4: Detect and fix moved symbols ──
        # Symbols not in old_usrs may have moved from another file.
        # Find existing row with same USR, same content_hash, but different
        # file_id → update file_id, keep analysis, delete duplicate.
        moved = 0
        for s in syms:
            if s.usr in old_usrs:
                continue  # already handled in Phase 3
            old_row = conn.execute(
                """SELECT s.id, s.file_id, s.qualified_name, s.signature, s.line, s.end_line,
                          s.docstring, f.path as abs_path,
                          a.summary, a.inputs, a.outputs, a.model, a.analyzed_at
                   FROM symbols s
                   LEFT JOIN llm_analysis a ON a.symbol_id = s.id
                   JOIN files f ON f.id = s.file_id
                   WHERE s.usr = ? AND s.config_hash = ?""",
                (s.usr, config_hash),
            ).fetchone()
            if old_row is None:
                continue  # genuine new symbol
            if old_row["summary"] is None:
                continue  # no analysis to preserve — nothing to move
            if old_row["file_id"] == file_id_cache[s.file]:
                continue  # same file, wasn't in saved_analyses → new symbol

            # Same USR, different file — check if content matches
            lines = _cached_lines(s.file)
            if lines is None:
                continue
            old_lines = _cached_lines(old_row["abs_path"])
            if old_lines is None:
                continue
            old_ch = _compute_content_hash(
                old_lines, old_row["line"], old_row["end_line"],
                old_row["signature"], old_row["qualified_name"], old_row["docstring"],
            )
            new_ch = _compute_content_hash(
                lines, s.line, s.end_line, s.signature, s.qualified_name, s.docstring,
            )
            if old_ch != new_ch:
                continue  # content changed — treat as new symbol

            # Same content, different file — moved
            try:
                new_rel = str(Path(s.file).resolve().relative_to(project_root))
            except ValueError:
                new_rel = s.file
            conn.execute(
                """UPDATE symbols SET file_id = ?, file_path = ?,
                   line = ?, col = ?, end_line = ?
                   WHERE id = ?""",
                (file_id_cache[s.file], new_rel, s.line, s.column,
                 s.end_line, old_row["id"]),
            )
            # Delete the duplicate row just inserted by insert_symbols_batch
            dup_id = conn.execute(
                "SELECT id FROM symbols WHERE config_hash = ? AND usr = ? AND id != ?",
                (config_hash, s.usr, old_row["id"]),
            ).fetchone()
            if dup_id:
                conn.execute("DELETE FROM symbols WHERE id = ?", (dup_id[0],))
            moved += 1
        if moved:
            log.debug(
                "Detected %d moved symbols in %s", moved, Path(file_path).name,
            )

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
        ref_rows = [
            (config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind)
            for r in refs
        ]
        insert_refs_batch(conn, ref_rows)
        refs_added = len(ref_rows)

    # Indirect call sites (function pointer invocations)
    if index_refs and indirect_call_sites:
        delete_indirect_call_sites_for_file(conn, config_hash, tu_rel)
        ics_rows = [
            (config_hash, _rel(ics.from_file), ics.from_line, ics.from_usr,
             ics.expr_text, ics.target_usr, ics.target_name, ics.fn_ptr_type)
            for ics in indirect_call_sites
        ]
        insert_indirect_call_sites_batch(conn, ics_rows)

    # Function pointer assignments (Phase 3 — links assignments to call sites)
    if index_refs and fp_assignments:
        delete_fp_assignments_for_file(conn, config_hash, tu_rel)
        fpa_rows = [
            (config_hash, _rel(fpa.from_file), fpa.from_line,
             fpa.lhs_usr, fpa.lhs_name, fpa.rhs_usr, fpa.rhs_name,
             fpa.fn_ptr_type, fpa.method, fpa.from_usr)
            for fpa in fp_assignments
        ]
        insert_fp_assignments_batch(conn, fpa_rows)

    # Inheritance
    if inheritance:
        inheritance_rows = [
            (config_hash, i.derived_usr, i.base_usr, i.access, int(i.is_virtual))
            for i in inheritance
        ]
        insert_inheritance_batch(conn, inheritance_rows)

    # Macros
    _t_macros = time.monotonic()
    if macros:
        macro_rows: list[tuple] = []
        for m in macros:
            m_abs = str(m.file) if m.file else file_path
            # Use absolute path as files table key (consistent with symbol storage).
            if m_abs not in file_id_cache:
                lang = "cpp" if Path(m_abs).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
                m_mtime = current_mtime if m_abs == file_path else 0.0
                try:
                    m_mtime = Path(m_abs).stat().st_mtime
                except OSError:
                    pass
                file_id_cache[m_abs] = upsert_file(conn, config_hash, m_abs, lang, mtime=m_mtime)
            m_file_id = file_id_cache[m_abs]
            macro_rows.append((
                config_hash,
                m_file_id,
                m.name,
                m.value,
                m.expanded_value,
                m.line,
                int(m.is_function_like),
            ))
        if macro_rows:
            insert_macros_batch(conn, macro_rows)
    _t_macros = time.monotonic() - _t_macros

    # Fill files.content with ifdef-filtered content (tokenization pass)
    _t_content = time.monotonic()
    _build_filtered_file_content(conn, unit, config_hash, project_root)
    _t_content = time.monotonic() - _t_content

    if _t_macros > 0.1 or _t_content > 0.1:
        log.info("store_symbols_for_unit %s: macros=%.2fs content_fill=%.2fs", Path(file_path).name, _t_macros, _t_content)

    return syms_added, refs_added
