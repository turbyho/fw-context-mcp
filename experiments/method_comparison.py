#!/usr/bin/env python3
"""Comprehensive comparison of retrieval methods on a real project.

Compares:
  - current_merge (score-based, current production)
  - fts5_only (raw FTS5 rank)
  - vec_only (raw cosine distance)
  - rrf_* (7 RRF variants: k × weights)

Metrics (comparative — no ground truth needed):
  - project_ratio: fraction of results from src/ or lib/ (not SDK)
  - unique_contribution: symbols found only by this method
  - overlap: Jaccard with current merge
  - rank_shift: Kendall tau vs FTS5 rank
  - def_ratio: fraction of is_definition results

Usage:
    python3 experiments/method_comparison.py [--project PATH] [--limit N]
"""

from __future__ import annotations

import json
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.search.scoring import score_result, stems_from_queries


# ── Queries ───────────────────────────────────────────────────────────────
# Built from real project symbols found via find_hotspots + file_map

QUERIES = [
    # Exact symbol names (known to exist in the project)
    ("exact", "get_key"),
    ("exact", "zdebug"),
    ("exact", "get_ctime"),
    ("exact", "get_num_slots"),
    ("exact", "_is_ok"),
    ("exact", "get_instance"),
    ("exact", "LSM6DSL_ACC_GYRO_read_reg"),
    # Concept queries
    ("concept", "modem send data packet"),
    ("concept", "BLE connection setup advertising"),
    ("concept", "flash storage write erase"),
    ("concept", "encrypt decrypt key security"),
    ("concept", "watchdog timer refresh"),
    ("concept", "I2C sensor accelerometer read"),
    ("concept", "serial RS485 communication handler"),
    ("concept", "timeout event dispatch loop"),
    # Mixed / short queries
    ("mixed", "init setup start"),
    ("mixed", "handler callback event"),
    ("mixed", "write send transmit"),
    ("mixed", "read receive get"),
    # Edge
    ("edge", "nonexisting_function_xyz_123"),
]


# ── Method implementations ─────────────────────────────────────────────────


def current_merge(fts5_rows, vec_rows, stems, limit=20):
    """Replicate current deduplicate.py merge."""
    seen: dict[tuple, dict] = {}
    all_rows = list(fts5_rows) + list(vec_rows)
    scored: list[tuple[int, dict]] = []
    for r in all_rows:
        name = r.get("name") or ""
        if name.startswith("("):
            continue
        if len(name) <= 2 and r.get("kind") in ("variable", "field"):
            continue
        key = (name, r.get("file_path"))
        prev = seen.get(key)
        if prev is None:
            s = score_result(r, stems)
            seen[key] = r
            scored.append((s, r))
        elif r.get("is_definition") and not prev.get("is_definition"):
            seen[key] = r
            for i, (_, existing) in enumerate(scored):
                if (existing.get("name") == name
                    and (existing.get("file_path") or "") == (r.get("file_path") or "")):
                    scored[i] = (score_result(r, stems), r)
                    break
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def rrf_fuse(fts5_rows, vec_rows, w_fts=1.2, w_vec=0.8, k=60, limit=20):
    """Merge two ranked lists via Reciprocal Rank Fusion."""
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}
    for rank, r in enumerate(fts5_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + w_fts / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    for rank, r in enumerate(vec_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + w_vec / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    results = []
    for key, _rrf_score in ranked[:limit]:
        entry = dict(all_rows[key])
        entry["_method"] = "rrf"
        results.append(entry)
    return results


METHODS = {
    "current": ("merge", current_merge),
    "fts5_only": ("retriever", lambda f, v, s, l=20: f[:l]),
    "vec_only": ("retriever", lambda f, v, s, l=20: v[:l]),
    "rrf_k10_f1.2v0.8": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, k=10, limit=l)),
    "rrf_k30_f1.2v0.8": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, k=30, limit=l)),
    "rrf_k60_f1.2v0.8": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, k=60, limit=l)),
    "rrf_k100_f1.2v0.8": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, k=100, limit=l)),
    "rrf_k60_f1.5v0.5": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, w_fts=1.5, w_vec=0.5, k=60, limit=l)),
    "rrf_k60_f1.0v1.0": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, w_fts=1.0, w_vec=1.0, k=60, limit=l)),
    "rrf_k60_f0.8v1.2": ("rrf", lambda f, v, s, l=20: rrf_fuse(f, v, w_fts=0.8, w_vec=1.2, k=60, limit=l)),
}


# ── Metrics ────────────────────────────────────────────────────────────────


def is_project(path: str) -> bool:
    return path.startswith("src/") or path.startswith("lib/")


def jaccard(a: set, b: set) -> float:
    u = len(a | b)
    return len(a & b) / u if u > 0 else 0.0


def analyze_query(query, fts5_rows, vec_rows, stems, limit):
    """Run all methods on one query, return per-method stats."""
    f_keys = {(r["name"], r.get("file_path", "")) for r in fts5_rows}
    v_keys = {(r["name"], r.get("file_path", "")) for r in vec_rows}
    all_keys = f_keys | v_keys

    methods = {}
    for label, (mtype, fn) in METHODS.items():
        results = fn(fts5_rows, vec_rows, stems, limit)
        keys = {(r["name"], r.get("file_path", "")) for r in results}
        paths = [r.get("file_path", "") for r in results]
        names = [r["name"] for r in results]
        kinds = [r["kind"] for r in results]
        definitions = [r for r in results if r.get("is_definition")]

        methods[label] = {
            "count": len(results),
            "keys": keys,
            "names": names,
            "project": sum(1 for p in paths if is_project(p)),
            "project_ratio": sum(1 for p in paths if is_project(p)) / max(len(paths), 1),
            "def_ratio": len(definitions) / max(len(results), 1),
            "fts5_overlap": len(keys & f_keys),
            "vec_overlap": len(keys & v_keys),
            "unique": len(keys - f_keys - v_keys) if mtype != "retriever" else 0,
            "type": mtype,
        }

    # Compute Jaccard vs current merge baseline
    baseline_keys = methods["current"]["keys"]
    for label in methods:
        methods[label]["jaccard_vs_current"] = jaccard(methods[label]["keys"], baseline_keys)

    return methods, f_keys, v_keys, all_keys


def print_table(aggregated: dict, all_queries: list):
    """Print a formatted comparison table."""
    n = len(all_queries)
    categories = sorted(set(q[0] for q in all_queries))

    header = f"{'Method':<25} {'Proj%':>6} {'Def%':>5} {'Jacc':>6} {'FTS5∩':>6} {'Vec∩':>6}"
    sep = "─" * len(header)

    print(f"\n{'=' * 70}")
    print(f"Aggregated across {n} queries ({', '.join(categories)}), limit=15")
    print(f"{'=' * 70}")
    print(header)
    print(sep)

    for label in METHODS:
        if label not in aggregated:
            continue
        a = aggregated[label]
        avg_p = sum(v["project_ratio"] for v in a) / n
        avg_d = sum(v["def_ratio"] for v in a) / n
        avg_j = sum(v["jaccard_vs_current"] for v in a) / n
        avg_f = sum(v["fts5_overlap"] for v in a) / n
        avg_v = sum(v["vec_overlap"] for v in a) / n

        print(
            f"{label:<25} {avg_p:>5.1%} {avg_d:>4.1%} {avg_j:>5.3f} {avg_f:>5.1f} {avg_v:>5.1f}"
        )

    print(sep)
    print()


def print_per_category(aggregated: dict, all_queries: list, category: str):
    """Print stats for a specific query category."""
    queries = [q for q in all_queries if q[0] == category]
    if not queries:
        return

    n = len(queries)
    indices = [i for i, q in enumerate(all_queries) if q[0] == category]

    header = f"{'Method':<25} {'Proj%':>6} {'Def%':>5} {'Jacc':>6} {'FTS5∩':>6} {'Vec∩':>6}"
    sep = "─" * len(header)

    print(f"\n── {category.upper()} ({n} queries) ──")
    print(header)
    print(sep)

    for label in METHODS:
        if label not in aggregated:
            continue
        a = aggregated[label]
        cat_values = [a[i] for i in indices]
        avg_p = sum(v["project_ratio"] for v in cat_values) / n
        avg_d = sum(v["def_ratio"] for v in cat_values) / n
        avg_j = sum(v["jaccard_vs_current"] for v in cat_values) / n
        avg_f = sum(v["fts5_overlap"] for v in cat_values) / n
        avg_v = sum(v["vec_overlap"] for v in cat_values) / n

        print(
            f"{label:<25} {avg_p:>5.1%} {avg_d:>4.1%} {avg_j:>5.3f} {avg_f:>5.1f} {avg_v:>5.1f}"
        )
    print(sep)


def print_per_query(all_queries: list, all_methods: list):
    """Print top-3 results per method per query."""
    for i, (cat, query) in enumerate(all_queries):
        print(f"\n{'─' * 70}")
        print(f"[{i+1}/{len(all_queries)}] [{cat}] {query!r}")

        # Show currents top-3 first
        current_data = all_methods[i].get("current", {})
        current_names = current_data.get("names", [])[:5]
        print(f"  current top-5: {', '.join(current_names)}")

        # Highlight differences from current
        current_keys = current_data.get("keys", set())
        for label in METHODS:
            if label == "current":
                continue
            data = all_methods[i].get(label, {})
            names = data.get("names", [])[:5]
            new_in = set(names[:5]) - current_keys
            missing = current_keys - set(names[:5])
            diff = ""
            if new_in:
                diff += f" +{list(new_in)[:2]}"
            if missing:
                diff += f" -{list(missing)[:2]}"
            print(f"  {label:<25} {', '.join(names[:3])}  {diff}" if diff else f"  {label:<25} {', '.join(names[:3])}")


# ── Main ───────────────────────────────────────────────────────────────────


def run(project_path: str, limit: int = 15):
    project_root = Path(project_path).resolve()
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index at {db_path}")
        return

    conn = open_db(db_path)
    build_cfg = get_active_config(conn, project_id)
    config_hash = build_cfg["config_hash"]
    conn.close()

    sym_count = dict(build_cfg).get("symbol_count", "?") if build_cfg else "?"
    print(f"Project: {project_root.name} | Symbols: {sym_count}")
    print(f"Limit: {limit} | Methods: {len(METHODS)} | Queries: {len(QUERIES)}")
    print(f"FTS5 may be slow on large DB — expect 10-90s per query.\n")

    aggregated = {label: [] for label in METHODS}
    all_methods_per_query = []

    for qi, (category, query) in enumerate(QUERIES):
        print(f"[{qi+1}/{len(QUERIES)}] {category}: {query!r}", end=" ", flush=True)

        # Embedding (one per query)
        t0 = time.monotonic()
        try:
            query_vec = call_ollama_embed([query], cfg.llm)[0]
        except Exception as e:
            print(f"SKIP (embed error: {e})")
            continue

        # FTS5
        conn = open_db(db_path)
        with conn:
            fts5_raw = [dict(r) for r in search_symbols(conn, query, config_hash, limit=limit * 4)]
        conn.close()
        t_fts = time.monotonic() - t0

        # Vector
        v0 = time.monotonic()
        conn = open_db(db_path)
        with conn:
            vec_raw = search_similar_vec(conn, query_vec, config_hash, threshold=0.50, limit=limit * 4)
            sym_ids = [r["symbol_id"] for r in vec_raw]
            vec_rows = []
            if sym_ids:
                placeholders = ",".join("?" * len(sym_ids))
                vec_rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM symbols WHERE config_hash = ? AND id IN ({placeholders}) AND is_definition = 1",
                    (config_hash, *sym_ids),
                ).fetchall()]
        conn.close()
        t_vec = time.monotonic() - v0

        f_names = {r["name"] for r in fts5_raw}
        v_names = {r["name"] for r in vec_rows}
        overlap = len(f_names & v_names)
        print(f"[FTS={len(fts5_raw)}/{t_fts:.0f}s Vec={len(vec_rows)}/{t_vec:.0f}s ∩={overlap}]", flush=True)

        # Run all methods
        stems = stems_from_queries([query])
        methods_result, _, _, _ = analyze_query(query, fts5_raw, vec_rows, stems, limit)
        all_methods_per_query.append(methods_result)

        for label in METHODS:
            aggregated[label].append(methods_result[label])

    # Print results
    print_table(aggregated, QUERIES)
    for cat in ["exact", "concept", "mixed", "edge"]:
        print_per_category(aggregated, QUERIES, cat)
    print_per_query(QUERIES, all_methods_per_query)

    print(f"\n✓ Done — {len(QUERIES)} queries × {len(METHODS)} methods")


if __name__ == "__main__":
    parser = ArgumentParser(description="Method comparison experiment")
    parser.add_argument("--project", default="/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    run(args.project, args.limit)
