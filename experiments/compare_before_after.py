#!/usr/bin/env python3
"""A/B comparison: before vs after for all feature changes.

Runs the same metrics as the validation in implementation-plan.md and
compares with the pre-change numbers recorded there.

Metrics:
  FTS5 weights: name precision (ExactHit), Proj%, composite scores
  PageRank: distribution stats (nodes, min/max/avg, top-20 pagerank functions)
  Hotspot cache: performance (ms per query) with cache vs live fallback
  LLM typedef/enum: coverage before (0) vs after (count + sample quality)
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, find_hotspots

HA_BOILER_DB = Path.home() / ".fw-context" / "index" / "39cef596a54c8de9" / "index.db"

# ── Before data from implementation-plan.md ───────────────────────────
# Format: {metric: {before, after_source}} — "after" measured below

BEFORE = {
    # From "Původní validace — před FTS5 column weights"
    "FTS5_HA_Boiler_baseline_composite": 0.584,
    "FTS5_HA_Boiler_RRF": 0.604,
    "FTS5_HA_Boiler_Proj_baseline": 16.9,  # percent
    "FTS5_HA_Boiler_Proj_RRF": 20.9,       # percent
    "FTS5_HA_Boiler_ExactHit_count": 71.4,  # percent (5/7)

    # From "Výsledky validace — po FTS5 column weights"
    "FTS5_HA_Boiler_baseline_after_weights": 0.582,
    "FTS5_HA_Boiler_RRF_after_weights": 0.604,
    "FTS5_HA_Boiler_FTS5_Proj_after": 22.2,    # percent
    "FTS5_HA_Boiler_FTS5_ExactHit_after": 85.7,  # percent (6/7)

    # Features that didn't exist before
    "PAGERANK_nodes": 0,
    "PAGERANK_symbols_with_score": 0,
    "HOTSPOT_cache_entries": 0,
    "HOTSPOT_cache_ms": 0,  # no cache — always live
    "LLM_typedef_analyzed": 0,
    "LLM_enum_analyzed": 0,
}

# HA_Boiler exact queries matching the validation suite
HA_EXACT_QUERIES = [
    "boiler_control",
    "modbus_poll",
    "ha_update",
    "decround",
    "loop",
    "sensors_setup",
    "onTempReq1",
]

# Concept queries matching the validation suite
HA_CONCEPT_QUERIES = [
    "boiler temperature control heat",
    "MQTT sensor home assistant update",
    "WiFi connection reconnect",
    "watchdog timer battery voltage",
    "CSV log data file",
]


def _exact_hit_pct(conn, ch, queries):
    """What percentage of exact-name queries return the symbol in top 10."""
    hits = 0
    for q in queries:
        rows = search_symbols(conn, q, ch, limit=10)
        names = [r["name"] for r in rows]
        if q in names:
            hits += 1
    return hits / len(queries) * 100, hits, len(queries)


def _proj_pct(rows):
    """Percentage of project-code results (src/)."""
    if not rows:
        return 0.0
    proj = sum(1 for r in rows if (r["file_path"] or "").startswith("src/"))
    return proj / len(rows) * 100


def _measure_hotspot_perf(conn, ch):
    """Measure hotspot query performance: cold cache vs live fallback."""
    # Cache should be warm after re-index
    t0 = time.perf_counter()
    cached = find_hotspots(conn, ch, limit=20, exclude_paths=[".pio/%"])
    t_cache = (time.perf_counter() - t0) * 1000

    # Simulate live query (same as cache miss path)
    t0 = time.perf_counter()
    live = conn.execute(
        """SELECT s.name, COUNT(r.rowid) as caller_count
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.config_hash = ?
             AND s.is_definition = 1
             AND r.ref_kind IN ('call', 'indirect')
             AND s.file_path NOT LIKE '.pio/%'
             AND s.file_path NOT LIKE '.platformio/%'
           GROUP BY s.usr ORDER BY caller_count DESC LIMIT 20""",
        (ch,),
    ).fetchall()
    t_live = (time.perf_counter() - t0) * 1000
    return t_cache, t_live, len(cached), len(live)


def main():
    if not HA_BOILER_DB.exists():
        print("HA_Boiler index not found — run fw-context index first")
        sys.exit(1)

    conn = open_db(HA_BOILER_DB)
    bc = get_active_config(conn, "39cef596a54c8de9")
    ch = bc["config_hash"]

    after = {}
    print("=" * 70)
    print("A/B COMPARISON: BEFORE vs AFTER (HA_Boiler)")
    print("=" * 70)

    # ── FTS5 weights ──
    print("\n── FTS5 Column Weights ──")
    exact_pct, exact_hits, exact_total = _exact_hit_pct(conn, ch, HA_EXACT_QUERIES)
    print(f"ExactHit FTS5-only: {exact_pct:.1f}% ({exact_hits}/{exact_total})")
    after["FTS5_HA_Boiler_FTS5_ExactHit"] = exact_pct

    # Proj% across concept queries
    total_proj = 0
    total_rows = 0
    for q in HA_CONCEPT_QUERIES:
        rows = search_symbols(conn, q, ch, limit=30)
        total_proj += sum(1 for r in rows if (r["file_path"] or "").startswith("src/"))
        total_rows += len(rows)
    fts5_proj_pct = total_proj / max(total_rows, 1) * 100
    print(f"FTS5-only Proj%: {fts5_proj_pct:.1f}% ({total_proj}/{total_rows})")
    after["FTS5_HA_Boiler_FTS5_Proj"] = fts5_proj_pct

    # ── PageRank ──
    print("\n── PageRank on Call Graph ──")
    # All nodes (including vendor — realistic for PageRank computation)
    pr_all = conn.execute(
        """SELECT COUNT(*) as nodes,
                  COALESCE(MIN(pagerank), 0) as min_pr,
                  COALESCE(MAX(pagerank), 0) as max_pr,
                  COALESCE(AVG(pagerank), 0) as avg_pr,
                  COALESCE(SUM(CASE WHEN pagerank > 0.5 THEN 1 ELSE 0 END), 0) as high_pr
           FROM symbols
           WHERE pagerank > 0
             AND kind IN ('function', 'method', 'constructor', 'destructor')
             AND is_definition = 1"""
    ).fetchone()

    pr_proj = conn.execute(
        """SELECT COUNT(*) as nodes, COALESCE(MAX(pagerank), 0) as max_pr, COALESCE(AVG(pagerank), 0) as avg_pr
           FROM symbols
           WHERE pagerank > 0
             AND kind IN ('function', 'method', 'constructor', 'destructor')
             AND is_definition = 1
             AND file_path LIKE 'src/%'"""
    ).fetchone()

    print(f"Total PR nodes (all code): {pr_all['nodes']}")
    print(f"  Range: [{pr_all['min_pr']:.4f}, {pr_all['max_pr']:.4f}]")
    print(f"  Average: {pr_all['avg_pr']:.4f}")
    print(f"  High PR (>0.5): {pr_all['high_pr']}")
    print(f"Project PR nodes (src/): {pr_proj['nodes']}")
    print(f"  Range: [{pr_proj['nodes'] and '...' or 'N/A'}, {pr_proj['max_pr']:.4f}]")

    # Top 10 by pagerank (all code)
    top_pr = conn.execute(
        """SELECT name, qualified_name, kind, ROUND(pagerank, 4) as pr
           FROM symbols
           WHERE pagerank > 0 AND is_definition = 1
           ORDER BY pagerank DESC LIMIT 10"""
    ).fetchall()
    print(f"\nTop 10 functions by PageRank (whole index):")
    for i, r in enumerate(top_pr, 1):
        fpath = conn.execute(
            "SELECT file_path FROM symbols WHERE name=? AND qualified_name=? LIMIT 1",
            (r["name"], r["qualified_name"]),
        ).fetchone()
        src = "proj" if (fpath and (fpath[0] or "").startswith("src/")) else "vendor"
        print(f"  {i:2d}. {r['name']:40s} {r['kind']:15s} PR={r['pr']:.4f} [{src}]")

    after["PAGERANK_nodes"] = pr_all["nodes"]
    after["PAGERANK_symbols_with_score"] = pr_all["nodes"]
    after["PAGERANK_max"] = pr_all["max_pr"]
    after["PAGERANK_avg"] = pr_all["avg_pr"]
    after["PAGERANK_proj_nodes"] = pr_proj["nodes"]

    # ── Hotspot cache ──
    print("\n── Hotspot Cache Performance ──")
    t_cache, t_live, n_cache, n_live = _measure_hotspot_perf(conn, ch)
    speedup = t_live / max(t_cache, 0.001)
    print(f"Cache query:  {t_cache:.2f} ms → {n_cache} results")
    print(f"Live query:   {t_live:.2f} ms → {n_live} results")
    print(f"Speedup:      {speedup:.1f}×")
    after["HOTSPOT_cache_ms"] = t_cache
    after["HOTSPOT_live_ms"] = t_live
    after["HOTSPOT_cache_entries"] = conn.execute(
        "SELECT COUNT(*) FROM hotspot_cache"
    ).fetchone()[0]
    after["HOTSPOT_speedup"] = speedup

    # ── LLM typedef/enum ──
    print("\n── LLM Analysis: typedef / enum Coverage ──")
    # Total (including vendor)
    typedef_total_all = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind='typedef' AND is_definition=1"
    ).fetchone()[0]
    typedef_done_all = conn.execute(
        """SELECT COUNT(*) FROM symbols s
           JOIN llm_analysis a ON a.symbol_id = s.id
           WHERE s.kind='typedef'"""
    ).fetchone()[0]
    enum_total_all = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind='enum' AND is_definition=1"
    ).fetchone()[0]
    enum_done_all = conn.execute(
        """SELECT COUNT(*) FROM symbols s
           JOIN llm_analysis a ON a.symbol_id = s.id
           WHERE s.kind='enum'"""
    ).fetchone()[0]
    # Project only
    typedef_proj = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind='typedef' AND is_definition=1 AND file_path LIKE 'src/%'"
    ).fetchone()[0]
    enum_proj = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind='enum' AND is_definition=1 AND file_path LIKE 'src/%'"
    ).fetchone()[0]

    print(f"Typedefs (all): {typedef_done_all}/{typedef_total_all} analyzed ({typedef_done_all/max(typedef_total_all,1)*100:.1f}%)")
    print(f"Enums (all):    {enum_done_all}/{enum_total_all} analyzed ({enum_done_all/max(enum_total_all,1)*100:.1f}%)")
    print(f"  (project code: {typedef_proj} typedefs, {enum_proj} enums)")

    # Sample from vendor (most typedefs/enums are vendor code in this project)
    samples = conn.execute(
        """SELECT s.name, s.kind, s.file_path, a.summary
           FROM symbols s
           JOIN llm_analysis a ON a.symbol_id = s.id
           WHERE s.kind IN ('typedef', 'enum')
           LIMIT 5"""
    ).fetchall()
    print(f"\nSample analyses:")
    for r in samples:
        summary = (r["summary"] or "")[:80]
        print(f"  [{r['kind']}] {r['name']}")
        print(f"    {summary}...")

    after["LLM_typedef_analyzed"] = typedef_done_all
    after["LLM_enum_analyzed"] = enum_done_all
    after["LLM_typedef_total"] = typedef_total_all
    after["LLM_enum_total"] = enum_total_all

    # ── Comparison table ──
    print("\n" + "=" * 70)
    print("DELTA: BEFORE → AFTER")
    print("=" * 70)

    comparisons = [
        ("FTS5 ExactHit (name precision)", BEFORE["FTS5_HA_Boiler_ExactHit_count"], after["FTS5_HA_Boiler_FTS5_ExactHit"], "%"),
        ("FTS5 Proj% (raw, no RRF boost)", BEFORE["FTS5_HA_Boiler_Proj_baseline"], after["FTS5_HA_Boiler_FTS5_Proj"], "pp"),
        ("PageRank — total nodes", BEFORE["PAGERANK_nodes"], after["PAGERANK_nodes"], "count"),
        ("PageRank — max score", 0.0, after["PAGERANK_max"], "score"),
        ("PageRank — avg score", 0.0, after["PAGERANK_avg"], "score"),
        ("Hotspot cache query time", 0, after["HOTSPOT_live_ms"], "ms_live"),
        ("Hotspot cache query time", 0, after["HOTSPOT_cache_ms"], "ms_cache"),
        ("Hotspot cache speedup", 1.0, after["HOTSPOT_speedup"], "×"),
        ("LLM typedef coverage", BEFORE["LLM_typedef_analyzed"], after["LLM_typedef_analyzed"], "count"),
        ("LLM enum coverage", BEFORE["LLM_enum_analyzed"], after["LLM_enum_analyzed"], "count"),
    ]

    print(f"\n{'Metric':<35} {'Before':>12} {'After':>12} {'Δ':>12} {'Δ%':>10}")
    print("-" * 82)
    for name, before, after_val, unit in comparisons:
        if unit == "%":
            delta = after_val - before
            delta_pct = delta / max(before, 1) * 100 if before > 0 else 0
            delta_str = f"{delta:+.1f}pp"
            delta_pct_str = f"{delta_pct:+.0f}%"
            before_str = f"{before:.1f}%"
            after_str = f"{after_val:.1f}%"
        elif unit == "pp":
            delta = after_val - before
            delta_str = f"{delta:+.1f}pp"
            delta_pct_str = f"{delta/max(abs(before),1)*100:+.0f}%"
            before_str = f"{before:.1f}pp"
            after_str = f"{after_val:.1f}pp"
        elif unit == "×":
            delta = after_val - before
            delta_str = f"{delta:+.1f}×"
            delta_pct_str = f"{(after_val/before - 1)*100:+.0f}%"
            before_str = f"{before:.1f}×"
            after_str = f"{after_val:.1f}×"
        elif unit == "count":
            delta = after_val - before
            before_str = f"{before}"
            after_str = f"{after_val}"
            delta_str = f"{delta:+d}"
            delta_pct_str = "new" if before == 0 else f"{delta/max(before,1)*100:+.0f}%"
        elif unit == "score":
            before_str = "N/A"
            after_str = f"{after_val:.4f}"
            delta_str = "new"
            delta_pct_str = ""
        elif unit == "ms_live":
            before_str = "N/A (live)"
            after_str = f"{after_val:.1f}ms"
            delta_str = "baseline"
            delta_pct_str = ""
            continue  # skip — next line is the cache comparison
        elif unit == "ms_cache":
            before_str = "N/A"
            after_str = f"{after_val:.2f}ms"
            delta_str = "new"
            delta_pct_str = ""
        else:
            delta = after_val - before
            delta_str = f"{delta:+d}"
            delta_pct_str = ""
            before_str = f"{before}"
            after_str = f"{after_val}"
        print(f"{name:<35} {before_str:>12} {after_str:>12} {delta_str:>12} {delta_pct_str:>10}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
