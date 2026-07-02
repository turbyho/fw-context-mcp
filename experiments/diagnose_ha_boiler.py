#!/usr/bin/env python3
"""Diagnostický skript: proč RRF prohrává s baseline na HA_Boiler.

Pro každý dotaz zobrazí:
  - Které výsledky jsou v baseline ale NE v RRF (a proč)
  - Které výsledky jsou v RRF ale NE v baseline
  - score_result rozklad pro baseline výsledky
  - Proj% a ExactHit per-query
  - Změnu pořadí u společných výsledků

Usage:
    python3 experiments/diagnose_ha_boiler.py
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

PPATH = Path("/home/turbyho/dev/sw/work/privat/HA_Boiler")


def is_project(path: str) -> bool:
    return path.startswith("src/")


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
    return [(dict(all_rows[key]), score) for key, score in ranked[:limit]]


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
    return [(r, s) for s, r in scored[:limit]]


def score_breakdown(r, stems):
    """Vrať breakdown score_result — co přispělo kolik body."""
    breakdown = {}
    name = (r.get("name") or "").lower()
    qname = (r.get("qualified_name") or "").lower()
    file_path = (r.get("file_path") or "").lower()
    kind = r.get("kind") or ""

    total = 0

    # name match
    for s in stems:
        if s in name:
            breakdown[f"name:{s}"] = 3
            total += 3

    # name_tokens match
    tokens = (r.get("name_tokens") or "").lower().split()
    for s in stems:
        for t in tokens:
            if s in t and f"tok:{t}" not in breakdown:
                breakdown[f"tok:{t}"] = 2
                total += 2

    # qualified_name match
    for s in stems:
        if s in qname:
            breakdown[f"qname:{s}"] = 2
            total += 2

    # file_path match
    for s in stems:
        if s in file_path:
            breakdown[f"file:{s}"] = 1
            total += 1

    # project local
    if is_project(r.get("file_path", "")):
        breakdown["project"] = 1
        total += 1

    # kind bonus
    kind_bonuses = {"function": 2, "method": 2, "constructor": 2, "destructor": 2,
                    "class": 1, "struct": 1, "enum": 1, "typedef": 1,
                    "enum_constant": 0, "variable": 0, "field": 0}
    bonus = kind_bonuses.get(kind, 0)
    if bonus:
        breakdown[f"kind:{kind}"] = bonus
        total += bonus

    return total, breakdown


# ── Queries ───────────────────────────────────────────────────────────────

QUERIES = [
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
]


def run():
    print("=" * 100)
    print("DIAGNOSTIKA: HA_Boiler — baseline vs RRF (fts-light_1.2/0.8_k60)")
    print("=" * 100)

    cfg = load_config(project_root=PPATH)
    pid = derive_project_id(PPATH)
    db_path = cfg.index.db_dir / pid / "index.db"
    conn = open_db(db_path)
    bc = get_active_config(conn, pid)
    ch = bc["config_hash"]
    conn.close()

    # Cache FTS5 + Vec
    cached = {}
    for cat, query in QUERIES:
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

    # ── Per-query analysis ─────────────────────────────────────────────
    total_baseline_proj = 0
    total_rrf_proj = 0
    total_baseline_exact_hit = 0
    total_rrf_exact_hit = 0
    total_exact = 0

    breakdown_agg = {"name": 0, "tok": 0, "qname": 0, "file": 0, "project": 0, "kind": 0}

    for cat, query in QUERIES:
        fts_raw, vec_rows = cached[(cat, query)]
        stems = stems_from_queries([query])

        base = current_merge(fts_raw, vec_rows, stems, limit=15)
        rrf_res = rrf_fuse(fts_raw, vec_rows, w_fts=1.2, w_vec=0.8, k=60, limit=15)

        base_keys = {(r["name"], r.get("file_path")) for r, _ in base}
        rrf_keys = {(r["name"], r.get("file_path")) for r, _ in rrf_res}

        base_only = base_keys - rrf_keys
        rrf_only = rrf_keys - base_keys

        base_proj = sum(1 for r, _ in base if is_project(r.get("file_path", "")))
        rrf_proj = sum(1 for r, _ in rrf_res if is_project(r.get("file_path", "")))
        total_baseline_proj += base_proj
        total_rrf_proj += rrf_proj

        if cat == "exact":
            total_exact += 1
            if any(r["name"] == query for r, _ in base[:3]):
                total_baseline_exact_hit += 1
            if any(r["name"] == query for r, _ in rrf_res[:3]):
                total_rrf_exact_hit += 1

        if base_only or rrf_only:
            print(f"\n{'─' * 100}")
            print(f"[{cat}] {query}")
            print(f"  FTS5 results: {len(fts_raw)}, Vec results: {len(vec_rows)}")
            print(f"  Baseline: Proj={base_proj}/15, Exact={'✓' if cat == 'exact' and any(r['name'] == query for r,_ in base[:3]) else '✗'}")
            print(f"  RRF:      Proj={rrf_proj}/15, Exact={'✓' if cat == 'exact' and any(r['name'] == query for r,_ in rrf_res[:3]) else '✗'}")

            if base_only:
                print(f"\n  ⬇ V baseline ALE NE v RRF ({len(base_only)}):")
                for r, s in base:
                    if (r["name"], r.get("file_path")) in base_only:
                        proj = "PROJ" if is_project(r.get("file_path", "")) else "SDK"
                        total_s, bd = score_breakdown(r, stems)
                        bd_str = " + ".join(f"{k}={v}" for k, v in sorted(bd.items()))
                        print(f"    [{proj}] {r['name']:<30} {r.get('kind','?'):<10} score={total_s:>2} ({bd_str})")
                        # Aggregate
                        for k in bd:
                            if k.startswith("name:"):
                                breakdown_agg["name"] += 1
                            elif k.startswith("tok:"):
                                breakdown_agg["tok"] += 1
                            elif k.startswith("qname:"):
                                breakdown_agg["qname"] += 1
                            elif k.startswith("file:"):
                                breakdown_agg["file"] += 1
                            elif k == "project":
                                breakdown_agg["project"] += 1
                            elif k.startswith("kind:"):
                                breakdown_agg["kind"] += 1

            if rrf_only:
                print(f"\n  ⬆ V RRF ALE NE v baseline ({len(rrf_only)}):")
                for r, _ in rrf_res:
                    if (r["name"], r.get("file_path")) in rrf_only:
                        proj = "PROJ" if is_project(r.get("file_path", "")) else "SDK"
                        print(f"    [{proj}] {r['name']:<30} {r.get('kind','?'):<10}")
        else:
            print(f"\n{'─' * 100}")
            print(f"[{cat}] {query}  →  IDENTICKÉ ({base_proj} proj)")

    # ── Shrnutí ───────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("SHRNUTÍ")
    print(f"{'=' * 100}")
    print(f"  Baseline Proj:     {total_baseline_proj}")
    print(f"  RRF Proj:          {total_rrf_proj}")
    print(f"  Rozdíl:            {total_baseline_proj - total_rrf_proj}")
    print(f"  Baseline ExactHit: {total_baseline_exact_hit}/{total_exact}")
    print(f"  RRF ExactHit:      {total_rrf_exact_hit}/{total_exact}")

    print(f"\n  Co score_result dává navíc (agregováno přes všechny 'base_only' výsledky):")
    for k in ["name", "tok", "qname", "file", "project", "kind"]:
        print(f"    {k}: {breakdown_agg[k]}")

    # ── Proj% per retriever ───────────────────────────────────────────
    print(f"\n  Samostatné retrievery:")
    fts5_all = []
    vec_all = []
    for cat, query in QUERIES:
        fts_raw, vec_rows = cached[(cat, query)]
        fts5_all.extend(fts_raw[:15])
        vec_all.extend(vec_rows[:15])
    fts5_proj = sum(1 for r in fts5_all if is_project(r.get("file_path", "")))
    vec_proj = sum(1 for r in vec_all if is_project(r.get("file_path", "")))
    print(f"    FTS5 only: {fts5_proj}/{len(fts5_all)} = {fts5_proj/len(fts5_all):.1%}")
    print(f"    Vec only:  {vec_proj}/{len(vec_all)} = {vec_proj/len(vec_all):.1%}")


if __name__ == "__main__":
    run()
