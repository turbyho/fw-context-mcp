"""Stale detection and recovery — file mtime checks, auto-reindex, query staleness wrapping."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config, get_file_mtime_indexed
from ...utils import MTIME_TOLERANCE_S, abs_path
from .context import _open_db_safe

log = logging.getLogger(__name__)


def _stale_files(conn, config_hash: str, file_paths: list[str]) -> list[str]:
    """Return the subset of *file_paths* whose on-disk mtime is newer than the index."""
    stale = []
    for path in dict.fromkeys(file_paths):
        try:
            stored = get_file_mtime_indexed(conn, config_hash, path)
            if stored is None:
                continue
            if os.path.getmtime(path) > stored + MTIME_TOLERANCE_S:
                stale.append(path)
        except FileNotFoundError:
            log.debug("Stale check skipped deleted file: %s", path)
        except OSError:
            pass
    return stale


def _count_modified_files(conn, config_hash: str, root: Path) -> int:
    """Count files whose on-disk mtime is newer than the stored mtime.

    Files with mtime=0 (pre-migration databases) are always counted as modified.
    """
    modified = 0
    rows = conn.execute(
        "SELECT path, mtime FROM files WHERE config_hash=?", (config_hash,)
    ).fetchall()
    for r in rows:
        path = r["path"]
        stored = r["mtime"]
        if not stored:
            # mtime=0 from a pre-migration database — unknown, treat as stale
            modified += 1
            continue
        p = Path(path)
        if not p.is_absolute():
            p = (root / path).resolve()
        try:
            if p.stat().st_mtime > stored + MTIME_TOLERANCE_S:
                modified += 1
        except OSError:
            pass
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
    from ..background import _start_bg_reindex_if_stale  # lazy — avoids circular import

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
        _start_bg_reindex_if_stale(root)
        results.append({"warning": f"Results may be stale — {len(stale_f)} file(s) changed. Background reindex in progress. Run 'fw-context index' to force full update."})
    results += safe_rows
    return results
