# Plán — fw-context-mcp iterace 3: opravy a vylepšení

**Projekt:** `fw-context-mcp` (`~/dev/sw/work/tools/fw-context-mcp`, repo `git.montyho.com/turbyho/fw-context-mcp`)
**Navazuje na:** iterace 2 (search quality, commity `d8c813f`..`7b16692`)
**Jira:** není vyžadováno (AI-tooling projekt, ne firmware)
**Status:** Návrh — čeká na schválení

---

## Cíl

Opravit regresi v konzistenci cest zavedenou v iteraci 2 a doplnit chybějící
funkce, které dělají z indexu skutečnou "code intelligence":
cross-reference (call graph), čtení zdrojového kódu symbolu a kvalitativní
vylepšení rankingu a cache.

---

## Fáze 1 — Oprava regrese: konzistence cest (MUST-FIX)

### Problém

Commit `3d686cc` odstranil `JOIN files` ze `search_symbols` (db.py:330) kvůli
výkonu a shadowing hazardu. Tím se ale změnil formát výstupu:

- `search_code` / `smart_search` nyní vrací **relativní** cestu z
  `symbols.file_path` (`src/zble.cpp`)
- `lookup_symbol` (server.py:297,306) a `explain_symbol` (server.py:652,660)
  stále vrací **absolutní** cestu přes `f.path as file_path`

Před `3d686cc` vracel `search_code` taky absolutní cestu → jde o regresi
výstupního formátu. Relativní cesta není pro konzumujícího agenta klikatelná
ani otevíratelná bez znalosti project rootu.

### Analýza

- `src/fw_context_mcp/indexer/db.py:330-352` — `search_symbols` vrací `s.*`
  (relativní `file_path`). FTS5 indexace relativní cesty je správně a má
  zůstat (tokenizace modulů).
- `src/fw_context_mcp/mcp/server.py:230-242` — `search_code` `_fmt` mapuje
  `r["file_path"]` → `file` (relativní).
- `src/fw_context_mcp/mcp/server.py:836-846` — `smart_search` `_fmt` totéž.
- `src/fw_context_mcp/mcp/server.py:318-329` — `lookup_symbol` (absolutní).
- `src/fw_context_mcp/mcp/server.py:698-704` — `explain_symbol` (absolutní,
  navíc cestu **potřebuje** pro `Path(file_path).read_text()` na ř. 677).

### Změna

Sjednotit na **absolutní cestu ve `file` výstupu** všech nástrojů, FTS5 si
nechá relativní `file_path` interně.

- Přidat helper `_abs_path(root, rel_or_abs)` v `server.py` — pokud je cesta
  relativní, spojí s `root`; absolutní vrací beze změny.
- V `_fmt` u `search_code` a `smart_search` aplikovat `_abs_path` na `file`.
- `lookup_symbol` / `explain_symbol` ponechat (už vrací absolutní), ale ověřit
  konzistenci klíče — sjednotit zdroj na `s.file_path` + `_abs_path` a
  odstranit i tady redundantní `JOIN files` (stejně jako u `search_symbols`),
  protože `explain_symbol` může rekonstruovat absolutní cestu z root + relativní.

### Rizika

- `_stale_files` (server.py:89) bere seznam cest a volá `os.path.getmtime` —
  potřebuje absolutní cesty. Po sjednocení musí dostávat absolutní (ověřit).
- CLI `cmd_search` (cli.py:201) používá `Path(r['file_path']).name` — basename,
  funguje s relativní i absolutní, beze změny.

### Ověření

- `search_code` a `lookup_symbol` vrací stejný formát `file` pro stejný symbol.
- `explain_symbol` čte soubor správně (regrese test na `onConnectionComplete`).

---

## Fáze 2 — Nová funkce: cross-reference / call graph (HIGH VALUE)

### Motivace

Největší capability gap. Index obsahuje jen definice. Nelze odpovědět
"kdo volá `modem_init`?" nebo "kde se používá `ZCfgDataManager`?". Plán
build-aware code intelligence "graph" tohle předpokládá.

### Návrh schématu

Nová tabulka `references`:

```sql
CREATE TABLE IF NOT EXISTS refs (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    to_usr       TEXT    NOT NULL,   -- USR cílového symbolu (definice)
    from_file    TEXT    NOT NULL,   -- relativní cesta zdroje reference
    from_line    INTEGER NOT NULL,
    from_usr     TEXT,               -- USR obklopující funkce (volající), může být NULL
    ref_kind     TEXT    NOT NULL    -- 'call' | 'ref' | 'member'
);
CREATE INDEX idx_refs_to_usr ON refs(config_hash, to_usr);
CREATE INDEX idx_refs_from   ON refs(config_hash, from_usr);
```

Linkování přes `usr` — `symbols.usr` už existuje a je unikátní per config_hash.

### Extrakce (symbols.py)

V `extract()` walk přidat druhý průchod (nebo rozšířit stávající
`walk_preorder`):

- Pro kurzory `CALL_EXPR`, `DECL_REF_EXPR`, `MEMBER_REF_EXPR`:
  - `target_usr = cursor.referenced.get_usr()` — USR volaného/odkazovaného
  - `from_usr` = USR nejbližšího obklopujícího `FUNCTION_DECL`/`CXX_METHOD`
    (sledovat `semantic_parent` nebo držet zásobník při průchodu)
  - filtr přes `_in_roots` / `_not_excluded` (jako u symbolů)
- Vrátit nový dataclass `Reference(to_usr, from_file, from_line, from_usr, ref_kind)`.

`extract()` bude vracet `tuple[list[Symbol], list[Reference]]` nebo dva
generátory — sjednotit s `runner.py` a `reindex_file`.

### Indexace (runner.py)

- Po `insert_symbols_batch` přidat `insert_refs_batch`.
- Při incremental update: `delete_refs_for_file(from_file)` před re-insertem
  (analogicky `delete_symbols_for_file`).
- **Pozor na objem:** referencí bude řádově víc než symbolů (každé volání).
  Na mbed-os projektu odhad statisíce. Filtr: ukládat jen reference, jejichž
  `to_usr` odpovídá nějakému indexovanému symbolu (join na `symbols.usr`), aby
  se nezahltil index referencemi na systémové hlavičky.

### Nové MCP nástroje (server.py)

```python
@mcp.tool()
def find_references(name, project_root=None, limit=50) -> list[dict]:
    """Najdi všechna místa, kde je symbol volán/odkazován."""
    # lookup USR symbolu podle name → join refs.to_usr → vrať from_file:from_line + volající

@mcp.tool()
def find_callers(name, project_root=None, limit=50) -> list[dict]:
    """Jen volání (ref_kind='call'), s rozlišením volající funkce."""
```

### Rizika

- **Objem indexu** — refs tabulka může být 5-10× větší než symbols. Měřit na
  zbox-ecb-fw, případně omezit jen na `ref_kind='call'` a jen zbox/lib symboly.
- **Čas indexace** — druhý walk nebo extrakce referencí prodlouží indexaci
  (aktuálně ~12 min). Měřit; případně udělat refs opt-in přes config flag
  `[index] index_refs = true`.
- **USR stabilita** — USR jsou stabilní napříč TU, takže linkování funguje
  i cross-file. Ověřit na reálném volání modem funkce z jiného souboru.

### Ověření

- `find_callers("modem_parser_oob_init")` vrátí místa volání ve zbox kódu.
- Cross-file: volající v `src/`, definice v `lib/modem/`.

---

## Fáze 3 — Nová funkce: get_source (MEDIUM)

### Motivace

`explain_symbol` čte zdroják ale vrátí ho jen při vypnuté Ollamě. Agent často
chce přečíst skutečnou implementaci bez 10-30s Ollama overhead.

### Návrh

```python
@mcp.tool()
def get_source(name, project_root=None, context_lines=0) -> dict:
    """Vrať zdrojový kód definice symbolu (tělo + volitelný kontext)."""
    # reuse lookup logiku z explain_symbol (definice preferována)
    # vrať: name, kind, file (absolutní), line, signature, source
```

Refaktorovat společnou lookup+read logiku z `explain_symbol` do helperu
`_load_symbol_source(conn, name, config_hash, context_lines)`, sdílet mezi
`explain_symbol` a `get_source`.

### Riziko

- Určení konce definice — libclang `cursor.extent` dává přesný rozsah
  (start/end line). Použít `extent` místo fixního `context_lines` okna, ať
  `get_source` vrací celé tělo funkce, ne jen ±N řádků. Vyžaduje uložit
  `end_line` do `symbols` (nový sloupec + migrace) NEBO re-parse souboru při
  volání (pomalejší, ale bez migrace). **Rozhodnutí:** začít re-parse přístupem
  (jednodušší), `end_line` sloupec zvážit později.

---

## Fáze 4 — Kvalitativní vylepšení (MEDIUM/LOW)

### 4a. explain_symbol preferuje projektový kód

`server.py:651-665` — při výběru definice preferovat `src/`, `lib/` před
mbed-os (zbox bonus jako ve scoring `smart_search`). Přidat `ORDER BY` klauzuli
penalizující `file_path LIKE '%mbed-os%'`.

### 4b. Ollama keyword cache

`smart_search` volá Ollamu pro každý dotaz. Přidat jednoduchou cache
`(query, config_hash) → keyword_queries` (in-memory dict s TTL nebo malá
SQLite tabulka `llm_cache`). Šetří opakované dotazy.

### 4c. Staleness sémantika

`get_active_build` (server.py:169) hlásí `stale` jen podle compile_commands.json
mtime. Doplnit počet zdrojáků změněných od indexace (sample přes `files.mtime`)
do výstupu jako `modified_files_count`, ať agent ví, že je vhodný reindex.

---

## Fáze 5 — Drobnosti (LOW, volitelné)

- **5a.** `_signature` (symbols.py:88) nezahrnuje `const`/template kvalifikátory
  — použít `cursor.displayname` jako fallback rozšířit o const detekci.
- **5b.** `.fw-context/config.toml` se tiše vytváří v každém projektu při prvním
  `load_config` — zvážit lazy creation jen při explicitním `fw-context init`.
- **5c.** Phase 1 rough search dělá až 80 sekvenčních dotazů (fallback bez
  Ollamy) — batchovat do jednoho OR dotazu.

---

## Doporučené pořadí implementace

| Krok | Fáze | Náročnost | Hodnota |
|------|------|-----------|---------|
| 1 | Fáze 1 — path regrese | malá | 🔴 nutné |
| 2 | Fáze 3 — get_source | střední | 🟢 vysoká |
| 3 | Fáze 2 — references/call graph | velká | 🟢 nejvyšší |
| 4 | Fáze 4a — explain prefer projekt | malá | 🟡 střední |
| 5 | Fáze 4b — Ollama cache | střední | 🟡 střední |
| 6 | Fáze 4c, 5* | malá | 🔵 nízká |

Fáze 1 a 3 jsou rychlé výhry bez migrace schématu. Fáze 2 je hlavní feature,
vyžaduje migraci + měření objemu/času — implementovat jako samostatný commit
s opt-in flagem.

---

## Globální rizika a ověření

- **Migrace schématu** (Fáze 2): `open_db` migrace vzorem jako u `file_path` /
  `name_tokens` — idempotentní `CREATE TABLE IF NOT EXISTS` + `ALTER`.
- **Zpětná kompatibilita**: existující index bez refs tabulky → migrace ji
  vytvoří prázdnou; `find_references` vrátí prázdno dokud neproběhne reindex.
- **Testy**: ke každé fázi unit testy v `tests/` (db schema, extrakce, MCP
  tool výstup). Cíl: zachovat 96/96 + nové testy.
- **Reindex**: po Fázi 2 nutný plný reindex zbox-ecb-fw (~12 min) pro naplnění
  refs.
- **Dokumentace**: aktualizovat README (nové nástroje, schéma) a tento plán
  doplnit o výsledky po implementaci.

---

## Výsledky implementace (2026-06-03)

Status: **Implementováno** — všechny 4 fáze hotové, 111 testů prochází, ruff čistý.

| Fáze | Commit | Stav |
|------|--------|------|
| 1 — path regrese | `51c7bcc` | ✅ + odhalena a opravena druhá tichá regrese (`_stale_files` dostával relativní cesty → per-file staleness nefungoval) |
| 3 — get_source + 4a | `430a337` | ✅ brace-matching tělo, `_lookup_definition` preferuje projekt před mbed-os |
| 2 — call graph | `22d1cab` | ✅ `refs` tabulka, `extract_all`, `find_callers`/`find_references`, opt-in `index_refs` |
| 4b/4c — cache + staleness | `621778a` | ✅ Ollama keyword cache, `modified_files_count`/`reference_count` v get_active_build |
| README | `d077ccb` | ✅ |

### Klíčová zjištění

- **Druhá regrese z `3d686cc`:** `_stale_files` query `files.path` (absolutní),
  ale od odstranění JOINu dostával relativní `symbols.file_path` → lookup vždy
  None → per-file staleness varování tiše přestalo fungovat. Opraveno `_abs_path`.
- **Reference objem:** jeden TU (`wdt.cpp`) = 515 projekt-interních referencí
  (99 call). Filtr „oba konce v source_roots" drží index ohraničený.
- **Aktivace call grafu vyžaduje full reindex:** inkrementální indexace přeskočí
  nezměněné TU *před* extrakcí referencí, takže `index --refs` na aktuálním
  indexu nic nepřidá. Nutný `reset` + `index --refs`.

### Odložené (nízká priorita, neimplementováno)

- 5a `_signature` const/template kvalifikátory
- 5b lazy `.fw-context/config.toml` creation
- 5c batch Phase 1 rough search dotazů
