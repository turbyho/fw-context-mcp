"""Deep dive into data quality — search-relevant aspects."""
import sqlite3
from pathlib import Path

dbs = {
    "zbox-ecb-fw": "/home/turbyho/.fw-context/index/452361ffbf84f774/index.db",
    "HA_Boiler": "/home/turbyho/.fw-context/index/39cef596a54c8de9/index.db",
}

for name, db_path in dbs.items():
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()['c']

    # 1. Variable pollution: how many variables in search results for common queries?
    print(f"\n--- Variable Pollution ---")
    for query in ["modem init", "ble connect", "uart send", "sensor read"]:
        expanded = " OR ".join(f"{w}*" for w in query.split())
        r = conn.execute("""SELECT s.id FROM symbols_fts
                          JOIN symbols s ON s.id = symbols_fts.rowid
                          WHERE symbols_fts MATCH ? LIMIT 20""", (expanded,)).fetchall()
        ids = [x[0] for x in r]
        if ids:
            placeholders = ",".join("?" * len(ids))
            counts = conn.execute(
                f"SELECT kind, COUNT(*) c FROM symbols WHERE id IN ({placeholders}) GROUP BY kind",
                ids,
            ).fetchall()
            vars_count = sum(x['c'] for x in counts if x['kind'] == 'variable')
            total_hits = len(ids)
            print(f"  '{query}': top-20: {vars_count} variables ({vars_count/total_hits*100:.0f}%) "
                  f"kinds: {[(x['kind'][:8],x['c']) for x in counts]}")

    # 2. Embedding coverage by kind
    print(f"\n--- Embedding Coverage by Kind ---")
    rows = conn.execute(
        """SELECT s.kind,
                  COUNT(DISTINCT s.id) total,
                  SUM(CASE WHEN s.is_definition=1 THEN 1 ELSE 0 END) defs,
                  COUNT(DISTINCT e.symbol_id) embedded,
                  COUNT(DISTINCT CASE WHEN s.docstring != '' THEN s.id END) docd
           FROM symbols s
           LEFT JOIN embeddings e ON e.symbol_id = s.id
           WHERE s.is_definition = 1
           GROUP BY s.kind ORDER BY total DESC"""
    ).fetchall()
    for r in rows:
        emb_pct = r['embedded'] / r['total'] * 100 if r['total'] > 0 else 0
        doc_pct = r['docd'] / r['total'] * 100 if r['total'] > 0 else 0
        print(f"  {r['kind']:15s} defs={r['defs']:>6,d} emb={r['embedded']:>5,d} ({emb_pct:4.0f}%) "
              f"docstring={r['docd']:>5,d} ({doc_pct:4.0f}%)")

    # 3. Name tokenization quality: check common patterns
    print(f"\n--- Name Tokenization Examples ---")
    rows = conn.execute(
        """SELECT name, qualified_name, name_tokens
           FROM symbols WHERE is_definition = 1 AND LENGTH(name) > 10
           ORDER BY RANDOM() LIMIT 10"""
    ).fetchall()
    for r in rows:
        print(f"  {r['name']:35s} → [{r['name_tokens']}]")

    # 4. % of symbols with useful text (docstring OR signature OR LLM analysis)
    rr = conn.execute(
        """SELECT COUNT(*) c FROM symbols
           WHERE is_definition = 1 AND (
               docstring != '' OR signature != '' OR summary != ''
               OR kind IN ('class', 'struct', 'enum', 'function', 'method', 'constructor', 'destructor')
           )"""
    ).fetchone()
    print(f"\n  Symbols with useful text: {rr['c']:,} / {total:,} ({rr['c']/total*100:.1f}%)")

    # 5. FTS5 search quality: test exact symbol names
    print(f"\n--- FTS5 Exact Symbol Recovery ---")
    test_names = [
        ("uart_init", "function"),
        ("main", "function"),
        ("setup", "function"),
        ("loop", "function"),
        ("connect", "method"),
        ("send", "method"),
    ]
    for tname, tkind in test_names:
        r = conn.execute(
            """SELECT s.name, s.kind, s.file_path, s.is_definition
               FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? LIMIT 3""",
            (f"name : {tname}",),
        ).fetchall()
        real = conn.execute(
            "SELECT name, kind, file_path FROM symbols WHERE name = ? AND kind = ? LIMIT 3",
            (tname, tkind),
        ).fetchall()
        if real:
            fts_found = len([x for x in r if x['name'] == tname])
            print(f"  [{tkind}] {tname}: FTS found {fts_found}/{len(real)} real symbols"
                  f"  (real total in DB: {len(real)})")

    # 6. Embedding description quality
    print(f"\n--- Embedding Description Samples ---")
    rows = conn.execute(
        """SELECT s.name, s.kind, s.file_path, s.signature, s.docstring, s.summary, s.qualified_name
           FROM symbols s JOIN embeddings e ON e.symbol_id = s.id
           WHERE s.is_definition = 1
           ORDER BY RANDOM() LIMIT 5"""
    ).fetchall()
    for r in rows:
        fp = (r['file_path'] or '').replace('\\', '/')
        path = file_ = ""
        if '/' in fp:
            *dirs, file_ = fp.split('/')
            path = '/'.join(dirs[-2:])
        elif fp:
            file_ = fp
        desc_parts = [path, file_]
        qname = r['qualified_name'] or ''
        if qname and '::' in qname:
            desc_parts.append(':'.join(qname.split('::')[:-1]))
        desc_parts.append(r['name'])
        if r['signature']:
            desc_parts.append(r['signature'])
        if r['docstring']:
            doc = r['docstring'].strip()[:150]
            desc_parts.append(doc)
        if r['summary']:
            desc_parts.append(r['summary'][:200])
        desc = ' : '.join(p for p in desc_parts if p)
        print(f"  [{r['kind']}] {r['name']}")
        print(f"    desc: {desc[:200]}")
        print()

    # 7. Check how many project-symbol definitions are NOT embedded
    proj_def_ids = conn.execute(
        """SELECT id FROM symbols
           WHERE is_definition = 1 AND (file_path LIKE 'src/%' OR file_path LIKE 'app/%' OR file_path LIKE 'lib/%')"""
    ).fetchall()
    proj_def_ids = [r[0] for r in proj_def_ids]
    if proj_def_ids:
        placeholders = ','.join('?' * len(proj_def_ids))
        emb_proj = conn.execute(
            f"SELECT COUNT(*) c FROM embeddings WHERE symbol_id IN ({placeholders})",
            proj_def_ids,
        ).fetchone()['c']
        print(f"\n  Project definitions: {len(proj_def_ids)}, embedded: {emb_proj} ({emb_proj/len(proj_def_ids)*100:.1f}%)")

    # 8. Check FTS5 column weights / BM25 quality
    print(f"\n--- FTS5 Query Testing ---")
    # Test: search for a specific concept
    queries = [
        "modem connect parse",
        "bluetooth advertising scan",
        "uart send receive",
    ]
    for q in queries:
        expanded = " OR ".join(f"{w}*" for w in q.split())
        rows = conn.execute(
            """SELECT s.name, s.kind, s.file_path, s.is_definition, rank
               FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 5""",
            (expanded,),
        ).fetchall()
        proj = sum(1 for r in rows if r['file_path'] and r['file_path'].startswith('src/'))
        kinds = [r['kind'] for r in rows]
        print(f"  '{q}': top-5 kinds: {kinds}, proj: {proj}/5")

    conn.close()
