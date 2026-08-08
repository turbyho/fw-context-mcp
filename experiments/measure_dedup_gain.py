"""Measure dedup gain: full insert vs pre-filtered insert.

Compares two strategies on the same TUs:
  A) Current: insert ALL symbols via store_symbols_for_unit, SQLite ON CONFLICT handles dedup
  B) Proposed: pre-filter in Python (set lookup), pass only new symbols to store_symbols_for_unit

Uses a real compile_commands.json project. Each strategy uses a fresh
temporary database so they don't interfere.

Usage:
    python experiments/measure_dedup_gain.py <compile_commands.json> [--max-tus N]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fw_context_mcp.indexer.compile_commands import parse as parse_cc, _SOURCE_EXTS
from fw_context_mcp.indexer.symbols import extract_all
from fw_context_mcp.indexer.db import (
    open_db,
    transaction,
    write_lock,
    drop_fts_triggers,
    rebuild_fts,
    upsert_project,
    upsert_build_config,
    get_file_hashes,
)
from fw_context_mcp.indexer.ops import store_symbols_for_unit
from fw_context_mcp.indexer._embedding import _fmt_dur
from fw_context_mcp.config.settings import derive_project_id
from fw_context_mcp.indexer.sdk_detect import _build_sdk_excludes, _normalize_patterns

log = logging.getLogger(__name__)


def _setup_db(conn, db_dir: Path, project_root: Path) -> str:
    """Initialize a fresh database for one strategy run."""
    pid = derive_project_id(project_root)
    ch = "dedup-poc"
    with write_lock(db_dir, timeout=30.0):
        with transaction(conn):
            upsert_project(conn, pid, "dedup-poc", str(project_root))
            upsert_build_config(conn, ch, pid, "", description="dedup PoC")
            drop_fts_triggers(conn)
            rebuild_fts(conn)
    return ch


def run_strategy_a(units, db_path, conn, config_hash, project_root, vendor_patterns,
                   project_patterns, existing_files, build_dir_patterns) -> dict:
    """Strategy A: insert ALL symbols (current behavior)."""
    t_acc_parse = 0.0
    t_acc_write = 0.0
    total_syms = 0

    for unit in units:
        t0 = time.monotonic()
        result = extract_all(unit, with_refs=False, return_tu=True)
        t_parse = time.monotonic() - t0

        with write_lock(db_path.parent, timeout=30.0):
            t_write = time.monotonic()
            syms_added, refs_added, _ = store_symbols_for_unit(
                conn, unit, config_hash, project_root,
                vendor_patterns=vendor_patterns,
                project_patterns=project_patterns,
                index_refs=False,
                pre_parsed=result,
                existing_files=existing_files,
                build_dir_patterns=build_dir_patterns,
            )
            t_write_dt = time.monotonic() - t_write

        t_acc_parse += t_parse
        t_acc_write += t_write_dt
        total_syms += syms_added
        log.info("A %s: parse=%.2fs write=%.2fs syms=%d",
                 unit.file.name, t_parse, t_write_dt, syms_added)

    return {"parse_s": t_acc_parse, "write_s": t_acc_write, "symbols": total_syms}


def run_strategy_b(units, db_path, conn, config_hash, project_root, vendor_patterns,
                   project_patterns, existing_files, build_dir_patterns) -> dict:
    """Strategy B: pre-filter duplicates in Python set before store_symbols_for_unit."""
    t_acc_parse = 0.0
    t_acc_filter = 0.0
    t_acc_write = 0.0
    total_syms = 0
    total_dupes = 0

    known_def_usrs: set[str] = set()
    known_decl_usrs: set[str] = set()  # declarations waiting for definition

    for unit in units:
        t0 = time.monotonic()
        result = extract_all(unit, with_refs=False, return_tu=True)
        t_parse = time.monotonic() - t0

        # ── Pre-filter ──
        t_filter = time.monotonic()
        new_symbols = []
        for sym in result.symbols:
            if sym.usr in known_def_usrs:
                continue  # already stored as definition → skip entirely
            new_symbols.append(sym)
            if sym.is_definition:
                known_def_usrs.add(sym.usr)
                known_decl_usrs.discard(sym.usr)
            else:
                known_decl_usrs.add(sym.usr)
        t_filter_dt = time.monotonic() - t_filter
        dupes = len(result.symbols) - len(new_symbols)

        # Override the symbols list in the result before passing to store
        result.symbols = new_symbols

        with write_lock(db_path.parent, timeout=30.0):
            t_write = time.monotonic()
            syms_added, refs_added, _ = store_symbols_for_unit(
                conn, unit, config_hash, project_root,
                vendor_patterns=vendor_patterns,
                project_patterns=project_patterns,
                index_refs=False,
                pre_parsed=result,
                existing_files=existing_files,
                build_dir_patterns=build_dir_patterns,
            )
            t_write_dt = time.monotonic() - t_write

        t_acc_parse += t_parse
        t_acc_filter += t_filter_dt
        t_acc_write += t_write_dt
        total_syms += syms_added
        total_dupes += dupes
        log.info("B %s: parse=%.2fs filter=%.1fms write=%.2fs syms=%d dupes_skipped=%d",
                 unit.file.name, t_parse, t_filter_dt * 1000, t_write_dt,
                 syms_added, dupes)

    return {"parse_s": t_acc_parse, "filter_s": t_acc_filter, "write_s": t_acc_write,
            "symbols": total_syms, "dupes_skipped": total_dupes}


def main():
    ap = argparse.ArgumentParser(description="Measure dedup gain: full vs pre-filtered insert")
    ap.add_argument("compile_commands", type=Path)
    ap.add_argument("--max-tus", type=int, default=30)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cc_path = args.compile_commands.resolve()
    project_root = cc_path.parent
    units = [u for u in parse_cc(cc_path) if u.file.suffix.lower() in _SOURCE_EXTS][:args.max_tus]

    log.info("TUs: %d  root: %s", len(units), project_root)

    vendor_patterns = list(_build_sdk_excludes(project_root))
    project_patterns: list[str] = []
    build_dir_patterns: list[str] | None = None

    # ── Strategy A: full insert (current) ──
    log.info("=== STRATEGY A: full insert (current) ===")
    with tempfile.TemporaryDirectory() as td:
        db_a = Path(td) / "a.db"
        conn_a = open_db(db_a)
        ch_a = _setup_db(conn_a, db_a.parent, project_root)
        ef_a = get_file_hashes(conn_a, ch_a)
        result_a = run_strategy_a(
            units, db_a, conn_a, ch_a, project_root, vendor_patterns, project_patterns, ef_a, build_dir_patterns,
        )
        conn_a.close()

    # ── Strategy B: pre-filtered insert ──
    log.info("=== STRATEGY B: pre-filtered insert ===")
    with tempfile.TemporaryDirectory() as td:
        db_b = Path(td) / "b.db"
        conn_b = open_db(db_b)
        ch_b = _setup_db(conn_b, db_b.parent, project_root)
        ef_b = get_file_hashes(conn_b, ch_b)
        result_b = run_strategy_b(
            units, db_b, conn_b, ch_b, project_root, vendor_patterns, project_patterns, ef_b, build_dir_patterns,
        )
        conn_b.close()

    # ── Report ──
    wa = result_a["write_s"]
    wb = result_b["write_s"]
    print()
    print(f"{'Strategy':<36} {'Parse':>8} {'Filter':>8} {'Write':>8} {'Total':>8} {'Syms':>8}")
    print("-" * 84)
    for label, r in [("A — full insert (current)", result_a), ("B — pre-filtered", result_b)]:
        parse = r["parse_s"]
        flt = r.get("filter_s", 0)
        write = r["write_s"]
        total = parse + flt + write
        print(f"{label:<36} {_fmt_dur(parse):>8} {_fmt_dur(flt):>8} "
              f"{_fmt_dur(write):>8} {_fmt_dur(total):>8} {r['symbols']:>8}")

    total_a = result_a["parse_s"] + result_a["write_s"]
    total_b = result_b["parse_s"] + result_b.get("filter_s", 0) + result_b["write_s"]
    skip = result_b.get("dupes_skipped", 0)
    print(f"\nWrite phase: {_fmt_dur(wa)} → {_fmt_dur(wb)}  ({wa/wb:.1f}x faster)")
    print(f"Overall:     {_fmt_dur(total_a)} → {_fmt_dur(total_b)}  ({total_a/total_b:.1f}x faster)")
    print(f"Duplicates skipped (no DB write): {skip}")


if __name__ == "__main__":
    main()
