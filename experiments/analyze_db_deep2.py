"""Deep dive: query plan analysis, hot path analysis, storage optimizations."""
import sqlite3
import time

db_path = "/home/turbyho/.fw-context/index/452361ffbf84f774/index.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=" * 60)
print("1. PRAGMA settings analysis")
print("=" * 60)
pragmas = [
    "page_size", "page_count", "cache_size", "mmap_size",
    "journal_mode", "synchronous", "foreign_keys",
    "busy_timeout", "cache_spill", "threads",
    "data_version", "application_id",
]
for p in pragmas:
    try:
        r = conn.execute(f"PRAGMA {p}").fetchone()
        print(f"  {p:20s} = {r[0]}")
    except Exception as e:
        print(f"  {p:20s} = ERROR: {e}")

print("\n" + "=" * 60)
print("2. Index effectiveness — which indexes are used?")
print("=" * 60)
# Check which indexes are actually being used by checking idx_stats or sqlite_stat1
try:
    stats = conn.execute(
        "SELECT * FROM sqlite_stat1 ORDER BY tbl, idx"
    ).fetchall()
    for s in stats:
        print(f"  {s['tbl']:25s} {s['idx']:40s} {s['stat']}")
except Exception as e:
    print(f"  sqlite_stat1 not available: {e}")

# Analyze tables to populate sqlite_stat1
try:
    conn.execute("ANALYZE")
    print("\n  (ANALYZE run to populate statistics)")
    stats = conn.execute(
        "SELECT * FROM sqlite_stat1 ORDER BY tbl, idx"
    ).fetchall()
    for s in stats[:10]:
        print(f"  {s['tbl']:25s} {s['idx']:40s} {s['stat']}")
except Exception as e:
    print(f"  ANALYZE error: {e}")

print("\n" + "=" * 60)
print("3. Qualified name prefix search — deep dive")
print("=" * 60)
# Test: how does idx_symbols_qname work with prefix LIKE?
queries = [
    ("exact qname", "SELECT COUNT(*) FROM symbols WHERE qualified_name = 'zbox::ZMODEM::connect'"),
    ("prefix LIKE", "SELECT COUNT(*) FROM symbols WHERE qualified_name LIKE 'zbox::%'"),
    ("prefix substr", "SELECT COUNT(*) FROM symbols WHERE SUBSTR(qualified_name, 1, 5) = 'zbox:'"),
]

for label, q in queries:
    plan = conn.execute(f"EXPLAIN QUERY PLAN {q}").fetchall()
    plan_text = " | ".join(str(r['detail']) for r in plan)
    start = time.perf_counter()
    result = conn.execute(q).fetchone()
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {label}:")
    print(f"    plan:  {plan_text}")
    print(f"    time:  {elapsed:.2f} ms, rows: {result[0]}")

# Test: how would an index on SUBSTR or a separate column help?
print(f"\n  Alternative: prefix column test")
# Count symbols with 'zbox::' prefix
cnt = conn.execute(
    "SELECT COUNT(*) FROM symbols WHERE qualified_name GLOB 'zbox::*'"
).fetchone()[0]
print(f"    GLOB 'zbox::*' matches: {cnt}")

print("\n" + "=" * 60)
print("4. Hot path analysis — ranking queries")
print("=" * 60)
# Test the full search pipeline: FTS5 → JOIN → filter → sort → limit
# This is what search_code does
test_queries = [
    ("simple (no filter)",
        """SELECT s.* FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH 'modem* OR connect* OR init*'
           ORDER BY rank LIMIT 20"""),
    ("with kind filter",
        """SELECT s.* FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH 'modem* OR connect* OR init*' AND s.kind = 'function'
           ORDER BY rank LIMIT 20"""),
    ("with project filter",
        """SELECT s.* FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH 'modem* OR connect* OR init*'
             AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%')
           ORDER BY rank LIMIT 20"""),
    ("with both filters",
        """SELECT s.* FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH 'modem* OR connect* OR init*'
             AND s.kind = 'function'
             AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%')
           ORDER BY rank LIMIT 20"""),
]

for label, q in test_queries:
    times = []
    rows = []
    for _ in range(5):
        start = time.perf_counter()
        r = conn.execute(q).fetchall()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
        rows.append(len(r))
    avg_t = sum(times) / len(times)
    avg_r = sum(rows) / len(rows)
    plan = conn.execute(f"EXPLAIN QUERY PLAN {q}").fetchall()
    plan_text = " | ".join(str(r['detail']) for r in plan)
    print(f"  {label}:")
    print(f"    plan:  {plan_text}")
    print(f"    time:  {avg_t:6.2f} ms (avg), rows: {avg_r:.0f}")

# Test: would pre-filtering by a materialized project column help?
print(f"\n  With pre-computed is_project column (simulated):")
q = """SELECT s.* FROM symbols_fts
       JOIN symbols s ON s.id = symbols_fts.rowid
       WHERE symbols_fts MATCH 'modem* OR connect* OR init*'
         AND s.is_definition = 1
       ORDER BY rank LIMIT 20"""
times2 = []
for _ in range(5):
    start = time.perf_counter()
    r = conn.execute(q).fetchall()
    times2.append((time.perf_counter() - start) * 1000)
print(f"    time:  {sum(times2)/len(times2):6.2f} ms (avg — is_definition filter)")
# The point: is_definition uses B-tree index on the rowid, fast.
# is_project would work the same way.

print("\n" + "=" * 60)
print("5. Storage optimization — BLOB vs vec0 duplication")
print("=" * 60)
# embeddings table vs vec_symbols table sizes
emb_count = conn.execute("SELECT COUNT(*) c FROM embeddings").fetchone()['c']
try:
    vec_count = conn.execute("SELECT COUNT(*) c FROM vec_symbols").fetchone()['c']
except Exception:
    vec_count = 0
print(f"  embeddings (BLOB): {emb_count} rows")
print(f"  vec_symbols (vec0): {vec_count} rows")
print(f"  Duplication: {emb_count == vec_count}")

# BLOB size estimate
avg_blob = conn.execute("SELECT AVG(LENGTH(embedding)) FROM embeddings").fetchone()[0]
print(f"  Avg BLOB size: {avg_blob:.0f} bytes")
print(f"  BLOB total: {emb_count * avg_blob / 1024 / 1024:.1f} MB")
print(f"  Note: vec0 stores same vectors → ~2x storage overhead")

print("\n" + "=" * 60)
print("6. Missing critical index: fts5 + file_path join")
print("=" * 60)
# Test: does the file_path column in symbols benefit from an index?
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT s.name FROM symbols s "
    "WHERE s.file_path = 'src/main.cpp' AND s.is_definition = 1"
).fetchall()
for p in plan:
    print(f"  plan: {p['detail']}")
# Time the query
start = time.perf_counter()
r = conn.execute(
    "SELECT COUNT(*) FROM symbols WHERE file_path = 'src/main.cpp'"
).fetchone()
print(f"  time: {(time.perf_counter() - start)*1000:.2f} ms, rows: {r[0]}")
print(f"  Note: No index on file_path → full table scan for file_path queries!")
print(f"  Impact: get_file_map() does this for every file_map query")

print("\n" + "=" * 60)
print("7. Unused/potentially removable indexes")
print("=" * 60)
# idx_symbols_parent and idx_symbols_template — are they used?
for idx_name in ["idx_symbols_parent", "idx_symbols_template", "idx_symbols_qname"]:
    # Check if index exists and how many pages
    pc = conn.execute(
        "SELECT pageno FROM dbstat WHERE name = ?", (idx_name,)
    ).fetchall()
    pages = len(pc)
    print(f"  {idx_name:35s} {pages:>6} pages")
    print(f"    Used in: get_class_members, get_template_instances")
    if idx_name == "idx_symbols_qname":
        print(f"    WARNING: LIKE prefix query scans table despite this index!")

print("\n" + "=" * 60)
print("8. Hotspot query deep dive")
print("=" * 60)
plan = conn.execute("""
    EXPLAIN QUERY PLAN
    SELECT s.name, s.kind, s.file_path, COUNT(r.id) as caller_count
    FROM refs r JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
    WHERE r.config_hash = (SELECT config_hash FROM build_configs LIMIT 1)
    GROUP BY r.to_usr ORDER BY caller_count DESC LIMIT 20
""").fetchall()
for p in plan:
    print(f"  plan: {p['detail']}")

print("\n" + "=" * 60)
print("9. WAL and checkpoint analysis")
print("=" * 60)
wal_path = db_path + "-wal"
shm_path = db_path + "-shm"
import os
for p in [wal_path, shm_path]:
    if os.path.exists(p):
        size = os.path.getsize(p) / 1024
        print(f"  {os.path.basename(p)}: {size:.0f} KB")
    else:
        print(f"  {os.path.basename(p)}: not found")

# Check WAL frame count
try:
    fc = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"  wal_checkpoint result: {dict(fc)}")
except Exception as e:
    print(f"  wal_checkpoint: {e}")

conn.close()
