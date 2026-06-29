"""Background services — file watcher daemon, auto-reindex subprocess."""

from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import derive_project_id
from ..config import load as load_config
from ..indexer.db import (
    get_active_config,
    open_db,
)
from .shared.context import _db_path

log = logging.getLogger(__name__)

# ── Per-module cache ──
_SOURCE_EXTS_WATCH = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}


# ── _start_bg_watcher (was at server.py:249) ──
def _start_bg_watcher(root: Path) -> None:
    """Spawn a daemon thread that watches project sources and reindexes on change.

    Two-phase approach:
    1. **Immediate** — reindex changed files (symbols + refs, no LLM), debounced
       at 500 ms so rapid IDE saves trigger at most one reindex per file.
    2. **Quiet-period LLM** — after 60 s without changes, regenerate LLM symbol
       analysis and file summaries for all symbols that lost their analysis
       (idempotent — only unanalyzed symbols are processed).

    Uses a pidfile (``<db_dir>/<project_id>/watcher.lock``) so only one
    watcher runs per project, even across multiple MCP server instances.
    """
    db_path = _db_path(root)
    if not db_path.exists():
        return
    lock_file = db_path.parent / "watcher.lock"

    # Check for an existing live watcher
    if lock_file.exists():
        try:
            stale_pid = int(lock_file.read_text().strip())
            os.kill(stale_pid, 0)
            # PID exists — but if the lock file is older than 24 h,
            # the watcher is stale regardless (e.g. old MCP server
            # left running across sessions).
            lock_age_s = time.time() - lock_file.stat().st_mtime
            if lock_age_s < 86400:
                log.debug("Watcher already running (pid %d), skipping.", stale_pid)
                return
            log.info("Stale watcher pid %d (lock age %d h), taking over.", stale_pid, int(lock_age_s / 3600))
        except (ProcessLookupError, ValueError, OSError):
            pass  # Stale lock — take over

    lock_file.write_text(str(os.getpid()))

    def _watch_loop() -> None:

        from watchfiles import watch

        from ..handlers.maintenance import reindex_file_impl
        from ..indexer.runner import _build_file_analysis, _build_llm_analysis

        exclude_rx = re.compile(r"(/\.git/|/\.pio/|/build/|/__pycache__/|/node_modules/)")
        debounce_s = 0.5
        llm_quiet_s = 60.0  # Trigger LLM analysis after 60 s without changes
        pending: dict[str, float] = {}
        last_change: float = 0.0
        any_changed = False

        log.info("Background watcher started for %s", root)
        try:
            for changes in watch(
                root, debounce=500, recursive=True,
                rust_timeout=5000, yield_on_timeout=True,
            ):
                now = time.monotonic()
                had_changes = False
                for _, changed_path_str in changes:
                    p = Path(changed_path_str)
                    if p.suffix.lower() not in _SOURCE_EXTS_WATCH:
                        continue
                    if exclude_rx.search(changed_path_str):
                        continue
                    pending[changed_path_str] = now
                    had_changes = True

                # Phase 1 — immediate symbol reindex (debounced)
                ready = [fp for fp, t in pending.items() if now - t >= debounce_s]
                for fp in ready:
                    del pending[fp]
                    try:
                        result = reindex_file_impl(fp, str(root), with_analysis=False)
                        if result.get("error"):
                            log.debug("Watcher reindex skipped %s: %s", Path(fp).name, result["error"])
                        else:
                            log.info("Watcher reindexed %s (%d symbols)", Path(fp).name, result.get("symbols_updated", 0))
                    except Exception:
                        log.debug("Watcher reindex failed for %s", Path(fp).name, exc_info=True)

                if had_changes:
                    last_change = now
                    any_changed = True

                # Phase 2 — LLM analysis after quiet period
                if any_changed and pending == {} and now - last_change >= llm_quiet_s:
                    any_changed = False
                    try:
                        conn = open_db(db_path)
                        try:
                            project_id = derive_project_id(root)
                            cfg_data = get_active_config(conn, project_id)
                            if cfg_data:
                                cfg = load_config(project_root=root)
                                if cfg.llm.enabled and cfg.llm.analyze_symbols:
                                    _build_llm_analysis(conn, cfg_data["config_hash"], cfg.llm, db_path.parent)
                                    if cfg.llm.analyze_files:
                                        _build_file_analysis(conn, cfg_data["config_hash"], cfg.llm, db_path.parent)
                                    conn.commit()
                                    log.info("Watcher LLM analysis completed for %s", root)
                        finally:
                            conn.close()
                    except Exception:
                        log.debug("Watcher LLM analysis failed", exc_info=True)

        except Exception:
            log.warning("Background watcher stopped unexpectedly", exc_info=True)
        finally:
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass

    t = threading.Thread(target=_watch_loop, daemon=True, name="fw-context-watcher")
    t.start()
    log.info("Background watcher thread started for %s (pid %d)", root, os.getpid())

# ── _is_bg_reindex_running (was at server.py:367) ──
def _is_bg_reindex_running(root: Path) -> bool:
    """Check whether any index process is running for *root*.

    Checks two lock files:

    1. ``<db_dir>/<project_id>/reindex.lock`` — held by the background
       auto-reindex subprocess spawned by ``_start_bg_reindex_if_stale``.
    2. ``<db_dir>/write.lock`` — held by ANY index process
       (``fw-context index``, ``reindex_file``, auto-reindex).

    If either lock is held, another index process is active — skip
    launching a duplicate background reindex.

    Uses ``fcntl.flock`` on each lock file — the kernel tracks the lock,
    auto-releases on process exit.  No PID tracking, no race conditions,
    no stale lock cleanup.
    """
    db_path = _db_path(root)
    if not db_path.exists():
        return False

    # ── Lock-check helper ──
    def _lock_held(lock_file: Path) -> bool:
        try:
            lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return True  # Lock held by another process
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return False

    # 1. Auto-reindex lock (db_dir/project_id/reindex.lock)
    if _lock_held(db_path.parent / "reindex.lock"):
        return True

    # 2. General write lock (db_dir/write.lock)
    if _lock_held(db_path.parent / "write.lock"):
        return True

    return False

# ── _request_bg_reindex_pause (was at server.py:414) ──
def _request_bg_reindex_pause(root: Path) -> None:
    """Signal the background reindex to release the write lock.

    Writes ``<pid>`` to ``<db_dir>/<project_id>/reindex.pause`` so the
    bg reindex can check whether the requesting process is still alive.
    The bg process checks this marker between translation units — if the
    requesting PID is dead, it ignores the stale marker and continues.
    """
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    try:
        pause_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

# ── _resume_bg_reindex (was at server.py:430) ──
def _resume_bg_reindex(root: Path) -> None:
    """Remove the pause marker — the bg reindex may now resume."""
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    try:
        pause_file.unlink(missing_ok=True)
    except OSError:
        pass

# ── _check_bg_pause (was at server.py:440) ──
def _check_bg_pause(root: Path) -> bool:
    """Check whether a pause was requested and the requester is still alive.

    Returns True if the bg reindex should pause (pause marker exists AND
    the requesting PID is still running).  Returns False if there is no
    pause marker or the requesting process has died (stale marker —
    cleaned up automatically).
    """
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    if not pause_file.exists():
        return False
    try:
        content = pause_file.read_text(encoding="utf-8").strip()
        requester_pid = int(content)
    except (OSError, ValueError):
        try:
            pause_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # Check if the requesting process is still alive
    try:
        os.kill(requester_pid, 0)
    except OSError:
        # Process dead — clean up stale marker
        try:
            pause_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True

# ── _fast_staleness_check (before _start_bg_reindex_if_stale) ──
def _fast_staleness_check(root: Path) -> tuple[bool, list[str]]:
    """Lightweight staleness check — COUNT queries and at most one ``stat()``.

    Returns ``(needs_reindex, reasons)`` where *reasons* is a list of
    human-readable strings explaining why a reindex is needed.

    Does **not** call ``_count_modified_files`` — the background subprocess
    does its own per-file mtime comparison during ``run()``.  This function
    only detects structural issues (missing data, schema changes,
    compile_commands.json modification).
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import (
        CURRENT_SCHEMA_VERSION,
        get_active_config,
        get_db_schema_version,
    )
    from .shared.context import _db_path, _is_stale, _open_db_safe

    db_path = _db_path(root)
    if not db_path.exists():
        return False, []

    conn, err = _open_db_safe(db_path)
    if err:
        return False, []
    assert conn is not None
    reasons: list[str] = []
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return False, []
            config_hash = cfg["config_hash"]

            # 1. compile_commands.json changed? (one stat call)
            cc_path = cfg["compile_commands_path"]
            if _is_stale(cfg, cc_path):
                reasons.append("compile_commands.json changed")

            # 2. Schema version mismatch?
            schema_ver = get_db_schema_version(conn)
            if schema_ver < CURRENT_SCHEMA_VERSION:
                reasons.append(f"schema {schema_ver} < {CURRENT_SCHEMA_VERSION}")

            # 3. Missing refs or indirect call sites?
            proj_cfg = load_config(root)
            if proj_cfg.index.index_refs:
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM refs WHERE config_hash=?",
                    (config_hash,),
                ).fetchone()[0]
                if ref_count == 0:
                    reasons.append("refs missing")
                    # Also check indirect call sites — but only flag them
                    # when refs are missing (if refs were populated, the
                    # indirect extraction ran too; empty table is legitimate).
                    ics_count = conn.execute(
                        "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
                        (config_hash,),
                    ).fetchone()[0]
                    if ics_count == 0:
                        reasons.append("indirect call sites missing")

            # 4. Unanalyzed symbols?
            if proj_cfg.llm.enabled and proj_cfg.llm.analyze_symbols:
                unanalyzed = conn.execute(
                    """SELECT COUNT(*)
                       FROM symbols s
                       WHERE s.config_hash = ?
                         AND s.is_definition = 1
                         AND s.kind IN ('function', 'method',
                                        'constructor', 'destructor',
                                        'class', 'struct')
                         AND s.file_path NOT LIKE 'mbed-os/%'
                         AND s.file_path NOT LIKE '.pio/%'
                         AND s.file_path NOT LIKE 'zephyr/%'
                         AND s.file_path NOT LIKE 'build/%'
                         AND s.file_path NOT LIKE 'modules/%'
                         AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)""",
                    (config_hash,),
                ).fetchone()[0]
                if unanalyzed > 0:
                    reasons.append(f"{unanalyzed} unanalyzed symbols")
    finally:
        conn.close()

    return len(reasons) > 0, reasons


# ── _start_bg_reindex_if_stale (rewritten with _fast_staleness_check) ──
def _start_bg_reindex_if_stale(root: Path) -> None:
    """Kick off a background ``fw-context index`` if the index needs work.

    Uses ``_fast_staleness_check`` (COUNT queries + one stat) — no O(n)
    mtime scan.  The subprocess does its own per-file mtime comparison
    during ``run()``.

    When refs or indirect call sites are completely missing, the subprocess
    is launched with ``FW_CONTEXT_FORCE_REFINDEX=1`` so it skips mtime
    checks and re-parses every file (one-time cost to backfill the data).
    Mtimes are **not** reset — the next run is fully incremental.

    No-op when no index exists, no work is needed, or a background reindex
    is already running.
    """
    if _is_bg_reindex_running(root):
        return
    db_path = _db_path(root)
    if not db_path.exists():
        return

    needs_reindex, reasons = _fast_staleness_check(root)
    if not needs_reindex:
        return

    reason = ", ".join(reasons)
    log.info("Starting background reindex for %s (%s)", root, reason)

    lock_file = db_path.parent / "reindex.lock"
    log_file = db_path.parent / "reindex.log"
    try:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        return  # Another reindex started between _is_bg_reindex_running and now

    # Write the first line so get_active_build() always has something to show
    try:
        with open(log_file, "w") as fh:
            fh.write(f"Starting reindex ({reason})...\n")
    except OSError:
        pass

    pid_file = db_path.parent / "reindex.pid"
    force_refs = "refs missing" in reason or "indirect call sites missing" in reason
    try:
        stdout_fh = open(log_file, "a")
        env: dict[str, str] = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "FW_CONTEXT_HEARTBEAT_LOG": str(log_file),
        }
        if force_refs:
            env["FW_CONTEXT_FORCE_REFINDEX"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "fw_context_mcp.cli", "index"],
            cwd=str(root),
            stdout=stdout_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
        stdout_fh.close()  # Close parent copy — child has its own fd
        # Store PID so manual operations can kill the bg process
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    except Exception as exc:
        log.exception("Failed to spawn background reindex subprocess")
        try:
            with open(log_file, "a") as fh:
                fh.write(f"Failed to spawn: {exc}\n")
        except OSError:
            pass
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return

    def _waiter() -> None:
        """Watchdog — kill the subprocess if heartbeat lines stop appearing.

        A healthy subprocess writes a heartbeat line to the log file every
        30 seconds from a daemon thread.  If 90 seconds pass without the log
        file being modified, the process is considered deadlocked and gets
        killed.  No total time limit — a legitimate reindex can run for hours.
        """
        kick_timeout = 90  # seconds without log modification → deadlocked
        try:
            while proc.poll() is None:
                time.sleep(30)
                try:
                    mtime = log_file.stat().st_mtime
                except OSError:
                    continue  # file not created yet — process is still starting
                if time.time() - mtime > kick_timeout:
                    log.warning(
                        "Background reindex for %s deadlocked — log not modified for %ds",
                        root, int(time.time() - mtime),
                    )
                    _kill_and_log(proc, f"no heartbeat for {int(time.time() - mtime)}s")
                    return
        except Exception:
            log.exception("Background reindex watcher for %s crashed", root)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass
        log.info("Background reindex finished for %s (exit %d)", root, proc.returncode)

    def _kill_and_log(p: subprocess.Popen, reason: str) -> None:
        try:
            p.kill()
            p.wait(timeout=10)
        except Exception:
            pass
        try:
            with open(log_file, "a") as fh:
                fh.write(f"Killed: {reason}\n")
        except OSError:
            pass

    threading.Thread(target=_waiter, daemon=True).start()
