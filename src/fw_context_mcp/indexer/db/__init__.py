"""SQLite storage layer for fw-context-mcp index.

Central ``__init__`` that re-exports all database operations from separate
specialized modules.  The package is split by concern:
- ``_symbols`` — symbol CRUD, macro CRUD, search
- ``_files`` — file metadata (mtime, content, hashes)
- ``_refs`` — reference edges (calls, reads, indirect calls)
- ``_inheritance`` — C++ class hierarchy edges
- ``_callgraph`` — graph traversal (callers, callees, paths)
- ``_embeddings`` — vector storage (BLOB and vec0)
- ``_fts`` — FTS5 rebuild utilities
- ``_llm`` — LLM analysis storage
- ``_memory`` — memory map (the MEMORY command of the linker script)
- ``_locking`` — file-system write lock
- ``_projects`` — project/build config CRUD
- ``_schema`` — schema versioning and migration
- ``_connection`` — connection management (open, schema, transactions)

WHY split by concern: a single monolithic ``db.py`` would exceed 2000 lines
and be unmaintainable.  Each module has a single responsibility, making it
testable in isolation and reducing merge conflicts in team workflows.
"""

from __future__ import annotations

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DatabaseCorruptionError",
    "FileHashRecord",
    "WriteLockTimeout",
    "_cosine_sim",
    "_ensure_column",
    "_expand_query",
    "_resolve_target_usr",
    "clean_orphan_embeddings",
    "clean_orphan_embeddings_vec",
    "compute_analysis_coverage",
    "count_fp_assignments",
    "count_indirect_call_sites",
    "count_llm_analysis",
    "count_pending_analysis",
    "count_refs",
    "delete_build_data",
    "delete_fp_assignments_for_files",
    "delete_indirect_call_sites_for_files",
    "delete_inheritance_for_file",
    "delete_macros_for_files",
    "delete_orphan_files",
    "delete_overrides_for_file",
    "delete_refs_for_files",
    "delete_symbols_for_file",
    "drop_fts_triggers",
    "ensure_schema",
    "find_all_callers_recursive",
    "find_call_path",
    "find_callees_recursive",
    "find_dead_code",
    "find_hotspots",
    "find_indirect_call_sites",
    "find_indirect_targets",
    "find_macro_refs",
    "find_refs",
    "get_active_config",
    "get_all_builds_for_project",
    "get_all_projects",
    "get_build_stats",
    "get_builds_for_scope",
    "get_class_members",
    "get_db_schema_version",
    "get_direct_bases",
    "get_direct_bases_batch",
    "get_direct_derived",
    "get_direct_derived_batch",
    "get_embeddings",
    "get_entry_point",
    "get_entry_points_by_config",
    "get_file_hashes",
    "get_file_map",
    "get_file_mtime_indexed",
    "get_file_mtimes",
    "get_function_address_arrays",
    "get_llm_analysis_for_symbol",
    "get_memory_regions",
    "get_memory_regions_by_config",
    "get_overrides_for_method",
    "get_table_coverage",
    "get_template_instances",
    "get_vec_dim",
    "get_vector_table",
    "init_vec_table",
    "insert_fp_assignments_batch",
    "insert_indirect_call_sites_batch",
    "insert_inheritance_batch",
    "insert_macros_batch",
    "insert_overrides_batch",
    "insert_refs_batch",
    "insert_symbols_batch",
    "lookup_macro",
    "make_analysis_summary",
    "open_db",
    "purge_file_records",
    "purge_missing_files_batch",
    "rebuild_files_fts",
    "rebuild_fts",
    "rebuild_macros_fts",
    "replace_file_data",
    "replace_memory_regions",
    "search_similar_hybrid",
    "search_similar_vec",
    "search_symbols",
    "set_entry_point",
    "split_tokens",
    "transaction",
    "upsert_build_config",
    "upsert_embeddings",
    "upsert_embeddings_vec",
    "upsert_file",
    "upsert_llm_analysis_batch",
    "upsert_project",
    "write_lock",
]

import logging

from ._callgraph import (
    _resolve_target_usr,  # noqa: F401 — re-exported for ops.py
    find_all_callers_recursive,
    find_call_path,
    find_callees_recursive,
    find_dead_code,
    find_hotspots,
    get_function_address_arrays,
    get_table_coverage,
    get_vector_table,
)
from ._connection import (
    DatabaseCorruptionError,
    ensure_schema,
    get_db_schema_version,
    open_db,
    transaction,
)
from ._embeddings import (
    _cosine_sim,
    clean_orphan_embeddings,
    clean_orphan_embeddings_vec,
    get_embeddings,
    get_vec_dim,
    init_vec_table,
    search_similar_hybrid,
    search_similar_vec,
    upsert_embeddings,
    upsert_embeddings_vec,
)
from ._files import (
    FileHashRecord,
    delete_orphan_files,
    delete_symbols_for_file,
    get_file_hashes,
    get_file_map,
    get_file_mtime_indexed,
    get_file_mtimes,
    purge_file_records,
    purge_missing_files_batch,
    replace_file_data,
    upsert_file,
)
from ._fts import (
    rebuild_files_fts,
    rebuild_fts,
    rebuild_macros_fts,
)
from ._inheritance import (
    delete_inheritance_for_file,
    delete_overrides_for_file,
    get_class_members,
    get_direct_bases,
    get_direct_bases_batch,
    get_direct_derived,
    get_direct_derived_batch,
    get_overrides_for_method,
    get_template_instances,
    insert_inheritance_batch,
    insert_overrides_batch,
)
from ._llm import (
    count_llm_analysis,
    get_llm_analysis_for_symbol,
    upsert_llm_analysis_batch,
)
from ._locking import WriteLockTimeout, write_lock
from ._memory import (
    get_entry_point,
    get_entry_points_by_config,
    get_memory_regions,
    get_memory_regions_by_config,
    replace_memory_regions,
    set_entry_point,
)
from ._projects import (
    compute_analysis_coverage,
    count_pending_analysis,
    delete_build_data,
    get_active_config,
    get_all_builds_for_project,
    get_all_projects,
    get_build_stats,
    get_builds_for_scope,
    make_analysis_summary,
    upsert_build_config,
    upsert_project,
)
from ._refs import (
    count_fp_assignments,
    count_indirect_call_sites,
    count_refs,
    delete_fp_assignments_for_files,
    delete_indirect_call_sites_for_files,
    delete_refs_for_files,
    find_indirect_call_sites,
    find_indirect_targets,
    find_refs,
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_refs_batch,
)
from ._schema import (
    CURRENT_SCHEMA_VERSION,
    _ensure_column,
    drop_fts_triggers,
)
from ._symbols import (
    _expand_query,
    delete_macros_for_files,
    find_macro_refs,
    insert_macros_batch,
    insert_symbols_batch,
    lookup_macro,
    search_symbols,
    split_tokens,
)

log = logging.getLogger(__name__)
