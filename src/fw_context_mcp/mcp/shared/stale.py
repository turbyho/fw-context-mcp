"""Stale detection and recovery — file mtime checks, auto-reindex, query staleness wrapping."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config, get_file_mtime_indexed
from ...indexer.dep_files import parse_dep_file
from ...utils import MTIME_TOLERANCE_S, abs_path
from .context import _open_db_safe

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
_MTIME_CACHE_TTL: float = 30.0


def _invalidate_modified_cache(config_hash: str | None = None) -> None:
    """Invalidate the mtime cache.  If *config_hash* is None, clear all entries."""
    if config_hash:
        _modified_cache.pop(config_hash, None)
    else:
        _modified_cache.clear()


def _stale_files(conn, config_hash: str, file_paths: list[str]) -> list[str]:
    """Return the subset of *file_paths* whose on-disk mtime is newer than the index."""
    stale = []
    for path in dict.fromkeys(file_paths):
        try:
            stored = get_file_mtime_indexed(conn, config_hash, path)
            if not stored:
                # stored=0.0 (pre-migration) or None (not indexed) — skip
                continue
            if os.path.getmtime(path) > stored + MTIME_TOLERANCE_S:
                stale.append(path)
        except FileNotFoundError:
            log.debug("Stale check skipped deleted file: %s", path)
        except OSError:
            pass
    return stale


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
    ``_MTIME_CACHE_TTL`` seconds old AND the DB hasn't been modified
    externally (verified via ``MAX(mtime)`` from the files table),
    the cached value is returned.
    Call ``_invalidate_modified_cache(config_hash)`` after any write
    operation that changes file mtimes.
    """
    if use_cache:
        cached = _modified_cache.get(config_hash)
        if cached is not None:
            ts, count, max_stored = cached
            if time.monotonic() - ts < _MTIME_CACHE_TTL:
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
                except Exception:
                    pass  # query failed — fall through to recompute
            # Cache stale — remove and recompute
            _modified_cache.pop(config_hash, None)

    # Deduplicate by resolved absolute path, keeping the newest stored mtime.
    # Duplicate rows arise when the same file was indexed under different
    # path formats (absolute vs relative) — e.g. after a reindex with a
    # different working directory or compile_commands.json format.
    best_mtime: dict[str, float] = {}
    rows = conn.execute(
        "SELECT path, mtime FROM files WHERE config_hash=?", (config_hash,)
    ).fetchall()
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

    modified = 0
    for key, stored_mtime in best_mtime.items():
        try:
            if Path(key).stat().st_mtime > stored_mtime + MTIME_TOLERANCE_S:
                modified += 1
        except OSError:
            pass

    if use_cache:
        max_stored = max(best_mtime.values(), default=0.0)
        _modified_cache[config_hash] = (time.monotonic(), modified, max_stored)
    return modified


def _auto_reindex_stale(
    stale_files: list[str],
    project_root: Path,
    max_files: int = 5,
    timeout_s: float = 120.0,
) -> tuple[list[str], list[str]]:
    """Re-index up to *max_files* stale files, bounded by *timeout_s*.

    Calls ``reindex_file_impl`` **without** LLM analysis or override
    regeneration — those are left for the background ``fw-context index``
    subprocess.  This keeps query-time recovery fast while the full
    reindex (including LLM analysis) catches up in the background.
    """
    from ..handlers.maintenance import reindex_file_impl  # lazy — avoids circular import

    succeeded: list[str] = []
    failed: list[str] = []
    t0 = time.monotonic()
    for fp in stale_files[:max_files]:
        if time.monotonic() - t0 > timeout_s:
            break
        try:
            result = reindex_file_impl(fp, str(project_root), with_analysis=False)
            if result.get("error"):
                failed.append(fp)
            else:
                succeeded.append(fp)
        except Exception:
            failed.append(fp)
    return succeeded, failed


# ── Header dependency staleness ──
# Cache for _check_header_staleness results.  Keyed as "deps:{config_hash}".
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

    Queries the ``files`` table for entries with ``deps_exist=1``,
    locates the corresponding ``.d`` file on disk, parses it to find
    included headers, and compares each header's current on-disk mtime
    against the source file's stored mtime.

    Returns ``(count, affected_files)`` where *count* is the number of
    TUs with stale headers and *affected_files* is the list of source
    file paths.

    Performance is bounded by *max_files* (default 200).  When
    *use_cache* is True (default), results are cached for 30 seconds.
    Callers that need fresh results (e.g. ``get_active_build``) should
    pass ``use_cache=False``.
    """
    if use_cache:
        cache_key = f"deps:{config_hash}"
        cached = _header_staleness_cache.get(cache_key)
        if cached is not None:
            ts, count = cached
            if time.monotonic() - ts < _MTIME_CACHE_TTL:
                return count, []

    rows = conn.execute(
        """SELECT path, mtime FROM files
           WHERE config_hash = ? AND deps_exist = 1
           ORDER BY path
           LIMIT ?""",
        (config_hash, max_files),
    ).fetchall()

    stale_count = 0
    affected: list[str] = []

    for r in rows:
        source_path = r["path"]
        stored_mtime = r["mtime"]
        if not stored_mtime:
            continue

        source_file = Path(source_path)
        if not source_file.is_absolute():
            source_file = (root / source_path).resolve()

        # .d file next to the source file (GCC/PlatformIO convention)
        d_path = source_file.with_suffix(".d")
        if not d_path.exists():
            # CMake convention: source.ext.d (e.g. main.c.o.d)
            d_path = source_file.parent / (source_file.name + ".d")
            if not d_path.exists():
                continue

        headers = parse_dep_file(d_path)
        for header_path in headers:
            try:
                if header_path.stat().st_mtime > stored_mtime + MTIME_TOLERANCE_S:
                    stale_count += 1
                    affected.append(source_path)
                    break  # One stale header is enough per TU
            except OSError:
                continue

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
    """Execute *query_fn(conn, config_hash)* with automatic stale-recovery.

    When the index or result files are stale, kicks off a background
    ``fw-context index`` and returns a warning.  The original (possibly
    stale) results are always returned — the background reindex handles
    the actual fix asynchronously.

    Connections are always closed before returning.
    """
    from ..background import _ensure_daemon_running  # lazy — avoids circular import

    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    assert conn is not None
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
            result_rows = query_fn(conn, config_hash)
            # Ensure plain dicts — sqlite3.Row objects become invalid after conn.close()
            safe_rows: list[dict] = [dict(r) for r in result_rows]
            stale_f = _stale_files(
                conn, config_hash,
                [abs_path(root, r["file"]) for r in result_rows if "file" in r],
            )
    finally:
        conn.close()

    results: list[dict] = []
    if stale_f:
        _ensure_daemon_running(root)
        results.append({"warning": f"Results may be stale — {len(stale_f)} file(s) changed. Background reindex in progress. Run 'fw-context index' to force full update."})
    results += safe_rows
    return results
