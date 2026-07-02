#!/usr/bin/env python3
"""E-A + E-B v2: Graph edge experiments — with proper scoring + diagnostics.

Key fix: call-graph neighbors are INSERTED (not appended), co-location uses
accumulative score based on actual edge count, not position weight.

Measures the YIELD of each edge type: how many new project symbols become
reachable through graph edges that weren't in the original top-15?
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

LIMIT = 15
N_SEEDS = 5


def is_project(path, pname):
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


def _rrf_base(fts5_rows, vec_rows, w_fts=1.8, w_vec=0.2, k=30, limit=LIMIT):
    """RRF with project×1.5 + func×1.2 — returns (results, scores_dict)."""
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
    top_scores = {key: sc for key, sc in ranked}
    top_rows = [dict(all_rows[key]) for key, _ in ranked[:limit]]
    return top_rows, top_scores


def baseline_merge(fts5_rows, vec_rows, stems, limit=LIMIT):
    """Production merge (same as weight_grid_search)."""
    seen: dict[tuple, dict] = {}
    all_rows_list = list(fts5_rows) + list(vec_rows)
    scored: list[tuple[int, dict]] = []
    for r in all_rows_list:
        name = r.get("name") or ""
        if name.startswith("(") or (len(name) <= 2 and r.get("kind") in ("variable", "field")):
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


def get_seed_info(conn, ch, results, n_seeds=N_SEEDS):
    """Extract edge info from top-N seeds."""
    seeds = results[:n_seeds]
    seed_usrs = [s["usr"] for s in seeds if s.get("usr")]
    if not seed_usrs:
        return set(), set(), set(), 0, 0

    ph = ",".join("?" * len(seed_usrs))
    extra = [ch]

    # parent_usr of seeds
    parent_rows = conn.execute(
        f"SELECT parent_usr, file_id FROM symbols WHERE usr IN ({ph}) AND config_hash=?", seed_usrs + extra
    ).fetchall()
    seed_parents = {r["parent_usr"] for r in parent_rows if r["parent_usr"]}
    seed_files = {r["file_id"] for r in parent_rows}

    # Call graph 1-hop neighbors
    callers = set(r["from_usr"] for r in conn.execute(
        f"SELECT from_usr FROM refs WHERE to_usr IN ({ph}) AND ref_kind='call' AND from_usr IS NOT NULL AND from_usr!='' AND config_hash=?",
        seed_usrs + extra
    ).fetchall())
    callees = set(r["to_usr"] for r in conn.execute(
        f"SELECT to_usr FROM refs WHERE from_usr IN ({ph}) AND ref_kind='call' AND to_usr IS NOT NULL AND to_usr!='' AND config_hash=?",
        seed_usrs + extra
    ).fetchall())

    return seed_parents, seed_files, callers | callees, len(callers), len(callees)


def get_project_neighbors(conn, ch, seed_parents, seed_files, seed_neighbors, pname):
    """Find all PROJECT symbols reachable through various edge types."""
    results: dict[str, list[str]] = {"parent": [], "file": [], "call": []}
    row_cache: dict[str, dict] = {}

    # Parent-based neighbors
    if seed_parents:
        ph = ",".join("?" * len(seed_parents))
        rows = conn.execute(
            f"""SELECT usr, name, kind, file_path, qualified_name, is_definition
                FROM symbols WHERE parent_usr IN ({ph}) AND config_hash=?""",
            list(seed_parents) + [ch],
        ).fetchall()
        for r in rows:
            fp = r["file_path"] or ""
            if is_project(fp, pname) and r["is_definition"]:
                row_cache[r["usr"]] = dict(r)
                results["parent"].append(r["usr"])

    # File-based neighbors
    if seed_files:
        ph = ",".join("?" * str(v) for v in seed_files)
        rows = conn.execute(
            f"""SELECT usr, name, kind, file_path, qualified_name, is_definition
                FROM symbols WHERE file_id IN ({','.join('?'*len(seed_files))}) AND config_hash=?""",
            list(seed_files) + [ch],
        ).fetchall()
        for r in rows:
            fp = r["file_path"] or ""
            if is_project(fp, pname) and r["is_definition"] and r["usr"] not in row_cache:
                row_cache[r["usr"]] = dict(r)
                results["file"].append(r["usr"])

    # Call-graph neighbors (filter for project)
    if seed_neighbors:
        ph = ",".join("?" * len(seed_neighbors))
        rows = conn.execute(
            f"""SELECT usr, name, kind, file_path, qualified_name, is_definition
                FROM symbols WHERE usr IN ({ph}) AND config_hash=?""",
            list(seed_neighbors) + [ch],
        ).fetchall()
        for r in rows:
            fp = r["file_path"] or ""
            if is_project(fp, pname) and r["is_definition"] and r["usr"] not in row_cache:
                row_cache[r["usr"]] = dict(r)
                results["call"].append(r["usr"])

    return results, row_cache


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

        print(f"  Retrieving {len(qlist)} queries...")
        cached: dict[str, tuple] = {}
        for cat, query in qlist:
            t0 = time.monotonic()
            qv = call_ollama_embed([query], cfg.llm)[0]
            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=30)]
                vr = search_similar_vec(conn, qv, ch, threshold=0.50, limit=30)
                sids = [r["symbol_id"] for r in vr]
                vec_rows = []
                if sids:
                    vec_rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({','.join('?'*len(sids))}) AND is_definition=1",
                        (ch, *sids),
                    ).fetchall()]
            elapsed = time.monotonic() - t0
            print(f"    [{cat}] {query[:45]:45s} FTS={len(fts_raw)} Vec={len(vec_rows)} ({elapsed:.0f}s)", flush=True)
            cached[f"{cat}|{query}"] = (fts_raw, vec_rows)

        # Aggregate metrics
        methods = {}
        edge_yields: dict[str, list[int]] = {
            "parent_reachable": [], "file_reachable": [], "call_reachable": [],
            "parent_in_top15": [], "file_in_top15": [], "call_in_top15": [],
        }

        for cat, query in qlist:
            fts_raw, vec_rows = cached[f"{cat}|{query}"]
            stems = stems_from_queries([query])

            # Baseline
            bl = baseline_merge(fts_raw, vec_rows, stems, LIMIT)
            bl_proj = sum(1 for r in bl if is_project(r.get("file_path", ""), pname))
            bl_exact = cat == "exact" and any(r["name"] == query for r in bl[:3])
            _acc("baseline", bl_proj / LIMIT, bl_exact, cat)

            # RRF optimal
            rrf, rrf_scores = _rrf_base(fts_raw, vec_rows)
            rrf_proj = sum(1 for r in rrf if is_project(r.get("file_path", ""), pname))
            rrf_exact = cat == "exact" and any(r["name"] == query for r in rrf[:3])
            _acc("rrf_optimal", rrf_proj / LIMIT, rrf_exact, cat)

            # Seed + edge analysis
            seed_parents, seed_files, seed_neighbors, n_callers, n_callees = get_seed_info(conn, ch, rrf)
            neigh, ncache = get_project_neighbors(conn, ch, seed_parents, seed_files, seed_neighbors, pname)

            existing_usrs = {r["usr"] for r in rrf if r.get("usr")}
            for etype in ["parent", "file", "call"]:
                reachable = neigh[etype]
                in_top = [u for u in reachable if u in existing_usrs]
                edge_yields[f"{etype}_reachable"].append(len(reachable))
                edge_yields[f"{etype}_in_top15"].append(len(in_top))

            # Test method: rrf + co-location scoring
            # Build a score for each result: RRF score + edge bonus
            # Edge bonus = count of edges shared with seeds × factor
            # Then re-sort
            bonus_configs = [
                ("rrf+parent", {"parent": 0.005, "file": 0, "call": 0}),
                ("rrf+file", {"parent": 0, "file": 0.003, "call": 0}),
                ("rrf+parent+file", {"parent": 0.005, "file": 0.003, "call": 0}),
                ("rrf+call", {"parent": 0, "file": 0, "call": 0.004}),
                ("rrf+parent+call", {"parent": 0.005, "file": 0, "call": 0.004}),
                ("rrf+file+call", {"parent": 0, "file": 0.003, "call": 0.004}),
                ("rrf+all_edges", {"parent": 0.005, "file": 0.003, "call": 0.004}),
            ]

            # Pre-compute edge counts for each candidate
            for blabel, bonuses in bonus_configs:
                # Build augmented scores
                aug_scores = dict(rrf_scores)
                for key, usr_dict in all_rows_cache.items():
                    if key not in aug_scores:
                        continue
                    usr = usr_dict.get("usr", "")
                    bonus = 0.0
                    if bonuses["parent"] and usr in parent_edge_count:
                        bonus += parent_edge_count[usr] * bonuses["parent"]
                    if bonuses["file"] and usr in file_edge_count:
                        bonus += file_edge_count[usr] * bonuses["file"]
                    if bonuses["call"] and usr in call_edge_count:
                        bonus += call_edge_count[usr] * bonuses["call"]
                    aug_scores[key] += bonus

                reranked = sorted(aug_scores.items(), key=lambda x: -x[1])
                top = [dict(all_rows_cache[key]) for key, _ in reranked[:LIMIT] if key in all_rows_cache]
                tproj = sum(1 for r in top if is_project(r.get("file_path", ""), pname))
                texact = cat == "exact" and any(r["name"] == query for r in top[:3])
                _acc(blabel, tproj / LIMIT, texact, cat)

            # Call-graph expansion: add new project neighbors with lower priority
            new_neighbors = [u for u in neigh["call"] if u not in existing_usrs]
            if new_neighbors:
                expanded = list(rrf)
                for u in new_neighbors:
                    if len(expanded) >= LIMIT:
                        break
                    if u in ncache:
                        expanded.append(ncache[u])
                exp_proj = sum(1 for r in expanded[:LIMIT] if is_project(r.get("file_path", ""), pname))
                exp_exact = cat == "exact" and any(r["name"] == query for r in expanded[:3])
                _acc("rrf+call_expand", exp_proj / LIMIT, exp_exact, cat)
            else:
                _acc("rrf+call_expand", rrf_proj / LIMIT, rrf_exact, cat)

        conn.close()

        # Print per-project
        exact_n = sum(1 for c, _ in qlist if c == "exact")
        print(f"\n{'Method':<25} {'Proj%':>7} {'ExactHit':>9} {'Score':>7}")
        print("─" * 52)
        for label in sorted(methods.keys()):
            data = methods[label]
            if data["count"] == 0:
                continue
            avg_proj = sum(data["proj"]) / data["count"]
            exact_pct = data["exact_hit"] / max(data["exact_total"], 1) if data["exact_total"] > 0 else 0
            composite = avg_proj * 0.5 + exact_pct * 0.5
            marker = " ←BASE" if label == "baseline" else (" ←RRF" if label == "rrf_optimal" else "")
            print(f"{label:<25} {avg_proj:>6.1%} {exact_pct:>8.1%} {composite:>6.3f}{marker}")

        # Edge yield summary
        print(f"\n{'Edge type':<15} {'Avg reachable':>14} {'Avg in top15':>14} {'Coverage%':>10}")
        print("─" * 57)
        for etype in ["parent", "file", "call"]:
            rch = edge_yields[f"{etype}_reachable"]
            intop = edge_yields[f"{etype}_in_top15"]
            if rch:
                avg_r = sum(rch) / len(rch) if rch else 0
                avg_i = sum(intop) / len(intop) if intop else 0
                cov = avg_i / avg_r * 100 if avg_r > 0 else 0
                print(f"{etype:<15} {avg_r:>14.1f} {avg_i:>14.1f} {cov:>9.1f}%")

        all_project_results[pname] = methods

    # Cross-project (simplified)
    if len(all_project_results) >= 2:
        pnames = list(all_project_results.keys())
        p1, p2 = pnames[0], pnames[1]
        cross = {}
        all_labels = set(list(all_project_results[p1].keys()) + list(all_project_results[p2].keys()))
        for label in all_labels:
            d1 = all_project_results[p1].get(label)
            d2 = all_project_results[p2].get(label)
            if d1 and d2 and d1["count"] > 0 and d2["count"] > 0:
                s1 = (sum(d1["proj"])/d1["count"])*0.5 + (d1["exact_hit"]/max(d1["exact_total"],1))*0.5
                s2 = (sum(d2["proj"])/d2["count"])*0.5 + (d2["exact_hit"]/max(d2["exact_total"],1))*0.5
                cross[label] = (s1 + s2) / 2
        print(f"\n{'Method':<25} {p1:>10} {p2:>10}  CrossAvg")
        print("─" * 52)
        for label, cavg in sorted(cross.items(), key=lambda x: -x[1]):
            d1 = all_project_results[p1].get(label, {"proj": [0], "exact_hit": 0, "exact_total": 0, "count": 1})
            d2 = all_project_results[p2].get(label, {"proj": [0], "exact_hit": 0, "exact_total": 0, "count": 1})
            s1 = (sum(d1["proj"])/d1["count"])*0.5 + (d1["exact_hit"]/max(d1["exact_total"],1))*0.5
            s2 = (sum(d2["proj"])/d2["count"])*0.5 + (d2["exact_hit"]/max(d2["exact_total"],1))*0.5
            marker = " ←BASE" if label == "baseline" else (" ←RRF" if label == "rrf_optimal" else "")
            print(f"{label:<25} {s1:>8.3f}  {s2:>8.3f}  {cavg:>8.3f}{marker}")


# Module-level accumulators
methods = {}
all_rows_cache = {}
parent_edge_count = {}
file_edge_count = {}
call_edge_count = {}
all_project_results = {}


def _acc(label, proj_pct, exact_hit, cat):
    methods.setdefault(label, {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
    methods[label]["proj"].append(proj_pct)
    methods[label]["count"] += 1
    if cat == "exact":
        methods[label]["exact_total"] += 1
        if exact_hit:
            methods[label]["exact_hit"] += 1


if __name__ == "__main__":
    run()
