"""Test specific data issues in detail."""
import sqlite3

db_path = "/home/turbyho/.fw-context/index/452361ffbf84f774/index.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# 1. FTS5 exact name issue — investigate
print("=== FTS5 Exact Name Investigation ===")
for name in ["main", "connect", "send", "uart_init"]:
    real = conn.execute("SELECT name, kind, qualified_name, file_path FROM symbols WHERE name = ? LIMIT 5", (name,)).fetchall()
    print(f"\nSymbols with name='{name}': {len(real)} found in DB")
    for r in real:
        print(f"  [{r['kind']}] {r['qualified_name']} @ {r['file_path']}")

    # Try FTS5 with different approaches
    # Approach 1: name : exact
    r1 = conn.execute(
        "SELECT s.name, s.kind FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid WHERE symbols_fts MATCH ? LIMIT 5",
        (f"name : {name}",),
    ).fetchall()
    print(f"  FTS5 'name : {name}': {[(r['name'], r['kind']) for r in r1]}")

    # Approach 2: just the name
    r2 = conn.execute(
        "SELECT s.name, s.kind FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid WHERE symbols_fts MATCH ? LIMIT 5",
        (name,),
    ).fetchall()
    print(f"  FTS5 '{name}': {[(r['name'], r['kind']) for r in r2]}")

    # Approach 3: name_tokens : name
    r3 = conn.execute(
        "SELECT s.name, s.kind FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid WHERE symbols_fts MATCH ? LIMIT 5",
        (f"name_tokens : {name}",),
    ).fetchall()
    print(f"  FTS5 'name_tokens : {name}': {[(r['name'], r['kind']) for r in r3]}")

# 2. Anonymous struct token pollution
print(f"\n=== Anonymous Struct Token Pollution ===")
anon = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE name LIKE '(unnamed %'"
).fetchone()
print(f"Anonymous symbols: {anon['c']}")
sample = conn.execute(
    "SELECT name, qualified_name, name_tokens FROM symbols WHERE name LIKE '(unnamed %' LIMIT 5"
).fetchall()
for r in sample:
    print(f"  name: {r['name'][:60]}")
    print(f"  qname: {r['qualified_name'][:120]}")
    print(f"  tokens: {r['name_tokens'][:120]}")
    print()

# 3. Check FTS5 default tokenizer settings
print(f"=== FTS5 Tokenizer ===")
ftsinfo = conn.execute("SELECT fts5_decode(block) FROM symbols_fts_data LIMIT 1").fetchone()
print(f"  FTS5 data sample: {ftsinfo[0] if ftsinfo else 'N/A'}")

# 4. How many symbols have NO signature, NO docstring, NO LLM analysis
tot = conn.execute("SELECT COUNT(*) c FROM symbols WHERE is_definition = 1").fetchone()['c']
bare = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE is_definition = 1 AND signature = '' AND docstring = '' AND summary = ''"
).fetchone()['c']
print(f"\n  Bare definitions (no sig/doc/llm): {bare}/{tot} ({bare/tot*100:.1f}%)")

# By kind
bare_by_kind = conn.execute(
    """SELECT kind, COUNT(*) c
       FROM symbols WHERE is_definition = 1 AND signature = '' AND docstring = '' AND summary = ''
       GROUP BY kind ORDER BY c DESC"""
).fetchall()
print("  Bare by kind:")
for r in bare_by_kind:
    pct = r['c'] / tot * 100
    print(f"    {r['kind']:15s} {r['c']:>6,d} ({pct:4.1f}% of all defs)")

# 5. Check what text FTS5 searches for a specific concept query
print(f"\n=== FTS5 MATCH Debug ===")
query = "modem connect"
expanded = " OR ".join(f"{w}*" for w in query.split())
print(f"  Query: {query} → expanded: {expanded}")

rows = conn.execute(
    """SELECT s.name, s.kind, s.file_path,
              highlight(symbols_fts, 0, '<', '>') hl_name,
              highlight(symbols_fts, 1, '<', '>') hl_qname,
              highlight(symbols_fts, 2, '<', '>') hl_sig,
              highlight(symbols_fts, 3, '<', '>') hl_doc,
              highlight(symbols_fts, 4, '<', '>') hl_filepath,
              highlight(symbols_fts, 5, '<', '>') hl_tokens,
              highlight(symbols_fts, 6, '<', '>') hl_summary
       FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid
       WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 5""",
    (expanded,),
).fetchall()
for r in rows:
    proj = "PROJ" if r['file_path'] and r['file_path'].startswith('src/') else "SDK"
    print(f"  [{proj}] [{r['kind']}] {r['name']}")
    for i, col in enumerate(['name', 'qname', 'sig', 'doc', 'filepath', 'tokens', 'summary']):
        hl = r[f'hl_{col}']
        if hl and '<' in hl:
            print(f"    {col}: ...{hl[:120]}...")

# 6. FTS5 snippet() function analysis
print(f"\n=== FTS5 snippet() analysis ===")
rows = conn.execute(
    """SELECT s.name, s.kind,
              snippet(symbols_fts, 2, '<', '>', '...', 64) snip_sig,
              snippet(symbols_fts, 3, '<', '>', '...', 64) snip_doc
       FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid
       WHERE symbols_fts MATCH 'uart* OR send*' ORDER BY rank LIMIT 5""",
).fetchall()
for r in rows:
    print(f"  [{r['kind']}] {r['name']}")
    if r['snip_sig']: print(f"    sig: {r['snip_sig']}")
    if r['snip_doc']: print(f"    doc: {r['snip_doc']}")

conn.close()
