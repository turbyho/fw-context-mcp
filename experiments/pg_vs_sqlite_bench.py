"""Benchmark: SQLite vs PostgreSQL bulk insert — 1 vs N threads.

Compares single-threaded and multi-threaded bulk insert throughput.
Threads each insert their own chunk of rows concurrently.

Usage:
    python experiments/pg_vs_sqlite_bench.py <compile_commands.json> [--max-tus N] [--threads N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fw_context_mcp.indexer.compile_commands import parse as parse_cc, _SOURCE_EXTS
from fw_context_mcp.indexer.symbols import extract_all
from fw_context_mcp.indexer._embedding import _fmt_dur
from fw_context_mcp.indexer.ops import _normalize_file_path

log = logging.getLogger(__name__)

SYM_COLS = [
    "config_hash", "file_id", "file_path", "name_tokens", "usr", "name",
    "qualified_name", "kind", "line", "col", "end_line", "is_definition",
    "signature", "docstring", "enum_value", "is_virtual", "is_pure_virtual",
    "parent_usr", "is_template", "template_usr", "is_project", "pagerank", "source",
]


def _parse_and_collect(units, project_root):
    """Parse all TUs, collect ALL symbols into a flat list of row tuples."""
    all_rows = []
    total = 0
    file_id = 0
    config_hash = "bench"

    for i, unit in enumerate(units):
        result = extract_all(unit, with_refs=False, return_tu=False)
        file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
        file_id += 1

        for sym in result.symbols:
            row = (
                config_hash, file_id, file_path_str, "",
                sym.usr, sym.name, sym.qualified_name, sym.kind,
                sym.line, sym.column, sym.end_line,
                1 if sym.is_definition else 0,
                sym.signature or "", sym.docstring or "",
                sym.enum_value or "",
                1 if sym.is_virtual else 0,
                1 if sym.is_pure_virtual else 0,
                sym.parent_usr or "",
                1 if sym.is_template else 0,
                sym.template_usr or "",
                1, 0.0, sym.source or "",
            )
            all_rows.append(row)
            total += 1

        log.info("[%d/%d] %s: %d syms", i + 1, len(units),
                 unit.file.name, len(result.symbols))

    return all_rows, total


def _chunk_rows(rows, n):
    """Split rows into n roughly equal chunks."""
    chunk_size = max(1, len(rows) // n)
    chunks = []
    for i in range(0, len(rows), chunk_size):
        chunks.append(rows[i:i + chunk_size])
    # Merge last small chunk into previous
    if len(chunks) > n:
        chunks[-2].extend(chunks[-1])
        chunks.pop()
    return chunks


# ═══════════════════════════════════════════════════════════════════════
# SQLite
# ═══════════════════════════════════════════════════════════════════════

def _sqlite_create_table(db_path, wal=True):
    import sqlite3
    os.makedirs(db_path.parent, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute(f"""
        CREATE TABLE symbols (
            config_hash TEXT, file_id INTEGER, file_path TEXT, name_tokens TEXT,
            usr TEXT, name TEXT, qualified_name TEXT, kind TEXT,
            line INTEGER, col INTEGER, end_line INTEGER, is_definition INTEGER,
            signature TEXT, docstring TEXT, enum_value TEXT,
            is_virtual INTEGER, is_pure_virtual INTEGER,
            parent_usr TEXT, is_template INTEGER, template_usr TEXT,
            is_project INTEGER, pagerank REAL, source TEXT
        )
    """)
    conn.commit()
    return conn


def _sqlite_insert_chunk(db_path, rows, use_wal):
    """Insert one chunk of rows — called from a thread."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    if use_wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA busy_timeout = 120000")

    placeholders = ", ".join(["?"] * len(SYM_COLS))
    sql = f"INSERT INTO symbols VALUES ({placeholders})"

    t0 = time.monotonic()
    conn.execute("BEGIN")
    conn.executemany(sql, rows)
    conn.commit()
    elapsed = time.monotonic() - t0
    conn.close()
    return elapsed


def _benchmark_sqlite(all_rows, threads, tmpdir):
    """SQLite benchmark: single-threaded + multi-threaded."""
    results = []

    # ── Single-threaded ──
    db_s = Path(tmpdir) / "sqlite_single.db"
    conn = _sqlite_create_table(db_s, wal=True)
    placeholders = ", ".join(["?"] * len(SYM_COLS))
    sql = f"INSERT INTO symbols VALUES ({placeholders})"

    conn.execute("PRAGMA journal_mode = OFF")  # fastest for single thread

    t0 = time.monotonic()
    conn.execute("BEGIN")
    conn.executemany(sql, all_rows)
    conn.commit()
    elapsed = time.monotonic() - t0
    conn.close()

    results.append({"label": "SQLite 1 thread", "elapsed": elapsed, "rows": len(all_rows)})

    # ── Multi-threaded (WAL mode required for concurrent writes) ──
    if threads > 1:
        db_m = Path(tmpdir) / "sqlite_multi.db"
        _sqlite_create_table(db_m, wal=True).close()

        chunks = _chunk_rows(all_rows, threads)
        log.info("SQLite multi: %d chunks of ~%d rows each", len(chunks), len(chunks[0]))

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(_sqlite_insert_chunk, db_m, c, True) for c in chunks]
            for f in as_completed(futs):
                f.result()
        elapsed = time.monotonic() - t0

        results.append({"label": f"SQLite {threads} threads", "elapsed": elapsed, "rows": len(all_rows)})

    return results


# ═══════════════════════════════════════════════════════════════════════
# PostgreSQL
# ═══════════════════════════════════════════════════════════════════════

def _pg_get_conn(run_dir):
    import psycopg2 as pg
    return pg.connect(f"host={run_dir} dbname=fwcontext_test", connect_timeout=5)


def _pg_create_table(conn):
    import psycopg2 as pg
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS symbols")
    cur.execute(f"""
        CREATE {'UNLOGGED' if True else ''} TABLE symbols (
            config_hash TEXT, file_id INTEGER, file_path TEXT, name_tokens TEXT,
            usr TEXT, name TEXT, qualified_name TEXT, kind TEXT,
            line INTEGER, col INTEGER, end_line INTEGER, is_definition INTEGER,
            signature TEXT, docstring TEXT, enum_value TEXT,
            is_virtual INTEGER, is_pure_virtual INTEGER,
            parent_usr TEXT, is_template INTEGER, template_usr TEXT,
            is_project INTEGER, pagerank REAL, source TEXT
        )
    """)
    conn.commit()
    cur.close()


def _pg_insert_chunk(run_dir, rows):
    """Insert one chunk of rows — called from a thread."""
    import psycopg2 as pg
    from psycopg2 import extras

    conn = pg.connect(f"host={run_dir} dbname=fwcontext_test", connect_timeout=5)

    t0 = time.monotonic()
    cur = conn.cursor()
    extras.execute_values(
        cur,
        f"INSERT INTO symbols ({', '.join(SYM_COLS)}) VALUES %s",
        rows,
        page_size=2000,
    )
    conn.commit()
    elapsed = time.monotonic() - t0
    cur.close()
    conn.close()
    return elapsed


def _benchmark_postgres(all_rows, run_dir, threads):
    """PostgreSQL benchmark: single-threaded + multi-threaded."""
    import psycopg2 as pg
    from psycopg2 import extras
    results = []

    # ── Single-threaded ──
    conn = _pg_get_conn(run_dir)
    _pg_create_table(conn)
    conn.close()

    conn = _pg_get_conn(run_dir)
    t0 = time.monotonic()
    cur = conn.cursor()
    extras.execute_values(
        cur,
        f"INSERT INTO symbols ({', '.join(SYM_COLS)}) VALUES %s",
        all_rows,
        page_size=2000,
    )
    conn.commit()
    elapsed = time.monotonic() - t0
    cur.close()
    conn.close()

    results.append({"label": "PG 1 thread", "elapsed": elapsed, "rows": len(all_rows)})

    # ── Multi-threaded ──
    if threads > 1:
        conn = _pg_get_conn(run_dir)
        _pg_create_table(conn)
        conn.close()

        chunks = _chunk_rows(all_rows, threads)
        log.info("PG multi: %d chunks of ~%d rows each", len(chunks), len(chunks[0]))

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(_pg_insert_chunk, run_dir, c) for c in chunks]
            for f in as_completed(futs):
                f.result()
        elapsed = time.monotonic() - t0

        results.append({"label": f"PG {threads} threads", "elapsed": elapsed, "rows": len(all_rows)})

    return results


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("compile_commands", type=Path)
    ap.add_argument("--max-tus", type=int, default=30)
    ap.add_argument("--threads", type=int, default=24)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cc_path = args.compile_commands.resolve()
    project_root = cc_path.parent
    units = [u for u in parse_cc(cc_path) if u.file.suffix.lower() in _SOURCE_EXTS][:args.max_tus]

    # ── Parse all TUs once ──
    log.info("Parsing %d TUs ...", len(units))
    t_p = time.monotonic()
    all_rows, total_syms = _parse_and_collect(units, project_root)
    parse_time = time.monotonic() - t_p
    total_mb = sum(len(str(r)) for r in all_rows) / (1024 * 1024)
    log.info("Parsed %d symbols (%.1f MB) in %.1fs", total_syms, total_mb, parse_time)

    # ── DB connections ──
    data_dir = os.path.expanduser("~/.local/share/fw-context-pg-data")
    run_dir = f"{data_dir}/run"

    from tempfile import TemporaryDirectory

    # ── SQLite ──
    log.info("=== SQLite ===")
    with TemporaryDirectory() as td:
        sqlite_results = _benchmark_sqlite(all_rows, args.threads, Path(td))

    # ── PostgreSQL ──
    log.info("=== PostgreSQL ===")
    pg_results = _benchmark_postgres(all_rows, run_dir, args.threads)

    # ── Report ──
    all_results = sqlite_results + pg_results
    print()
    print(f"{'Database':<24} {'Time':>10} {'Rows':>10} {'Rows/s':>12} {'vs SQLite 1t':>14}")
    print("-" * 76)
    baseline = sqlite_results[0]["elapsed"]
    for r in all_results:
        rs = r["rows"] / r["elapsed"]
        vs = baseline / r["elapsed"]
        print(f"{r['label']:<24} {_fmt_dur(r['elapsed']):>10} {r['rows']:>10} {rs:>10.0f} {vs:>12.1f}x")


if __name__ == "__main__":
    main()
