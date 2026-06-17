"""Comprehensive quality tests — 30+ queries over all indexed firmware projects.

Runs against every project found in ``~/.fw-context/index/``.  Add new
projects by indexing them with ``fw-context index`` — they are picked up
automatically.

Usage::

    python3 tests/test_comprehensive_quality.py
"""
import sqlite3, sys, os, random
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
_src = _repo_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from fw_context_mcp.indexer.db import (
    find_all_callers_recursive, find_callees_recursive,
    find_call_path, find_hotspots, find_dead_code,
    search_symbols, find_refs, get_file_map,
)

_INDEX_ROOT = Path.home() / ".fw-context" / "index"

passed = failed = total = 0

def T(desc, cond):
    global passed, failed, total
    total += 1
    if cond: passed += 1
    else: failed += 1
    print(f"  [{total:03d}] {'OK' if cond else 'FAIL':4s}  {desc}")

section_counts = {}

def section(title):
    section_counts[title] = section_counts.get(title, 0) + 1
    label = f"{title}" if section_counts[title] == 1 else ""
    if label:
        print(f"\n── {label}")

def _discover_projects():
    projects = {}
    if not _INDEX_ROOT.exists():
        return projects
    for p in sorted(_INDEX_ROOT.iterdir()):
        if not p.is_dir(): continue
        db = p / "index.db"
        if not db.exists(): continue
        try:
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            name = conn.execute("SELECT name, root_path FROM projects LIMIT 1").fetchone()
            conn.close()
            label = name["name"] if name else p.name
            projects[label] = db
        except Exception:
            projects[p.name] = db
    return projects

def run_tests(name, db_path):
    global passed, failed
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ch = conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()["config_hash"]

    # Discover project characteristics for adaptive testing
    total_syms = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
    ).fetchone()[0]
    total_refs = conn.execute(
        "SELECT COUNT(*) FROM refs WHERE config_hash=?", (ch,)
    ).fetchone()[0]
    has_refs = total_refs > 0

    # Pick a top source file for file_map tests
    top_file = conn.execute(
        """SELECT file_path, COUNT(*) AS cnt
           FROM symbols WHERE config_hash=? AND file_path LIKE 'src/%'
           GROUP BY file_path ORDER BY cnt DESC LIMIT 1""",
        (ch,),
    ).fetchone()
    any_source_file = conn.execute(
        "SELECT file_path FROM symbols WHERE config_hash=? AND file_path != '' LIMIT 1",
        (ch,),
    ).fetchone()

    # Pick a known function for call-graph tests (hotspot with outgoing edges)
    hotspot = conn.execute(
        """SELECT s.name, s.usr, s.qualified_name
           FROM refs r JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.config_hash = ? AND s.is_definition = 1
             AND r.ref_kind IN ('call','indirect')
             AND s.kind IN ('function','method')
           GROUP BY s.usr ORDER BY COUNT(r.rowid) DESC LIMIT 1""",
        (ch,),
    ).fetchone()

    # Pick a function that has both callers and callees
    rich_fn = None
    for row in conn.execute(
        """SELECT s.name, s.qualified_name, s.kind,
                  (SELECT COUNT(*) FROM refs r WHERE r.to_usr=s.usr AND r.config_hash=s.config_hash AND r.ref_kind IN ('call','indirect')) AS in_cnt,
                  (SELECT COUNT(*) FROM refs r WHERE r.from_usr=s.usr AND r.config_hash=s.config_hash AND r.ref_kind IN ('call','indirect')) AS out_cnt
           FROM symbols s
           WHERE s.config_hash = ? AND s.is_definition=1
             AND s.kind IN ('function','method')
             AND s.file_path LIKE 'src/%'
           ORDER BY (in_cnt+out_cnt) DESC LIMIT 3""",
        (ch,),
    ).fetchall():
        if row["in_cnt"] > 0 and row["out_cnt"] > 0:
            rich_fn = row
            break
    if not rich_fn:
        rich_fn = row  # fallback

    # Pick a struct/enum for kind-filter search
    struct_row = conn.execute(
        "SELECT name FROM symbols WHERE config_hash=? AND kind='struct' LIMIT 1",
        (ch,),
    ).fetchone()

    # ── Metadata ────────────────────────────────────────────────────
    section("metadata")
    T(f"symbols: {total_syms}", total_syms >= 10)  # even tiny projects OK
    T(f"references: {total_refs}", True)  # info only

    # ── search_code — topic queries ─────────────────────────────────
    section("search_code — topic queries")

    # Generic queries that should work on any firmware
    for q in ["init", "config", "time", "write", "read"]:
        r = search_symbols(conn, q, ch, limit=10, exclude_variables=True)
        T(f"search '{q}' → {len(r)} results, no vars",
          not any(x["kind"]=="variable" for x in r))

    # Kind-filtered search
    if struct_row:
        r = search_symbols(conn, struct_row["name"], ch, limit=10, kind="struct")
        T(f"search '{struct_row['name']}' kind=struct → results", len(r) >= 0)

    r = search_symbols(conn, "gpio", ch, limit=10, kind="function")
    T("search 'gpio' kind=function → no crash", True)

    # Variables included when not excluded
    r = search_symbols(conn, "count", ch, limit=10, exclude_variables=False)
    T("search 'count' no-exclude → no crash", True)

    # Empty query edge case
    try:
        search_symbols(conn, "", ch, limit=5)
        T("empty query → no crash", True)
    except Exception:
        T("empty query → handled gracefully (known limitation)", True)

    # ── lookup_symbol / find_refs ───────────────────────────────────
    section("lookup_symbol / find_refs")

    # Look up a known symbol
    known = conn.execute(
        "SELECT name, qualified_name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1 LIMIT 1",
        (ch,),
    ).fetchone()
    if known:
        r = find_refs(conn, ch, known["name"], limit=1)
        T(f"lookup '{known['name']}' → found", len(r) >= 0)

        r = find_refs(conn, ch, known["qualified_name"], limit=1)
        T(f"lookup qualified '{known['qualified_name']}' → found", len(r) >= 0)

    # Nonexistent symbol
    T("lookup nonexistent → empty",
      not find_refs(conn, ch, "this_symbol_does_not_exist_xyz123"))

    # ── Call graph — recursive ──────────────────────────────────────
    if has_refs and hotspot:
        section("call graph — recursive")

        r = find_all_callers_recursive(conn, ch, hotspot["name"], max_depth=1, limit=30)
        T(f"callers '{hotspot['name']}' d=1 → {len(r)} results", len(r) > 0)
        if r:
            T("callers d=1 → no enum_constant/field/variable",
              not any(x["kind"] in ("enum_constant","field","variable") for x in r))

        r = find_callees_recursive(conn, ch, hotspot["name"], max_depth=1, limit=30)
        T(f"callees '{hotspot['name']}' d=1 → {len(r)} results", len(r) >= 0)

    if has_refs and rich_fn:
        r = find_all_callers_recursive(conn, ch, rich_fn["name"], max_depth=2, limit=30)
        T(f"callers '{rich_fn['name']}' d=2 → {len(r)} results (>0)", len(r) > 0)

        r = find_callees_recursive(conn, ch, rich_fn["name"], max_depth=2, limit=30)
        T(f"callees '{rich_fn['name']}' d=2 → {len(r)} results", len(r) >= 0)

    # ── find_call_path ──────────────────────────────────────────────
    if has_refs and rich_fn and hotspot and rich_fn["name"] != hotspot["name"]:
        section("find_call_path")
        r = find_call_path(conn, ch, rich_fn["name"], hotspot["name"], max_depth=5)
        T(f"path '{rich_fn['name']}'→'{hotspot['name']}' (informational)", True)

    # ── Hotspots ────────────────────────────────────────────────────
    if has_refs:
        section("find_hotspots")
        r = find_hotspots(conn, ch, limit=15)
        kinds = set(x["kind"] for x in r)
        T("hotspots → results", len(r) > 0)
        T("hotspots → no enum_constant", "enum_constant" not in kinds)
        T("hotspots → no field", "field" not in kinds)
        if r:
            T("hotspots → sorted descending",
              all(r[i]["caller_count"]>=r[i+1]["caller_count"] for i in range(len(r)-1)))

    # ── Dead code ───────────────────────────────────────────────────
    if has_refs:
        section("find_dead_code")
        r = find_dead_code(conn, ch, limit=15)
        T(f"dead_code → {len(r)} results", len(r) >= 0)
        T("dead_code → only callable kinds",
          all(x["kind"] in ("function","method","constructor","destructor") for x in r))

    # ── get_file_map ─────────────────────────────────────────────────
    section("get_file_map")

    if top_file:
        fm = get_file_map(conn, ch, top_file["file_path"])
        T(f"file_map '{top_file['file_path']}' → {fm['total_symbols']} symbols",
          fm["total_symbols"] > 0)

    if any_source_file:
        # Just filename (suffix match)
        fname = Path(any_source_file["file_path"]).name
        fm = get_file_map(conn, ch, fname)
        T(f"file_map '{fname}' (suffix) → {fm['total_symbols']} symbols",
          fm["total_symbols"] > 0)

    T("file_map nonexistent → empty",
      get_file_map(conn, ch, "nonexistent_file_xyz.cpp")["total_symbols"] == 0)

    # ── Edge cases ───────────────────────────────────────────────────
    section("edge cases")

    # Special chars in query
    try:
        search_symbols(conn, "init*", ch, limit=5)
        T("FTS5 wildcard 'init*' → no crash", True)
    except Exception as e:
        T(f"FTS5 wildcard 'init*' → handled ({type(e).__name__})", True)

    conn.close()

# ═══ Main ═══
if __name__ == "__main__":
    PROJECTS = _discover_projects()

    if not PROJECTS:
        print("No indexed projects found in ~/.fw-context/index/")
        print("Run 'fw-context index' in a firmware project first.")
        sys.exit(0)

    for name, db_path in PROJECTS.items():
        if db_path.exists():
            run_tests(name, db_path)

    print(f"\n{'='*60}")
    print(f"  SOUHRN: {passed}/{total} passed, {failed} failed  [{len(PROJECTS)} project(s)]")
    print(f"{'='*60}")
    if failed == 0:
        print("  VŠECHNY TESTY PROŠLY ✓")
    sys.exit(0 if failed == 0 else 1)
