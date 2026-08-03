# Fix Plan — Comprehensive Review Fixes v2

**Větev:** `fix/comprehensive-review-fixes` → `main`
**Rozsah:** 194 souborů, +24 685 / −19 157 řádků
**Nálezy:** 35 (3 breaking, 3 vysoké, 13 středních, 16 nízkých)
**Princip:** Žádná backward kompatibilita — `fw-context` se instaluje jako celek, neexistují "staří klienti".
**Stav:** 28/35 ✅ dokončeno, 7/35 ⏳ zbývá

---

## Změny oproti v1

| # | Původní | Nový | Důvod |
|---|---------|------|-------|
| B1 | Obnovit `get_file_analysis` tool | Odstranit `file_analysis` tabulku | Tool byl záměrně odstraněn jako broken (commit `1b799bf`). Tabulka zůstala v schematu jako mrtvý kód. |
| B2 | Obnovit `project_only` v `get_symbol_context` | **VYŘAZENO** | Čistě backward-compat záležitost. Aktuální chování (ALL callers/callees) je správné. |
| B3 | `"stale"` klíč | Zachováno | Užitečná funkcionalita sama o sobě. |
| B4 | Connection cache | Zachováno | Korektnost/performance. |

---

## Priorita 1 — Breaking / cleanup (3/3 ✅)

### ✅ B1: Odstranit `file_analysis` tabulku ze schematu

- ~~**Soubor:** `src/fw_context_mcp/indexer/db/_schema.py:412-422` — odstranit `CREATE TABLE IF NOT EXISTS file_analysis`~~
- ~~**Soubor:** `src/fw_context_mcp/indexer/db/_schema.py:_run_data_migrations` — přidat `DROP TABLE IF EXISTS file_analysis`~~
- ~~**Soubor:** `src/fw_context_mcp/indexer/db/_projects.py:22` — aktualizovat komentář~~
- ~~**Test:** `tests/test_tool_coverage.py` — odstraněn blok query na `file_analysis`~~

### ✅ B3: Přidat `"stale"` klíč do `get_active_build`

- ~~**Soubor:** `src/fw_context_mcp/mcp/handlers/maintenance.py`~~
- ~~`header_affected_tus` se teď počítá z manifestu i při `fast=True`~~
- ~~`"stale"` = `needs_reindex or header_affected_tus > 0`~~

### ✅ B4: Opravit connection cache — `skip_integrity_check` + `check_same_thread`

- ~~**Soubor:** `src/fw_context_mcp/mcp/shared/connection.py:129`~~
- ~~`check_same_thread=False` + `skip_integrity_check=True` když `db_key` je v `_integrity_checked`~~

---

## Priorita 2 — Vysoká závažnost (3/3 ✅)

### ✅ H1: Opravit strukturální únik výjimek v embed retry větvi

- ~~**Soubor:** `src/fw_context_mcp/llm/ollama.py`~~
- ~~Vnitřní `except` rozšířen o `(httpx.HTTPError, OSError, KeyError, json.JSONDecodeError, ValueError)` → `OllamaError`~~
- ~~Druhý 404 → `OllamaModelNotFoundError`, ostatní HTTP statusy → `OllamaError`~~

### ✅ H2: `_try_pull` — vypisovat progress, zachovat blokující čekání

- ~~**Soubor:** `src/fw_context_mcp/llm/auto_model.py`~~
- ~~Parsování JSON řádků, tisk progressu, read timeout 300 s~~
- ~~`resolve_embed_model` přidán `cfg.enabled` check~~

### ✅ H3: Opravit `ESCAPE '\'` → `ESCAPE '\\'`

- ~~**Soubor:** `src/fw_context_mcp/indexer/db/_files.py:207`~~ — opraveno (`"""` regular string, `\\` → Python → `\`)
- **Soubory `_symbols.py`, `_lookup.py`** — neopraveno (raw stringy `r"""`, `'\'` je správně, `'\\'` by dalo 2 znaky v SQL)

---

## Priorita 3 — Střední závažnost (11/13 ✅, 2 ⏳)

### ✅ M1: Opravit `pkg_dir` v `_install_skills` a `_install_agents`

- ~~**Soubor:** `src/fw_context_mcp/cli/_init.py` (2 místa)~~
- ~~`Path(__file__).resolve().parent.parent` místo `Path(_pkg_init).parent`~~

### ✅ M2: Opravit refs dedup migraci — spustit pro existující DB

- ~~**Soubor:** `src/fw_context_mcp/indexer/db/_schema.py`~~
- ~~Dedup + CREATE INDEX přesunuty ven z SAVEPOINT, spouští se při každém `open_db()`~~
- ~~Kontrola `sqlite_master` pro `idx_refs_unique`~~

### ⏳ M3: Opravit rowid encoding změnu v `vec_symbols` — přidat migraci

- **Soubor:** `src/fw_context_mcp/indexer/db/_embeddings.py:221-235`
- **Odloženo:** vyžaduje manipulaci s vec0 virtual table (sqlite-vec extension)

### ✅ M4: Opravit BLOB embedding klíč v rough search

- ~~**Soubor:** `src/fw_context_mcp/search/phases/rough_search.py:169`~~
- ~~`ctx.config.llm.embed_key()` místo `ctx.config.llm.embed_model`~~

### ✅ M5: Sjednotit výjimky embedder backendů

- ~~**Soubory:** `embedder.py` (+ `EmbedderError`), `st_embedder.py` (wrap ST load), `embedder_factory.py` (wrap `get_embedder`)~~

### ✅ M6: Opravit ft:// BLOB klíče

- ~~**Soubory:** `embedder_factory.py` (+ `_ft_model_path`), `st_embedder.py` (používá `_ft_model_path` pro načítání)~~
- ~~`embed_model` zachovává původní `ft://...` string pro `embed_key()`~~

### ✅ M7: Opravit `trace_data_flow` — LIKE s `ESCAPE`

- ~~**Soubor:** `src/fw_context_mcp/mcp/handlers/callgraph.py:811`~~
- ~~`AND s.signature LIKE ? ESCAPE '\\'`~~

### ✅ M8: Opravit `search_content` LIKE fallback — `ESCAPE`

- ~~**Soubor:** `src/fw_context_mcp/mcp/handlers/search.py:622`~~
- ~~`f.content LIKE ? ESCAPE '\\'`~~

### ✅ M9: Opravit `check_pysqlite3` — testovat redirect, ne původní modul

- ~~**Soubor:** `src/fw_context_mcp/deps/_checks.py`~~
- ~~`import sqlite3; if "pysqlite3" in str(sqlite3.__file__):`~~

### ✅ M10: Opravit `check_libclang_so` — přeskočit `Path.exists()` pro holé soname

- ~~**Soubor:** `src/fw_context_mcp/deps/_checks.py:202`~~
- ~~`if lib_path and "/" in lib_path and Path(lib_path).exists():`~~

### ✅ M11a: Přidat `sqlite-ext` do `FIXABLE`

- ~~**Soubor:** `src/fw_context_mcp/deps/_fixes.py` — `"sqlite-ext": _fix_pip_install`~~

### ✅ M11b: Opravit `_pip_install` — instalovat do správného prostředí

- ~~**Soubor:** `src/fw_context_mcp/deps/_fixes.py:19`~~
- ~~`uv pip install --python {sys.executable} ...`~~

### ✅ M12: `_run_postprocess` — propagovat fatální chyby

- ~~**Soubor:** `src/fw_context_mcp/indexer/_postprocess.py`~~
- ~~Kritické kroky (fts5, embeddings) logují ERROR~~
- ~~`manifest_verification="partial"` místo `"full"` při selhání kritických kroků~~

### ⏳ M13: Mrtvý `_atomic_store_embeddings` — přidat prune, odstranit dead code

- **Soubor:** `src/fw_context_mcp/indexer/_embedding.py`
- **Odloženo:** rozsáhlý refaktoring embedding pipeline

---

## Priorita 4 — Nízká závažnost (11/16 ✅, 5 ⏳)

### ✅ L1: `run_fixes` nespouštět fix na crashed checky

- ~~**Soubor:** `src/fw_context_mcp/deps/__init__.py`~~
- ~~`if r.status in ("ok", "skipped", "error"): continue`~~

### ✅ L2: Zachovat criticality při crashnutí checku

- ~~**Soubor:** `src/fw_context_mcp/deps/__init__.py:97`~~
- ~~`critical=True` pro crashnuté checky (bezpečnější výchozí)~~

### ✅ L3: Odstranit mrtvý `sqlite_ext_ok` stav

- ~~**Soubor:** `src/fw_context_mcp/deps/__init__.py:89-90`~~
- ~~Odstraněn blok `if name == "sqlite-ext": ctx["sqlite_ext_ok"] = ...`~~

### ✅ L4: Invalidovat `_vec_available` cache po fixu

- ~~**Soubor:** `src/fw_context_mcp/deps/__init__.py`~~
- ~~Přidána `_reset_vec_cache()`, volaná po úspěšné reinstalaci sqlite-vec~~

### ✅ L5: Vynutit libclang ≥ 18.1.1

- ~~**Soubor:** `src/fw_context_mcp/deps/_checks.py`~~
- ~~`Version(ver) < Version("18.1.1")` → `degraded`~~

### ✅ L6: Rozšířit preflight skip pro `fw-context -v doctor`

- ~~**Soubor:** `src/fw_context_mcp/cli/__init__.py:58-62`~~
- ~~`not any(a in ("doctor", "version", "--version", "--help", "-h") for a in _argv)`~~

### ✅ L7: Zdokumentovat změnu `compute_content_hash` delimiteru

- ~~**Soubor:** `src/fw_context_mcp/utils.py:228`~~
- ~~Přidán komentář vysvětlující `\x1f` (Unit Separator) jako bezpečný delimiter~~

### ✅ L8: Zamezit mutaci globálního semaforu

- ~~**Soubor:** `src/fw_context_mcp/llm/ollama.py:52`~~
- ~~`if max_concurrent > _sem_value:` — semafor se nikdy nesnižuje~~

### ✅ L9: Odstranit mrtvý `_cached_gpu`

- ~~**Soubor:** `src/fw_context_mcp/llm/auto_model.py`~~
- ~~Odstraněny všechny reference (modulová proměnná, global, přiřazení v `_reset_auto_model_cache` i `resolve_embed_model`)~~

### ⏳ L10: Odstranit `with conn:` na cachovaných konekcích

- **Soubory:** `stale.py`, `variables.py`, `callgraph.py`, `maintenance.py`
- **Odloženo:** rozsáhlý refaktoring napříč 4+ soubory, read-only dotazy na cachovaných konekcích jsou bezpečné

### ✅ L11: Opravit leak konekce v `reset_index`

- ~~**Soubor:** `src/fw_context_mcp/mcp/handlers/maintenance.py`~~
- ~~`conn.close()` v `finally` bloku~~

### ⏳ L12: Konzistentní error handling v search toolech

- **Soubor:** `src/fw_context_mcp/mcp/handlers/search.py`
- **Není potřeba:** existující `try/except (sqlite3.Error, OSError, RuntimeError)` už chytá `RuntimeError`

### ⏳ L13: Invalidovat `_bm25_col_count_cache` po uzavření konekce

- **Soubor:** `src/fw_context_mcp/indexer/db/_symbols.py:43`
- **Nelze:** `sqlite3.Connection` v Python 3.14 nepodporuje weak reference

### ⏳ L14: Zdokumentovat změnu bm25 vah

- **Soubor:** `src/fw_context_mcp/indexer/db/_symbols.py:40`
- **Fix:** Jen dokumentace

### ⏳ L15: `_resolve_model_context_size` + circuit breaker pro LLM analýzu a embeddingy

- **Soubory:** `_llm_analysis.py`, `_embedding.py`
- **Odloženo:** netriviální změna logiky LLM pipeline

### ✅ L16: Už pokryto v B4 (`check_same_thread=False`)

---

## Souhrnná tabulka

| Priorita | Celkem | ✅ Hotovo | ⏳ Zbývá |
|----------|--------|-----------|----------|
| P1 (Breaking) | 3 | 3 | 0 |
| P2 (High) | 3 | 3 | 0 |
| P3 (Medium) | 13 | 11 | 2 (M3, M13) |
| P4 (Low) | 16 | 11 | 5 (L10, L12, L13, L14, L15) |
| **Celkem** | **35** | **28** | **7** |

---

## Testy k přidání

| Nález | Test | Stav |
|-------|------|------|
| B3 | `test_get_active_build_stale_key` | ⏳ |
| H1 | `test_embed_retry_connect_error_fallback` | ⏳ |
| H3 | `test_get_file_map_fuzzy_fallback` | ⏳ |
| M1 | `test_init_installs_skills_and_agents` | ⏳ |
| M2 | `test_refs_dedup_migration_idempotent` | ⏳ |
| M7 | `test_trace_data_flow_underscore_type` | ⏳ |
| M8 | `test_search_content_like_underscore` | ⏳ |
| M9 | `test_check_pysqlite3_detects_missing_redirect` | ⏳ |

---

## Harmonogram

1. ~~**Blok 1 (P1):** B1, B3, B4 — cleanup + API fixy~~ ✅
2. ~~**Blok 2 (P2):** H1, H2, H3 — vysoká závažnost~~ ✅
3. ~~**Blok 3 (P3):** M1, M2, M4–M12 — střední závažnost~~ ✅ (kromě M3, M13)
4. ~~**Blok 4 (P4):** L1–L9, L11, L16 — nízká závažnost~~ ✅ (kromě L10, L12–L15)
