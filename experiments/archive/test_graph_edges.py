#!/usr/bin/env python3
"""E-A + E-B: Graph edge experiments — co-location boosts + call-graph expansion.

Tests on both projects (zbox-ecb-fw + HA_Boiler) with the same 33 queries.
Iterates through: class co-membership, file co-location, 1-hop call proximity,
and call-graph expansion from top seeds.

Usage:
    python3 experiments/test_graph_edges.py
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

# ─── config ───────────────────────────────────────────────────────────

PROJECTS = {
    "zbox-ecb-fw": Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw"),
    "HA_Boiler": Path("/home/turbyho/dev/sw/work/privat/HA_Boiler"),
}

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

# Optimal RRF config from previous experiments
RRF_W_FTS, RRF_W_VEC = 1.8, 0.2
RRF_K = 30
RRF_LIMIT = 15

# ─── helpers ────────────────────────────────────────────────────────────

def is_project(path: str, project_name: str) -> bool:
    if project_name == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")


def rrf_fuse(fts5_rows, vec_rows, w_fts=1.8, w_vec=0.2, k=30, limit=15):
    """Reciprocal rank fusion — same as weight_grid_search.py."""
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}

    # Add project boost ×1.5, func boost ×1.2
    def _boost(r):
        fp = r.get("file_path", "")
        kind = r.get("kind", "")
        b = 1.0
        if fp.startswith("src/") or fp.startswith("lib/") or fp.startswith("app/"):
            b *= 1.5
        if kind in ("function", "method", "constructor", "destructor"):
            b *= 1.2
        return b

    for rank, r in enumerate(fts5_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + _boost(r) * w_fts / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)

    for rank, r in enumerate(vec_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + _boost(r) * w_vec / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [dict(all_rows[key]) for key, _ in ranked[:limit]]


def current_merge(fts5_rows, vec_rows, stems, limit=15):
    """Production merge — same as weight_grid_search.py."""
    from fw_context_mcp.search.scoring import score_result

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


# ─── E-A: Co-location boosts ────────────────────────────────────────────

def apply_co_location_boost(results, conn, ch, boost_parent=0.0, boost_file=0.0,
                             boost_call_1hop=0.0, pname="zbox-ecb-fw"):
    """Apply graph-edge-based boosts to RRF results, re-sorting by boosted score.

    boost_parent: multiplier for sharing parent_usr with top-5 result
    boost_file:   multiplier for sharing file_id with top-5 result
    boost_call_1hop: multiplier for being 1-hop caller/callee of top-5 result
    """
    if not results:
        return results

    # Collect seeds (top-5)
    top5 = results[:5]
    seed_usrs = [r["usr"] for r in top5 if r.get("usr")]

    seed_parents: set[str] = set()
    seed_files: set[int] = set()
    seed_neighbor_usrs: set[str] = set()

    if boost_parent and seed_usrs:
        placeholder = ",".join("?" * len(seed_usrs))
        rows = conn.execute(
            f"SELECT parent_usr FROM symbols WHERE usr IN ({placeholder}) AND parent_usr != '' AND config_hash=?",
            seed_usrs + [ch],
        ).fetchall()
        seed_parents = {r["parent_usr"] for r in rows}

    if boost_file and seed_usrs:
        placeholder = ",".join("?" * len(seed_usrs))
        rows = conn.execute(
            f"SELECT file_id FROM symbols WHERE usr IN ({placeholder}) AND config_hash=?",
            seed_usrs + [ch],
        ).fetchall()
        seed_files = {r["file_id"] for r in rows}

    if boost_call_1hop and seed_usrs:
        placeholder = ",".join("?" * len(seed_usrs))
        # Callers (who calls the seed)
        caller_rows = conn.execute(
            f"SELECT from_usr FROM refs WHERE to_usr IN ({placeholder}) AND ref_kind='call' AND from_usr IS NOT NULL AND from_usr != '' AND config_hash=?",
            seed_usrs + [ch],
        ).fetchall()
        seed_neighbor_usrs = {r["from_usr"] for r in caller_rows}
        # Callees (whom the seed calls)
        callee_rows = conn.execute(
            f"SELECT to_usr FROM refs WHERE from_usr IN ({placeholder}) AND ref_kind='call' AND to_usr IS NOT NULL AND to_usr != '' AND config_hash=?",
            seed_usrs + [ch],
        ).fetchall()
        seed_neighbor_usrs |= {r["to_usr"] for r in callee_rows}

    # Pre-fetch parent_usr and file_id for all results in batch
    all_usrs = [r.get("usr", "") for r in results if r.get("usr")]
    usr_info: dict[str, dict] = {}
    if all_usrs:
        placeholder = ",".join("?" * len(all_usrs))
        rows = conn.execute(
            f"SELECT usr, parent_usr, file_id FROM symbols WHERE usr IN ({placeholder}) AND config_hash=?",
            all_usrs + [ch],
        ).fetchall()
        usr_info = {r["usr"]: {"parent_usr": r["parent_usr"], "file_id": r["file_id"]} for r in rows}

    # Compute boosted scores: position-based weight × extra boost
    n = len(results)
    scored: list[tuple[float, dict]] = []
    for i, r in enumerate(results):
        usr = r.get("usr", "")
        base_score = n - i  # position: top gets highest
        extra = 1.0
        info = usr_info.get(usr, {})
        if boost_parent and info.get("parent_usr") and str(info["parent_usr"]) in seed_parents:
            extra *= boost_parent
        if boost_file and info.get("file_id") and info["file_id"] in seed_files:
            extra *= boost_file
        if boost_call_1hop and usr in seed_neighbor_usrs:
            extra *= boost_call_1hop
        scored.append((base_score * extra, r))

    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:RRF_LIMIT]]


# ─── E-B: Call-graph expansion ──────────────────────────────────────────

def call_graph_expand(results, conn, ch, n_seeds=5, direction="both", pname="zbox-ecb-fw"):
    """Expand results by adding 1-hop call graph neighbors of top seeds.

    direction: "callers", "callees", or "both"
    """
    seeds = results[:n_seeds]
    seed_usrs = [r["usr"] for r in seeds if r.get("usr")]
    if not seed_usrs:
        return results[:RRF_LIMIT]

    added_usrs: set[str] = set()
    added_rows: list[dict] = []

    # Find callers (who calls the seed)
    if direction in ("callers", "both"):
        caller_rows = conn.execute(
            f"""SELECT DISTINCT from_usr FROM refs
                WHERE to_usr IN ({','.join('?'*len(seed_usrs))})
                AND ref_kind = 'call' AND from_usr IS NOT NULL AND from_usr != ''
                AND config_hash = ?""",
            seed_usrs + [ch],
        ).fetchall()
        added_usrs.update(r["from_usr"] for r in caller_rows)

    # Find callees (whom the seed calls)
    if direction in ("callees", "both"):
        callee_rows = conn.execute(
            f"""SELECT DISTINCT to_usr FROM refs
                WHERE from_usr IN ({','.join('?'*len(seed_usrs))})
                AND ref_kind = 'call' AND to_usr IS NOT NULL AND to_usr != ''
                AND config_hash = ?""",
            seed_usrs + [ch],
        ).fetchall()
        added_usrs.update(r["to_usr"] for r in callee_rows)

    if added_usrs:
        added_usrs_list = list(added_usrs)
        added_rows = [dict(r) for r in conn.execute(
            f"""SELECT * FROM symbols
                WHERE config_hash = ? AND usr IN ({','.join('?'*len(added_usrs_list))})
                AND is_definition = 1""",
            (ch, *added_usrs_list),
        ).fetchall()]

    # Filter for project code + deduplicate
    existing_keys = {(r.get("name"), r.get("file_path")) for r in results}
    new = []
    for r in added_rows:
        fp = r.get("file_path", "")
        if not is_project(fp, pname):
            continue
        key = (r["name"], fp)
        if key not in existing_keys:
            existing_keys.add(key)
            new.append(r)

    # Combine: original results + new expanded symbols (appended at end)
    combined = list(results) + new
    return combined[:RRF_LIMIT]


# ─── main ────────────────────────────────────────────────────────────────

def run():
    all_results = {}

    for pname, ppath in PROJECTS.items():
        print(f"\n{'='*80}")
        print(f"PROJECT: {pname}")
        print(f"{'='*80}")

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
        print(f"  Retrieving {len(qlist)} queries...")

        # Phase 1: cache FTS5 + Vec results (expensive, done once)
        cached: dict[str, tuple] = {}
        conn = open_db(db_path)
        for cat, query in qlist:
            t0 = time.monotonic()
            qv = call_ollama_embed([query], cfg.llm)[0]

            with conn:
                fts_raw = [dict(r) for r in search_symbols(conn, query, ch, limit=30)]
                vr = search_similar_vec(conn, qv, ch, threshold=0.50, limit=30)
                sids = [r["symbol_id"] for r in vr]
                vec_rows = []
                if sids:
                    ph = ",".join("?" * len(sids))
                    vec_rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM symbols WHERE config_hash=? AND id IN ({ph}) AND is_definition=1",
                        (ch, *sids),
                    ).fetchall()]

            elapsed = time.monotonic() - t0
            print(f"    [{cat}] {query[:45]:45s} FTS={len(fts_raw)} Vec={len(vec_rows)} ({elapsed:.0f}s)", flush=True)
            cached[f"{cat}|{query}"] = (fts_raw, vec_rows)

        # Phase 2: test all methods
        print(f"\n  Testing methods...")
        methods = {}

        # Open a fresh connection for all boost/expansion queries
        conn = open_db(db_path)

        for cat, query in qlist:
            fts_raw, vec_rows = cached[f"{cat}|{query}"]
            from fw_context_mcp.search.scoring import stems_from_queries
            stems = stems_from_queries([query])

            # Baseline (production merge)
            cur = current_merge(fts_raw, vec_rows, stems, limit=RRF_LIMIT)
            cur_proj = sum(1 for r in cur if is_project(r.get("file_path", ""), pname))
            cur_exact = cat == "exact" and any(r["name"] == query for r in cur[:3])

            methods.setdefault("baseline", {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
            methods["baseline"]["proj"].append(cur_proj / RRF_LIMIT)
            methods["baseline"]["count"] += 1
            if cat == "exact":
                methods["baseline"]["exact_total"] += 1
                if cur_exact:
                    methods["baseline"]["exact_hit"] += 1

            # Optimal RRF (with project×1.5 + func×1.2, no extra graph boosts)
            rrf_base = rrf_fuse(fts_raw, vec_rows, w_fts=RRF_W_FTS, w_vec=RRF_W_VEC, k=RRF_K, limit=RRF_LIMIT)
            rrf_proj = sum(1 for r in rrf_base if is_project(r.get("file_path", ""), pname))
            rrf_exact = cat == "exact" and any(r["name"] == query for r in rrf_base[:3])

            methods.setdefault("rrf_optimal", {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
            methods["rrf_optimal"]["proj"].append(rrf_proj / RRF_LIMIT)
            methods["rrf_optimal"]["count"] += 1
            if cat == "exact":
                methods["rrf_optimal"]["exact_total"] += 1
                if rrf_exact:
                    methods["rrf_optimal"]["exact_hit"] += 1

            # ─── E-A: Co-location boosts ───

            boost_configs = [
                # (label, parent_boost, file_boost, call_1hop_boost)
                ("rrf+class×1.3", 1.3, 0.0, 0.0),
                ("rrf+file×1.2", 0.0, 1.2, 0.0),
                ("rrf+class1.3+file1.2", 1.3, 1.2, 0.0),
                ("rrf+call1hop×1.15", 0.0, 0.0, 1.15),
                ("rrf+class1.3+call1.15", 1.3, 0.0, 1.15),
                ("rrf+file1.2+call1.15", 0.0, 1.2, 1.15),
                ("rrf+class1.3+file1.2+call1.15", 1.3, 1.2, 1.15),
            ]

            for blabel, bp, bf, bc in boost_configs:
                bres = apply_co_location_boost(
                    rrf_base, conn, ch,
                    boost_parent=bp, boost_file=bf, boost_call_1hop=bc,
                    pname=pname,
                )
                bproj = sum(1 for r in bres if is_project(r.get("file_path", ""), pname))
                bexact = cat == "exact" and any(r["name"] == query for r in bres[:3])

                methods.setdefault(blabel, {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
                methods[blabel]["proj"].append(bproj / RRF_LIMIT)
                methods[blabel]["count"] += 1
                if cat == "exact":
                    methods[blabel]["exact_total"] += 1
                    if bexact:
                        methods[blabel]["exact_hit"] += 1

            # ─── E-B: Call-graph expansion ───

            expand_configs = [
                # (label, n_seeds, direction)
                ("rrf+expand_callees_s3", 3, "callees"),
                ("rrf+expand_callers_s3", 3, "callers"),
                ("rrf+expand_both_s3", 3, "both"),
                ("rrf+expand_callees_s5", 5, "callees"),
                ("rrf+expand_callers_s5", 5, "callers"),
                ("rrf+expand_both_s5", 5, "both"),
                ("rrf+expand_both_s10", 10, "both"),
            ]

            for elabel, n_seeds, direction in expand_configs:
                eres = call_graph_expand(rrf_base, conn, ch, n_seeds=n_seeds, direction=direction, pname=pname)
                eproj = sum(1 for r in eres if is_project(r.get("file_path", ""), pname))
                eexact = cat == "exact" and any(r["name"] == query for r in eres[:3])

                methods.setdefault(elabel, {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
                methods[elabel]["proj"].append(eproj / RRF_LIMIT)
                methods[elabel]["count"] += 1
                if cat == "exact":
                    methods[elabel]["exact_total"] += 1
                    if eexact:
                        methods[elabel]["exact_hit"] += 1

            # ─── E-A + E-B combined: boost THEN expand ───
            combined_rrf = apply_co_location_boost(
                rrf_base, conn, ch,
                boost_parent=1.3, boost_file=1.2, boost_call_1hop=1.15,
                pname=pname,
            )
            expanded = call_graph_expand(combined_rrf, conn, ch, n_seeds=5, direction="both", pname=pname)
            com_proj = sum(1 for r in expanded if is_project(r.get("file_path", ""), pname))
            com_exact = cat == "exact" and any(r["name"] == query for r in expanded[:3])

            methods.setdefault("rrf+all_boosts+expand_s5", {"proj": [], "exact_hit": 0, "exact_total": 0, "count": 0})
            methods["rrf+all_boosts+expand_s5"]["proj"].append(com_proj / RRF_LIMIT)
            methods["rrf+all_boosts+expand_s5"]["count"] += 1
            if cat == "exact":
                methods["rrf+all_boosts+expand_s5"]["exact_total"] += 1
                if com_exact:
                    methods["rrf+all_boosts+expand_s5"]["exact_hit"] += 1

        conn.close()

        # Print per-project results
        exact_n = sum(1 for c, _ in qlist if c == "exact")
        print(f"\n{'Method':<35} {'Proj%':>7} {'ExactHit':>9} {'Score':>8}")
        print("─" * 65)

        scored = []
        for label, data in methods.items():
            avg_proj = sum(data["proj"]) / max(data["count"], 1)
            exact_pct = data["exact_hit"] / max(data["exact_total"], 1) if data["exact_total"] > 0 else 0
            composite = avg_proj * 0.5 + exact_pct * 0.5

            scored.append((composite, label, avg_proj, exact_pct, data))
            marker = " ← BASE" if label == "baseline" else (" ← RRF" if label == "rrf_optimal" else "")
            print(f"{label:<35} {avg_proj:>6.1%} {exact_pct:>8.1%} {composite:>7.3f}{marker}")

        all_results[pname] = scored

    # Cross-project analysis
    if len(all_results) >= 2:
        print(f"\n{'='*80}")
        print("CROSS-PROJECT")
        print(f"{'='*80}")
        pnames = list(all_results.keys())
        p1, p2 = pnames[0], pnames[1]

        cross = {}
        all_labels = set()
        for _, lbl, _, _, _ in all_results[p1]:
            all_labels.add(lbl)
        for _, lbl, _, _, _ in all_results[p2]:
            all_labels.add(lbl)

        for label in all_labels:
            v1 = next((s for s, l_, _, _, _ in all_results[p1] if l_ == label), None)
            v2 = next((s for s, l_, _, _, _ in all_results[p2] if l_ == label), None)
            if v1 is not None and v2 is not None:
                cross[label] = (v1 + v2) / 2

        print(f"\n{'Method':<35} {p1:>10} {p2:>10}  CrossAvg")
        print("─" * 72)
        for label, cross_avg in sorted(cross.items(), key=lambda x: -x[1]):
            v1 = next((s for s, l_, _, _, _ in all_results[p1] if l_ == label), 0)
            v2 = next((s for s, l_, _, _, _ in all_results[p2] if l_ == label), 0)
            marker = ""
            if label == "baseline":
                marker = " ← BASE"
            elif label == "rrf_optimal":
                marker = " ← RRF_OPT"
            print(f"{label:<35} {v1:>8.3f}  {v2:>8.3f}  {cross_avg:>8.3f}{marker}")

        # Best
        best_p1 = max(all_results[p1], key=lambda x: x[0])
        best_p2 = max(all_results[p2], key=lambda x: x[0])
        best_cross = max(cross.items(), key=lambda x: x[1])
        print(f"\nBest per project: {p1}={best_p1[1]} ({best_p1[0]:.3f}), {p2}={best_p2[1]} ({best_p2[0]:.3f})")
        print(f"Best cross-project: {best_cross[0]} ({best_cross[1]:.3f})")


if __name__ == "__main__":
    run()
