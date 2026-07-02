#!/usr/bin/env python3
"""Graph edge experiment v4 — tune call-graph expansion parameters.

Tune: number of seeds, expansion direction, priority strategy.
Optimize for cross-project score (Proj% × 0.5 + ExactHit × 0.5).
"""
from __future__ import annotations
import sys, time
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed
from fw_context_mcp.search.scoring import score_result, stems_from_queries

PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}
QUERIES = {
    "zbox-ecb-fw": [
        ("exact", "get_key"), ("exact", "zdebug"), ("exact", "get_ctime"),
        ("exact", "get_num_slots"), ("exact", "_is_ok"), ("exact", "get_instance"),
        ("concept", "modem send data packet"), ("concept", "BLE connection setup advertising"),
        ("concept", "flash storage write erase"), ("concept", "encrypt decrypt key security"),
        ("concept", "watchdog timer refresh"), ("concept", "serial RS485 communication handler"),
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
        ("concept", "WiFi connection reconnect"), ("concept", "watchdog timer battery voltage"),
        ("concept", "CSV log data file"),
        ("mixed", "init setup pin output"), ("mixed", "read write poll modbus"),
        ("edge", "nonexisting_function_xyz"),
    ],
}


def is_project(path, pname):
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


def rrf_top(fts5_rows, vec_rows, w_fts=1.8, w_vec=0.2, k=30, limit=15):
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}
    def _b(r):
        fp, kd = r.get("file_path", ""), r.get("kind", "")
        b = 1.0
        if fp.startswith("src/") or fp.startswith("lib/") or fp.startswith("app/"):
            b *= 1.5
        if kd in ("function", "method", "constructor", "destructor"):
            b *= 1.2
        return b
    for rank, r in enumerate(fts5_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + _b(r) * w_fts / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    for rank, r in enumerate(vec_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + _b(r) * w_vec / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [dict(all_rows[key]) for key, _ in ranked[:limit]]


def baseline(fts5_rows, vec_rows, stems, limit=15):
    seen: dict[tuple, dict] = {}
    all_r = list(fts5_rows) + list(vec_rows)
    sc: list[tuple[int, dict]] = []
    for r in all_r:
        name = r.get("name") or ""
        if name.startswith("(") or (len(name) <= 2 and r.get("kind") in ("variable", "field")):
            continue
        key = (name, r.get("file_path"))
        prev = seen.get(key)
        if prev is None:
            s = score_result(r, stems)
            seen[key] = r
            sc.append((s, r))
        elif r.get("is_definition") and not prev.get("is_definition"):
            seen[key] = r
    sc.sort(key=lambda x: -x[0])
    return [r for _, r in sc[:limit]]


def metrics(top15, cat, query, pname):
    proj = sum(1 for r in top15 if is_project(r.get("file_path",""), pname))
    exact = cat == "exact" and any(r["name"] == query for r in top15[:3])
    return proj, exact


def run():
    all_cross = {}

    for pname, ppath in PROJECTS.items():
        print(f"\n{'='*80}")
        print(f"PROJECT: {pname}")
        print(f"{'='*80}")
        cfg = load_config(project_root=ppath)
        pid = derive_project_id(ppath)
        db_path = cfg.index.db_dir / pid / "index.db"
        if not db_path.exists():
            continue
        conn = open_db(db_path)
        bc = get_active_config(conn, pid)
        ch = bc["config_hash"]
        qlist = QUERIES[pname]

        # Phase 1: retrieve
        print(f"  Retrieving {len(qlist)} queries...")
        cached = {}
        for cat, query in qlist:
            t0 = time.monotonic()
            qv = call_ollama_embed([query], cfg.llm)[0]
            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=50)]
                vr = search_similar_vec(conn, qv, ch, threshold=0.50, limit=50)
                sids = [r["symbol_id"] for r in vr]
                vec_rows = []
                if sids:
                    vec_rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({','.join('?'*len(sids))}) AND is_definition=1",
                        (ch, *sids)).fetchall()]
            elapsed = time.monotonic() - t0
            print(f"    [{cat}] {query[:45]:45s} FTS={len(fts_raw)} Vec={len(vec_rows)} ({elapsed:.0f}s)", flush=True)
            cached[f"{cat}|{query}"] = (fts_raw, vec_rows)

        # Phase 2: test expansion configs
        exact_n = sum(1 for c, _ in qlist if c == "exact")

        # Expansion configs to test
        configs = [
            # (label, n_seeds, direction, priority_strategy)
            ("no_expand", 0, "none", "none"),
            ("callers_s3", 3, "callers", "proj_first"),
            ("callees_s3", 3, "callees", "proj_first"),
            ("both_s3", 3, "both", "proj_first"),
            ("callers_s5", 5, "callers", "proj_first"),
            ("callees_s5", 5, "callees", "proj_first"),
            ("both_s5", 5, "both", "proj_first"),
            ("both_s10", 10, "both", "proj_first"),
            # Different priority strategies
            ("both_s5_append", 5, "both", "append"),
            ("both_s5_mixed", 5, "both", "mixed"),
            ("both_s10_mixed", 10, "both", "mixed"),
        ]

        methods = {c[0]: {"proj": [], "exact_hit": 0, "exact_total": 0} for c in configs}
        methods["baseline"] = {"proj": [], "exact_hit": 0, "exact_total": 0}

        for cat, query in qlist:
            fts_raw, vec_rows = cached[f"{cat}|{query}"]
            stems = stems_from_queries([query])

            # Baseline
            bl = baseline(fts_raw, vec_rows, stems, 15)
            bp, be = metrics(bl, cat, query, pname)
            methods["baseline"]["proj"].append(bp / 15)
            if cat == "exact":
                methods["baseline"]["exact_total"] += 1
                if be: methods["baseline"]["exact_hit"] += 1

            # RRF
            rrf = rrf_top(fts_raw, vec_rows)
            rp, re_ = metrics(rrf, cat, query, pname)

            for label, n_seeds, direction, strategy in configs:
                if label == "no_expand":
                    methods[label]["proj"].append(rp / 15)
                    if cat == "exact":
                        methods[label]["exact_total"] += 1
                        if re_: methods[label]["exact_hit"] += 1
                    continue

                # Get seeds
                seeds = rrf[:n_seeds]
                seed_usrs = [s["usr"] for s in seeds if s.get("usr")]
                neighbors = set()
                if seed_usrs:
                    ph = ",".join("?" * len(seed_usrs))
                    if direction in ("callers", "both"):
                        for r in conn.execute(f"SELECT from_usr FROM refs WHERE to_usr IN ({ph}) AND ref_kind='call' AND from_usr IS NOT NULL AND from_usr!='' AND config_hash=?", seed_usrs + [ch]):
                            neighbors.add(r["from_usr"])
                    if direction in ("callees", "both"):
                        for r in conn.execute(f"SELECT to_usr FROM refs WHERE from_usr IN ({ph}) AND ref_kind='call' AND to_usr IS NOT NULL AND to_usr!='' AND config_hash=?", seed_usrs + [ch]):
                            neighbors.add(r["to_usr"])

                # Get project symbols for neighbors
                neighbor_rows: list[dict] = []
                if neighbors:
                    nlist = list(neighbors)
                    for batch_start in range(0, len(nlist), 100):
                        batch = nlist[batch_start:batch_start+100]
                        ph2 = ",".join("?" * len(batch))
                        for r in conn.execute(
                            f"SELECT * FROM symbols WHERE usr IN ({ph2}) AND config_hash=? AND is_definition=1",
                            batch + [ch],
                        ):
                            fp = r["file_path"] or ""
                            if is_project(fp, pname):
                                neighbor_rows.append(dict(r))

                existing_keys = {(r["name"], r.get("file_path")) for r in rrf}
                new_neighbors = [n for n in neighbor_rows if (n["name"], n.get("file_path")) not in existing_keys]

                # Apply expansion strategy
                if strategy == "proj_first":
                    # Sort: project first, then by original RRF position or new
                    expanded = list(rrf)
                    for n in new_neighbors:
                        expanded.append(n)
                    expanded.sort(key=lambda x: (
                        0 if is_project(x.get("file_path", ""), pname) else 1,
                        rrf.index(x) if x in rrf else 999
                    ))
                    result = expanded[:15]
                elif strategy == "append":
                    # Just append new neighbors at end
                    result = list(rrf) + [n for n in new_neighbors]
                    result = result[:15]
                elif strategy == "mixed":
                    # Alternate: keep original top-10, insert new project neighbors in positions 11-15
                    keep = rrf[:10]
                    new_proj = [n for n in new_neighbors if is_project(n.get("file_path",""), pname)]
                    fill = rrf[10:10+(15-10-len(new_proj))]
                    result = keep + new_proj + fill
                    result = result[:15]
                else:
                    result = rrf

                mp, me = metrics(result, cat, query, pname)
                methods[label]["proj"].append(mp / 15)
                if cat == "exact":
                    methods[label]["exact_total"] += 1
                    if me: methods[label]["exact_hit"] += 1

        conn.close()

        # Print results
        print(f"\n{'Method':<22} {'Proj%':>7} {'ExactHit':>9} {'Score':>7}")
        print("─" * 48)
        p_results = {}
        for label in methods:
            data = methods[label]
            n = len(data["proj"])
            if n == 0: continue
            avg_p = sum(data["proj"]) / n
            exact_p = data["exact_hit"] / data["exact_total"] if data["exact_total"] > 0 else 0
            comp = avg_p * 0.5 + exact_p * 0.5
            p_results[label] = comp
            marker = " ←BASE" if label == "baseline" else (" ←RRF" if label == "no_expand" else "")
            print(f"{label:<22} {avg_p:>6.1%} {exact_p:>8.1%} {comp:>6.3f}{marker}")

        all_cross[pname] = p_results

    # Cross-project
    if len(all_cross) >= 2:
        pnames = list(all_cross.keys())
        print(f"\n{'='*80}")
        print("CROSS-PROJECT")
        print(f"{'='*80}")
        print(f"\n{'Method':<22} {pnames[0]:>10} {pnames[1]:>10}  CrossAvg")
        print("─" * 52)

        all_labels = set()
        for p in all_cross.values():
            all_labels.update(p.keys())

        cross = {}
        for label in all_labels:
            v1 = all_cross[pnames[0]].get(label, 0)
            v2 = all_cross[pnames[1]].get(label, 0)
            c = (v1 + v2) / 2
            cross[label] = c

        for label, cavg in sorted(cross.items(), key=lambda x: -x[1]):
            v1 = all_cross[pnames[0]].get(label, 0)
            v2 = all_cross[pnames[1]].get(label, 0)
            m = ""
            if label == "baseline": m = " ←BASE"
            elif label == "no_expand": m = " ←RRF"
            print(f"{label:<22} {v1:>8.3f}  {v2:>8.3f}  {cavg:>8.3f}{m}")


if __name__ == "__main__":
    run()
