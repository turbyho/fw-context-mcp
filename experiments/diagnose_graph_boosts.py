#!/usr/bin/env python3
"""Diagnose why graph edge boosts have no effect — check seeds, boost targets."""
from __future__ import annotations
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed

def is_project(path, pname):
    if pname == "HA_Boiler":
        return path.startswith("src/")
    return path.startswith("src/") or path.startswith("lib/")

def rrf_fuse(fts5_rows, vec_rows, w_fts=1.8, w_vec=0.2, k=30, limit=15):
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}
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

ppath = Path("/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw")
cfg = load_config(project_root=ppath)
pid = derive_project_id(ppath)
db_path = cfg.index.db_dir / pid / "index.db"
conn = open_db(db_path)
bc = get_active_config(conn, pid)
ch = bc["config_hash"]

queries = [
    ("concept", "modem send data packet"),
    ("concept", "BLE connection setup advertising"),
    ("exact", "get_key"),
    ("mixed", "init setup start"),
]

for cat, query in queries:
    print(f"\n{'='*60}")
    print(f"  [{cat}] {query}")
    print(f"{'='*60}")

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

    rrf = rrf_fuse(fts_raw, vec_rows)
    print(f"RRF top-15: {sum(1 for r in rrf if is_project(r.get('file_path',''),'zbox-ecb-fw'))} project")
    for i, r in enumerate(rrf[:10]):
        fp = r.get("file_path", "")
        tag = "PROJ" if is_project(fp, 'zbox-ecb-fw') else "SDK "
        print(f"  {i+1:2d}. [{tag}] [{r.get('kind',''):12s}] {r.get('name',''):40s} @ {fp[:50]}")

    # Seeds analysis
    seeds = rrf[:5]
    print(f"\nSeeds (top-5): {sum(1 for s in seeds if is_project(s.get('file_path',''), 'zbox-ecb-fw'))} project")
    seed_usrs = [s["usr"] for s in seeds if s.get("usr")]
    print(f"  Seed USRs: {len(seed_usrs)}")

    if seed_usrs:
        # Class co-membership check
        ph = ",".join("?" * len(seed_usrs))
        parent_rows = conn.execute(
            f"SELECT usr, parent_usr, file_id, file_path FROM symbols WHERE usr IN ({ph}) AND config_hash=?",
            seed_usrs + [ch],
        ).fetchall()
        seed_parents = {r["parent_usr"] for r in parent_rows if r["parent_usr"]}
        seed_files = {r["file_id"] for r in parent_rows}
        print(f"  Seed parent_usrs: {len(seed_parents)} unique")
        print(f"  Seed file_ids: {len(seed_files)} unique")

        # How many OTHER symbols share these parents/files?
        if seed_parents:
            ph2 = ",".join("?" * len(seed_parents))
            co_class = conn.execute(
                f"""SELECT COUNT(*) c FROM symbols
                    WHERE parent_usr IN ({ph2}) AND config_hash=?
                    AND file_path LIKE 'src/%'""",
                list(seed_parents) + [ch],
            ).fetchone()[0]
            print(f"  Project symbols sharing seed parent_usr: {co_class}")

        if seed_files:
            ph2 = ",".join("?" * len(seed_files))
            co_file = conn.execute(
                f"""SELECT COUNT(*) c FROM symbols
                    WHERE file_id IN ({ph2}) AND config_hash=?
                    AND file_path LIKE 'src/%'""",
                list(seed_files) + [ch],
            ).fetchone()[0]
            print(f"  Project symbols sharing seed file_id: {co_file}")

        # Call graph 1-hop: project neighbors?
        ph = ",".join("?" * len(seed_usrs))
        callers = conn.execute(
            f"""SELECT COUNT(DISTINCT from_usr) c FROM refs
                WHERE to_usr IN ({ph}) AND ref_kind='call' AND from_usr IS NOT NULL AND from_usr!='' AND config_hash=?""",
            seed_usrs + [ch],
        ).fetchone()[0]
        callees = conn.execute(
            f"""SELECT COUNT(DISTINCT to_usr) c FROM refs
                WHERE from_usr IN ({ph}) AND ref_kind='call' AND to_usr IS NOT NULL AND to_usr!='' AND config_hash=?""",
            seed_usrs + [ch],
        ).fetchone()[0]
        print(f"  1-hop callers: {callers}, callees: {callees}")

        # Project callers/callees
        proj_callers = conn.execute(
            f"""SELECT COUNT(DISTINCT r.from_usr) c FROM refs r
                JOIN symbols s ON s.usr = r.from_usr AND s.config_hash = r.config_hash
                WHERE r.to_usr IN ({ph}) AND r.ref_kind='call'
                AND r.from_usr IS NOT NULL AND r.from_usr!='' AND r.config_hash=?
                AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%')""",
            seed_usrs + [ch],
        ).fetchone()[0]
        proj_callees = conn.execute(
            f"""SELECT COUNT(DISTINCT r.to_usr) c FROM refs r
                JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
                WHERE r.from_usr IN ({ph}) AND r.ref_kind='call'
                AND r.to_usr IS NOT NULL AND r.to_usr!='' AND r.config_hash=?
                AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%')""",
            seed_usrs + [ch],
        ).fetchone()[0]
        print(f"  PROJECT 1-hop callers: {proj_callers}, callees: {proj_callees}")
        print(f"  Ratio: project edges / total = {(proj_callers+proj_callees)/(callers+callees)*100:.1f}%"
              if (callers+callees) > 0 else "  Ratio: N/A")

conn.close()
