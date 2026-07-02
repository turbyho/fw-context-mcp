"""FTS5 config and tokenization check."""
import sqlite3

conn = sqlite3.connect("/home/turbyho/.fw-context/index/452361ffbf84f774/index.db")
conn.row_factory = sqlite3.Row

# FTS5 config
info = conn.execute("SELECT * FROM symbols_fts_config").fetchall()
print("FTS5 config:")
for r in info:
    d = {k: r[k] for k in r.keys()}
    print(f"  {d}")

# FTS5 table info from sqlite_master
tables = conn.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'"
).fetchall()
print("\nFTS5 tables:")
for t in tables:
    print(f"  {t['name']}")
    if t['sql']:
        print(f"    SQL: {t['sql'][:200]}")

# Test column-weight FTS5 search
print("\n=== Column-weighted FTS5 ===")
# Test: how well does 'name : main*' work?
for q in ["main*", "connect*", "uart init*"]:
    r = conn.execute(
        "SELECT s.name, s.kind, s.file_path FROM symbols_fts "
        "JOIN symbols s ON s.id = symbols_fts.rowid "
        "WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 5",
        (q,),
    ).fetchall()
    proj = sum(1 for x in r if x['file_path'] and x['file_path'].startswith('src/'))
    print(f"  '{q}': top-5: {[(x['name'][:30], x['kind'][:8]) for x in r]}, proj={proj}")

# Check what FTS5 does with the expanded query format
expanded = " OR ".join(f"{w}*" for w in "modem connect".split())
print(f"\n  Expanded 'modem connect': '{expanded}'")
r = conn.execute(
    "SELECT s.name, s.kind, s.file_path, rank "
    "FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid "
    "WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 5",
    (expanded,),
).fetchall()
for x in r:
    print(f"    rank={x['rank']:.2f} [{x['kind']:12s}] {x['name'][:40]} @ {x['file_path']}")

print("\n=== Project definitions: embedded vs total ===")
# Get real counts of project definitions that COULD be embedded
for kind_filter in [
    "kind IN ('function','method','constructor','destructor','struct','class')",
    "kind IN ('function','method','constructor','destructor','struct','class','typedef','enum')",
]:
    proj_defs = conn.execute(
        f"""SELECT COUNT(*) c FROM symbols
           WHERE is_definition = 1
             AND (file_path LIKE 'src/%' OR file_path LIKE 'app/%' OR file_path LIKE 'lib/%')
             AND {kind_filter}"""
    ).fetchone()['c']
    proj_emb = conn.execute(
        f"""SELECT COUNT(*) c FROM symbols s
           JOIN embeddings e ON e.symbol_id = s.id
           WHERE s.is_definition = 1
             AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'app/%' OR s.file_path LIKE 'lib/%')
             AND {kind_filter}"""
    ).fetchone()['c']
    print(f"  {kind_filter[:60]}: {proj_emb}/{proj_defs} ({proj_emb/proj_defs*100:.0f}%)")

conn.close()
