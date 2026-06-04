# Code Review Findings — fw-context-mcp

Datum: 2026-06-05
Scope: celý codebase (~3000 LOC, 25+ souborů)
Status: nutné opravy + doporučená rozšíření

---

## Část 1: Bugy a nutné opravy

### B1 — File descriptor leak v MCP tools (server.py)
**Závažnost:** Vysoká (postupná degradace serveru)
**Soubor:** `src/fw_context_mcp/mcp/server.py`

Všechny MCP tool funkce (`lookup_symbol`, `search_code`, `get_active_build`, `explain_symbol`,
`get_source`, `reindex_file`, `_references_result`) používají pattern:
```python
conn, err = _open_db_safe(db_path)
...
with conn:      # ← sqlite3 __exit__ jen commitne/rollbackne, NEZAVÍRÁ konekci
    ...
# conn zůstává otevřená — spoléhá se na GC
```

Pro dlouho běžící MCP server to znamená postupný únik file descriptorů. Každý tool call
otevře 1-3 konekce a žádnou explicitně nezavře.

**Fix:** Všechna místa s `with conn:` změnit na `with conn: ... conn.close()` nebo
použít `contextlib.closing`.

**Postižené lokace:**
- `lookup_symbol()` — řádky ~310, ~330
- `search_code()` — řádky ~590, ~620
- `get_active_build()` — řádek ~290
- `explain_symbol()` — řádek ~420
- `get_source()` — řádek ~470
- `reindex_file()` — řádek ~350
- `_references_result()` — řádek ~520

---

### B2 — Memory: `_read_symbol_body` načítá celý soubor do paměti (server.py)
**Závažnost:** Střední
**Soubor:** `src/fw_context_mcp/mcp/server.py:169`

```python
lines = Path(file_path).read_text(errors="replace").splitlines()
```
Pro 100MB+ generated soubory (běžné v embedded projektech) zbytečně alokuje celý obsah,
i když je potřeba jen 400 řádků od `line_no`.

**Fix:** Streamovat soubor po řádcích od `start` pozice, nebo použít `mmap` + hledání `\n`.
Minimálně omezit `read_text` na prvních `(line_no + max_lines)` řádků.

---

### B3 — Duplicitní `_resolve_project_root` (3 kopie)
**Závažnost:** Nízká (DRY, udržovatelnost)
**Soubory:**
- `src/fw_context_mcp/mcp/server.py:120`
- `src/fw_context_mcp/search/context.py:104`
- `src/fw_context_mcp/cli.py` (inline)

Tři identické implementace logiky "zadaná cesta → git root → cwd".

**Fix:** Extrahovat do `src/fw_context_mcp/utils.py` (nový soubor) nebo do `config/settings.py`.

---

### B4 — Duplicitní `_abs_path` (2 kopie)
**Závažnost:** Nízká (DRY)
**Soubory:**
- `src/fw_context_mcp/mcp/server.py:129`
- `src/fw_context_mcp/search/phases/format.py:58`

**Fix:** Přesunout do `src/fw_context_mcp/utils.py`.

---

### B5 — Duplicitní auto-reindex pattern (lookup_symbol + search_code)
**Závažnost:** Střední (100+ řádků duplicity, divergence risk)
**Soubor:** `src/fw_context_mcp/mcp/server.py`

`lookup_symbol` a `search_code` sdílejí identickou strukturu:
1. Otevřít DB → config_hash
2. Spustit vyhledávání
3. Detekce stale files
4. Auto-reindex + retry
5. Warning agregace

**Fix:** Extrahovat do generického wrapperu:
```python
def _with_stale_recovery(db_path, root, query_fn) -> tuple[list[dict], list[str]]:
    """Execute query_fn(conn, config_hash), auto-reindex stale files, retry."""
```

---

### B6 — Magická konstanta `+ 1` v `_is_stale` (server.py:135)
**Závažnost:** Nízká (čitelnost)
```python
return cc_mtime > indexed_at.timestamp() + 1
```

**Fix:** Pojmenovat konstantu: `_MTIME_TOLERANCE_S = 1.0  # 1s tolerance na clock skew`

Stejná konstanta se používá i v `_stale_files`, `_count_modified_files`, a `_process_unit`.

---

### B7 — `_auto_reindex_stale` volá `reindex_file` bez importu (server.py:266)
**Závažnost:** Nízká (funguje díky Python closure ve stejném souboru)
```python
def _auto_reindex_stale(...):
    result = reindex_file(fp, str(project_root))  # ← náhodou funguje
```
`reindex_file` je dekorovaná `@mcp.tool()` ve stejném modulu — closure ji najde.
Ale kdyby se funkce přesunula do jiného modulu, spadne to.

**Fix:** Buď explicitní import, nebo extrahovat core logiku `reindex_file` do samostatné
funkce (např. `_reindex_file_impl`) a tool wrapper ať ji volá.

---

### B8 — `_build_embeddings` bez explicitního transaction wrapperu (runner.py)
**Závažnost:** Nízká (spoléhá na auto-commit, WAL může narůst)
**Soubor:** `src/fw_context_mcp/indexer/runner.py:119`

```python
for i in range(0, len(rows), chunk_size):
    ...
    upsert_embeddings(conn, batch)
```

**Fix:** Použít `transaction(conn)` context manager, nebo alespoň explicitní commit
po každém batchi a checkpoint na konci.

---

### B9 — `count_refs` vrací `int` ne `Row`, nekonzistentní API (db.py)
**Závažnost:** Nízká (překvapivé chování)
```python
def count_refs(conn, config_hash: str) -> int:
    return conn.execute("SELECT COUNT(*) ...").fetchone()[0]
```

Pokud config_hash neexistuje, vrací `0` (COUNT(*) vrací 0, ne None). To je správně,
ale liší se od ostatních `get_*` funkcí které vrací `Row | None`.

**Fix:** OK tak jak je, ale přidat docstring poznámku, že prázdný výsledek = 0.

---

## Část 2: Doporučená rozšíření

### R1 — Vektorové vyhledávání přes sqlite-vec (embedding search)
**Priorita:** Vysoká
**Dopad:** 10-100x zrychlení embedding fáze, hybrid search

**Současný stav:** `EmbeddingPhase` dělá brute-force cosine similarity v Pythonu:
```python
for sym_id, emb_vec in stored.items():       # O(n) loop
    sim = _cosine_similarity(query_vec, emb_vec)  # 1024 float ops
    if sim > 0.5:
        scored.append((sym_id, sim))
```
Pro 50k symbolů = 51M float operací na request.

**Návrh:** Integrovat `sqlite-vec` (Alex Garcia, MIT licence):
- HNSW index uvnitř SQLite — sub-ms ANN search
- Umožňuje hybrid search: `ORDER BY bm25(symbols_fts) * 0.7 + cosine_distance * 0.3`
- Žádná externí vector DB — vše v jednom `.db` souboru
- Python binding: `pip install sqlite-vec`

**Změny:**
1. `pyproject.toml`: přidat `sqlite-vec>=0.1.0`
2. `db.py`: přidat `create_virtual_table("vec_symbols", ...)`, `search_similar()`
3. `embedding.py`: nahradit brute-force za SQL query s `vec_distance_cosine`
4. `runner.py:_build_embeddings`: ukládat do sqlite-vec tabulky místo BLOB sloupce

**Architektura po změně:**
```sql
CREATE VIRTUAL TABLE vec_symbols USING vec0(
    symbol_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
);

-- Hybrid search (FTS5 + vector):
SELECT s.*,
       bm25(symbols_fts) AS text_score,
       vec_distance_cosine(v.embedding, :query_vec) AS vec_score
FROM symbols s
JOIN vec_symbols v ON v.symbol_id = s.id
WHERE s.config_hash = :hash
  AND vec_distance_cosine(v.embedding, :query_vec) < :threshold
ORDER BY text_score * 0.7 + (1.0 - vec_score) * 0.3
LIMIT 20;
```

**Alternativa:** Pokud sqlite-vec nelze použít (např. kompatibilita s Python 3.11),
použít alespoň `numpy` pro vektorizovaný dot product — 10x rychlejší než Python loop.

---

### R2 — Grafová analytika: call-graph traversační dotazy
**Priorita:** Vysoká
**Dopad:** Nová dimenze dotazů — "co se rozbije když změním X"

**Současný stav:** `refs` tabulka umí jen přímé dotazy:
- `find_callers("modem_init")` → kdo volá modem_init
- `find_references("UART::write")` → všechna použití

Neumí:
- "Najdi cestu z `main` do `modem_init`"
- "Co všechno (tranzitivně) volá `UART::write`?"
- "Které funkce nejsou nikým volané?" (dead code)
- "Které funkce mají nejvíc callerů?" (hotspots)

**Návrh:**

#### R2a — `find_call_path` (BFS v SQLite)
```sql
WITH RECURSIVE path(from_usr, to_usr, depth, chain) AS (
    SELECT from_usr, to_usr, 1, from_usr || '→' || to_usr
    FROM refs WHERE from_usr = :start_usr
    UNION ALL
    SELECT r.from_usr, r.to_usr, p.depth + 1, p.chain || '→' || r.to_usr
    FROM refs r
    JOIN path p ON p.to_usr = r.from_usr
    WHERE p.depth < :max_depth
)
SELECT * FROM path WHERE to_usr = :end_usr
ORDER BY depth LIMIT 5
```

#### R2b — `find_all_callers_recursive` (tranzitivní uzávěr)
```sql
WITH RECURSIVE callers(usr, depth) AS (
    SELECT from_usr, 1 FROM refs WHERE to_usr = :target_usr
    UNION
    SELECT r.from_usr, c.depth + 1
    FROM refs r
    JOIN callers c ON r.to_usr = c.usr
    WHERE c.depth < :max_depth
)
SELECT DISTINCT s.name, s.qualified_name, s.file_path, c.depth
FROM callers c
JOIN symbols s ON s.usr = c.usr
ORDER BY c.depth
```

#### R2c — `find_dead_code` (symboly s 0 references)
```sql
SELECT s.name, s.qualified_name, s.kind, s.file_path
FROM symbols s
WHERE s.config_hash = :hash
  AND s.is_definition = 1
  AND s.kind IN ('function', 'method')
  AND s.usr NOT IN (SELECT DISTINCT to_usr FROM refs WHERE config_hash = :hash)
ORDER BY s.name
```

**Nové MCP tools:**
- `find_call_path(from_name, to_name, max_depth=10)` → seznam cest
- `find_all_callers_recursive(name, max_depth=5)` → tranzitivní callers
- `find_call_graph(name, direction='both', max_depth=2)` → okolí v call grafu
- `find_dead_code()` → potenciálně mrtvý kód

**Implementační poznámka:** Vše výše uvedené jde čistě v SQLite přes recursive CTE —
není potřeba externí grafová DB. Pro větší projekty (100k+ symbolů) může být recursive
CTE pomalé — pak zvážit NetworkX in-memory graf z `refs` tabulky.

---

### R3 — SCIP kompatibilní export
**Priorita:** Střední
**Dopad:** Interoperabilita se Sourcegraph ekosystémem

**Návrh:** Přidat `fw-context export --format scip` — serializuje index do
SCIP protobuf formátu. Umožňuje:
- Nahrát index na Sourcegraph.com pro webové procházení
- Sdílet index mezi stroji (menší než SQLite díky kompresi)
- Použít existující SCIP tooling (scip-clang, scip-java)

**Změny:**
1. `pyproject.toml`: přidat `protobuf>=4.0`
2. Nový modul `src/fw_context_mcp/export/scip.py`
3. CLI: `fw-context export --format scip --output index.scip`

---

### R4 — Content-based chunking pro LLM
**Priorita:** Střední
**Dopad:** Přesnější kontext pro Claude/LLM při vysvětlování kódu

**Současný stav:** `explain_symbol` vrací `context_lines` pevné délky kolem definice.
Pro velké funkce (200+ řádků) usekne důležitý kontext, pro malé vrací zbytečné okolí.

**Návrh:** Místo pevného `context_lines` použít brace-matching (už implementován
v `_read_symbol_body`) jako primární strategii. Pro soubory s `end_line` (z libclang)
použít přesnou hranici.

**Nový tool:** `get_symbol_context(name)` — vrátí:
1. Tělo funkce (brace-matched)
2. Signaturu
3. Bezprostřední callers (max 5)
4. Bezprostřední callees (max 5)
→ Ideální jako LLM kontext pro "co dělá tahle funkce a jak zapadá do systému?"

---

### R5 — `fw-context watch` — inkrementální indexing na pozadí
**Priorita:** Nízká (složitější, dobré mít)
**Dopad:** Index je vždy aktuální bez manuálního spouštění `fw-context index`

**Návrh:**
- `inotify` (Linux) / `FSEvents` (macOS) na projektovém adresáři
- Při změně `.c/.cpp` → automaticky `reindex_file`
- Při změně `compile_commands.json` → notifikace, že je potřeba plný re-index
- Status přes `get_active_build()` ukazuje `stale=false` dokud watchdog běží
- Pozadí, neblokující, volitelný (ne každý to chce)

**Rizika:**
- libclang není thread-safe — parsování musí běžet v main thread nebo s GIL
- Embedded projekty často používají network mounty (NFS) — inotify nemusí fungovat

---

### R6 — Rozšíření FTS5 o komentáře
**Priorita:** Nízká
**Dopad:** Hledání v komentářích a dokumentaci

**Návrh:** Přidat `docstring` obsah do FTS5 indexu (už tam je). Zvážit extrakci
komentářů z těl funkcí (libclang to neumí — musel by se parsovat zdroják zvlášť).

---

### R7 — MCP Resources pro browsování
**Priorita:** Nízká
**Dopad:** Lepší integrace s MCP klienty

**Návrh:** Kromě MCP Tools přidat MCP Resources:
- `fw-context://<project>/symbols/<name>` → definice symbolu
- `fw-context://<project>/callgraph/<name>` → call graph okolí
- `fw-context://<project>/stats` → statistiky indexu

Resources umožňují "watch" pattern — klient subscribne a server notifikuje při změně.

---

## Část 3: Odmítnuté návrhy

### Tree-sitter jako doplněk k libclang — ODMÍTNUTO
**Důvody:**
1. libclang je pro C/C++ definitivní parser — tree-sitter parsuje jen syntax, ne typy,
   makra, šablony. V embedded kódu kritické `#ifdef`/`#ifndef` bloky by minul.
2. Inkrementální parsing řeší špatný problém — fw-context neparsuje po keystroke,
   ale celé translation unity. Změna headeru stejně zneplatní desítky TU.
3. Dva parsery = dvojitá údržba, mapování dvou různých AST formátů, riziko divergencí.
4. Embedded kód v `compile_commands.json` je vždy kompilovatelný — error recovery
   tree-sitteru nemá co zachraňovat.
5. Tree-sitter dává smysl pro multi-language nástroje (narsil-mcp pokrývá 16 jazyků),
   ne pro single-language C/C++ nástroj.

---

## Část 4: Pořadí implementace

### Fáze 1 — Opravy (1-2 dny)
- [x] B1: File descriptor leak — `conn.close()` ve všech MCP toolech
- [x] B2: Streamovat `_read_symbol_body`
- [x] B3: Extrahovat `_resolve_project_root` do `utils.py`
- [x] B4: Extrahovat `_abs_path` do `utils.py`
- [x] B5: Extrahovat auto-reindex wrapper
- [x] B6: Pojmenovat `MTIME_TOLERANCE_S`
- [~] B7: Opravit import `reindex_file` v `_auto_reindex_stale` — funguje díky stejnému modulu, extrakce `_reindex_file_impl` odložena
- [x] B8: Přidat transaction do `_build_embeddings`

### Fáze 2 — Vektorové vyhledávání (2-3 dny)
- [x] R1a: Integrovat `sqlite-vec`
- [x] R1b: Nahradit brute-force embedding search za vec0 KNN
- [x] R1c: Hybridní re-rank (FTS5 kandidáti skórováni vektorovou vzdáleností)
- [x] R1d: Backward-compat fallback na starou BLOB tabulku

### Fáze 3 — Grafová analytika (2-3 dny)
- [x] R2a: `find_call_path` — BFS v SQLite recursive CTE
- [x] R2b: `find_all_callers_recursive` — tranzitivní uzávěr
- [x] R2c: `find_dead_code` — symboly bez referencí
- [x] R2d: Nové MCP tools (5 nových toolů)
- [x] R2e: `find_callees_recursive` — inverzní tranzitivní traversál
- [x] R2f: `find_hotspots` — funkce s nejvíce callery

### Fáze 4 — SCIP + LLM chunking (2-3 dny)
- [x] R3: JSON export (`fw-context export --format json`, bez protobuf — praktičtější pro embedded)
- [x] R4: `get_symbol_context` tool — tělo funkce + callers + callees pro LLM kontext

### Fáze 5 — Watch + Resources (budoucnost)
- [x] R5: `fw-context watch` — auto-reindex při změně .c/.cpp/.h/.hpp (watchfiles + debounce)
- [x] R7: MCP Resources — `fw-context://stats`, `fw-context://projects`, `fw-context://symbols/{{name}}`

---

## Část 5: Technické reference

- [sqlite-vec](https://github.com/asg017/sqlite-vec) — vector search v SQLite (MIT)
- [SCIP protobuf schema](https://github.com/sourcegraph/scip/blob/main/scip.proto)
- [SCIP spec](https://github.com/sourcegraph/scip/blob/main/docs/protocol.md)
- [SQLite recursive CTE](https://www.sqlite.org/lang_with.html) — grafové dotazy
- [Hybrid search s SQLite](https://simonwillison.net/2024/Oct/4/hybrid-full-text-search-and-vector-search-with-sqlite/) — RRF scoring
- [narsil-mcp](https://github.com/postrv/narsil-mcp) — konkurenční MCP server (Rust)
- [CodeGraphMCPServer](https://github.com/nahisaho/CodeGraphMCPServer) — GraphRAG v Pythonu
- [srclight](https://himcp.ai/server/srclight) — SQLite FTS5 + tree-sitter + embeddings
