"""PoC: multiprocess parallel libclang indexing experiment.

Compares sequential vs parallel (N workers) indexing throughput —
parse in worker processes, serialize DB writes in main process.

Workers parse with ``extract_all(return_tu=False)`` so the
libclang TranslationUnit stays in-process (it cannot be pickled).
``_build_filtered_file_content`` falls back to a separate parse
when existing_tu is None — acceptable overhead for a PoC.

Usage:
    python experiments/parallel_index_poc.py \\
        <compile_commands.json> [--max-tus N] [--workers W] [--refs]

Output: timing comparison table (parse vs lock-wait vs write).
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Ensure the package root is on sys.path for direct script execution.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fw_context_mcp.indexer.compile_commands import parse as parse_cc_json
from fw_context_mcp.indexer.config_hash import compute_tu_content_hash
from fw_context_mcp.indexer.db import (
    CURRENT_SCHEMA_VERSION,
    drop_fts_triggers,
    get_file_hashes,
    open_db,
    rebuild_fts,
    transaction,
    upsert_build_config,
    upsert_project,
    write_lock,
)
from fw_context_mcp.indexer._unit_processor import _handle_unchanged_or_reuse
from fw_context_mcp.indexer.ops import (
    store_symbols_for_unit,
    _normalize_file_path,
)
from fw_context_mcp.indexer.sdk_detect import _build_sdk_excludes, _normalize_patterns
from fw_context_mcp.config.settings import derive_project_id
from fw_context_mcp.indexer.manifest import build_preliminary
from fw_context_mcp.indexer._embedding import _fmt_dur

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Worker function (runs in subprocess — no shared state with main process)
# ═══════════════════════════════════════════════════════════════════════════

def _worker_parse(args: tuple) -> tuple[bool, str, object]:
    """Parse one TU in a worker process.

    Each worker creates its own libclang Index — the shared singleton
    pattern in symbols.py uses a process-local global, so each worker
    gets a fresh Index without contention.

    Returns:
        (success, unit_file_name, result_or_error)
    """
    unit, index_refs = args

    from fw_context_mcp.indexer.symbols import extract_all

    try:
        result = extract_all(unit, with_refs=index_refs, return_tu=False)
        # Drop the libclang TU reference if any — never crosses process boundary.
        if hasattr(result, 'tu'):
            result.tu = None
        return (True, unit.file.name, result)
    except Exception as exc:
        return (False, unit.file.name, str(exc))


# ═══════════════════════════════════════════════════════════════════════════
# Sequential baseline (single process — same logic as runner.py loop)
# ═══════════════════════════════════════════════════════════════════════════

def sequential_index(
    units: list,
    conn: sqlite3.Connection,
    db_path: Path,
    config_hash: str,
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
    index_refs: bool,
    existing_files: dict,
    build_dir_patterns: list[str] | None,
) -> dict:
    """Index TUs one at a time in the main process (baseline)."""
    from fw_context_mcp.indexer.symbols import extract_all
    from fw_context_mcp.indexer._unit_processor import _check_and_parse_unit, _process_unit

    total_syms = 0
    total_refs = 0
    acc_parse = 0.0
    acc_lock = 0.0
    acc_write = 0.0

    t_start = time.monotonic()

    for i, unit in enumerate(units):
        # Parse outside lock (CPU-bound)
        t_parse_start = time.monotonic()
        try:
            parsed = extract_all(unit, with_refs=index_refs, return_tu=True)
        except Exception as exc:
            log.warning("skip TU %s: %s", unit.file.name, exc)
            continue
        t_parse_end = time.monotonic()

        # Store inside lock
        with write_lock(db_path.parent, timeout=120.0):
            t_write_start = time.monotonic()
            status, syms, refs, timing, headers = _process_unit(
                unit, config_hash, project_root, vendor_patterns,
                project_patterns, index_refs, db_path, existing_files,
                conn=conn, force=True, pre_parsed=parsed,
                parse_timing=(t_parse_start, t_parse_end),
                build_dir_patterns=build_dir_patterns,
            )
            t_write_end = time.monotonic()

        if status == "updated":
            acc_parse += t_parse_end - t_parse_start
            acc_lock += t_write_start - t_parse_end
            acc_write += t_write_end - t_write_start
            total_syms += syms
            total_refs += refs

        log.info("[%d/%d] %s: %d syms, %d refs, %.1fs",
                 i + 1, len(units), unit.file.name, syms, refs,
                 t_write_end - t_parse_start)

    elapsed = time.monotonic() - t_start
    return {
        "mode": "sequential",
        "total_s": elapsed,
        "parse_s": acc_parse,
        "lock_s": acc_lock,
        "write_s": acc_write,
        "symbols": total_syms,
        "references": total_refs,
        "tus": len(units),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Parallel path — ProcessPoolExecutor for parsing, serialized DB writes
# ═══════════════════════════════════════════════════════════════════════════

def parallel_index(
    units: list,
    conn: sqlite3.Connection,
    db_path: Path,
    config_hash: str,
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
    index_refs: bool,
    existing_files: dict,
    build_dir_patterns: list[str] | None,
    num_workers: int,
) -> dict:
    """Index TUs with parallel parsing, serialized DB writes."""

    total_syms = 0
    total_refs = 0
    acc_parse_queued = 0.0
    acc_lock = 0.0
    acc_write = 0.0
    parse_times: dict[int, float] = {}  # unit_id → t_parse_end

    t_global_start = time.monotonic()

    # Assign stable IDs for correlating parse results (avoid re-sending
    # the full CompilationUnit back through the pickling pipe).
    unit_by_id: dict[int, object] = {}
    tasks: list[tuple] = []
    for i, u in enumerate(units):
        unit_by_id[i] = u
        tasks.append((u, index_refs))

    # Launch parallel parse workers
    results_queue: list[tuple[int, bool, str, object]] = []
    parse_start_ts: dict[int, float] = {}
    parse_end_ts: dict[int, float] = {}

    t_before_map = time.monotonic()

    with ProcessPoolExecutor(max_workers=num_workers, initializer=_mp_init) as executor:
        # Submit all tasks, recording parse-start time per unit
        future_to_uid: dict = {}
        for uid, task in enumerate(tasks):
            parse_start_ts[uid] = time.monotonic()
            future_to_uid[executor.submit(_worker_parse, task)] = uid

        # Collect results as they complete
        for future in as_completed(future_to_uid):
            uid = future_to_uid[future]
            parse_end_ts[uid] = time.monotonic()
            success, fname, data = future.result()
            results_queue.append((uid, success, fname, data))

    # ── Process results sequentially (single writer) ──
    results_queue.sort(key=lambda x: x[0])  # stable order for reproducibility

    for uid, success, fname, data in results_queue:
        unit = unit_by_id[uid]
        t_parse = parse_end_ts[uid] - parse_start_ts[uid]
        acc_parse_queued += t_parse

        if not success:
            log.warning("worker failed: %s: %s", fname, data)
            continue

        # Determine file path for staleness/mtime bookkeeping
        file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)

        t_lock_start = time.monotonic()
        lock_waited = t_lock_start - parse_end_ts[uid]

        with write_lock(db_path.parent, timeout=120.0):
            t_write_start = time.monotonic()
            try:
                syms_added, refs_added, headers = store_symbols_for_unit(
                    conn, unit, config_hash, project_root,
                    vendor_patterns=vendor_patterns,
                    project_patterns=project_patterns,
                    index_refs=index_refs,
                    pre_parsed=data,  # ExtractionResult with tu=None
                    existing_files=existing_files,
                    build_dir_patterns=build_dir_patterns,
                )
            except Exception as exc:
                log.warning("store failed: %s: %s", fname, exc)
                continue
            t_write_end = time.monotonic()

        t_lock = t_write_start - t_lock_start
        t_write = t_write_end - t_write_start
        acc_lock += t_lock
        acc_write += t_write
        total_syms += syms_added
        total_refs += refs_added

        log.info("%s: %d syms, %d refs  p=%.1fs wl=%.1fs wr=%.1fs",
                 fname, syms_added, refs_added, t_parse, t_lock, t_write)

    elapsed = time.monotonic() - t_global_start
    return {
        "mode": f"parallel x{num_workers}",
        "total_s": elapsed,
        "parse_s": acc_parse_queued,  # wall sum across all workers
        "lock_s": acc_lock,
        "write_s": acc_write,
        "symbols": total_syms,
        "references": total_refs,
        "tus": len(units),
    }


def _mp_init() -> None:
    """Worker process initializer — set up logging that won't clash with parent."""
    logging.basicConfig(
        level=logging.WARNING,
        format="[worker] %(levelname)s %(message)s",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Setup and main
# ═══════════════════════════════════════════════════════════════════════════

def _setup_temp_db(conn: sqlite3.Connection, project_root: Path, build_dir_patterns: list[str] | None) -> str:
    """Initialize a fresh temporary database for the experiment."""
    # The test DB must have a valid schema so store_symbols_for_unit works.
    # open_db already runs ensure_schema().  We just need project + build_config.
    with write_lock(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent, timeout=30.0):
        with transaction(conn):
            _project_id = derive_project_id(project_root)
            upsert_project(conn, _project_id, "poc-test", str(project_root))
            upsert_build_config(
                conn, "poc-config", _project_id, "",
                description="PoC experiment",
                manifest_verification="indexing",
            )
            drop_fts_triggers(conn)
            rebuild_fts(conn)
    return "poc-config"


def main() -> None:
    ap = argparse.ArgumentParser(description="PoC: multiprocess parallel indexing")
    ap.add_argument("compile_commands", type=Path, help="Path to compile_commands.json")
    ap.add_argument("--max-tus", type=int, default=0, help="Limit TUs (0=all)")
    ap.add_argument("--workers", type=int, default=0, help="Parallel workers (0=os.cpu_count())")
    ap.add_argument("--refs", action="store_true", help="Extract call-graph references")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cc_path = args.compile_commands.resolve()
    if not cc_path.exists():
        log.error("compile_commands.json not found: %s", cc_path)
        sys.exit(1)

    project_root = cc_path.parent
    num_workers = args.workers or os.cpu_count() or 4

    # ── Discover TUs ──
    all_units = list(parse_cc_json(cc_path))
    from fw_context_mcp.indexer.compile_commands import _SOURCE_EXTS
    all_units = [u for u in all_units if u.file.suffix.lower() in _SOURCE_EXTS]
    if args.max_tus > 0:
        all_units = all_units[:args.max_tus]

    log.info("TUs: %d  workers: %d  refs: %s", len(all_units), num_workers, args.refs)
    log.info("project_root: %s", project_root)

    # ── Prepare vendor/project patterns ──
    vendor_patterns = list(_build_sdk_excludes(project_root))
    # project_patterns empty for PoC — is_project computed from vendor_patterns only
    project_patterns: list[str] = []

    # Build a preliminary manifest so Tier 2 staleness checks can run.
    # For the PoC we force=True (always re-parse), but the manifest is needed
    # by store_symbols_for_unit internal lookups.
    build_dir_patterns: list[str] | None = None
    try:
        build_preliminary(
            cc_path, project_root / ".fw-context", project_root,
            all_units, build_dir_patterns, project_id="poc",
        )
    except Exception:
        pass  # manifest is optional for the PoC

    # ── Run sequential baseline ──
    log.info("=== SEQUENTIAL BASELINE ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "poc_seq.db"

        # Suppress open_db logging noise during experiments
        dblog = logging.getLogger("fw_context_mcp.indexer.db._connection")
        old_level = dblog.level
        dblog.setLevel(logging.WARNING)

        conn_seq = open_db(db_path)
        dblog.setLevel(old_level)
        config_hash = _setup_temp_db(conn_seq, project_root, build_dir_patterns)
        existing_files_seq = get_file_hashes(conn_seq, config_hash)

        seq_result = sequential_index(
            all_units, conn_seq, db_path, config_hash,
            project_root, vendor_patterns, project_patterns,
            args.refs, existing_files_seq, build_dir_patterns,
        )
        conn_seq.close()

    # ── Run parallel ──
    log.info("=== PARALLEL x%d ===", num_workers)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "poc_par.db"

        dblog = logging.getLogger("fw_context_mcp.indexer.db._connection")
        old_level = dblog.level
        dblog.setLevel(logging.WARNING)

        conn_par = open_db(db_path)
        dblog.setLevel(old_level)
        config_hash = _setup_temp_db(conn_par, project_root, build_dir_patterns)
        existing_files_par = get_file_hashes(conn_par, config_hash)

        par_result = parallel_index(
            all_units, conn_par, db_path, config_hash,
            project_root, vendor_patterns, project_patterns,
            args.refs, existing_files_par, build_dir_patterns,
            num_workers,
        )
        conn_par.close()

    # ── Report ──
    results = [seq_result, par_result]
    print()
    print(f"{'Mode':<20} {'Total':>8} {'Parse':>8} {'Lock':>8} {'Write':>8} {'Syms':>8} {'Refs':>8} {'Speedup':>8}")
    print("-" * 84)
    baseline_s = seq_result["total_s"]
    for r in results:
        speedup = baseline_s / r["total_s"] if r["total_s"] > 0 else 0
        print(f"{r['mode']:<20} {_fmt_dur(r['total_s']):>8} "
              f"{_fmt_dur(r['parse_s']):>8} {_fmt_dur(r['lock_s']):>8} "
              f"{_fmt_dur(r['write_s']):>8} {r['symbols']:>8} {r['references']:>8} "
              f"{speedup:>7.1f}x")

    # ── Breakdown ──
    print()
    print("Breakdown (sequential):")
    for r in results:
        total = r["total_s"]
        if total <= 0:
            continue
        print(f"  {r['mode']}:")
        print(f"    parse={r['parse_s']/total*100:.0f}%  "
              f"lock_wait={r['lock_s']/total*100:.0f}%  "
              f"db_write={r['write_s']/total*100:.0f}%")


if __name__ == "__main__":
    main()
