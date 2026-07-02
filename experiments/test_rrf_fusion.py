#!/usr/bin/env python3
"""RRF fusion experiment — minimal version for fast iteration.

Usage:
    python3 experiments/test_rrf_fusion.py [--project PROJECT_ROOT]
"""

from __future__ import annotations

import sys
import time
from argparse import ArgumentParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.search.scoring import score_result, stems_from_queries

# Only 3 queries for quick iteration
QUERIES = [
    ("exact", "uart_init"),
    ("concept", "how does the modem send data"),
    ("mixed", "BLE pairing failure handling"),
]

# Core RRF combos to test
RRF_COMBOS = [
    (10, 1.2, 0.8, "fts-biased/k10"),
    (30, 1.2, 0.8, "fts-biased/k30"),
    (60, 1.2, 0.8, "fts-biased/k60"),
    (60, 1.5, 0.5, "fts-heavy/k60"),
    (60, 1.0, 1.0, "balanced/k60"),
    (60, 0.8, 1.2, "vec-biased/k60"),
    (100, 1.2, 0.8, "fts-biased/k100"),
]


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
    for key, rrf_score in ranked[:limit]:
        entry = dict(all_rows[key])
        entry["_rrf_score"] = round(rrf_score, 6)
        f_keys = {(r.get("name"), r.get("file_path")) for r in fts5_rows}
        v_keys = {(r.get("name"), r.get("file_path")) for r in vec_rows}
        kk = (entry.get("name"), entry.get("file_path"))
        if kk in f_keys and kk in v_keys:
            entry["_source"] = "both"
        elif kk in f_keys:
            entry["_source"] = "fts5"
        else:
            entry["_source"] = "vec"
        results.append(entry)
    return results


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
                if (existing.get("name") == name and (existing.get("file_path") or "") == (r.get("file_path") or "")):
                    scored[i] = (score_result(r, stems), r)
                    break
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def fmt(r, idx=0):
    src = r.get("_source", "")
    return f"  #{idx}: {r.get('name','?')} ({r.get('kind','?')}) — {r.get('file_path','?')}:{r.get('line','?')}" + (f" [{src}]" if src else "")


def run(project_path: str, limit: int = 20):
    project_root = Path(project_path).resolve()
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index at {db_path}")
        return

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        config_hash = build_cfg["config_hash"]
    finally:
        conn.close()

    print(f"Project: {project_root.name} | Limit: {limit}\n")

    # ---- Phase 0: Threshold sweep (single query) ----
    print("=" * 60)
    print("PHASE 0: Threshold sweep — 'how does the modem send data'")
    print("=" * 60)
    t0 = time.monotonic()
    query_vec = call_ollama_embed(["how does the modem send data"], cfg.llm)[0]
    print(f"  Embedding generated in {time.monotonic() - t0:.1f}s")
    for threshold in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65]:
        conn = open_db(db_path)
        with conn:
            rows = search_similar_vec(conn, query_vec, config_hash, threshold=threshold, limit=50)
        conn.close()
        print(f"  threshold={threshold:.2f}: {len(rows):3d} results")

    # ---- Phase 1: Per-query retrieval + RRF ----
    print()
    print("=" * 60)
    print("PHASE 1+2: Independent retrieval & RRF fusion")
    print("=" * 60)

    for category, query in QUERIES:
        print(f"\n{'─' * 50}")
        print(f"[{category}] '{query}'")

        # Generate embedding (one per query)
        t0 = time.monotonic()
        try:
            query_vec = call_ollama_embed([query], cfg.llm)[0]
            print(f"  Embed: {time.monotonic() - t0:.1f}s")
        except Exception as e:
            print(f"  SKIP: {e}")
            continue

        # FTS5
        t0 = time.monotonic()
        conn = open_db(db_path)
        with conn:
            fts5_rows = [dict(r) for r in search_symbols(conn, query, config_hash, limit=60)]
        conn.close()
        print(f"  FTS5: {len(fts5_rows)} results ({time.monotonic() - t0:.1f}s)")

        # Vector (independent KNN, threshold=0.50)
        t0 = time.monotonic()
        conn = open_db(db_path)
        with conn:
            vec_raw = search_similar_vec(conn, query_vec, config_hash, threshold=0.50, limit=60)
        sym_ids = [r["symbol_id"] for r in vec_raw]
        vec_rows = []
        if sym_ids:
            with conn:
                placeholders = ",".join("?" * len(sym_ids))
                emb_rows = conn.execute(
                    f"SELECT * FROM symbols WHERE config_hash = ? AND id IN ({placeholders}) AND is_definition = 1",
                    (config_hash, *sym_ids),
                ).fetchall()
                vec_rows = [dict(r) for r in emb_rows]
        conn.close()

        f_keys = {(r.get("name"), r.get("file_path")) for r in fts5_rows}
        v_keys = {(r.get("name"), r.get("file_path")) for r in vec_rows}
        both = f_keys & v_keys
        only_v = v_keys - f_keys
        print(f"  Vec: {len(vec_rows)} results, overlap={len(both)}, vec-only={len(only_v)} ({time.monotonic() - t0:.1f}s)")

        # Current approach (FTS5 only in practice)
        stems = stems_from_queries([query])
        cur = current_merge(fts5_rows, [], stems, limit=limit)
        print(f"  Current top-5:")
        for i, r in enumerate(cur[:5], 1):
            print(fmt(r, i))

        # RRF — test all combos in-memory (fast!)
        t0 = time.monotonic()
        best_quality = -1
        best_label = None
        best_results = None
        for k, w_fts, w_vec, label in RRF_COMBOS:
            results = rrf_fuse(fts5_rows, vec_rows, w_fts=w_fts, w_vec=w_vec, k=k, limit=limit)
            vec_only = sum(1 for r in results if r.get("_source") == "vec")
            defs = sum(1 for r in results if r.get("is_definition"))
            quality = defs + vec_only * 1.5
            if quality > best_quality:
                best_quality = quality
                best_label = label
                best_results = results

        print(f"  RRF in-memory: {time.monotonic() - t0:.3f}s (all {len(RRF_COMBOS)} combos)")
        print(f"  Best RRF: {best_label} (quality={best_quality:.0f})")
        print(f"  Best RRF top-5:")
        for i, r in enumerate(best_results[:5], 1):
            print(fmt(r, i))

    print()
    print("✓ Done — no source code modified.")


if __name__ == "__main__":
    parser = ArgumentParser(description="RRF fusion experiment (minimal)")
    parser.add_argument("--project", default="/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    run(args.project, args.limit)
