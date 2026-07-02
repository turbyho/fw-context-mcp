#!/usr/bin/env python3
"""Diagnostika: proč projektový kód neprochází top ranky.

Pro každý dotaz zobrazí:
  - Kolik projektových symbolů je v top-30 FTS5 a top-30 Vec
  - Na jakých rankových pozicích se nacházejí
  - Kolik projektových symbolů je v indexu celkem
  - Pro exact dotazy: kde je exact match v FTS5 a Vec ranku
  - Porovnání zbox-ecb-fw vs HA_Boiler

Usage:
    python3 experiments/diagnose_project_ranks.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed

# ── Projekty ────────────────────────────────────────────────────────────────

PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}


def is_project(path: str, pname: str) -> bool:
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


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


def get_symbol_counts(conn, ch, pname: str):
    """Celkový počet symbolů + projektový podíl."""
    total = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND is_definition=1",
        (ch,),
    ).fetchone()[0]

    # Project count — estimate via LIKE on file_path
    proj_rows = conn.execute(
        "SELECT file_path FROM symbols WHERE config_hash=? AND is_definition=1",
        (ch,),
    ).fetchall()
    proj = sum(1 for (fp,) in proj_rows if is_project(fp or "", pname))

    return total, proj


def analyze_rank_distribution(results, pname: str, query_type: str, query: str):
    """Analyzuj kde se v rank listu nachází projektový kód."""
    proj_ranks = []
    sdk_ranks = []
    exact_rank = None

    for rank, r in enumerate(results, start=1):
        fp = r.get("file_path") or ""
        if is_project(fp, pname):
            proj_ranks.append(rank)
        else:
            sdk_ranks.append(rank)

        if query_type == "exact" and r.get("name") == query:
            exact_rank = rank

    return proj_ranks, exact_rank


def run():
    for pname, ppath in PROJECTS.items():
        print(f"\n{'=' * 100}")
        print(f"PROJECT: {pname}")
        print(f"{'=' * 100}")

        cfg = load_config(project_root=ppath)
        pid = derive_project_id(ppath)
        db_path = cfg.index.db_dir / pid / "index.db"

        if not db_path.exists():
            print("  SKIP: no index")
            continue

        conn = open_db(db_path)
        bc = get_active_config(conn, pid)
        ch = bc["config_hash"]

        total_sym, proj_sym = get_symbol_counts(conn, ch, pname)
        conn.close()

        print(f"\n  Index: {total_sym} total symbols, {proj_sym} project symbols ({proj_sym / max(total_sym, 1):.1%})")
        print(f"  SDK/vendor symbols: {total_sym - proj_sym}")

        qlist = QUERIES_ALL[pname]
        print(f"\n  Query analysis ({len(qlist)} queries):")

        # Aggregates
        fts5_proj_ranks: list[int] = []
        vec_proj_ranks: list[int] = []
        fts5_exact_ranks: list[int] = []
        vec_exact_ranks: list[int] = []
        per_query: list[dict] = []

        for cat, query in qlist:
            qv = call_ollama_embed([query], cfg.llm)[0]

            # FTS5
            conn = open_db(db_path)
            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=50)]
            conn.close()

            # Vec
            conn = open_db(db_path)
            with conn:
                vr = search_similar_vec(conn, qv, ch, threshold=0.50, limit=50)
                sids = [r["symbol_id"] for r in vr]
                vec_rows = []
                if sids:
                    ph = ",".join("?" * len(sids))
                    vec_rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({ph}) AND is_definition=1",
                        (ch, *sids),
                    ).fetchall()]
            conn.close()

            fts_proj_ranks, fts_exact = analyze_rank_distribution(fts_raw, pname, cat, query)
            vec_proj_ranks, vec_exact = analyze_rank_distribution(vec_rows, pname, cat, query)

            fts5_proj_ranks.extend(fts_proj_ranks)
            vec_proj_ranks.extend(vec_proj_ranks)
            if fts_exact:
                fts5_exact_ranks.append(fts_exact)
            if vec_exact:
                vec_exact_ranks.append(vec_exact)

            per_query.append({
                "cat": cat, "query": query,
                "fts5_total": len(fts_raw), "vec_total": len(vec_rows),
                "fts5_proj": len(fts_proj_ranks), "vec_proj": len(vec_proj_ranks),
                "fts5_proj_ranks": fts_proj_ranks, "vec_proj_ranks": vec_proj_ranks,
                "fts_exact": fts_exact, "vec_exact": vec_exact,
            })

        # ── Per-query table ──────────────────────────────────────────
        print(f"\n  {'Query':<40} {'FTS5_P':>7} {'FTS5_r':>14} {'Vec_P':>7} {'Vec_r':>14}")
        print(f"  {'─'*40} {'─'*7} {'─'*14} {'─'*7} {'─'*14}")

        for pq in per_query:
            fts_r_str = f"[{min(pq['fts5_proj_ranks']) if pq['fts5_proj_ranks'] else '-'}..{max(pq['fts5_proj_ranks']) if pq['fts5_proj_ranks'] else '-'}]" if pq['fts5_proj_ranks'] else "none"
            vec_r_str = f"[{min(pq['vec_proj_ranks']) if pq['vec_proj_ranks'] else '-'}..{max(pq['vec_proj_ranks']) if pq['vec_proj_ranks'] else '-'}]" if pq['vec_proj_ranks'] else "none"

            exc = ""
            if pq["cat"] == "exact":
                exc = f" FTS_exact@{pq['fts_exact']} Vec_exact@{pq['vec_exact']}"

            print(
                f"  [{pq['cat'][:1]}]{pq['query']:<38} "
                f"{pq['fts5_proj']:>3}/{pq['fts5_total']:<3} {fts_r_str:<14} "
                f"{pq['vec_proj']:>3}/{pq['vec_total']:<3} {vec_r_str:<14}"
                f"{exc}"
            )

        # ── Summary stats ────────────────────────────────────────────
        print(f"\n  ── Aggregate ──")

        # FTS5
        n_q = len(qlist)
        fts_any_proj = sum(1 for pq in per_query if pq["fts5_proj"] > 0)
        vec_any_proj = sum(1 for pq in per_query if pq["vec_proj"] > 0)

        print(f"  Queries with ≥1 project result:  FTS5={fts_any_proj}/{n_q}  Vec={vec_any_proj}/{n_q}")
        print(f"  Total project results in top-50: FTS5={len(fts5_proj_ranks)}  Vec={len(vec_proj_ranks)}")

        if fts5_proj_ranks:
            print(f"  FTS5 project rank distribution: min={min(fts5_proj_ranks)} max={max(fts5_proj_ranks)} "
                  f"median={sorted(fts5_proj_ranks)[len(fts5_proj_ranks)//2]} "
                  f"avg={sum(fts5_proj_ranks)/len(fts5_proj_ranks):.1f}")

            # Top-15 vs 16-30 vs 31-50
            top15 = sum(1 for r in fts5_proj_ranks if r <= 15)
            mid = sum(1 for r in fts5_proj_ranks if 16 <= r <= 30)
            deep = sum(1 for r in fts5_proj_ranks if r > 30)
            print(f"  FTS5 project in top-15: {top15}, rank 16-30: {mid}, rank 31-50: {deep}")

        if vec_proj_ranks:
            print(f"  Vec  project rank distribution: min={min(vec_proj_ranks)} max={max(vec_proj_ranks)} "
                  f"median={sorted(vec_proj_ranks)[len(vec_proj_ranks)//2]} "
                  f"avg={sum(vec_proj_ranks)/len(vec_proj_ranks):.1f}")

            top15 = sum(1 for r in vec_proj_ranks if r <= 15)
            mid = sum(1 for r in vec_proj_ranks if 16 <= r <= 30)
            deep = sum(1 for r in vec_proj_ranks if r > 30)
            print(f"  Vec  project in top-15: {top15}, rank 16-30: {mid}, rank 31-50: {deep}")

        if fts5_exact_ranks:
            print(f"  FTS5 exact match ranks: {fts5_exact_ranks} (avg={sum(fts5_exact_ranks)/len(fts5_exact_ranks):.1f})")
        if vec_exact_ranks:
            print(f"  Vec  exact match ranks: {vec_exact_ranks} (avg={sum(vec_exact_ranks)/len(vec_exact_ranks):.1f})")

        # Per-category breakdown
        print(f"\n  ── Per category ──")
        for cat in ["exact", "concept", "mixed", "edge"]:
            cat_qs = [pq for pq in per_query if pq["cat"] == cat]
            if not cat_qs:
                continue
            fts_p = sum(pq["fts5_proj"] for pq in cat_qs)
            vec_p = sum(pq["vec_proj"] for pq in cat_qs)
            fts_t = sum(pq["fts5_total"] for pq in cat_qs)
            vec_t = sum(pq["vec_total"] for pq in cat_qs)
            fts_any = sum(1 for pq in cat_qs if pq["fts5_proj"] > 0)
            vec_any = sum(1 for pq in cat_qs if pq["vec_proj"] > 0)
            print(f"  {cat:<8} FTS5 project: {fts_p:>3}/{fts_t} ({fts_any}/{len(cat_qs)} queries)  "
                  f"Vec project: {vec_p:>3}/{vec_t} ({vec_any}/{len(cat_qs)} queries)")

        # ── Overfetch analysis ───────────────────────────────────────
        print(f"\n  ── Overfetch potential ──")
        # Kolik projektových výsledků by se přidalo přechodem z top-30 na top-50?
        fts30 = sum(1 for r in fts5_proj_ranks if r <= 30)
        fts50 = len(fts5_proj_ranks)
        vec30 = sum(1 for r in vec_proj_ranks if r <= 30)
        vec50 = len(vec_proj_ranks)

        print(f"  FTS5: top-30 has {fts30} project results, top-50 adds {fts50 - fts30} more")
        print(f"  Vec:  top-30 has {vec30} project results, top-50 adds {vec50 - vec30} more")
        if fts30 > 0:
            print(f"  FTS5 overfetch gain: {(fts50 - fts30) / fts30 * 100:.0f}% more project results from 30→50")
        if vec30 > 0:
            print(f"  Vec  overfetch gain: {(vec50 - vec30) / vec30 * 100:.0f}% more project results from 30→50")


if __name__ == "__main__":
    run()
