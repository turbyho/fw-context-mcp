#!/usr/bin/env python3
"""Test adaptive RRF weights vs fixed defaults on real projects.

Compares:
  - fixed: always w_fts=1.8, w_vec=0.2, k=30 (current production default)
  - adaptive: w_fts/w_vec/overfetch based on proj_ratio tier
  - adaptive-baseline: per-tier baseline only, no per-query adjustment

Uses the same query sets and metrics as weight_grid_search.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.config import load as load_config


# ── Projects ───────────────────────────────────────────────────────────────

PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}


def is_project(path: str, project_name: str) -> bool:
    if project_name == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


# ── Queries ────────────────────────────────────────────────────────────────

QUERIES = {
    "zbox-ecb-fw": [
        ("exact", "get_key"), ("exact", "zdebug"), ("exact", "get_ctime"),
        ("exact", "get_num_slots"), ("exact", "_is_ok"), ("exact", "get_instance"),
        ("concept", "modem send data packet"),
        ("concept", "BLE connection setup advertising"),
        ("concept", "flash storage write erase"),
        ("concept", "encrypt decrypt key security"),
        ("concept", "watchdog timer refresh"),
        ("concept", "serial RS485 communication handler"),
        ("concept", "timeout event dispatch loop"),
        ("mixed", "init setup start"), ("mixed", "handler callback event"),
        ("mixed", "write send transmit"), ("mixed", "read receive get"),
        ("edge", "nonexisting_function_xyz_123"),
    ],
    "HA_Boiler": [
        ("exact", "boiler_control"), ("exact", "modbus_poll"), ("exact", "ha_update"),
        ("exact", "decround"), ("exact", "loop"), ("exact", "sensors_setup"),
        ("exact", "onTempReq1"),
        ("concept", "boiler temperature control heat"),
        ("concept", "MQTT sensor home assistant update"),
        ("concept", "WiFi connection reconnect"),
        ("concept", "modbus RS485 communication"),
        ("concept", "PID regulation"),
        ("mixed", "send receive data"), ("mixed", "init setup"),
        ("edge", "nonexisting_symbol_xyz"),
    ],
}


# ── Adaptive logic ─────────────────────────────────────────────────────────

def adaptive_params(proj_ratio: float) -> dict[str, float]:
    """Determine per-project RRF params from project code ratio.

    Thresholds derived from cross-project grid search data (sec 8.8-8.10).
    """
    if proj_ratio < 0.02:       # < 2% → microproject (HA_Boiler: 0.7%)
        return {"w_fts": 1.8, "w_vec": 0.2, "overfetch": 30}
    elif proj_ratio < 0.10:     # 2-10% → small project (intermediate)
        return {"w_fts": 1.5, "w_vec": 0.5, "overfetch": 40}
    else:                        # ≥ 10% → large project (zbox-ecb-fw: 19%)
        return {"w_fts": 1.2, "w_vec": 0.8, "overfetch": 50}


# ── RRF scoring ────────────────────────────────────────────────────────────

def rrf_score(fts5_results: list[dict], vec_results: list[dict],
              w_fts: float, w_vec: float, k: int = 30,
              proj_name: str = "") -> list[dict]:
    """Reciprocal Rank Fusion with project/kind/pagerank boosts."""
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}

    for rank, row in enumerate(fts5_results, start=1):
        key = (row["name"], row["file_path"])
        scores[key] = scores.get(key, 0) + w_fts / (k + rank)
        if key not in all_rows:
            all_rows[key] = row

    for rank, row in enumerate(vec_results, start=1):
        key = (row["name"], row["file_path"])
        scores[key] = scores.get(key, 0) + w_vec / (k + rank)
        if key not in all_rows:
            all_rows[key] = row

    # Post-RRF boosts (matching production rrf_fusion.py)
    for key, score in list(scores.items()):
        row = all_rows[key]
        if is_project(row.get("file_path", ""), proj_name):
            score *= 1.5  # project boost
        if row.get("kind") in ("function", "method", "constructor", "destructor"):
            score *= 1.2  # kind boost
        if row.get("pagerank", 0) > 0:
            score *= (1.0 + row["pagerank"] * 0.2)  # pagerank boost
        scores[key] = score

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [all_rows[key] for key, _ in ranked[:15]]


# ── Metrics ────────────────────────────────────────────────────────────────

def compute_metrics(results: list[dict], query_type: str, query: str,
                    proj_name: str) -> dict:
    """Proj%, Def%, Vec-ratio, composite score."""
    top15 = results[:15]
    proj_count = sum(1 for r in top15 if is_project(r.get("file_path", ""), proj_name))
    def_count = sum(1 for r in top15 if r.get("is_definition"))
    vec_from = sum(1 for r in top15 if r.get("_source") == "vec")

    proj_pct = proj_count / len(top15) * 100 if top15 else 0
    def_pct = def_count / len(top15) * 100 if top15 else 0

    # For exact queries: is the target in top-3?
    exact_hit = False
    if query_type == "exact":
        names = [r["name"] for r in top15[:3]]
        exact_hit = any(n == query for n in names)

    # Composite score: Proj% × 0.5 + ExactHit% × 0.5 (same as grid search)
    composite = proj_pct / 100 * 0.5 + (1.0 if exact_hit else 0.0) * 0.5

    return {
        "proj_pct": proj_pct,
        "def_pct": def_pct,
        "exact_hit": exact_hit,
        "vec_from": vec_from,
        "composite": composite,
        "result_count": len(top15),
    }


# ── Main ───────────────────────────────────────────────────────────────────

def test_project(project_name: str, project_path: Path) -> dict:
    """Run queries on one project with fixed and adaptive RRF."""
    # DB path — may be centralized
    from fw_context_mcp.config import derive_project_id
    project_id = derive_project_id(project_path)
    local_db = project_path / ".fw-context" / "index.db"
    central_db = Path.home() / ".fw-context" / "index" / project_id / "index.db"
    db_path = central_db if central_db.exists() else local_db

    print(f"\n{'='*70}")
    print(f"Project: {project_name}")
    print(f"DB: {db_path}")
    print(f"{'='*70}")

    conn = open_db(db_path)
    from fw_context_mcp.config import derive_project_id
    project_id = derive_project_id(project_path)
    config = get_active_config(conn, project_id)
    if config is None:
        config = get_active_config(conn, project_id)
        # Try getting config hash from build_configs directly
        row = conn.execute(
            "SELECT config_hash FROM build_configs ORDER BY indexed_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print(f"ERROR: no config found for {project_name}")
            conn.close()
            return {"name": project_name, "fixed": {}, "adaptive": {}}
        config_hash = row["config_hash"]
    else:
        config_hash = config["config_hash"]

    # Compute proj_ratio
    proj_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE is_project = 1 AND config_hash = ?",
        (config_hash,),
    ).fetchone()[0]
    total_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash = ?",
        (config_hash,),
    ).fetchone()[0]
    proj_ratio = proj_count / max(total_count, 1)
    print(f"Symbols: {total_count} total, {proj_count} project ({proj_ratio*100:.1f}%)")

    # Adaptive params
    adaptive = adaptive_params(proj_ratio)
    fixed = {"w_fts": 1.8, "w_vec": 0.2, "overfetch": 50}
    print(f"Fixed:     w_fts={fixed['w_fts']}, w_vec={fixed['w_vec']}, overfetch={fixed['overfetch']}")
    print(f"Adaptive:  w_fts={adaptive['w_fts']}, w_vec={adaptive['w_vec']}, overfetch={adaptive['overfetch']}")

    queries = QUERIES.get(project_name, QUERIES["zbox-ecb-fw"])

    fixed_metrics: list[dict] = []
    adaptive_metrics: list[dict] = []

    for qtype, query in queries:
        print(f"  {qtype}: {query}", end=" ... ", flush=True)

        # FTS5 search
        fts5_results = [dict(r) for r in search_symbols(
            conn, query, config_hash, limit=max(fixed["overfetch"], adaptive["overfetch"]),
        )]
        for r in fts5_results:
            r["_source"] = "fts5"

        # Vector search
        vec_results: list[dict] = []
        vec_ok = False
        try:
            cfg = load_config(project_path)
            query_vec_list = call_ollama_embed([query], cfg.llm)
            if query_vec_list and query_vec_list[0]:
                query_vec = query_vec_list[0]
                vr = search_similar_vec(
                    conn, query_vec, config_hash,
                    threshold=0.5, limit=max(fixed["overfetch"], adaptive["overfetch"]),
                )
                sids = [r["symbol_id"] for r in vr]
                if sids:
                    ph = ",".join("?" * len(sids))
                    vec_results = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({ph}) AND is_definition=1",
                        (config_hash, *sids),
                    ).fetchall()]
                    for r in vec_results:
                        r["_source"] = "vec"
                vec_ok = len(vec_results) > 0
        except Exception:
            pass

        # Fixed RRF
        fixed_results = rrf_score(
            fts5_results, vec_results,
            w_fts=fixed["w_fts"], w_vec=fixed["w_vec"], k=30,
            proj_name=project_name,
        )
        fixed_m = compute_metrics(fixed_results, qtype, query, project_name)
        fixed_metrics.append(fixed_m)

        # Adaptive RRF
        adaptive_results = rrf_score(
            fts5_results, vec_results,
            w_fts=adaptive["w_fts"], w_vec=adaptive["w_vec"], k=30,
            proj_name=project_name,
        )
        adaptive_m = compute_metrics(adaptive_results, qtype, query, project_name)
        adaptive_metrics.append(adaptive_m)

        # Track per-result differences
        n_diff = sum(1 for a, b in zip(fixed_results[:10], adaptive_results[:10])
                     if a.get("name") != b.get("name"))
        vec_in = sum(1 for r in adaptive_results[:10] if r.get("_source") == "vec")

        print(f"fixed={fixed_m['composite']:.3f} adaptive={adaptive_m['composite']:.3f} "
              f"diff={n_diff}/10 vec_in_top10={vec_in} vec_ok={vec_ok}")

    conn.close()

    # Aggregate
    def aggregate(metrics: list[dict], label: str) -> dict:
        exact = [m for m, (qtype, _) in zip(metrics, queries) if qtype == "exact"]
        concept = [m for m, (qtype, _) in zip(metrics, queries) if qtype == "concept"]
        all_m = metrics

        avg_proj = sum(m["proj_pct"] for m in all_m) / len(all_m) if all_m else 0
        avg_composite = sum(m["composite"] for m in all_m) / len(all_m) if all_m else 0
        exact_hits = sum(1 for m in exact if m["exact_hit"])
        exact_total = len(exact)
        avg_vec = sum(m.get("vec_from", 0) for m in all_m) / len(all_m) if all_m else 0

        return {
            "label": label,
            "avg_proj_pct": avg_proj,
            "avg_composite": avg_composite,
            "exact_hit": f"{exact_hits}/{exact_total}",
            "exact_hit_pct": exact_hits / max(exact_total, 1) * 100,
            "avg_vec": avg_vec,
        }

    fixed_agg = aggregate(fixed_metrics, f"fixed ({fixed['w_fts']}/{fixed['w_vec']})")
    adaptive_agg = aggregate(adaptive_metrics, f"adaptive ({adaptive['w_fts']}/{adaptive['w_vec']})")

    print(f"\n{'Label':<25} {'Proj%':>7} {'Composite':>9} {'ExactHit':>12} {'Vec':>6}")
    print("-" * 65)
    for agg in [fixed_agg, adaptive_agg]:
        print(f"{agg['label']:<25} {agg['avg_proj_pct']:>6.1f}% {agg['avg_composite']:>8.3f}  "
              f"{agg['exact_hit']:>8} ({agg['exact_hit_pct']:.0f}%) {agg['avg_vec']:>5.1f}")

    return {"name": project_name, "fixed": fixed_agg, "adaptive": adaptive_agg}


def main():
    results = []
    for name, path in PROJECTS.items():
        result = test_project(name, path)
        results.append(result)

    print(f"\n{'='*70}")
    print("CROSS-PROJECT SUMMARY")
    print(f"{'='*70}")
    print(f"{'Method':<25} {'zbox-ecb-fw':>15} {'HA_Boiler':>15} {'Cross Avg':>12}")
    print("-" * 68)

    for method, key in [("Fixed (1.8/0.2)", "fixed"), ("Adaptive", "adaptive")]:
        vals = [r[key]["avg_composite"] for r in results]
        cross = sum(vals) / len(vals) if vals else 0
        parts = "  ".join(f"{v:>10.3f}" for v in vals)
        print(f"{method:<25} {parts}  {cross:>10.3f}")

    # Delta
    fixed_cross = sum(r["fixed"]["avg_composite"] for r in results) / len(results)
    adaptive_cross = sum(r["adaptive"]["avg_composite"] for r in results) / len(results)
    delta = adaptive_cross - fixed_cross
    print(f"\nAdaptive delta vs fixed: {delta:+.3f} ({(delta/fixed_cross)*100:+.1f}%)")


if __name__ == "__main__":
    main()
