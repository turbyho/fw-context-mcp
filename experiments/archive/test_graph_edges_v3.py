#!/usr/bin/env python3
"""Graph edge experiment v3 — measure what each edge type can actually contribute.

For each query:
1. Run RRF to get top-15 candidates
2. Check how many project neighbors exist via each edge type (parent, file, call)
3. Check how many are already in top-15 vs how many are new
4. For new ones, add them and see if Proj% changes
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
        if is_project(fp, "zbox-ecb-fw"):  # generic enough
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
    return [dict(all_rows[key]) for key, _ in ranked[:limit]], dict(ranked)


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


def compute_metrics(top15, cat, query, pname):
    proj = sum(1 for r in top15 if is_project(r.get("file_path", ""), pname))
    exact = cat == "exact" and any(r["name"] == query for r in top15[:3])
    return proj, exact


def run():
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

        # ── Phase 1: retrieve ──
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

        # ── Phase 2: per-query edge analysis + expansion ──
        print(f"\n  Per-query edge analysis:")
        print(f"  {'Query':<30s} {'Cat':7s} {'BL':4s} {'RRF':4s} "
              f"{'Parent':>8s} {'File':>8s} {'Call':>8s} {'CallNew':>8s} {'+All':4s}")
        print(f"  {'─'*30} {'─'*7} {'─'*4} {'─'*4} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*4}")

        aggregate = {
            "baseline": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf_parent": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf_file": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf_call": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf_call_expand": {"proj": [], "exact_hit": 0, "exact_total": 0},
            "rrf_all_edges": {"proj": [], "exact_hit": 0, "exact_total": 0},
        }
        edge_stats = {"parent_reach": [], "parent_in": [], "file_reach": [], "file_in": [],
                       "call_reach": [], "call_in": [], "call_new": []}

        for cat, query in qlist:
            fts_raw, vec_rows = cached[f"{cat}|{query}"]
            stems = stems_from_queries([query])

            # Baseline + RRF
            bl = baseline(fts_raw, vec_rows, stems, limit=15)
            rrf, scores = rrf_top(fts_raw, vec_rows)

            bp, be = compute_metrics(bl, cat, query, pname)
            rp, re = compute_metrics(rrf, cat, query, pname)
            aggregate["baseline"]["proj"].append(bp / 15)
            aggregate["rrf"]["proj"].append(rp / 15)
            if cat == "exact":
                aggregate["baseline"]["exact_total"] += 1
                aggregate["rrf"]["exact_total"] += 1
                if be: aggregate["baseline"]["exact_hit"] += 1
                if re: aggregate["rrf"]["exact_hit"] += 1

            # Get edge info from top-5 RRF seeds
            seeds = rrf[:5]
            seed_usrs = [s["usr"] for s in seeds if s.get("usr")]
            parents, files, neighbors = set(), set(), set()
            n_callers = n_callees = 0
            if seed_usrs:
                ph = ",".join("?" * len(seed_usrs))
                # Parent + file
                for r in conn.execute(f"SELECT parent_usr, file_id FROM symbols WHERE usr IN ({ph}) AND config_hash=?", seed_usrs + [ch]):
                    if r["parent_usr"]:
                        parents.add(r["parent_usr"])
                    files.add(r["file_id"])
                # Call graph
                cr = conn.execute(f"SELECT from_usr FROM refs WHERE to_usr IN ({ph}) AND ref_kind='call' AND from_usr IS NOT NULL AND from_usr!='' AND config_hash=?", seed_usrs + [ch]).fetchall()
                neighbors.update(r["from_usr"] for r in cr)
                n_callers = len(cr)
                ce = conn.execute(f"SELECT to_usr FROM refs WHERE from_usr IN ({ph}) AND ref_kind='call' AND to_usr IS NOT NULL AND to_usr!='' AND config_hash=?", seed_usrs + [ch]).fetchall()
                neighbors.update(r["to_usr"] for r in ce)
                n_callees = len(ce)

            # Find project neighbors
            existing_usrs = {r["usr"] for r in rrf if r.get("usr")}
            neighbors_cache: dict[str, dict] = {}

            def _load_project_neighbors(usr_set, col_name):
                if not usr_set:
                    return []
                ulist = list(usr_set)
                result = []
                for batch_start in range(0, len(ulist), 100):
                    batch = ulist[batch_start:batch_start+100]
                    ph = ",".join("?" * len(batch))
                    rows = conn.execute(
                        f"SELECT usr, name, kind, file_path, qualified_name, is_definition FROM symbols WHERE {col_name} IN ({ph}) AND config_hash=?",
                        batch + [ch],
                    ).fetchall()
                    for r in rows:
                        fp = r["file_path"] or ""
                        if is_project(fp, pname) and r["is_definition"]:
                            neighbors_cache[r["usr"]] = dict(r)
                            result.append(r["usr"])
                return result

            parent_neighbors = _load_project_neighbors(parents, "parent_usr")
            file_neighbors = _load_project_neighbors(files, "file_id")

            call_neighbors_usr = neighbors & {r[0] for r in conn.execute(
                f"SELECT usr FROM symbols WHERE usr IN ({','.join('?'*len(neighbors))}) AND config_hash=?"
                if neighbors else "SELECT usr FROM symbols WHERE 0",
                list(neighbors) + [ch] if neighbors else [ch],
            ).fetchall()} if neighbors else set()

            # Count what's in RRF top-15 vs new
            parent_in = [u for u in parent_neighbors if u in existing_usrs]
            parent_new = [u for u in parent_neighbors if u not in existing_usrs]
            file_in = [u for u in file_neighbors if u in existing_usrs]
            file_new = [u for u in file_neighbors if u not in existing_usrs]
            call_in = [u for u in call_neighbors_usr if u in existing_usrs]
            call_new = [u for u in call_neighbors_usr if u not in existing_usrs]

            edge_stats["parent_reach"].append(len(parent_neighbors))
            edge_stats["parent_in"].append(len(parent_in))
            edge_stats["file_reach"].append(len(file_neighbors))
            edge_stats["file_in"].append(len(file_in))
            edge_stats["call_reach"].append(len(call_neighbors_usr))
            edge_stats["call_in"].append(len(call_in))
            edge_stats["call_new"].append(len(call_new))

            # ── Test: add NEW neighbors to results ──
            def _make_top(rrf_base, extra_usrs):
                result = list(rrf_base)
                added = 0
                for u in extra_usrs:
                    if u in neighbors_cache and len(result) < 15:
                        r = neighbors_cache[u]
                        if (r["name"], r.get("file_path")) not in {(x["name"], x.get("file_path")) for x in result}:
                            result.append(r)
                            added += 1
                return result[:15], added

            # Parent boost: add parent neighbors
            pt, pa = _make_top(rrf, parent_new)
            pp, pe = compute_metrics(pt, cat, query, pname)
            aggregate["rrf_parent"]["proj"].append(pp / 15)
            if cat == "exact":
                aggregate["rrf_parent"]["exact_total"] += 1
                if pe: aggregate["rrf_parent"]["exact_hit"] += 1

            # File boost
            ft, fa = _make_top(rrf, file_new)
            fp, fe = compute_metrics(ft, cat, query, pname)
            aggregate["rrf_file"]["proj"].append(fp / 15)
            if cat == "exact":
                aggregate["rrf_file"]["exact_total"] += 1
                if fe: aggregate["rrf_file"]["exact_hit"] += 1

            # Call boost
            ct, ca = _make_top(rrf, call_new)
            cp, ce2 = compute_metrics(ct, cat, query, pname)
            aggregate["rrf_call"]["proj"].append(cp / 15)
            if cat == "exact":
                aggregate["rrf_call"]["exact_total"] += 1
                if ce2: aggregate["rrf_call"]["exact_hit"] += 1

            # Call-graph expansion (add new call neighbors, also trim original non-project if needed)
            call_expanded = list(rrf)
            for u in call_new:
                if u in neighbors_cache:
                    r = neighbors_cache[u]
                    if (r["name"], r.get("file_path")) not in {(x["name"], x.get("file_path")) for x in call_expanded}:
                        call_expanded.append(r)
            # Trim to 15, preferring project
            call_expanded.sort(key=lambda x: (0 if is_project(x.get("file_path",""), pname) else 1, rrf.index(x) if x in rrf else 999))
            cet = call_expanded[:15]
            cep, cee = compute_metrics(cet, cat, query, pname)
            aggregate["rrf_call_expand"]["proj"].append(cep / 15)
            if cat == "exact":
                aggregate["rrf_call_expand"]["exact_total"] += 1
                if cee: aggregate["rrf_call_expand"]["exact_hit"] += 1

            # All edges combined
            all_new = set(parent_new + file_new + call_new)
            allt, _ = _make_top(rrf, all_new)
            ap, ae = compute_metrics(allt, cat, query, pname)
            aggregate["rrf_all_edges"]["proj"].append(ap / 15)
            if cat == "exact":
                aggregate["rrf_all_edges"]["exact_total"] += 1
                if ae: aggregate["rrf_all_edges"]["exact_hit"] += 1

            # Per-query summary
            print(f"  {query[:30]:30s} {cat:7s} {bp/15:3.0%} {rp/15:3.0%} "
                  f"{len(parent_new):>7d} {len(file_new):>7d} {len(call_new):>7d} {ca:>7d} {ap/15:3.0%}")

        conn.close()

        # Print aggregate
        exact_n = sum(1 for c, _ in qlist if c == "exact")
        print(f"\n{'Method':<20} {'Proj%':>7} {'ExactHit':>9} {'Score':>7}")
        print("─" * 46)
        for label in ["baseline", "rrf", "rrf_parent", "rrf_file", "rrf_call", "rrf_call_expand", "rrf_all_edges"]:
            data = aggregate[label]
            n = len(data["proj"])
            if n == 0: continue
            avg_p = sum(data["proj"]) / n
            exact_p = data["exact_hit"] / data["exact_total"] if data["exact_total"] > 0 else 0
            comp = avg_p * 0.5 + exact_p * 0.5
            marker = " ←BASE" if label == "baseline" else (" ←RRF" if label == "rrf" else "")
            print(f"{label:<20} {avg_p:>6.1%} {exact_p:>8.1%} {comp:>6.3f}{marker}")

        # Edge stats
        print(f"\n{'Edge type':<15} {'Avg reach':>10} {'Avg in top15':>14} {'Avg NEW':>10} {'Coverage':>10}")
        print("─" * 64)
        for etype in ["parent", "file", "call"]:
            rch = edge_stats[f"{etype}_reach"]
            itp = edge_stats[f"{etype}_in"]
            nw = edge_stats.get(f"{etype}_new", [])
            if rch:
                ar = sum(rch) / len(rch)
                ai = sum(itp) / len(itp)
                an = sum(nw) / len(nw) if nw else 0
                cov = ai / ar * 100 if ar > 0 else 0
                print(f"{etype:<15} {ar:>10.1f} {ai:>14.1f} {an:>9.1f} {cov:>9.1f}%")


if __name__ == "__main__":
    run()
