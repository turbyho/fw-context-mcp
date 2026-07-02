"""Test anonymous struct token pollution impact on FTS5 quality."""
import sqlite3

db_path = "/home/turbyho/.fw-context/index/452361ffbf84f774/index.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# 1. How many symbols have '(unnamed' in qualified_name?
anon = conn.execute(
    "SELECT COUNT(*) c FROM symbols WHERE qualified_name LIKE '%(unnamed%'"
).fetchone()['c']
print(f"Symbols with '(unnamed' in qualified_name: {anon}")

# 2. Check token pollution: what tokens does FTS5 index for these?
anon_sample = conn.execute(
    """SELECT s.name, s.qualified_name, s.name_tokens, s.file_path
       FROM symbols s
       WHERE s.qualified_name LIKE '%(unnamed%'
       LIMIT 10"""
).fetchall()
print("\nSample anonymous symbol tokens:")
for r in anon_sample:
    tokens = r['name_tokens'].split()
    # Filter tokens that are likely from anonymous struct paths
    path_tokens = [t for t in tokens if t in ('unnamed', 'struct', 'enum', 'at', 'include', 'src', 
                                               'mbed', 'os', 'cmsis', 'rtos2', 'rtx', 'ble', 'libraries',
                                               'target', 'cordio', 'll', 'stack', 'controller', 'sources',
                                               'lctr', 'l2c', 'coc', 'api', 'feature', 'connectivity',
                                               'nordic', 'nrf5x', 'nrf52', 'hal', 'usb', 'ti')]
    line_tokens = [t for t in tokens if t.isdigit() or (len(t) <= 3 and t.isdigit())]
    meaningful = [t for t in tokens if t not in path_tokens and t not in line_tokens and t != 'unnamed']
    print(f"  name={r['name'][:50]}")
    print(f"  file={r['file_path'][:60]}")
    print(f"  all_tokens: {tokens}")
    print(f"  meaningful: {meaningful}")
    print()

# 3. How many search queries accidentally match anonymous struct tokens?
queries = ["struct", "unnamed", "enum", "mbed", "controller", "include", "rtx"]
for q in queries:
    r = conn.execute(
        """SELECT COUNT(*) c FROM symbols_fts
           WHERE symbols_fts MATCH ?""",
        (f"name_tokens : {q}",),
    ).fetchone()['c']
    print(f"  FTS5 'name_tokens : {q}' matches: {r} symbols")

# 4. Test: improved split_tokens that filters anonymous names
print("\n=== Improved split_tokens proposal ===")
import re

def improved_split_tokens(name: str, qualified_name: str = "") -> str:
    """Improved: filter out anonymous struct/enum names."""
    def _tokenize(s: str) -> list[str]:
        # Filter out anonymous names before tokenizing
        if '(unnamed' in s:
            # Replace the anonymous part with nothing
            s = re.sub(r'\(unnamed\s+(struct|enum|union)\s+at\s+[^)]+\)', '', s)
        
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
        parts = re.split(r"[^a-zA-Z0-9]+", s)
        # Filter out common noise tokens from paths
        noise = {'at', 'unnamed', 'struct', 'enum', 'union', 'mbed', 'os'}
        return [p.lower() for p in parts if len(p) > 1 and p.lower() not in noise]

    tokens: list[str] = []
    seen: set[str] = set()
    for src in (name, qualified_name):
        if src:
            for tok in _tokenize(src):
                if tok not in seen:
                    seen.add(tok)
                    tokens.append(tok)
    return " ".join(tokens)

# Test on anonymous symbols
anon_sample2 = conn.execute(
    """SELECT s.name, s.qualified_name, s.name_tokens
       FROM symbols s
       WHERE s.qualified_name LIKE '%(unnamed%'
       LIMIT 5"""
).fetchall()
print("Before vs After:")
for r in anon_sample2:
    improved = improved_split_tokens(r['name'], r['qualified_name'])
    print(f"  OLD: [{r['name_tokens'][:100]}]")
    print(f"  NEW: [{improved[:100]}]")
    print()

conn.close()
