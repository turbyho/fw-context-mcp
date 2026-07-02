"""Broader DB analysis: sizes, query plans, performance, architecture gaps."""
import sqlite3
import time
from pathlib import Path
import json

db_path = Path("/home/turbyho/.fw-context/index/452361ffbf84f774/index.db")
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

# ===== 1. File sizes =====
print("=" * 60)
print("1. FILE SIZES")
print("=" * 60)
for suffix in ["", "-wal", "-shm"]:
    p = Path(str(db_path) + suffix)
    if p.exists():
        size_mb = p.stat().st_size / (1024 * 1024)
        print(f"  {p.name:25s} {size_mb:8.2f} MB")

# ===== 2. Table sizes =====
print("\n" + "=" * 60)
print("2. TABLE SIZES (rows + pages)")
print("=" * 60)
tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()
for t in tables:
    tname = t[0]
    try:
        rc = conn.execute(f"SELECT COUNT(*) c FROM [{tname}]").fetchone()
        count = rc['c'] if rc else 0
    except Exception:
        count = None
    try:
        pc = conn.execute(
            "SELECT pageno FROM dbstat WHERE name = ?", (tname,)
        ).fetchall()
        pages = len(pc) if pc else 0
    except Exception:
        pages = 0

    if count is not None and count > 0:
        print(f"  {tname:35s} {count:>10,} rows  {pages:>6} pages")

# ===== 3. Query plans for common operations =====
print("\n" + "=" * 60)
print("3. EXPLAIN QUERY PLAN — common operations")
print("=" * 60)

queries = {
    "FTS5 concept search": """
        EXPLAIN QUERY PLAN
        SELECT s.id FROM symbols_fts
        JOIN symbols s ON s.id = symbols_fts.rowid
        WHERE symbols_fts MATCH 'modem* OR connect*'
        ORDER BY rank LIMIT 20
    """,
    "FTS5 + kind filter": """
        EXPLAIN QUERY PLAN
        SELECT s.id FROM symbols_fts
        JOIN symbols s ON s.id = symbols_fts.rowid
        WHERE symbols_fts MATCH 'modem* OR connect*' AND s.kind = 'function'
        ORDER BY rank LIMIT 20
    """,
    "FTS5 + project filter (LIKE)": """
        EXPLAIN QUERY PLAN
        SELECT s.id FROM symbols_fts
        JOIN symbols s ON s.id = symbols_fts.rowid
        WHERE symbols_fts MATCH 'modem* OR connect*'
          AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'app/%')
        ORDER BY rank LIMIT 20
    """,
    "Name lookup (exact)": """
        EXPLAIN QUERY PLAN
        SELECT * FROM symbols WHERE name = 'uart_init' AND is_definition = 1
    """,
    "Qualified name lookup": """
        EXPLAIN QUERY PLAN
        SELECT * FROM symbols WHERE qualified_name LIKE 'zbox::%'
    """,
    "Callers (refs join)": """
        EXPLAIN QUERY PLAN
        SELECT r.* FROM refs r
        JOIN symbols s ON s.usr = r.to_usr
        WHERE s.name = 'uart_init' AND r.config_hash = s.config_hash
        LIMIT 50
    """,
    "Vec KNN search": """
        EXPLAIN QUERY PLAN
        SELECT symbol_id, distance FROM vec_symbols
        WHERE embedding MATCH ? AND config_hash = ? AND k = 30
        ORDER BY distance
    """,
}

for label, q in queries.items():
    try:
        rows = conn.execute(q).fetchall()
        plans = " | ".join(f"{r['detail']}" for r in rows)
        print(f"  {label}:")
        print(f"    {plans}")
    except Exception as e:
        print(f"  {label}: ERROR - {e}")

# ===== 4. Index usage analysis =====
print("\n" + "=" * 60)
print("4. INDEX STATISTICS")
print("=" * 60)
indexes = conn.execute(
    "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name"
).fetchall()
for idx in indexes:
    try:
        stmt = conn.execute(
            "SELECT pageno FROM dbstat WHERE name = ?", (idx['name'],)
        ).fetchall()
        pages = len(stmt)
        if pages > 0:
            print(f"  {idx['tbl_name']:20s}.{idx['name']:35s} {pages:>6} pages")
    except Exception:
        pass

# ===== 5. Data density analysis =====
print("\n" + "=" * 60)
print("5. DATA DENSITY (null/empty rate per column)")
print("=" * 60)
cols = [
    ("signature", "signature = '' OR signature IS NULL"),
    ("docstring", "docstring = '' OR docstring IS NULL"),
    ("name_tokens", "name_tokens = ''"),
    ("summary", "summary = ''"),
    ("inputs", "inputs = ''"),
    ("outputs", "outputs = ''"),
    ("parent_usr", "parent_usr = ''"),
    ("template_usr", "template_usr = ''"),
]
total = conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()['c']
for col_name, condition in cols:
    cnt = conn.execute(
        f"SELECT COUNT(*) c FROM symbols WHERE {condition}"
    ).fetchone()['c']
    pct = cnt / total * 100
    bar = "█" * int(pct // 5)
    print(f"  {col_name:20s} empty: {cnt:>6,}/{total:,} ({pct:5.1f}%) {bar}")

# ===== 6. Query timing =====
print("\n" + "=" * 60)
print("6. QUERY TIMING (average of 3 runs)")
print("=" * 60)
timing_queries = {
    "FTS5 concept search (modem connect)": (
        """SELECT s.id FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH 'modem* OR connect*'
           ORDER BY rank LIMIT 20""",
    ),
    "Name exact lookup (uart_init)": (
        "SELECT * FROM symbols WHERE name = 'uart_init' AND is_definition = 1",
    ),
    "Callers query (complex join)": (
        """SELECT r.*, s.name as target_name
           FROM refs r JOIN symbols s ON s.usr = r.to_usr
           WHERE r.config_hash = s.config_hash AND r.to_usr IN (
               SELECT usr FROM symbols WHERE name = 'uart_init'
           ) LIMIT 50""",
    ),
    "File map query": (
        """SELECT s.kind, s.name, s.line, s.is_definition
           FROM symbols s
           WHERE s.file_path LIKE '%main.cpp'
           ORDER BY s.line LIMIT 100""",
    ),
    "Vector search (mock — no vec0)": (
        "SELECT COUNT(*) FROM symbols WHERE is_definition = 1 AND kind IN ('function', 'method', 'class', 'struct')",
    ),
    "Hotspots query": (
        """SELECT s.name, s.kind, s.file_path, COUNT(r.id) as caller_count
           FROM refs r JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.ref_kind = 'call'
           GROUP BY r.to_usr ORDER BY caller_count DESC LIMIT 20""",
    ),
}

for label, (query,) in timing_queries.items():
    times = []
    for _ in range(3):
        start = time.perf_counter()
        conn.execute(query).fetchall()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    avg = sum(times) / len(times) * 1000  # ms
    print(f"  {label:45s} {avg:7.2f} ms")

# ===== 7. FTS5 index size detail =====
print("\n" + "=" * 60)
print("7. FTS5 INDEX DETAIL")
print("=" * 60)
for tbl in ["symbols_fts_data", "symbols_fts_idx", "symbols_fts_docsize", "symbols_fts_config"]:
    try:
        cnt = conn.execute(f"SELECT COUNT(*) c FROM [{tbl}]").fetchone()['c']
        pc = conn.execute(f"SELECT pageno FROM dbstat WHERE name = ?", (tbl,)).fetchall()
        pages = len(pc)
        print(f"  {tbl:35s} {cnt:>10,} entries  {pages:>6} pages")
    except Exception as e:
        print(f"  {tbl:35s} ERROR: {e}")

# ===== 8. Schema for other code-search systems (comparison) =====
print("\n" + "=" * 60)
print("8. CURRENT SCHEMA — FEATURE GAPS")
print("=" * 60)

# Check what's missing
has_file_summary = False
try:
    c = conn.execute("SELECT COUNT(*) c FROM file_analysis").fetchone()['c']
    has_file_summary = c > 0
except Exception:
    pass

has_overrides = False
try:
    c = conn.execute("SELECT COUNT(*) c FROM overrides").fetchone()['c']
    has_overrides = c > 0
except Exception:
    pass

has_inheritance = False
try:
    c = conn.execute("SELECT COUNT(*) c FROM inheritance").fetchone()['c']
    has_inheritance = c > 0
except Exception:
    pass

has_indirect_calls = False
try:
    c = conn.execute("SELECT COUNT(*) c FROM indirect_call_sites").fetchone()['c']
    has_indirect_calls = c > 0
except Exception:
    pass

has_fp_assignments = False
try:
    c = conn.execute("SELECT COUNT(*) c FROM fp_assignments").fetchone()['c']
    has_fp_assignments = c > 0
except Exception:
    pass

print(f"  file_analysis (file-level summaries):   {has_file_summary}")
print(f"  overrides (virtual method overrides):     {has_overrides}")
print(f"  inheritance (C++ class hierarchy):        {has_inheritance}")
print(f"  indirect_call_sites (fp call sites):      {has_indirect_calls}")
print(f"  fp_assignments (func ptr assignments):    {has_fp_assignments}")

# Missing features analysis
print(f"\n  MISSING from current schema:")
missing = [
    "No type-usage index (which symbols use which types?)",
    "No symbol co-occurrence tracking (what symbols are often used together?)",
    "No pre-computed symbol similarity (beyond embeddings)",
    "No macro definitions table",
    "No include-graph edges (which files include which?)",
    "No pre-computed project-symbol flag (per-query LIKE filter)",
    "No file-level FTS5 index (!) — file_analysis exists but not in FTS5",
    "No concept/driver tagging (e.g. 'this is a UART driver', 'this is BLE')",
    "No dead-code pre-computation (computed on-demand)",
    "No symbol importance/centrality score",
]
for m in missing:
    print(f"    - {m}")

conn.close()
