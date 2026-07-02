#!/usr/bin/env python3
"""Check what data exists in existing indexes — which features need re-index."""
from __future__ import annotations

import sqlite3
from pathlib import Path

INDEX_ROOT = Path.home() / ".fw-context" / "index"

REAL_PROJECTS = {
    "zbox-ecb-fw": "452361ffbf84f774",
    "HA_Boiler": "39cef596a54c8de9",
}

for name, pid in REAL_PROJECTS.items():
    db_path = INDEX_ROOT / pid / "index.db"
    if not db_path.exists():
        print(f"{name}: no index")
        continue
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    ver = conn.execute("PRAGMA user_version").fetchone()[0]

    # Symbol counts
    total_syms = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    def_syms = conn.execute("SELECT COUNT(*) FROM symbols WHERE is_definition=1").fetchone()[0]

    # typedef/enum analysis coverage
    typedef_defs = conn.execute("SELECT COUNT(*) FROM symbols WHERE kind='typedef' AND is_definition=1").fetchone()[0]
    typedef_analyzed = conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN llm_analysis a ON a.symbol_id=s.id WHERE s.kind='typedef'"
    ).fetchone()[0]
    enum_defs = conn.execute("SELECT COUNT(*) FROM symbols WHERE kind='enum' AND is_definition=1").fetchone()[0]
    enum_analyzed = conn.execute(
        "SELECT COUNT(*) FROM symbols s JOIN llm_analysis a ON a.symbol_id=s.id WHERE s.kind='enum'"
    ).fetchone()[0]

    # Total LLM analysis
    total_analyzed = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]

    # PageRank
    pagerank_nonzero = conn.execute("SELECT COUNT(*) FROM symbols WHERE pagerank > 0").fetchone()[0] if "pagerank" in cols else 0

    # Hotspot cache
    hotspot_count = conn.execute("SELECT COUNT(*) FROM hotspot_cache").fetchone()[0] if "hotspot_cache" in tables else 0

    # Ref count
    ref_count = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]

    print(f"\n{'='*60}")
    print(f"Project: {name}")
    print(f"  schema_version: {ver}")
    print(f"  total symbols: {total_syms}, definitions: {def_syms}")
    print(f"  references: {ref_count}")
    print(f"  pagerank column: {'yes' if 'pagerank' in cols else 'NO — needs re-index'}")
    print(f"  pagerank computed: {pagerank_nonzero} nodes")
    print(f"  hotspot_cache: {'yes' if 'hotspot_cache' in tables else 'NO — needs re-index'}")
    print(f"  hotspot_cache entries: {hotspot_count}")
    print(f"  llm_analysis total: {total_analyzed}")
    print(f"  typedef analyzed: {typedef_analyzed}/{typedef_defs}")
    print(f"  enum analyzed: {enum_analyzed}/{enum_defs}")

    conn.close()

print("\n---")
print("SUMMARY:")
print("  FTS5 weights comparison: NO re-index needed (code change only)")
print("  PageRank boost: NEEDS re-index (pagerank column not populated)")
print("  Hotspot cache: NEEDS re-index (table doesn't exist)")
print("  LLM typedef/enum: NEEDS re-index with --analyze")
