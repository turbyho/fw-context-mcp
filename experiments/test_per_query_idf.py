#!/usr/bin/env python3
"""Test per-query RRF weight adaptation via name-match count.

Combines:
  1. Per-project baseline: proj_ratio → w_fts ∈ {1.2, 1.5, 1.8}
  2. Per-query modifier: exact name match count → Δw_fts

Compares: fixed (always 1.8/0.2) vs adaptive (baseline + per-query).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed


PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}


def is_project(path: str, pname: str) -> bool:
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


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


def baseline_weights(proj_ratio: float) -> tuple[float, float]:
    if proj_ratio < 0.02:
        return 1.8, 0.2
    elif proj_ratio < 0.10:
        return 1.5, 0.5
    else:
        return 1.2, 0.8


def per_query_adjust(conn, config_hash: str, query: str) -> float:
    """Delta to apply to w_fts based on query characteristics."""
    tokens = query.lower().split()
    if len(tokens) == 1:
        return +0.2  # single token → exact lookup → more FTS

    min_match = float("inf")
    for t in tokens:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND name = ?",
            (config_hash, t),
        ).fetchone()[0]
        min_match = min(min_match, cnt)

    if min_match == 0:
        return -0.3  # no token matches any symbol → concept → more Vec
    elif min_match <= 3:
        return 0.0   # rare matches → neutral
    else:
        return -0.1  # common terms → slight Vec for disambiguation


def rrf_fuse(fts5: list[dict], vec: list[dict], w_fts: float, w_vec: float,
             k: int, pname: str) -> list[dict]:
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}

    for rank, r in enumerate(fts5, 1):
        key = (r["name"], r["file_path"])
        scores[key] = scores.get(key, 0) + w_fts / (k + rank)
        if key not in all_rows:
            all_rows[key] = r

    for rank, r in enumerate(vec, 1):
        key = (r["name"], r["file_path"])
        scores[key] = scores.get(key, 0) + w_vec / (k + rank)
        if key not in all_rows:
            all_rows[key] = r

    for key, sc in list(scores.items()):
        r = all_rows[key]
        if is_project(r.get("file_path", ""), pname):
            sc *= 1.5
        if r.get("kind") in ("function", "method", "constructor", "destructor"):
            sc *= 1.2
        pr = r.get("pagerank", 0) or 0
        if pr > 0:
            sc *= (1.0 + pr * 0.2)
        scores[key] = sc

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [all_rows[key] for key, _ in ranked[:15]]


def compute_metrics(results: list[dict], qtype: str, query: str, pname: str) -> dict:
    top15 = results[:15]
    proj = sum(1 for r in top15 if is_project(r.get("file_path", ""), pname))
    eh = qtype == "exact" and any(r["name"] == query for r in top15[:3])
    return {
        "proj_pct": proj / len(top15) * 100 if top15 else 0,
        "composite": proj / len(top15) * 0.5 + (0.5 if eh else 0),
        "exact_hit": eh,
    }


def main():
    for pname, ppath in PROJECTS.items():
        pid = derive_project_id(ppath)
        db_path = Path.home() / ".fw-context" / "index" / pid / "index.db"

        conn = open_db(db_path)
        proj_cnt = conn.execute("SELECT COUNT(*) FROM symbols WHERE is_project = 1").fetchone()[0]
        total_cnt = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        proj_ratio = proj_cnt / max(total_cnt, 1)
        base_w_fts, base_w_vec = baseline_weights(proj_ratio)

        row = conn.execute("SELECT config_hash FROM build_configs LIMIT 1").fetchone()
        config_hash = row[0]
        cfg = load_config(ppath)

        print(f"\n{'='*70}")
        print(f"Project: {pname}")
        print(f"Symbols: {total_cnt} total, {proj_cnt} project ({proj_ratio*100:.1f}%)")
        print(f"Baseline: w_fts={base_w_fts}, w_vec={base_w_vec}")
        print(f"{'='*70}")

        fixed_metrics: list[dict] = []
        adaptive_metrics: list[dict] = []

        for qtype, query in QUERIES[pname]:
            # FTS5
            fts5 = [dict(r) for r in search_symbols(conn, query, config_hash, limit=50)]
            for r in fts5:
                r["_source"] = "fts5"

            # Vec
            vec: list[dict] = []
            try:
                qv_list = call_ollama_embed([query], cfg.llm)
                if qv_list and qv_list[0]:
                    vr = search_similar_vec(conn, qv_list[0], config_hash, threshold=0.5, limit=50)
                    sids = [r["symbol_id"] for r in vr]
                    if sids:
                        ph = ",".join("?" * len(sids))
                        vec = [dict(r) for r in conn.execute(
                            f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({ph}) AND is_definition=1",
                            (config_hash, *sids),
                        ).fetchall()]
                        for r in vec:
                            r["_source"] = "vec"
            except Exception:
                pass

            # Fixed: always 1.8/0.2
            fr = rrf_fuse(fts5, vec, 1.8, 0.2, 30, pname)
            fixed_metrics.append(compute_metrics(fr, qtype, query, pname))

            # Adaptive: baseline + per-query
            adj = per_query_adjust(conn, config_hash, query)
            w_fts = max(0.8, min(2.0, base_w_fts + adj))
            w_vec = 2.0 - w_fts
            ar = rrf_fuse(fts5, vec, w_fts, w_vec, 30, pname)
            adaptive_metrics.append(compute_metrics(ar, qtype, query, pname))

            feh = "✓" if fixed_metrics[-1]["exact_hit"] else " "
            aeh = "✓" if adaptive_metrics[-1]["exact_hit"] else " "
            marker = ""
            if abs(fixed_metrics[-1]["composite"] - adaptive_metrics[-1]["composite"]) > 0.001:
                d = adaptive_metrics[-1]["composite"] - fixed_metrics[-1]["composite"]
                marker = f" Δ={d:+.3f}"
            print(f"  {qtype:>7s}: {query:<40s} "
                  f"adj={adj:+.1f} w={w_fts:.1f}/{w_vec:.1f} "
                  f"fix={fixed_metrics[-1]['composite']:.3f}{feh} "
                  f"adp={adaptive_metrics[-1]['composite']:.3f}{aeh}"
                  f"{marker}")

        # Aggregate
        fx_avg = sum(m["composite"] for m in fixed_metrics) / len(fixed_metrics)
        ad_avg = sum(m["composite"] for m in adaptive_metrics) / len(adaptive_metrics)
        fx_proj = sum(m["proj_pct"] for m in fixed_metrics) / len(fixed_metrics)
        ad_proj = sum(m["proj_pct"] for m in adaptive_metrics) / len(adaptive_metrics)

        exact_n = sum(1 for qtype, _ in QUERIES[pname] if qtype == "exact")
        fx_eh = sum(1 for m in fixed_metrics if m["exact_hit"])
        ad_eh = sum(1 for m in adaptive_metrics if m["exact_hit"])

        print(f"\n  {'FIXED':>7s}:  comp={fx_avg:.3f}  proj={fx_proj:.1f}%  exact={fx_eh}/{exact_n}")
        print(f"  {'ADAPTIVE':>7s}: comp={ad_avg:.3f}  proj={ad_proj:.1f}%  exact={ad_eh}/{exact_n}  "
              f"Δ={ad_avg-fx_avg:+.3f} ({(ad_avg/fx_avg-1)*100:+.1f}%)")
        conn.close()

    print()


if __name__ == "__main__":
    main()
