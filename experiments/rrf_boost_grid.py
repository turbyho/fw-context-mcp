#!/usr/bin/env python3
"""RRF boost grid search — project + kind bonuse pro embedded C/C++.

Testuje:
  - RRF bez boostů (baseline reference)
  - Project boost: násobitel RRF skóre pro projektový kód
  - Kind boost: násobitel RRF skóre pro funkce/metody/konstruktory
  - Kombinace obou

Na obou projektech současně. Používá dotazy z předchozích experimentů.

Usage:
    python3 experiments/rrf_boost_grid.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.search.scoring import score_result, stems_from_queries

# ── Projekty ────────────────────────────────────────────────────────────────

PROJECTS: dict[str, tuple[Path, str]] = {
    "zbox-ecb-fw": (Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"), "project"),
    "HA_Boiler": (Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"), "project"),
}


def is_project(path: str, pname: str) -> bool:
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


FUNCTION_KINDS = {"function", "method", "constructor", "destructor"}
STRUCT_KINDS = {"class", "struct", "enum", "typedef"}


# ── Dotazy ──────────────────────────────────────────────────────────────────

QUERIES_ALL = {
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


# ── RRF s boosty ────────────────────────────────────────────────────────────


def rrf_fuse_boosted(
    fts5_rows: list[dict],
    vec_rows: list[dict],
    project_boost: float = 1.0,
    kind_func_boost: float = 1.0,
    kind_struct_boost: float = 1.0,
    w_fts: float = 1.2,
    w_vec: float = 0.8,
    k: int = 60,
    limit: int = 15,
    pname: str = "",
) -> list[dict[str, Any]]:
    """RRF s volitelnými doménovými boosty.

    Boost je multiplikativní na RRF skóre:
      final_score = RRF_score * boost_multiplicator

    boosty:
      project_boost: pro symboly v projektovém kódu (src/, lib/)
      kind_func_boost: pro funkce, metody, konstruktory, destruktory
      kind_struct_boost: pro třídy, struktury, enumy, typedefy
    """
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

    # Aplikuj boosty
    boosted: list[tuple[float, tuple]] = []
    for key, base_score in scores.items():
        row = all_rows[key]
        multiplier = 1.0

        if project_boost != 1.0 and pname:
            fp = row.get("file_path") or ""
            if is_project(fp, pname):
                multiplier *= project_boost

        if kind_func_boost != 1.0 or kind_struct_boost != 1.0:
            kind = row.get("kind") or ""
            if kind in FUNCTION_KINDS:
                multiplier *= kind_func_boost
            elif kind in STRUCT_KINDS:
                multiplier *= kind_struct_boost

        boosted.append((base_score * multiplier, key))

    boosted.sort(key=lambda x: -x[0])
    return [dict(all_rows[key]) for _, key in boosted[:limit]]


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


# ── Boost grid ──────────────────────────────────────────────────────────────

PROJECT_BOOSTS = [1.0, 1.1, 1.2, 1.3, 1.5]
KIND_FUNC_BOOSTS = [1.0, 1.1, 1.2]
# Struct boost testujeme samostatně (není v full gridu kvůli kombinatorice)
KIND_STRUCT_BOOSTS = [1.0, 1.1]


def make_variants():
    """Vygeneruje všechny testovací varianty."""
    variants = [
        ("baseline", None, None, None, None),
        ("fts5_only", "fts5", None, None, None),
        ("vec_only", "vec", None, None, None),
    ]

    # RRF bez boostů
    variants.append(("rrf_no_boost", "rrf", 1.0, 1.0, 1.0))

    # Project boost only
    for pb in PROJECT_BOOSTS:
        if pb == 1.0:
            continue
        variants.append((f"rrf_proj×{pb:.1f}", "rrf", pb, 1.0, 1.0))

    # Kind func boost only
    for kb in KIND_FUNC_BOOSTS:
        if kb == 1.0:
            continue
        variants.append((f"rrf_func×{kb:.1f}", "rrf", 1.0, kb, 1.0))

    # Kind struct boost only
    for sb in KIND_STRUCT_BOOSTS:
        if sb == 1.0:
            continue
        variants.append((f"rrf_struct×{sb:.1f}", "rrf", 1.0, 1.0, sb))

    # Combo: project + func
    for pb in [1.1, 1.2, 1.3, 1.5]:
        for kb in [1.1, 1.2]:
            variants.append((f"rrf_proj×{pb:.1f}_func×{kb:.1f}", "rrf", pb, kb, 1.0))

    # Combo: project + struct
    for pb in [1.1, 1.2, 1.3, 1.5]:
        for sb in [1.1]:
            variants.append((f"rrf_proj×{pb:.1f}_struct×{sb:.1f}", "rrf", 1.0, 1.0, sb))

    # Triple combo (jen pár)
    for pb in [1.2, 1.3]:
        for kb in [1.1]:
            for sb in [1.1]:
                variants.append((f"rrf_proj×{pb:.1f}_func×{kb:.1f}_struct×{sb:.1f}", "rrf", pb, kb, sb))

    return variants


# ── Main ────────────────────────────────────────────────────────────────────


def run():
    variants = make_variants()
    print(f"Testing {len(variants)} variants on 2 projects\n")

    all_results: dict[str, list[tuple[float, str, float, float, float]]] = {}

    for pname, (ppath, _) in PROJECTS.items():
        print(f"{'=' * 90}")
        print(f"PROJECT: {pname}")
        print(f"{'=' * 90}")

        cfg = load_config(project_root=ppath)
        pid = derive_project_id(ppath)
        db_path = cfg.index.db_dir / pid / "index.db"
        if not db_path.exists():
            print(f"  SKIP: no index")
            continue

        conn = open_db(db_path)
        bc = get_active_config(conn, pid)
        ch = bc["config_hash"]
        conn.close()

        qlist = QUERIES_ALL[pname]
        print(f"  Queries: {len(qlist)}")

        # Cache FTS5 + Vec
        print(f"  Retrieving...", end="", flush=True)
        cached: dict[tuple, tuple] = {}
        for cat, query in qlist:
            qv = call_ollama_embed([query], cfg.llm)[0]
            conn = open_db(db_path)
            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=30)]
            conn.close()
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
            cached[(cat, query)] = (fts_raw, vec_rows)
        print(" done")

        # Evaluate
        methods: dict[str, dict] = {}

        for label, method, pb, kb, sb in variants:
            proj_vals: list[int] = []
            exact_hits = 0
            exact_total = 0
            vec_contribs: list[float] = []

            for cat, query in qlist:
                fts_raw, vec_rows = cached[(cat, query)]
                stems = stems_from_queries([query])
                f_keys = {r["name"] for r in fts_raw}
                v_keys = {r["name"] for r in vec_rows}

                if method is None:
                    results = current_merge(fts_raw, vec_rows, stems, limit=15)
                elif method == "fts5":
                    results = fts_raw[:15]
                elif method == "vec":
                    results = vec_rows[:15]
                else:
                    results = rrf_fuse_boosted(
                        fts_raw, vec_rows,
                        project_boost=pb, kind_func_boost=kb, kind_struct_boost=sb,
                        w_fts=1.2, w_vec=0.8, k=60, limit=15, pname=pname,
                    )

                proj = sum(1 for r in results if is_project(r.get("file_path", ""), pname))
                proj_vals.append(proj)

                if cat == "exact":
                    exact_total += 1
                    if any(r["name"] == query for r in results[:3]):
                        exact_hits += 1

                r_keys = {r["name"] for r in results}
                vc = len(v_keys - f_keys)  # kolik vec-only symbolů se dostalo do výsledků
                vec_contribs.append(vc)

            avg_proj = sum(proj_vals) / max(len(proj_vals), 1)
            proj_pct = avg_proj / 15
            exact_pct = exact_hits / max(exact_total, 1) if exact_total > 0 else 0.0
            avg_vec = sum(vec_contribs) / max(len(vec_contribs), 1)
            composite = proj_pct * 0.5 + exact_pct * 0.5

            methods[label] = {
                "proj_pct": proj_pct, "exact_pct": exact_pct,
                "avg_vec": avg_vec, "composite": composite,
                "proj_vals": proj_vals, "exact_hits": exact_hits, "exact_total": exact_total,
            }

        # Sort by composite
        sorted_methods = sorted(methods.items(), key=lambda x: -x[1]["composite"])

        print(f"\n{'Method':<42} {'Proj%':>7} {'Exact':>7} {'Vec+':>6} {'Score':>7}")
        print("─" * 72)
        for label, data in sorted_methods:
            marker = " ← BASE" if label == "baseline" else ""
            if label == "rrf_no_boost":
                marker = " ← RRF base"
            print(
                f"{label:<42} {data['proj_pct']:>6.1%} {data['exact_pct']:>6.1%} "
                f"{data['avg_vec']:>5.1f} {data['composite']:>6.3f}{marker}"
            )

        all_results[pname] = [(d["composite"], label, d["proj_pct"], d["exact_pct"], d["avg_vec"])
                              for label, d in sorted_methods]

    # ── Cross-project ──────────────────────────────────────────────────
    if len(all_results) >= 2:
        print(f"\n{'=' * 90}")
        print("CROSS-PROJECT RANKING (průměrné skóre obou projektů)")
        print(f"{'=' * 90}")

        pnames = list(all_results.keys())
        p1, p2 = pnames[0], pnames[1]

        cross: dict[str, tuple[float, float, float]] = {}
        all_labels = {label for _, label, _, _, _ in all_results[p1]} | {label for _, label, _, _, _ in all_results[p2]}

        for label in all_labels:
            v1 = next((s for s, l, _, _, _ in all_results[p1] if l == label), None)
            v2 = next((s for s, l, _, _, _ in all_results[p2] if l == label), None)
            pp1 = next((p for _, l, p, _, _ in all_results[p1] if l == label), 0)
            pp2 = next((p for _, l, p, _, _ in all_results[p2] if l == label), 0)
            if v1 is not None and v2 is not None:
                cross[label] = ((v1 + v2) / 2, v1, v2)
            elif v1 is not None:
                cross[label] = (v1, v1, 0)
            else:
                cross[label] = (v2, 0, v2)

        # Top 30
        top = sorted(cross.items(), key=lambda x: -x[1][0])[:30]

        print(f"\n{'Method':<45} {'CrossAvg':>9} {p1[:12]:>12} {p2[:12]:>12}")
        print("─" * 82)
        for label, (avg, s1, s2) in top:
            marker = ""
            if label == "baseline":
                marker = " ← BASE"
            elif label == "rrf_no_boost":
                marker = " ← RRF base"
            print(f"{label:<45} {avg:>8.4f}   {s1:>8.4f}   {s2:>8.4f}{marker}")

        # Best per category
        print(f"\n── Per-project best (top 3) ──")
        for pn in pnames:
            top3 = sorted(all_results[pn], key=lambda x: -x[0])[:3]
            print(f"  {pn}:")
            for s, l, pp, ep, _ in top3:
                print(f"    {l:<45} score={s:.4f}  Proj={pp:.1%} Exact={ep:.1%}")

        # Delta analysis
        print(f"\n── Delta vs baseline ──")
        base_avg = cross.get("baseline", (0, 0, 0))[0]
        base_p1 = cross.get("baseline", (0, 0, 0))[1]
        base_p2 = cross.get("baseline", (0, 0, 0))[2]
        rrf_no_boost = cross.get("rrf_no_boost", (0, 0, 0))

        print(f"  Baseline cross-project:       {base_avg:.4f}")
        print(f"  RRF no-boost cross-project:   {rrf_no_boost[0]:.4f}  (Δ {rrf_no_boost[0] - base_avg:+.4f})")

        # Best combo
        best_combo = top[0]
        if best_combo[0] != "baseline":
            print(f"  Best RRF variant:             {best_combo[0]}  ({best_combo[1][0]:.4f}, Δ {best_combo[1][0] - base_avg:+.4f})")


if __name__ == "__main__":
    run()
