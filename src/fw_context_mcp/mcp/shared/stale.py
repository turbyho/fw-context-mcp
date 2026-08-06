"""Stale detection and recovery — file mtime checks, auto-reindex, query staleness wrapping."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config
from ...utils import MTIME_TOLERANCE_S, abs_path
from .context import _quick_open_readonly, get_executor

log = logging.getLogger(__name__)

# ── Mtime cache ──
# Cache for _count_modified_files results with a 30-second TTL.
# Invalidation is done explicitly by _invalidate_modified_cache after
# write operations (reindex_file_impl, file watcher).  The background
# reindex is a separate OS process and cannot invalidate this cache;
# the TTL ensures staleness is bounded to at most 30 seconds.
#
# Cache entries are (timestamp, count, max_stored_mtime) — the last
# field is the MAX(mtime) from the files table at cache time.  Before
# serving a cached value we verify this hasn't changed, catching
# external DB modifications by bg-reindex / manual fw-context index.
_modified_cache: dict[str, tuple[float, int, float]] = {}
_CACHE_TTL_S: float = 30.0  # shared TTL for mtime cache and header staleness cache


def _invalidate_modified_cache(config_hash: str | None = None) -> None:
    """Invalidate the mtime cache.  If *config_hash* is None, clear all entries."""
    if config_hash:
        _modified_cache.pop(config_hash, None)
    else:
        _modified_cache.clear()


def _path_matches_patterns(path: str, patterns: list[str]) -> bool:
    """Return True if *path* contains any of the build-directory *patterns*."""
    if not patterns:
        return False
    return any(pat in path for pat in patterns)


# ── Structural staleness ──────────────────────────────────────────────────
# Shared by background._fast_staleness_check and daemon._staleness_check.


def check_structural_staleness(
    conn,
    config_hash: str,
    cfg: dict,
    root: Path,
) -> list[str]:
    """Check structural staleness — compile_commands.json, schema, refs.

    Returns a list of human-readable reasons the index needs a reindex.
    These are the checks that both the background reindex trigger and the
    daemon startup perform — file-level mtime checks are added separately
    by callers that need them.

    All imports are lazy to avoid circular dependencies at module level.
    """
    from ...config import load as load_config
    from ...indexer.db import CURRENT_SCHEMA_VERSION, get_db_schema_version
    from .context import _is_stale

    reasons: list[str] = []

    # 1. compile_commands.json changed? (one stat call)
    cc_path = cfg["compile_commands_path"]
    if _is_stale(cfg, cc_path)[0]:
        reasons.append("compile_commands.json changed")

    # 2. Schema version mismatch?
    schema_ver = get_db_schema_version(conn)
    if schema_ver < CURRENT_SCHEMA_VERSION:
        reasons.append(f"schema {schema_ver} < {CURRENT_SCHEMA_VERSION}")

    # 3. Missing refs (and indirect call sites when refs are missing)?
    proj_cfg = load_config(root)
    if proj_cfg.index.index_refs:
        ref_count = conn.execute(
            "SELECT COUNT(*) FROM refs WHERE config_hash=?",
            (config_hash,),
        ).fetchone()[0]
        if ref_count == 0:
            reasons.append("refs missing")
            ics_count = conn.execute(
                "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
                (config_hash,),
            ).fetchone()[0]
            if ics_count == 0:
                reasons.append("indirect call sites missing")

    return reasons


def _stale_files(conn, config_hash: str, file_paths: list[str], root: Path) -> list[str]:
    """Return the subset of *file_paths* whose on-disk mtime is newer than the index."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ...indexer.manifest import load as load_manifest
    from ...indexer.ops import _normalize_file_path

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    manifest = load_manifest(db_path.parent)
    build_patterns = manifest.get("build_dir_patterns", []) if manifest else []

    # Build work items: batch-fetch stored mtimes in one query (was N+1).
    normalized: list[tuple[str, str]] = []  # (abs_path, db_key)
    seen: set[str] = set()
    for path in dict.fromkeys(file_paths):
        if path in seen:
            continue
        seen.add(path)
        if _path_matches_patterns(path, build_patterns):
            continue
        db_key = _normalize_file_path(path, root)
        normalized.append((path, db_key))

    if not normalized:
        return []

    # Batch lookup: one SELECT for all keys
    keys = [db_key for _, db_key in normalized]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT path, mtime FROM files WHERE config_hash = ? AND path IN ({placeholders})",
        (config_hash, *keys),
    ).fetchall()
    stored_map: dict[str, float] = {r["path"]: r["mtime"] for r in rows}

    # Build work items with resolved mtimes
    work_items: list[tuple[str, str, float]] = []  # (abs_path, db_key, stored_mtime)
    for file_path, db_key in normalized:
        stored = stored_map.get(db_key)
        if stored is not None:
            work_items.append((file_path, db_key, stored))

    if not work_items:
        return []

    # Parallel stat() — beneficial for NFS/CIFS where each stat() is high-latency
    stale: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(work_items))) as ex:
        futures = {
            ex.submit(_check_file_stale, path, stored): path
            for path, _db_key, stored in work_items
        }
        for f in as_completed(futures):
            try:
                if f.result():
                    stale.append(futures[f])
            except OSError:
                pass
    return stale


def _check_file_stale(path: str, stored_mtime: float) -> bool:
    """Return True if *path* on-disk mtime is newer than *stored_mtime*."""
    try:
        return os.path.getmtime(path) > stored_mtime + MTIME_TOLERANCE_S
    except (FileNotFoundError, OSError):
        return False


def _count_modified_files(
    conn,
    config_hash: str,
    root: Path,
    *,
    use_cache: bool = False,
) -> int:
    """Count files whose on-disk mtime is newer than the stored mtime.

    Files with mtime=0 (pre-migration databases) are skipped — their
    mtime is unknown, not "modified".

    Deduplicates by resolved absolute path, keeping the maximum stored
    mtime.  This prevents false positives from duplicate ``files`` rows
    (e.g. one with an absolute path whose mtime was updated and one with
    a relative path whose mtime is stale).

    When *use_cache* is True and a cached value is less than
    ``_CACHE_TTL_S`` seconds old AND the DB hasn't been modified
    externally (verified via ``MAX(mtime)`` from the files table),
    the cached value is returned.
    Call ``_invalidate_modified_cache(config_hash)`` after any write
    operation that changes file mtimes.
    """
    if use_cache:
        cached = _modified_cache.get(config_hash)
        if cached is not None:
            ts, count, max_stored = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                # Verify DB hasn't been modified externally (bg reindex,
                # manual fw-context index from another process, etc.)
                try:
                    row = conn.execute(
                        "SELECT MAX(mtime) FROM files WHERE config_hash=? AND mtime > 0",
                        (config_hash,),
                    ).fetchone()
                    current_max = row[0] if row else 0.0
                    if current_max == max_stored:
                        return count
                except sqlite3.Error:
                    pass  # query failed — fall through to recompute
            # Cache stale — remove and recompute
            _modified_cache.pop(config_hash, None)

    # Deduplicate by resolved absolute path, keeping the newest stored mtime.
    # Duplicate rows arise when the same file was indexed under different
    # path formats (absolute vs relative) — e.g. after a reindex with a
    # different working directory or compile_commands.json format.
    best_mtime: dict[str, float] = {}
    rows = conn.execute("SELECT path, mtime FROM files WHERE config_hash=?", (config_hash,)).fetchall()
    for r in rows:
        path = r["path"]
        stored = r["mtime"]
        if not stored:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = (root / path).resolve()
        key = str(p)
        if key not in best_mtime or stored > best_mtime[key]:
            best_mtime[key] = stored

    # Load build_dir_patterns from manifest to skip build-generated files
    from ...indexer.manifest import load as load_manifest

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    manifest = load_manifest(db_path.parent)
    build_patterns = manifest.get("build_dir_patterns", []) if manifest else []

    # NOTE: TOCTOU — file may change between DB read and stat() below.
    # Not a security issue: worst case is missed detection until next query.
    modified = 0
    for key, stored_mtime in best_mtime.items():
        if build_patterns and _path_matches_patterns(key, build_patterns):
            continue  # build-generated file — skip
        try:
            # NOTE: individual stat() calls — O(n) for n files. On modern NVMe
            # filesystems this is <10ms for 10K files. If slow, consider
            # os.scandir() batch processing or caching more aggressively.
            if os.path.getmtime(key) > stored_mtime + MTIME_TOLERANCE_S:
                modified += 1
        except OSError:
            pass

    if use_cache:
        max_stored = max(best_mtime.values(), default=0.0)
        _modified_cache[config_hash] = (time.monotonic(), modified, max_stored)
    return modified




# ── Header dependency staleness ──
# Cache for _check_header_staleness results.  Keyed as "headers:{config_hash}".
# Uses the same TTL as _modified_cache (30 seconds).
_header_staleness_cache: dict[str, tuple[float, int]] = {}


def _check_header_staleness(
    conn,
    config_hash: str,
    root: Path,
    *,
    max_files: int = 200,
    use_cache: bool = True,
) -> tuple[int, list[str]]:
    """Count TUs whose header dependencies have changed since indexing.

    Loads ``manifest.json`` from the index directory, compares stored
    header hashes against current on-disk content for project headers.

    Returns ``(count, affected_files)`` where *count* is the number of
    TUs with stale headers and *affected_files* is the list of source
    file paths.

    Performance is bounded by *max_files* (default 200).  When
    *use_cache* is True (default), results are cached for 30 seconds.
    Callers that need fresh results (e.g. ``get_active_build``) should
    pass ``use_cache=False``.
    """
    if use_cache:
        cache_key = f"headers:{config_hash}"
        cached = _header_staleness_cache.get(cache_key)
        if cached is not None:
            ts, count = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                return count, []

    from ...indexer.manifest import check_tu_staleness
    from ...indexer.manifest import load as load_manifest
    from ...indexer.sdk_detect import _build_sdk_excludes

    # Find the DB dir from the conn's path
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    manifest = load_manifest(db_path.parent)
    if not manifest:
        return 0, []

    project_root = Path(manifest.get("project_root", str(root)))
    vendor_patterns = list(_build_sdk_excludes(project_root))

    stale_count = 0
    affected: list[str] = []

    for entry in manifest.get("entries", [])[:max_files]:
        stale, _ = check_tu_staleness(entry, project_root, vendor_patterns)
        if stale:
            stale_count += 1
            affected.append(entry["file"])

    if use_cache:
        _header_staleness_cache[cache_key] = (time.monotonic(), stale_count)
    return stale_count, affected


def _with_stale_recovery(
    root: Path,
    db_path: Path,
    query_fn,
    *,
    stale_msg: str = "",
) -> list[dict]:
    """Execute *query_fn(conn, config_hash)* on the executor with stale-recovery.

    When the index or result files are stale, kicks off a background
    ``fw-context index`` and returns a warning.  The original (possibly
    stale) results are always returned — the background reindex handles
    the actual fix asynchronously.

    The ENTIRE body (config read + ``query_fn`` + ``_stale_files``) runs
    inside ``executor.execute_sync``: the connection is needed for the
    whole search query, not just the staleness check.  A shorter variant
    that closed or released the connection after the config read would
    break every search tool — do NOT "optimise" this by splitting the
    connection usage.

    ``config_hash`` is read fresh per request via a short-lived
    read-only connection (no write transaction), then passed per call
    to the executor — a reindex with a changed build config can never
    leave queries filtering by a stale hash.
    """
    from ..background import _ensure_daemon_running  # lazy — avoids circular import

    # Fresh config_hash via a short-lived read-only connection.
    conn = _quick_open_readonly(db_path)
    try:
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return [{"error": "No build config indexed."}]
        config_hash = cfg["config_hash"]
    finally:
        conn.close()

    executor = get_executor(db_path)

    def _query(db_conn, cfg_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        result_rows = query_fn(db_conn, cfg_hash)
        # Ensure plain dicts — rows must be materialised before returning.
        safe_rows: list[dict] = [dict(r) for r in result_rows]
        stale_f = _stale_files(
            db_conn,
            cfg_hash,
            [abs_path(root, r["file"]) for r in result_rows if "file" in r],
            root,
        )
        return safe_rows, stale_f

    safe_rows, stale_f = executor.execute_sync(_query, config_hash)

    results: list[dict] = []
    if stale_f:
        _ensure_daemon_running(root)
        results.append(
            {
                "warning": f"Results may be stale — {len(stale_f)} file(s) changed. Background reindex in progress. Run 'fw-context index' to force full update."
            }
        )
    results += safe_rows
    return results
