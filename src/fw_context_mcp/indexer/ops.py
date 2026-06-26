"""Shared index operations used by runner.py and MCP server tools.

Extracts the duplicated "parse TU → store symbols" loop into a single
function so the indexer, reindex_file, and auto-reindex all use the
same code path.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fw_context_mcp.indexer.db import (
    delete_indirect_call_sites_for_file,
    delete_inheritance_for_file,
    delete_refs_for_file,
    delete_symbols_for_file,
    get_file_mtimes,
    insert_indirect_call_sites_batch,
    insert_inheritance_batch,
    insert_refs_batch,
    insert_symbols_batch,
    split_tokens,
    upsert_file,
)

log = logging.getLogger(__name__)


def store_symbols_for_unit(
    conn,
    unit,
    config_hash: str,
    project_root: Path,
    source_roots: list[Path],
    exclude_paths: list[Path] | None = None,
    index_refs: bool = False,
) -> tuple[int, int]:
    """Parse one translation unit and store its symbols + refs in the DB.

    Handles:
    - Running libclang ``extract_all``
    - Managing file_id cache and mtimes
    - Deleting old symbols for the TU
    - Building and inserting symbol rows
    - Building and inserting reference rows

    Returns ``(symbols_added, refs_added)``.

    *conn* must be open; the caller is responsible for transactions.
    """
    from fw_context_mcp.indexer.symbols import extract_all

    if exclude_paths is None:
        exclude_paths = []

    file_path = str(unit.file.resolve())
    try:
        current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    # Parse
    try:
        syms, refs, inheritance, indirect_call_sites = extract_all(
            unit,
            source_roots=source_roots,
            exclude_paths=exclude_paths,
            with_refs=index_refs,
        )
    except Exception as exc:
        log.warning("skip TU %s: %s", unit.file.name, exc)
        return 0, 0

    # Delete old symbols for this TU
    existing = get_file_mtimes(conn, config_hash)
    if file_path in existing:
        file_id_old, _ = existing[file_path]
        delete_inheritance_for_file(conn, config_hash, file_id_old)
        delete_symbols_for_file(conn, file_id_old)

    # Upsert the TU file record
    upsert_file(conn, config_hash, file_path, unit.language, mtime=current_mtime)

    syms_added = 0
    refs_added = 0

    if syms:
        file_id_cache: dict[str, int] = {}
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
            ))
        insert_symbols_batch(conn, rows)
        syms_added = len(rows)

    # Path-relative helper used by refs and indirect_call_sites blocks
    def _rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(project_root))
        except ValueError:
            return p

    # References
    if index_refs and refs:
        tu_rel = _rel(file_path)
        delete_refs_for_file(conn, config_hash, tu_rel)
        ref_rows = [
            (config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind)
            for r in refs
        ]
        insert_refs_batch(conn, ref_rows)
        refs_added = len(ref_rows)

    # Indirect call sites (function pointer invocations)
    if index_refs and indirect_call_sites:
        tu_rel = _rel(file_path)
        delete_indirect_call_sites_for_file(conn, config_hash, tu_rel)
        ics_rows = [
            (config_hash, _rel(ics.from_file), ics.from_line, ics.from_usr,
             ics.expr_text, ics.target_usr, ics.target_name, ics.fn_ptr_type)
            for ics in indirect_call_sites
        ]
        insert_indirect_call_sites_batch(conn, ics_rows)

    # Inheritance
    if inheritance:
        inheritance_rows = [
            (config_hash, i.derived_usr, i.base_usr, i.access, int(i.is_virtual))
            for i in inheritance
        ]
        insert_inheritance_batch(conn, inheritance_rows)

    return syms_added, refs_added
