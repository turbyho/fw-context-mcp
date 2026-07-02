#!/usr/bin/env python3
"""Iterative weight grid search — find optimal RRF params for embedded C/C++.

Tests on two projects with realistic queries.
Grid: w_fts in [0.2, 0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0], k in [10, 30, 60]
Plus: adaptive strategy based on query type.

Quality metrics:
  - Proj%: project code fraction (higher = better)
  - Exact_hit: for exact queries, is the target symbol in top-3?
  - Vec_contrib: how many vec-only results appear?
  - FTS5_speed: how many results per second from FTS5?

Usage:
    python3 experiments/weight_grid_search.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.search.scoring import score_result, stems_from_queries


# ── Projects ───────────────────────────────────────────────────────────────

PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}


def is_project(path: str, project_name: str) -> bool:
    if project_name == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


# ── Queries per project ───────────────────────────────────────────────────

QUERIES = {
    "zbox-ecb-fw": [
        ("exact", "get_key"),
        ("exact", "zdebug"),
        ("exact", "get_ctime"),
        ("exact", "get_num_slots"),
        ("exact", "_is_ok"),
        ("exact", "get_instance"),
        ("concept", "modem send data packet"),
        ("concept", "BLE connection setup advertising"),
        ("concept", "flash storage write erase"),
        ("concept", "encrypt decrypt key security"),
        ("concept", "watchdog timer refresh"),
        ("concept", "serial RS485 communication handler"),
        ("concept", "timeout event dispatch loop"),
        ("mixed", "init setup start"),
        ("mixed", "handler callback event"),
        ("mixed", "write send transmit"),
        ("mixed", "read receive get"),
        ("edge", "nonexisting_function_xyz_123"),
    ],
    "HA_Boiler": [
        ("exact", "boiler_control"),
        ("exact", "modbus_poll"),
        ("exact", "ha_update"),
        ("exact", "decround"),
        ("exact", "loop"),
        ("exact", "sensors_setup"),
        ("exact", "onTempReq1"),
        ("concept", "boiler temperature control heat"),
        ("concept", "MQTT sensor home assistant update"),
        ("concept", "WiFi connection reconnect"),
        ("concept", "watchdog timer battery voltage"),
        ("concept", "CSV log data file"),
        ("mixed", "init setup pin output"),
        ("mixed", "read write poll modbus"),
        ("edge", "nonexisting_function_xyz"),
    ],
}


# ── RRF ────────────────────────────────────────────────────────────────────


def rrf_fuse(fts5_rows, vec_rows, w_fts=1.2, w_vec=0.8, k=60, limit=15):
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
    return [dict(all_rows[key]) for key, _ in ranked[:limit]]


def current_merge(fts5_rows, vec_rows, stems, limit=15):
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


# ── Weight grid ───────────────────────────────────────────────────────────

WEIGHT_GRID = [
    # (w_fts, w_vec, label)
    (0.2, 1.8, "vec-heavy_0.2/1.8"),
    (0.5, 1.5, "vec-biased_0.5/1.5"),
    (0.8, 1.2, "vec-light_0.8/1.2"),
    (1.0, 1.0, "balanced_1.0/1.0"),
    (1.2, 0.8, "fts-light_1.2/0.8"),
    (1.5, 0.5, "fts-biased_1.5/0.5"),
    (1.8, 0.2, "fts-heavy_1.8/0.2"),
    (2.0, 0.0, "fts-only_2.0/0.0"),
]

K_VALUES = [10, 30, 60]


# ── Run ────────────────────────────────────────────────────────────────────


def run():
    all_results = {}

    for pname, ppath in PROJECTS.items():
        print(f"\n{'=' * 80}")
        print(f"PROJECT: {pname}")
        print(f"{'=' * 80}")

        cfg = load_config(project_root=ppath)
        pid = derive_project_id(ppath)
        db_path = cfg.index.db_dir / pid / "index.db"

        if not db_path.exists():
            print(f"  SKIP: no index at {db_path}")
            continue

        conn = open_db(db_path)
        bc = get_active_config(conn, pid)
        ch = bc["config_hash"]
        sym_count = bc["symbol_count"] if "symbol_count" in bc.keys() else "?"
        conn.close()
        print(f"  Symbols: {sym_count}")

        qlist = QUERIES[pname]
        project_results = {}

        # Collect FTS5 + Vec results for all queries (expensive — do once)
        print(f"  Retrieving {len(qlist)} queries...")
        cached: dict[str, tuple] = {}
        for cat, query in qlist:
            t0 = time.monotonic()

            # Embedding
            qv = call_ollama_embed([query], cfg.llm)[0]

            # FTS5
            conn = open_db(db_path)
            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=30)]
            conn.close()

            # Vec
            conn = open_db(db_path)
            with conn:
                vr = search_similar_vec(conn, qv, ch, threshold=0.50, limit=30)
                sids = [r["symbol_id"] for r in vr]
                vec_rows = []
                if sids:
                    ph = ",".join("?" * len(sids))
                    vec_rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({ph}) AND is_definition=1",
                        (ch, *sids),
                    ).fetchall()]
            conn.close()

            elapsed = time.monotonic() - t0
            print(f"    [{cat}] {query[:45]:45s} FTS={len(fts_raw)} Vec={len(vec_rows)} ({elapsed:.0f}s)", flush=True)

            cached[f"{cat}|{query}"] = (fts_raw, vec_rows)

        # Test all methods
        print(f"\n  Testing methods: baseline + {len(WEIGHT_GRID)}×{len(K_VALUES)} RRF variants...")
        methods = {}

        for cat, query in qlist:
            fts_raw, vec_rows = cached[f"{cat}|{query}"]
            stems = stems_from_queries([query])

            # Baseline
            cur = current_merge(fts_raw, vec_rows, stems, limit=15)
            cur_proj = sum(1 for r in cur if is_project(r.get("file_path", ""), pname))
            cur_exact_hit = False
            if cat == "exact":
                cur_exact_hit = any(
                    r["name"] == query for r in cur[:3]
                )

            key_base = f"baseline"
            if key_base not in methods:
                methods[key_base] = {"proj": [], "exact_hit": 0, "exact_total": 0, "vec_contrib": [], "count": 0}
            methods[key_base]["proj"].append(cur_proj)
            methods[key_base]["count"] += 1
            if cat == "exact":
                methods[key_base]["exact_total"] += 1
                if cur_exact_hit:
                    methods[key_base]["exact_hit"] += 1
            methods[key_base]["vec_contrib"].append(0)  # baseline doesn't track this

            # FTS5-only
            fts5_only = fts_raw[:15]
            fts5_proj = sum(1 for r in fts5_only if is_project(r.get("file_path", ""), pname))
            fts5_exact = cat == "exact" and any(r["name"] == query for r in fts5_only[:3])
            key_fts5 = "fts5_only"
            if key_fts5 not in methods:
                methods[key_fts5] = {"proj": [], "exact_hit": 0, "exact_total": 0, "vec_contrib": [], "count": 0}
            methods[key_fts5]["proj"].append(fts5_proj)
            methods[key_fts5]["count"] += 1
            if cat == "exact":
                methods[key_fts5]["exact_total"] += 1
                if fts5_exact:
                    methods[key_fts5]["exact_hit"] += 1
            methods[key_fts5]["vec_contrib"].append(0)

            # Vec-only
            vec_only = vec_rows[:15]
            vec_proj = sum(1 for r in vec_only if is_project(r.get("file_path", ""), pname))
            vec_exact = cat == "exact" and any(r["name"] == query for r in vec_only[:3])
            key_vec = "vec_only"
            if key_vec not in methods:
                methods[key_vec] = {"proj": [], "exact_hit": 0, "exact_total": 0, "vec_contrib": [], "count": 0}
            methods[key_vec]["proj"].append(vec_proj)
            methods[key_vec]["count"] += 1
            if cat == "exact":
                methods[key_vec]["exact_total"] += 1
                if vec_exact:
                    methods[key_vec]["exact_hit"] += 1
            f_keys_set = {r["name"] for r in fts_raw}
            v_keys_set = {r["name"] for r in vec_rows}
            methods[key_vec]["vec_contrib"].append(len(v_keys_set - f_keys_set))

            # RRF variants
            for w_fts, w_vec, wlabel in WEIGHT_GRID:
                for kv in K_VALUES:
                    label = f"rrf_{wlabel}_k{kv}"
                    results = rrf_fuse(fts_raw, vec_rows, w_fts=w_fts, w_vec=w_vec, k=kv, limit=15)
                    proj = sum(1 for r in results if is_project(r.get("file_path", ""), pname))
                    exact_hit = cat == "exact" and any(r["name"] == query for r in results[:3])

                    r_keys = {r["name"] for r in results}
                    vec_contrib = len(v_keys_set & r_keys) - len(f_keys_set & r_keys)

                    if label not in methods:
                        methods[label] = {"proj": [], "exact_hit": 0, "exact_total": 0, "vec_contrib": [], "count": 0}
                    methods[label]["proj"].append(proj)
                    methods[label]["count"] += 1
                    if cat == "exact":
                        methods[label]["exact_total"] += 1
                        if exact_hit:
                            methods[label]["exact_hit"] += 1
                    methods[label]["vec_contrib"].append(max(0, vec_contrib))

        # Print results
        n = len(qlist)
        exact_n = sum(1 for c, _ in qlist if c == "exact")

        # Header
        print(f"\n{'Method':<28} {'Proj%':>7} {'ExactHit':>9} {'Vec+':>6} {'Score':>7}")
        print("─" * 60)

        # Score = Proj% × 0.6 + ExactHit% × 0.4 (normalized to useful range)
        scored = []
        for label, data in methods.items():
            avg_proj = sum(data["proj"]) / max(data["count"], 1)
            proj_pct = avg_proj / 15  # out of 15 results
            exact_pct = data["exact_hit"] / max(data["exact_total"], 1) if data["exact_total"] > 0 else 0
            avg_vec = sum(data["vec_contrib"]) / max(data["count"], 1)
            # Composite score: Proj ratio is main quality metric
            composite = proj_pct * 0.5 + exact_pct * 0.5

            scored.append((composite, label, proj_pct, exact_pct, avg_vec, data))

            marker = " ← BASELINE" if label == "baseline" else ""
            print(
                f"{label:<28} {proj_pct:>6.1%} {exact_pct:>8.1%} {avg_vec:>5.1f} {composite:>6.3f}{marker}"
            )

        all_results[pname] = scored

    # ── Cross-project analysis ──────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("CROSS-PROJECT ANALYSIS")
    print(f"{'=' * 80}")

    if len(all_results) >= 2:
        pnames = list(all_results.keys())
        p1, p2 = pnames[0], pnames[1]

        # Compute cross-project average for each method
        cross = {}
        for label in {l for _, l, _, _, _, _ in all_results[p1]}:
            v1 = next((s for s, l_, _, _, _, _ in all_results[p1] if l_ == label), None)
            v2 = next((s for s, l_, _, _, _, _ in all_results[p2] if l_ == label), None)
            if v1 is not None and v2 is not None:
                cross[label] = (v1 + v2) / 2

        print(f"\nMethod                              {p1:>16} {p2:>16}  CrossAvg")
        print("─" * 70)

        for label, cross_avg in sorted(cross.items(), key=lambda x: -x[1]):
            v1 = next((s for s, l_, _, _, _, _ in all_results[p1] if l_ == label), 0)
            v2 = next((s for s, l_, _, _, _, _ in all_results[p2] if l_ == label), 0)
            marker = " ← BASELINE" if label == "baseline" else ""
            print(f"{label:<35} {v1:>6.3f}      {v2:>6.3f}      {cross_avg:>6.3f}{marker}")

        # Find best per-project and best compromise
        best_p1 = max(all_results[p1], key=lambda x: x[0])
        best_p2 = max(all_results[p2], key=lambda x: x[0])
        best_cross = max(cross.items(), key=lambda x: x[1])

        print(f"\nBest per project:")
        print(f"  {p1}: {best_p1[1]} (score={best_p1[0]:.3f})")
        print(f"  {p2}: {best_p2[1]} (score={best_p2[0]:.3f})")
        print(f"Best cross-project compromise: {best_cross[0]} (avg={best_cross[1]:.3f})")


if __name__ == "__main__":
    run()
