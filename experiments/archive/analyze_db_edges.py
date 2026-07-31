"""Map all graph edges and structural relationships in the DB."""
import sqlite3
from pathlib import Path

db_path = Path("/home/turbyho/.fw-context/index/452361ffbf84f774/index.db")
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=" * 70)
print("  ALL GRAPH EDGES & STRUCTURAL RELATIONSHIPS")
print("=" * 70)

# === 1. Call graph edges (refs table) ===
print("\n--- 1. refs (call graph edges) ---")
total_refs = conn.execute("SELECT COUNT(*) c FROM refs").fetchone()['c']
print(f"  Total edges: {total_refs:,}")

ref_types = conn.execute(
    "SELECT ref_kind, COUNT(*) c FROM refs GROUP BY ref_kind ORDER BY c DESC"
).fetchall()
print("  By kind:")
for r in ref_types:
    pct = r['c'] / total_refs * 100
    print(f"    {r['ref_kind']:15s} {r['c']:>10,} ({pct:5.1f}%)")

# Call edges: project vs SDK
call_edges = conn.execute(
    "SELECT ref_kind FROM refs WHERE ref_kind = 'call'"
).fetchall()
proj_calls = conn.execute(
    """SELECT COUNT(*) c FROM refs r
       JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
       WHERE r.ref_kind = 'call'
         AND (s.file_path LIKE 'src/%' OR s.file_path LIKE 'app/%' OR s.file_path LIKE 'lib/%')"""
).fetchone()['c']
print(f"\n  Call edges to project symbols: {proj_calls:,}")
print(f"  Call edges to SDK symbols: {len(call_edges) - proj_calls:,}")

# Call edges: project → project, project → SDK, SDK → SDK, SDK → project
print("\n  Call edge source/target breakdown:")
edge_types = [
    ("project → project", 
     "(s1.file_path LIKE 'src/%' OR s1.file_path LIKE 'lib/%') AND (s2.file_path LIKE 'src/%' OR s2.file_path LIKE 'lib/%')"),
    ("project → SDK",
     "(s1.file_path LIKE 'src/%' OR s1.file_path LIKE 'lib/%') AND NOT (s2.file_path LIKE 'src/%' OR s2.file_path LIKE 'lib/%')"),
    ("SDK → project",
     "NOT (s1.file_path LIKE 'src/%' OR s1.file_path LIKE 'lib/%') AND (s2.file_path LIKE 'src/%' OR s2.file_path LIKE 'lib/%')"),
    ("SDK → SDK",
     "NOT (s1.file_path LIKE 'src/%' OR s1.file_path LIKE 'lib/%') AND NOT (s2.file_path LIKE 'src/%' OR s2.file_path LIKE 'lib/%')"),
]
for label, cond in edge_types:
    cnt = conn.execute(
        f"""SELECT COUNT(*) c FROM refs r
            JOIN symbols s1 ON s1.usr = r.from_usr AND s1.config_hash = r.config_hash
            JOIN symbols s2 ON s2.usr = r.to_usr AND s2.config_hash = r.config_hash
            WHERE r.ref_kind = 'call' AND r.from_usr IS NOT NULL AND r.from_usr != ''
              AND {cond}"""
    ).fetchone()['c']
    print(f"    {label:25s} {cnt:>8,}")

# === 2. Inheritance edges ===
print("\n--- 2. inheritance (C++ class hierarchy) ---")
inh_total = conn.execute("SELECT COUNT(*) c FROM inheritance").fetchone()['c']
print(f"  Total edges: {inh_total:,}")

inh_access = conn.execute(
    "SELECT access, is_virtual, COUNT(*) c FROM inheritance GROUP BY access, is_virtual"
).fetchall()
print("  By access:")
for r in inh_access:
    virt = "virtual" if r['is_virtual'] else "non-virtual"
    print(f"    {r['access']:12s} {virt:12s} {r['c']:>5}")

# Inheritance: project derived classes?
proj_derived = conn.execute(
    """SELECT COUNT(*) c FROM inheritance i
       JOIN symbols s ON s.usr = i.derived_usr AND s.config_hash = i.config_hash
       WHERE s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%'"""
).fetchone()['c']
print(f"  Project-derived classes: {proj_derived}/{inh_total}")

# === 3. Override edges ===
print("\n--- 3. overrides (virtual method overrides) ---")
ov_total = conn.execute("SELECT COUNT(*) c FROM overrides").fetchone()['c']
print(f"  Total edges: {ov_total:,}")

proj_ov = conn.execute(
    """SELECT COUNT(*) c FROM overrides o
       JOIN symbols s ON s.usr = o.derived_usr AND s.config_hash = o.config_hash
       WHERE s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%'"""
).fetchone()['c']
print(f"  Project overrides: {proj_ov}/{ov_total}")

# === 4. File → Symbol relationships ===
print("\n--- 4. files ↔ symbols (structural containment) ---")
file_count = conn.execute("SELECT COUNT(*) c FROM files").fetchone()['c']
print(f"  Total files: {file_count:,}")

# Symbols per file distribution
dist = conn.execute(
    "SELECT file_id, COUNT(*) c FROM symbols GROUP BY file_id ORDER BY c DESC LIMIT 10"
).fetchall()
print("  Top files by symbol count:")
for r in dist:
    fpath = conn.execute("SELECT path FROM files WHERE id = ?", (r['file_id'],)).fetchone()
    print(f"    [{r['c']:>5,d} symbols] {fpath['path'][:70]}")

# Average symbols per file
avg = conn.execute(
    "SELECT AVG(cnt) avg FROM (SELECT COUNT(*) cnt FROM symbols GROUP BY file_id)"
).fetchone()['avg']
print(f"\n  Avg symbols per file: {avg:.1f}")

# Project files vs SDK files
proj_files = conn.execute(
    "SELECT COUNT(*) c FROM files WHERE path LIKE 'src/%' OR path LIKE 'lib/%' OR path LIKE 'app/%'"
).fetchone()['c']
print(f"  Project files: {proj_files}/{file_count}")

# === 5. Class/struct → members (parent_usr) ===
print("\n--- 5. parent_usr (class/struct → members) ---")
parent_refs = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE parent_usr != ''"
).fetchone()['c']
print(f"  Symbols with parent (class members): {parent_refs:,}")

# Members per parent distribution
parent_dist = conn.execute(
    """SELECT parent_usr, COUNT(*) c FROM symbols
       WHERE parent_usr != ''
       GROUP BY parent_usr ORDER BY c DESC LIMIT 10"""
).fetchall()
print("  Top classes by member count:")
for r in parent_dist:
    pname = conn.execute(
        "SELECT name FROM symbols WHERE usr = ? LIMIT 1", (r['parent_usr'],)
    ).fetchone()
    pn = pname['name'] if pname else r['parent_usr'][:40]
    print(f"    [{r['c']:>4,d} members] {pn[:50]}")

# Project members
proj_members = conn.execute(
    """SELECT COUNT(*) c FROM symbols
       WHERE parent_usr != ''
         AND (file_path LIKE 'src/%' OR file_path LIKE 'lib/%' OR file_path LIKE 'app/%')"""
).fetchone()['c']
print(f"  Project members: {proj_members}/{parent_refs}")

# === 6. Template → instantiation (template_usr) ===
print("\n--- 6. template_usr (template → instantiation) ---")
templ_inst = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE template_usr != ''"
).fetchone()['c']
print(f"  Template instantiations: {templ_inst:,}")

templ_defs = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE is_template = 1"
).fetchone()['c']
print(f"  Template definitions: {templ_defs:,}")

# === 7. Indirect calls (fp_assignments ↔ indirect_call_sites) ===
print("\n--- 7. Indirect call edges ---")
ics = conn.execute("SELECT COUNT(*) c FROM indirect_call_sites").fetchone()['c']
fpa = conn.execute("SELECT COUNT(*) c FROM fp_assignments").fetchone()['c']
print(f"  Indirect call sites: {ics:,}")
print(f"  FP assignments: {fpa:,}")
print(f"  Together: Phase 3 links assignments → call sites")

# === 8. Type references (ref_kind = ref) ===
print("\n--- 8. Type/var/enum references (ref_kind = ref) ---")
type_refs = conn.execute(
    "SELECT COUNT(*) c FROM refs WHERE ref_kind = 'ref'"
).fetchone()['c']
print(f"  Variable/enum reads: {type_refs:,}")

member_refs = conn.execute(
    "SELECT COUNT(*) c FROM refs WHERE ref_kind = 'member'"
).fetchone()['c']
print(f"  Member accesses: {member_refs:,}")

# === 9. Config/LLM analysis relationships ===
print("\n--- 9. LLM analysis edges ---")
lla = conn.execute("SELECT COUNT(*) c FROM llm_analysis").fetchone()['c']
print(f"  LLM analysis → symbols: {lla:,}")

cache = conn.execute("SELECT COUNT(*) c FROM llm_analysis_cache").fetchone()['c']
print(f"  LLM cache entries: {cache:,}")

# === 10. Missing edges — what relationships exist in code but are NOT in DB? ===
print("\n--- 10. MISSING EDGES (exist in code, not in DB) ---")
print("  ❌ Include graph: which files #include which other files")
print("  ❌ Type usage: which symbols use which types (beyond current ref table)")
print("  ❌ Macro expansion: which macros expand where")
print("  ❌ Symbol co-occurrence: symbols that appear together in same function")
print("  ❌ Data flow: which variables are read/written by which functions")
print("  ❌ Call frequency: how many times A calls B (not just existence)")
print("  ❌ Conditional call: A calls B only under certain preprocessor conditions")

# === 11. Edge density: how connected is the graph? ===
print("\n--- 11. GRAPH METRICS ---")
sym_count = conn.execute("SELECT COUNT(*) c FROM symbols WHERE is_definition = 1").fetchone()['c']
# Function-like symbols (those that can be callers/callees)
fn_count = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE is_definition = 1 AND kind IN ('function','method','constructor','destructor')"
).fetchone()['c']

call_edges_count = len(call_edges)
avg_out = call_edges_count / fn_count if fn_count > 0 else 0
print(f"  Function-like symbols: {fn_count:,}")
print(f"  Call edges: {call_edges_count:,}")
print(f"  Avg outgoing calls per function: {avg_out:.1f}")

# Most-called functions
print("\n  Top 5 most-called functions:")
top = conn.execute(
    """SELECT s.name, s.qualified_name, s.file_path, COUNT(r.id) c
       FROM refs r JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
       WHERE r.ref_kind = 'call'
       GROUP BY r.to_usr ORDER BY c DESC LIMIT 5"""
).fetchall()
for r in top:
    proj = "PROJ" if (r['file_path'] or '').startswith('src/') else "SDK"
    print(f"    [{proj}] {r['name']:30s} → {r['c']:>5,d} callers  ({r['qualified_name']})")

# Most-calling functions
print("\n  Top 5 most-calling functions (most outgoing edges):")
top_out = conn.execute(
    """SELECT s.name, s.qualified_name, s.file_path, COUNT(r.id) c
       FROM refs r JOIN symbols s ON s.usr = r.from_usr AND s.config_hash = r.config_hash
       WHERE r.ref_kind = 'call'
       GROUP BY r.from_usr ORDER BY c DESC LIMIT 5"""
).fetchall()
for r in top_out:
    proj = "PROJ" if (r['file_path'] or '').startswith('src/') else "SDK"
    print(f"    [{proj}] {r['name']:30s} calls {r['c']:>5,d} functions  ({r['qualified_name']})")

conn.close()
