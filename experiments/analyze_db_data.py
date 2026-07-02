"""Analyze DB data quality across both indexed projects."""
import sqlite3
from pathlib import Path

dbs = {
    "zbox-ecb-fw": Path("/home/turbyho/.fw-context/index/452361ffbf84f774/index.db"),
    "HA_Boiler": Path("/home/turbyho/.fw-context/index/39cef596a54c8de9/index.db"),
}

for name, db_path in dbs.items():
    if not db_path.exists():
        print(f"  {name}: DB not found at {db_path}")
        continue
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Overall stats
    r = conn.execute("SELECT COUNT(*) c, SUM(is_definition) defs FROM symbols").fetchone()
    total = r['c']
    defcount = r['defs']
    print(f"  Total symbols: {total:,}  Definitions: {defcount:,}  ({defcount/total*100:.1f}%)")

    # Kind distribution
    kinds = conn.execute(
        "SELECT kind, COUNT(*) c, SUM(is_definition) defs FROM symbols GROUP BY kind ORDER BY c DESC"
    ).fetchall()
    print(f"\n  Kind distribution:")
    for k in kinds:
        print(f"    {k['kind']:20s} {k['c']:>6,} (defs: {k['defs']:>6,})")

    # Docstring coverage
    r = conn.execute("SELECT COUNT(*) c FROM symbols WHERE docstring != ''").fetchone()
    r2 = conn.execute(
        "SELECT COUNT(*) c FROM symbols WHERE docstring != '' AND is_definition = 1"
    ).fetchone()
    print(
        f"\n  Docstrings: {r['c']:,}/{total:,} ({r['c']/total*100:.1f}%)  "
        f"defs only: {r2['c']:,}/{defcount:,} ({r2['c']/defcount*100:.1f}%)"
    )

    # LLM analysis coverage
    r = conn.execute("SELECT COUNT(*) c FROM symbols WHERE summary != ''").fetchone()
    print(f"  LLM analysis: {r['c']:,}/{total:,} ({r['c']/total*100:.1f}%)")

    # Embedding coverage
    r = conn.execute("SELECT COUNT(*) c FROM embeddings").fetchone()
    print(f"  Embeddings: {r['c']:,}/{defcount:,} defs ({r['c']/defcount*100:.1f}% of defs)")

    # File count
    r = conn.execute("SELECT COUNT(*) c FROM files").fetchone()
    print(f"  Files: {r['c']:,}")

    # Avg docstring length
    r = conn.execute(
        "SELECT AVG(LENGTH(docstring)) avg, MAX(LENGTH(docstring)) mx FROM symbols WHERE docstring != ''"
    ).fetchone()
    print(f"  Docstring avg: {r['avg']:.0f} chars, max: {r['mx']}")

    # Avg signature length
    r = conn.execute(
        "SELECT AVG(LENGTH(signature)) avg FROM symbols WHERE signature != ''"
    ).fetchone()
    print(f"  Signature avg: {r['avg']:.0f} chars")

    # Check name_tokens fill rate
    r = conn.execute("SELECT COUNT(*) c FROM symbols WHERE name_tokens = ''").fetchone()
    print(f"  Empty name_tokens: {r['c']:,}")

    # Sample rows
    print(f"\n  Sample rows (defs, with docstring):")
    rows = conn.execute(
        """SELECT name, qualified_name, kind, file_path, LENGTH(signature) siglen,
                  LENGTH(docstring) doclen, LENGTH(summary) sumlen,
                  LENGTH(name_tokens) toklen, is_definition
           FROM symbols WHERE is_definition = 1 AND docstring != ''
           ORDER BY RANDOM() LIMIT 5"""
    ).fetchall()
    for rw in rows:
        print(
            f"    [{rw['kind']:12s}] {rw['name']:30s} sig={rw['siglen']:>4,d} "
            f"doc={rw['doclen']:>4,d} sum={rw['sumlen']:>4,d} tokens={rw['toklen']:>4,d}"
        )
        try:
            if rw['toklen']:
                print(f"      tokens: {rw['name_tokens'][:80]}")
        except Exception:
            pass

    # Top files by symbol count
    print(f"\n  Top files by symbol count (project code):")
    rows = conn.execute(
        """SELECT file_path, COUNT(*) c, SUM(is_definition) defs
           FROM symbols WHERE file_path LIKE 'src/%' OR file_path LIKE 'app/%'
           GROUP BY file_path ORDER BY c DESC LIMIT 10"""
    ).fetchall()
    for rw in rows:
        print(f"    {rw['file_path']:60s} {rw['c']:>5,d} ({rw['defs']:>4,d} defs)")

    # FTS5 check
    try:
        r = conn.execute("SELECT COUNT(*) c FROM symbols_fts").fetchone()
        print(f"\n  FTS5 rows: {r['c']:,}")
    except sqlite3.OperationalError:
        print(f"\n  FTS5: table not found or error")

    # Vec table check
    try:
        r = conn.execute("SELECT COUNT(*) c FROM vec_symbols").fetchone()
        print(f"  Vec rows: {r['c']:,}")
    except sqlite3.OperationalError:
        print(f"  Vec: table not found or error")

    # Project vs SDK symbols
    proj = conn.execute(
        "SELECT COUNT(*) c FROM symbols WHERE file_path LIKE 'src/%' OR file_path LIKE 'app/%'"
    ).fetchone()
    print(f"\n  Project-code symbols (src/ or app/): {proj['c']:,} ({proj['c']/total*100:.1f}%)")

    # Token size analysis
    r = conn.execute(
        "SELECT AVG(LENGTH(name_tokens)) avg, MAX(LENGTH(name_tokens)) mx FROM symbols WHERE name_tokens != ''"
    ).fetchone()
    print(f"  name_tokens avg length: {r['avg']:.0f} chars, max: {r['mx']}")

    # Only definitions with both docstring and LLM analysis
    r = conn.execute(
        """SELECT COUNT(*) c FROM symbols
           WHERE is_definition = 1 AND docstring != '' AND summary != ''"""
    ).fetchone()
    print(f"  Definitions with docstring AND LLM: {r['c']:,}")

    # Only definitions with embeddings
    r = conn.execute(
        """SELECT COUNT(*) c FROM symbols s
           INNER JOIN embeddings e ON e.symbol_id = s.id"""
    ).fetchone()
    print(f"  Definitions with embedding: {r['c']:,}")

    # Check for empty/null qualified_name
    r = conn.execute(
        "SELECT COUNT(*) c FROM symbols WHERE qualified_name = '' OR qualified_name IS NULL"
    ).fetchone()
    print(f"  Empty qualified_name: {r['c']:,}")

    conn.close()
