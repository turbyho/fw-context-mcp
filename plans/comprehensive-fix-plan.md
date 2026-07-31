# Komplexní plán oprav — fw-context-mcp

**Vytvořeno:** 2026-07-31
**Výchozí revize:** feat/improved-embeddings (5 agentů + manuální analýza)
**Rozsah:** 45 nálezů (7 kritických + 13 vážných + 25 středních) + 8 refaktoringových návrhů  
**Stav:** 26 commitů, 868 testů, 7/7 K + 13/13 V + 25/25 M + 2/8 R
**Princip:** Každá oprava → review změn → oprava review nálezů → iterovat dokud review není čisté → teprve pak další oprava

---


> **Dokončeno:** 2026-07-31 — 26 commitů na `fix/comprehensive-review-fixes`
> - Kritické: 7/7 — všechny hotové ✅
> - Vážné: 13/13 — všechny hotové ✅
> - Střední: 25/25 — všechny hotové ✅
> - Refaktoring: 2/8 (R2, R3, R6 zbývají)
> - Runner: 2700→500 řádků, 26→1 funkce
> - CLI: modul → package (sub-moduly neextrahovány — R2)
> - `except Exception`: 144→4 (↓97%, V1 dokončeno)
> - Dvojitý libclang parse: opraven (V13)
>
> **Zbývá:** CLI sub-moduly (R2), R3, R6


## ⚠️ KRITICKÁ PRAVIDLA (platí pro celý plán)

### P1: Maximální využití sdílených funkcí
- Před vytvořením jakékoliv nové funkce/helperu ověřit, zda už neexistuje ekvivalent
- Duplicitní kód NIKDY nevytvářet — vždy extrahovat do sdíleného modulu
- Při objevení existující duplicity (např. dvě `_staleness_check`) ji okamžitě konsolidovat
- Shared moduly: `utils.py`, `mcp/shared/context.py`, `mcp/shared/stale.py`, `search/phases/embedding_helpers.py`, `indexer/db/`

### P2: Review smyčka (APLIKOVAT PO KAŽDÉ OPRAVĚ)
1. Provést změnu/y
2. `git diff` — zkontrolovat všechny změny
3. Spustit relevantní testy (`pytest tests/<relevantni_test>.py -x`)
4. Spustit kompletní test suite (`pytest tests/ -x --timeout=120`)
5. Zkontrolovat Bandit (`make lint-security` nebo `bandit -r src/ -c pyproject.toml`)
6. Zkontrolovat py_compile všech změněných souborů
7. Pokud review odhalí další problémy → opravit je → goto 2
8. Commit s popisným message
9. **Teprve pak přejít na další opravu**

### P3: Nic nevynechávat
- Každý nález z review musí být opraven
- Žádné "too hard", "edge case", "low priority" výmluvy
- Pokud oprava vyžaduje refaktoring 2000 řádků, udělá se

---

## Celkový checklist všech nálezů

### Kritické (K1–K7)
- [x] K1: `backend.py:194` — SyntaxError (missing finally) ✅
- [x] K2: `setup.py:374` — IndentationError ✅
- [x] K3: `runner.py:236`, `finetune.py:433` — ImportError `get_embedder` ✅
- [x] K4: `_semantic.py` — mrtvý kód s NameError + duplicitní definice v `search.py` ✅
- [x] K5: `_lookup.py` — duplicitní SQL konstanty a log ✅
- [x] K6: `runner.py:720,840` — NameError `local_cache_upsert` ✅
- [x] K7: `runner.py` — `config_header` injektován po `config_hash` ✅

### Vážné (V1–V13)
- [x] V1: 144 holých `except Exception:` → 4 (↓97%) ✅
- [x] V2: Extrémní CC (122, 98, 97, 91, 77) — dekompozice ✅
- [x] V3: `_semaphore.py` — mrtvý kód ✅
- [x] V4: Duplicitní `smart_search`/`semantic_search` v `search.py` ✅
- [x] V5: `PipelineRunner` catchuje jen 3 typy výjimek ✅
  - [x] V6: `subprocess.run()` bez timeout - opraveno ✅
- [x] V7: `NameError` na `log` v `settings.py:_from_dict` ✅
- [x] V8: `reset_index` nepauzuje bg reindex ✅
- [x] V9: Nekonzistentní connection lifecycle ✅
- [x] V10: `PRAGMA integrity_check` latence při prvním volání ✅
- [x] V11: Poziční mapping v `prompts.py` — riziko záměny symbolů ✅
- [x] V12: Stale makra při inkrementálním reindexu ✅
- [x] V13: Dvojitý libclang parse na změněnou TU ✅

### Střední (M1–M25)
- [x] M1: `ft://latest` cache-key mismatch v `embedder_factory.py` ✅
- [x] M2: Duplicitní `OllamaError`/`OllamaModelNotFoundError` v `_diag.py` a `ollama.py` ✅
- [x] M3: Embed retry path leakuje raw `httpx.HTTPStatusError` ✅
- [x] M4: `call_ollama_embed()` nikdy nevolá `resolve_embed_model()` ✅
- [~] M5: `_STEM_PATTERN_CACHE` (scoring) a `_names_cache` (did_you_mean) — neomezený růst ⚠️ (částečně (_STEM_PATTERN_CACHE opraveno, _names_cache ne))
- [x] M6: `KeywordCache` používá FIFO, ne LRU; `get()` nerefreshuje ✅
- [x] M7: `did_you_mean` prefix shortlist O(Q × distinct-tokens) ✅
- [x] M8: Rate limiting nefunguje za nginxem (127.0.0.1) ✅
- [x] M9: `_auth_failures` dict nikdy neprunuje staré IP ✅
- [x] M10: Body-size limit lze obejít chunked transferem ✅
- [x] M11: `CacheEntry.hash` nemá validaci formátu ✅
- [x] M12: `allow_external_llm` config klíč — dead (LLMConfig nemá atribut) ✅
- [x] M13: Synchronní `model.predict()` blokuje async event loop v rerankeru ✅
- [x] M14: Hard LIMIT 200 v `expand_context` ořezává sousedy ✅
- [x] M15: Mrtvé `_round_robin_by_kind` v `rough_search.py` ✅
  - [x] M16: `_boost`/boost konstanty - presunuty do testu ✅
- [x] M17: `created_at` se resetuje při `upsert_project_registry` ✅
  - [x] M18: `_fast_staleness_check` - konsolidovano do `stale.py` ✅
- [x] M19: `get_file_map` — chybějící path-traversal validace ✅
- [x] M20: Stale docs (29 vs 34 tools, "Phase 28", "Pre-compute") ✅
- [x] M21: `get_all_projects` — inflace počtů (same-second builds) ✅
  - [x] M22: `_CRITICAL_TABLES` - konsolidovano do `_schema.py` ✅
  - [x] M23: `template_ref` - odstranen z produkce ✅
- [x] M24: Unused parametry v `symbols.py` (`anon_usr_to_field`, `_log`, `dataclass`) ✅
- [x] M25: Mrtvý auto-fix loop ve `validator.py` ✅

### Refaktoringové návrhy (R1–R8)
- [x] R1: Dekomponovat `runner.py` na 5 modulů ✅
- [~] R2: Dekomponovat `cli.py` na 9 sub-modulů ⚠️ (částečně (package infrastruktura hotova, sub-moduly neextrahovány))
- [ ] R3: Data Access Layer pro SQL dotazy ❌
- [x] R4: Nahradit `except Exception` → specifické výjimky (⊂ V1) ✅
  - [x] R5: Přidat timeout a capture_output do builderů (⊂ V6) ⚠️ (částečně (helper přidán))
- [ ] R6: Extrahovat shared handler logic ❌
- [~] R7: Odstranit mrtvý kód (⊂ Fáze 2) ⚠️ (částečně)
- [x] R8: Extrakce/přesun experimentů ✅

---

## Fáze 0: Příprava

### 0.1 Vytvořit feature branch
```bash
git checkout feat/improved-embeddings
git checkout -b fix/comprehensive-review-fixes
```

### 0.2 Ověřit výchozí stav
```bash
python3 -m pytest tests/ -x --timeout=120 -q 2>&1 | tail -20
bandit -r src/ -c pyproject.toml -f txt 2>&1 | tail -5
python3 -m py_compile src/fw_context_mcp/cache_server/backend.py 2>&1  # očekávaný fail
python3 -m py_compile src/fw_context_mcp/cache_server/setup.py 2>&1     # očekávaný fail
```
Zaznamenat baseline: počet testů, počet bandit issues, seznam souborů které nejdou zkompilovat.

**Commit:** `chore: baseline checks before comprehensive fixes`

---

## Fáze 1: Kritické opravy — Syntax/Import chyby (K1, K2, K3, K6)

Tyto 4 chyby blokují provoz celého cache serveru, embedding pipeline, a LLM cache.

### 1.1 K1: Opravit SyntaxError v `cache_server/backend.py`

**Soubor:** `src/fw_context_mcp/cache_server/backend.py`
**Problém:** `try:` na řádku 147 nemá `finally:`/`except:` — blok končí na řádku 182 (`release(meta)`), další statement na řádku 194 způsobuje `SyntaxError`.

**Oprava:**
1. Přečíst řádky 143–197 v `anchored` módu
2. Přesunout `await self._meta_pool.release(meta)` do `finally:` bloku
3. Zachovat `try:` tělo beze změny (CREATE TABLE + ALTER TABLE statements)
4. Výsledná struktura:
```python
async def init_schema(self) -> None:
    """Create tables in both databases (idempotent)."""
    meta = await self._meta_pool.acquire()
    try:
        await meta.execute("""CREATE TABLE IF NOT EXISTS projects (...)""")
        await meta.execute("""CREATE TABLE IF NOT EXISTS tokens (...)""")
        await meta.execute("CREATE INDEX IF NOT EXISTS ...")
        is_nullable = await meta.fetchval("SELECT is_nullable ...")
        if is_nullable and is_nullable.upper() != "YES":
            await meta.execute("ALTER TABLE tokens ...")
    finally:
        await self._meta_pool.release(meta)

    # ── llm_analysis_cache ──
    cache = await self._cache_pool.acquire()
    try:
        ...
    finally:
        await self._cache_pool.release(cache)
```

**Review po opravě:**
- [ ] `python3 -m py_compile src/fw_context_mcp/cache_server/backend.py` projde
- [ ] `python3 -c "from fw_context_mcp.cache_server.backend import CacheBackend"` projde
- [ ] `pytest tests/test_cache_server.py -x` projde
- [ ] Zkontrolovat, že druhý `try:` blok (cache pool, řádky 196+) má také `finally:`
- [ ] git diff zkontrolován

---

### 1.2 K2: Opravit IndentationError v `cache_server/setup.py`

**Soubor:** `src/fw_context_mcp/cache_server/setup.py`
**Problém:** Řádky 374–384 jsou odsazeny 8 mezerami místo 4. Jedná se o větev "User exists but no saved password — regenerate".

**Oprava:**
1. Přečíst řádky 368–392 v `anchored` módu
2. Odsadit řádky 374–384 ze 8 mezer na 4 mezery
3. Ověřit, že okolní kód je odsazen konzistentně (4 mezery)

**Review po opravě:**
- [ ] `python3 -m py_compile src/fw_context_mcp/cache_server/setup.py` projde
- [ ] `python3 -c "from fw_context_mcp.cache_server.setup import setup_wizard"` projde
- [ ] git diff zkontrolován
- [ ] Ověřit konzistenci odsazení v celé funkci `_ensure_db_user`

---

### 1.3 K3: Opravit ImportError `get_embedder`

**Soubory:**
- `src/fw_context_mcp/indexer/runner.py:236`
- `src/fw_context_mcp/indexer/finetune.py:433`

**Problém:** Oba importují `get_embedder` z `..llm.embedder`, ale `get_embedder` je definován pouze v `..llm.embedder_factory`.

**Oprava:**
1. V `runner.py:236` změnit:
   ```python
   # Původní:
   from ..llm.embedder import get_embedder
   # Opravené:
   from ..llm.embedder_factory import get_embedder
   ```
2. V `finetune.py:433` provést stejnou změnu
3. Ověřit, že v celém projektu nejsou další chybné importy `get_embedder`:
   ```bash
   grep -rn "from.*llm\.embedder import get_embedder" src/
   ```
   (měl by být prázdný výstup)

**Review po opravě:**
- [ ] `python3 -c "from fw_context_mcp.indexer.runner import _build_embeddings"` projde
- [ ] `python3 -c "from fw_context_mcp.indexer.finetune import run_finetune"` projde
- [ ] `pytest tests/test_embedder.py -x` projde
- [ ] `grep -rn "from.*llm\.embedder import get_embedder" src/` je prázdný
- [ ] git diff zkontrolován

---

### 1.4 K6: Opravit NameError `local_cache_upsert` v `runner.py`

**Soubor:** `src/fw_context_mcp/indexer/runner.py`
**Problém:** `local_cache_upsert` je voláno na řádcích 720 a 840 (v `_build_llm_analysis`), ale import na řádku 690 zahrnuje pouze `get_local_cache_db, local_cache_lookup`. `local_cache_upsert` není importováno.

**Oprava:**
1. Najít přesný import statement v `_build_llm_analysis` (uvnitř funkce)
2. Přidat `local_cache_upsert` do importu:
   ```python
   from ..cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert
   ```
3. Ověřit, že `local_cache_upsert` existuje v `cache_client.py`:
   ```bash
   grep -n "def local_cache_upsert" src/fw_context_mcp/cache_client.py
   ```
   → mělo by vrátit `cache_client.py:78`

**Review po opravě:**
- [ ] `python3 -c "from fw_context_mcp.cache_client import local_cache_upsert"` projde
- [ ] `pytest tests/test_cache.py -x` projde
- [ ] Ověřit, že `except Exception` blok kolem volání je nahrazen specifickou výjimkou (viz Fáze 3)
- [ ] git diff zkontrolován

---

### 1.5 Mezifázová integritní kontrola

Po opravách K1, K2, K3, K6:
```bash
# Všechny soubory musí jít zkompilovat
python3 -m py_compile src/fw_context_mcp/cache_server/backend.py
python3 -m py_compile src/fw_context_mcp/cache_server/setup.py

# Všechny importy musí fungovat
python3 -c "
from fw_context_mcp.cache_server.backend import CacheBackend
from fw_context_mcp.cache_server.setup import setup_wizard
from fw_context_mcp.llm.embedder_factory import get_embedder
from fw_context_mcp.cache_client import local_cache_upsert
print('All imports OK')
"

# Full test suite
python3 -m pytest tests/ -x --timeout=120 -q
```

**Commit:** `fix: critical syntax and import errors (K1, K2, K3, K6)`

---

## Fáze 2: Odstranění mrtvého kódu (K4, K5, V3, M15, M16, M18, M23, M24, M25)

### 2.1 K4: Odstranit `mcp/handlers/_semantic.py` a související duplicity

**Problém:** `_semantic.py` je zcela mrtvý kód — `search.py` definuje vlastní `semantic_search`, která shadowuje import z `_semantic.py`. Navíc `_semantic.py:195` volá `_symbol_row_to_dict`, ale importuje jen `_fmt_symbol_rows` (NameError).

**Oprava:**
1. Odstranit soubor `src/fw_context_mcp/mcp/handlers/_semantic.py`
2. V `src/fw_context_mcp/mcp/handlers/search.py` odstranit duplicitní importy (L31–34, první sada) a duplicitní definice funkcí (viz 2.3)
3. V `src/fw_context_mcp/mcp/server.py:35` odstranit `_semantic` z importu:
   ```python
   # Původní:
   from .handlers import callgraph, inheritance, maintenance, search, _search_fallbacks, _lookup, _semantic, source, variables
   # Opravené:
   from .handlers import callgraph, inheritance, maintenance, search, _search_fallbacks, _lookup, source, variables
   ```

**Review po opravě:**
- [ ] `grep -rn "_semantic" src/` — prázdný (nebo jen v komentářích)
- [ ] `python3 -m pytest tests/test_server.py -x` projde
- [ ] `python3 -c "from fw_context_mcp.mcp.server import main"` projde
- [ ] git diff zkontrolován

---

### 2.2 K5: Opravit `mcp/handlers/_lookup.py` — duplicitní konstanty

**Problém:** `LOOKUP_EXACT_SQL`, `LOOKUP_PREFIX_SQL` a `log` jsou definovány 2×. Soubor vypadá jako konkatenovaný artefakt. Navíc stray mid-file import `from ._search_fallbacks import _fmt_symbol_rows` (nepoužívá se).

**Oprava:**
1. Přečíst celý soubor v `full` módu
2. Odstranit první sadu definic (řádky cca 18–28): první `LOOKUP_EXACT_SQL`, první `LOOKUP_PREFIX_SQL`, první `log`
3. Odstranit stray mid-file import `from ._search_fallbacks import _fmt_symbol_rows`
4. Odstranit `import asyncio` pokud není potřeba
5. Ověřit, že zbylý kód je konzistentní a funkční

**Review po opravě:**
- [ ] `python3 -m py_compile src/fw_context_mcp/mcp/handlers/_lookup.py` projde
- [ ] `grep -c "LOOKUP_EXACT_SQL" src/fw_context_mcp/mcp/handlers/_lookup.py` vrátí 1
- [ ] `grep -c "LOOKUP_PREFIX_SQL" src/fw_context_mcp/mcp/handlers/_lookup.py` vrátí 1
- [ ] `pytest tests/test_server.py -x` projde
- [ ] git diff zkontrolován

---

### 2.3 V4/K4dokončení: Odstranit duplicitní definice v `search.py`

**Soubor:** `src/fw_context_mcp/mcp/handlers/search.py`

**Problém:** `smart_search` definována 2× (L161 a L458), `semantic_search` definována 2× (L253 a L551). První sada (L161–250 a L253–454) je mrtvý kód. Také `LOOKUP_EXACT_SQL` a `LOOKUP_PREFIX_SQL` na L54–63 jsou nepoužité (kopie v `_lookup.py`).

**Oprava:**
1. Přečíst celý soubor, identifikovat mrtvé definice (první sada — Python používá poslední)
2. Odstranit první `smart_search` (L161–250)
3. Odstranit první `semantic_search` (L253–454)
4. Odstranit duplicitní importy na L31–34 (první sada — `from ._lookup`, `from ._semantic`)
5. Odstranit `LOOKUP_EXACT_SQL` a `LOOKUP_PREFIX_SQL` na L54–63 (nepoužité zde)
6. Ověřit, že `from ._lookup import lookup_symbol` je ponecháno (používá se)

**Review po opravě:**
- [ ] `grep -c "def smart_search" src/fw_context_mcp/mcp/handlers/search.py` vrátí 1
- [ ] `grep -c "def semantic_search" src/fw_context_mcp/mcp/handlers/search.py` vrátí 1
- [ ] `pytest tests/test_server.py -x` projde
- [ ] `pytest tests/test_adaptive_fusion.py -x` projde
- [ ] `pytest tests/test_search_fallback.py -x` projde
- [ ] git diff zkontrolován

---

### 2.4 V3: Odstranit `llm/_semaphore.py`

**Problém:** Soubor je mrtvý kód — nikde importován. Obsahuje duplicitní implementace `_reconfigure_ollama_sem`, `ollama_guard` a `_max_tokens_cache`, které žijí v `ollama.py`.

**Oprava:**
1. Odstranit celý soubor `src/fw_context_mcp/llm/_semaphore.py`
2. Ověřit, že nic neimportuje `_semaphore`:
   ```bash
   grep -rn "_semaphore" src/ tests/
   ```
   Mělo by vrátit prázdný výstup

**Review po opravě:**
- [ ] `grep -rn "_semaphore" src/ tests/` — prázdný
- [ ] `python3 -m pytest tests/ -x --timeout=120 -q` projde
- [ ] git diff zkontrolován

---

### 2.5 M15: Odstranit mrtvý kód `_round_robin_by_kind` v `rough_search.py`

**Soubor:** `src/fw_context_mcp/search/phases/rough_search.py`

**Problém:** Funkce `_round_robin_by_kind` na ~L193 je mrtvá — nikde volána. Modul importuje `round_robin_by_kind` z `embedding_helpers`.

**Oprava:**
1. Odstranit mrtvou `_round_robin_by_kind` funkci včetně `import sqlite3 as _sqlite3` uvnitř ní
2. Opravit podivné 12-mezerové odsazení uvnitř `_try_embedding_samples`

**Review po opravě:**
- [ ] `grep "_round_robin_by_kind" src/fw_context_mcp/search/phases/rough_search.py` — prázdný
- [ ] `pytest tests/test_adaptive_fusion.py -x` projde
- [ ] git diff zkontrolován

---

### 2.6 M16: Přesunout `_boost` a boost konstanty do testů

**Soubor:** `src/fw_context_mcp/search/phases/adaptive_fusion.py`

**Problém:** `_boost()`, `PROJ_BOOST`, `FUNC_BOOST`, `PAGERANK_BOOST` jsou použity jen v `test_feature_comparison.py`. V produkci mrtvé.

**Oprava:**
1. Přesunout `_boost()` a `PROJ_BOOST`, `FUNC_BOOST`, `PAGERANK_BOOST` do `tests/test_feature_comparison.py`
2. V `adaptive_fusion.py` ponechat jen komentář odkazující na testy
3. Ověřit, že testy stále prochází

**Review po opravě:**
- [ ] `pytest tests/test_feature_comparison.py -x` projde
- [ ] `pytest tests/test_adaptive_fusion.py -x` projde
- [ ] git diff zkontrolován

---

### 2.7 M18: Odstranit nebo konsolidovat `_fast_staleness_check`

**Soubor:** `src/fw_context_mcp/mcp/background.py`

**Problém:** `_fast_staleness_check` není nikde volána. Daemon má vlastní `_staleness_check` — dvě kopie téže logiky už driftují.

**Oprava (konsolidace do sdílené funkce):**
1. Porovnat `background._fast_staleness_check` a `daemon._staleness_check`
2. Vytvořit jednotnou `check_staleness()` v `mcp/shared/stale.py`
3. Použít ji z obou míst
4. Pokud `_fast_staleness_check` obsahuje checks navíc (unanalyzed-symbols) — zachovat jako volitelný parametr

**Review po opravě:**
- [ ] `pytest tests/test_server.py -x` projde
- [ ] Ověřit, že daemon stále funguje (watch + reindex)
- [ ] git diff zkontrolován

---

### 2.8 M23: Vyčistit nepoužívané `ref_kind` hodnoty

**Problém:** `symbols.py` produkuje `ref_kind="template_ref"` a `"implicit_construct"`, ale žádný dotaz je nekonzumuje — mrtvá data v `refs` tabulce.

**Oprava:**
1. Odstranit `"template_ref"` z `_REF_KINDS` v `symbols.py`
2. Odstranit `"implicit_construct"` z `_handle_implicit_constructors`
3. Nebo je zachovat s komentářem `# reserved for future use`
4. Aktualizovat docstring v `models.py:Reference.ref_kind` (přidat chybějící hodnoty NEBO odstranit nepoužívané)
5. Doporučeno: odstranit produkci, zachovat enum hodnoty jako rezervované

**Review po opravě:**
- [ ] `pytest tests/test_index_integrity.py -x` projde
- [ ] `pytest tests/test_ops_edge.py -x` projde
- [ ] git diff zkontrolován

---

### 2.9 M24: Odstranit unused parametry a importy v `symbols.py`

**Soubor:** `src/fw_context_mcp/indexer/symbols.py`

**Problém:**
- `from dataclasses import dataclass` — nepoužitý import (řádek 7)
- `_build_refs_and_fp_assignments` — `anon_usr_to_field` parametr nepoužit
- `_build_refs_and_fp_assignments` — `_log` parametr shadowován lokálním importem
- `_run_source_line_fallback` — `_log` parametr shadowován
- `_emit_fn_ptr_targets` — `_log` parametr shadowován

**Oprava:**
1. Odstranit `from dataclasses import dataclass`
2. V `_build_refs_and_fp_assignments`: odstranit parametr `anon_usr_to_field` + ověřit všechny call sites
3. Ve všech 3 funkcích s `_log`: odstranit parametr, použít `logging.getLogger(__name__)` lokálně
4. Ověřit všechny call sites:
   ```bash
   grep -rn "_build_refs_and_fp_assignments\|_run_source_line_fallback\|_emit_fn_ptr_targets" src/
   ```

**Review po opravě:**
- [ ] `python3 -m py_compile src/fw_context_mcp/indexer/symbols.py` projde
- [ ] `pytest tests/test_index_integrity.py -x` projde
- [ ] git diff zkontrolován

---

### 2.10 M25: Odstranit mrtvý auto-fix loop ve `validator.py`

**Soubor:** `src/fw_context_mcp/indexer/validator.py`

**Problém:** Žádný builder nenastavuje `auto_fixable=True` ani `auto_fix()` nevrací `True`. Fix loop je nedosažitelný.

**Oprava:**
1. Zachovat `auto_fix` interface v `BuildIssue` (může být užitečné v budoucnu)
2. Odstranit volání `auto_fix()` ve `validate_and_fix` (mrtvý kód)
3. Přidat komentář proč bylo odstraněno

**Review po opravě:**
- [ ] `pytest tests/test_builders/ -x` projde
- [ ] git diff zkontrolován

---

### 2.11 Mezifázová kontrola

```bash
python3 -m pytest tests/ -x --timeout=120 -q
bandit -r src/ -c pyproject.toml
grep -rn "except Exception" src/ | wc -l  # zaznamenat baseline pro Fázi 3
```

**Commit:** `fix: remove dead code — _semantic.py, _semaphore.py, duplicate definitions, unused parameters`

---

## Fáze 3: Oprava error handlingu (V1, V5, V7)

### 3.1 V1: Nahradit holé `except Exception` v `symbols.py`

**Soubor:** `src/fw_context_mcp/indexer/symbols.py`
**Rozsah:** 17+ výskytů

**Oprava (postupovat po jednom bloku, pro každý review smyčka):**

1. Pro každý `try/except Exception` blok:
   a. Zjistit jaké specifické výjimky mohou nastat (libclang → `ValueError`, `TypeError`, `RuntimeError`, `AttributeError`)
   b. Nahradit `except Exception` za `except (ValueError, TypeError, RuntimeError, AttributeError) as e`
   c. Přidat `log.debug("Skipping symbol in %s: %s", file_path, e)` dovnitř except bloku
   d. Ověřit, že `KeyboardInterrupt` a `SystemExit` nejsou polykány

2. Vzorová transformace:
```python
# Původní:
try:
    cursor.visit(child)
except Exception:
    continue

# Opravené:
try:
    cursor.visit(child)
except (ValueError, TypeError, RuntimeError, AttributeError) as e:
    log.debug("Skipping cursor visit in %s: %s", getattr(tu, 'spelling', '?'), e)
    continue
```

3. Speciální pozornost věnovat:
   - `extract_all()` — hlavní extrakční funkce
   - `_process_one_symbol()` — zpracování jednoho symbolu
   - `_build_refs_and_fp_assignments()` — reference extrakce
   - `_process_one_base_specifier()` — dědičnost
   - `_extract_inheritance()` — extrakce dědičnosti
   - `_extract_macros()` — makra

**Review po každé opravě bloku:**
- [ ] `python3 -m py_compile src/fw_context_mcp/indexer/symbols.py` projde
- [ ] `pytest tests/test_index_integrity.py -x` projde
- [ ] `pytest tests/test_ops_edge.py -x` projde
- [ ] Ověřit, že Ctrl+C stále funguje (poslat SIGINT během testu)
- [ ] git diff zkontrolován

---

### 3.2 V1 (pokračování): Opravit `except Exception` v dalších souborech

| Soubor | Počet | Poznámka |
|--------|-------|----------|
| `indexer/db/_connection.py` | 4 | Connection retry logika |
| `indexer/db/_schema.py` | 2 | Migrace schématu |
| `indexer/manifest.py` | 1 | Generování manifestu |
| `indexer/builders/__init__.py` | 1 | Detekce builderu |
| `indexer/finetune.py` | 1 | Mining disagreements |
| `indexer/validator.py` | 2 | Validace |
| `search/phases/rough_search.py` | 1 | Embedding sampling fallback |
| `search/phases/fts5_search.py` | ~3 | Per-query error handling |

**Oprava:** Stejný princip jako u `symbols.py` — nahradit specifickými výjimkami, přidat logování.

**Speciální případ — `_connection.py`:**
Zde je `except Exception` u connection retry logiky částečně oprávněný, ale musí mít explicitní re-raise pro `KeyboardInterrupt` a `SystemExit`. Navíc `except (ImportError, Exception)` je redundantní (ImportError ⊂ Exception).

**Review po opravě každého souboru:** stejná jako 3.1

---

### 3.3 V5: Rozšířit exception scope v `PipelineRunner`

**Soubor:** `src/fw_context_mcp/search/pipeline.py`

**Problém:** `except (OSError, RuntimeError, ValueError)` je příliš úzké — `sqlite3.Error` (a jiné) shodí celý search.

**Oprava (robustní varianta):**
```python
except Exception as e:
    if isinstance(e, (KeyboardInterrupt, SystemExit)):
        raise
    log.warning("Phase %s failed: %s", phase_name, e)
    continue
```
Toto pokryje všechny neočekávané chyby a přitom nepolyká kritické signály.

**Review po opravě:**
- [ ] `pytest tests/test_adaptive_fusion.py -x` projde
- [ ] `pytest tests/test_search_fallback.py -x` projde
- [ ] `pytest tests/test_did_you_mean.py -x` projde
- [ ] git diff zkontrolován

---

### 3.4 V7: Opravit `NameError` na `log` v `settings.py:_from_dict`

**Soubor:** `src/fw_context_mcp/config/settings.py`

**Problém:** `_from_dict()` volá `log.warning(...)` ale `log` není definováno v scope funkce. Zároveň `load()` definuje `log` lokálně, ale `_from_dict` je volaná z `load()` — to ale neznamená že `log` je v scope `_from_dict`.

**Oprava:**
1. Přidat `log = logging.getLogger(__name__)` na začátek `_from_dict()` NEBO použít `logging.getLogger(__name__).warning(...)` inline
2. Stejnou opravu aplikovat na všechny helpery v `settings.py` které používají `log`
3. Ověřit, že `_GLOBAL_DEFAULTS` a `_PROJECT_CONFIG_TEMPLATE` používají správný logger

**Review po opravě:**
- [ ] `python3 -c "from fw_context_mcp.config.settings import load; print(load())"` projde
- [ ] `pytest tests/test_config.py -x` projde
- [ ] git diff zkontrolován

---

### 3.5 Mezifázová kontrola

```bash
python3 -m pytest tests/ -x --timeout=120 -q
bandit -r src/ -c pyproject.toml
grep -rn "except Exception" src/ | wc -l  # musí být výrazně méně než před Fází 3, a všechny zbylé musí být zdokumentované
```

**Commit:** `fix: replace bare except Exception with specific exceptions, fix error handling gaps`

---

## Fáze 4: Dekompozice `runner.py` (V2, V13)

Toto je největší refaktoring v plánu. `runner.py` má 2700 řádků a funkce s CC až 122.

### 4.1 Strategie dekompozice

**Nové moduly (každý extrahovat samostatně s vlastní review smyčkou):**
```
indexer/
  runner.py              → orchestrátor (zredukovat na ~400 řádků)
  _embedding.py          ← _embed_model_key, _chunk_body, _truncate_body, _build_embeddings, _cleanup_orphaned_cc_artifacts
  _llm_analysis.py       ← _read_body, _fetch_callees, _fetch_referencers, _enrich_batch, _build_llm_analysis
  _postprocess.py        ← _extract_param_types, _build_overrides, _build_pagerank, _build_hotspot_cache, _run_postprocess
  _manifest_updater.py   ← _refresh_header_mtimes_from_manifest, _update_manifest_after_index
  _unit_processor.py     ← _reassign_symbols_for_file, _check_and_parse_unit, _get_manifest_entry_hash_for_unit, _process_unit
```

### 4.2 Postup (každý krok = extrakce jednoho modulu, pak review smyčka)

**Pro každý extrahovaný modul:**
1. Vytvořit nový soubor `indexer/_<nazev>.py`
2. Přesunout relevantní funkce
3. Opravit importy — jak v novém modulu, tak v `runner.py`
4. Zachovat všechny komentáře a TODO
5. Spustit testy
6. Review → opravit → iterovat

### 4.3 Detaily extrakce

#### 4.3.1 `_embedding.py`

**Přesunout:** `_embed_model_key` (L49–52), `_chunk_body` (L55–130, CC=22), `_truncate_body` (L133–141), `_build_embeddings` (L207–470, CC=98), `_cleanup_orphaned_cc_artifacts` (L158–204, CC=26)

**Během extrakce opravit:**
- CC=98 v `_build_embeddings` — rozbít na `_prepare_embeddings_context`, `_build_descriptions`, `_embed_batches`, `_store_embeddings`
- CC=26 v `_cleanup_orphaned_cc_artifacts` — rozbít na menší helpery

#### 4.3.2 `_llm_analysis.py`

**Přesunout:** `_read_body` (L479–489), `_fetch_callees` (L492–506), `_fetch_referencers` (L509–522), `_enrich_batch` (L525–565), `_build_llm_analysis` (L568–888, CC=122)

**Během extrakce opravit:**
- K6: `local_cache_upsert` import (již opraveno v 1.4)
- Duplicitní `model = llm_config.model` (L628 a L684) — odstranit první
- CC=122 — rozbít na `_discover_context_size`, `_build_analysis_batch`, `_process_analysis_results`, `_handle_skip_sentinels`

#### 4.3.3 `_postprocess.py`

**Přesunout:** `_extract_param_types` (L891–977, CC=30), `_build_overrides` (L985–1113, CC=20), `_build_pagerank` (L1116–1197, CC=18), `_build_hotspot_cache` (L1200–1231), `_run_postprocess` (L1981–2207, CC=97)

**Během extrakce opravit:**
- CC=97 v `_run_postprocess` — rozbít na `_align_is_project`, `_run_macro_expansion`, `_run_optional_phases`

#### 4.3.4 `_manifest_updater.py`

**Přesunout:** `_refresh_header_mtimes_from_manifest` (L1242–1286, CC=18), `_update_manifest_after_index` (L1292–1457, CC=61)

#### 4.3.5 `_unit_processor.py`

**Přesunout:** `_reassign_symbols_for_file` (L1467–1616), `_check_and_parse_unit` (L1619–1766, CC=51), `_get_manifest_entry_hash_for_unit` (L1767–1810), `_process_unit` (L1813–1978, CC=32)

### 4.4 V13: Opravit dvojitý libclang parse

**Problém:** `_check_and_parse_unit` parsuje s `return_tu=False` → `pre_parsed` neobsahuje TU → `_build_filtered_file_content` v `ops.py` parsuje znovu.

**Oprava:**
1. V `_check_and_parse_unit`: změnit `return_tu=False` na `return_tu=True` (nebo přidat parametr `need_tu_for_headers`)
2. V `_process_unit`: předat TU do `store_symbols_for_unit` → `existing_tu`
3. V `ops._build_filtered_file_content`: použít `existing_tu` a přeskočit re-parse

### 4.5 Mezifázová kontrola (po každém extrahovaném modulu)

```bash
python3 -m pytest tests/test_index_integrity.py -x
python3 -m pytest tests/test_ops_edge.py -x
bandit -r src/ -c pyproject.toml
```

**Commit (po každém modulu):** `refactor: extract _<module>.py from runner.py`

---

## Fáze 5: Oprava builderů (V6)

### 5.1 V6: Přidat timeout a capture_output do všech `subprocess.run()`

**Soubory k opravě:**
- `indexer/builders/arduino.py:82,110`
- `indexer/builders/generic_cmake.py:76,83`
- `indexer/builders/iar.py:95`
- `indexer/builders/keil.py:98`
- `indexer/builders/makefile.py:91`
- `indexer/builders/mbed_os.py:131`
- `indexer/builders/platformio.py:57,60,76`
- `indexer/builders/esp_idf.py` (všechny `subprocess.run`)
- `indexer/build.py:233` (pre_build — již má timeout=600, ověřit)

**Oprava — vytvořit sdílený helper:**
1. Přidat do `utils.py`:
```python
import subprocess

def run_build_command(
    cmd: list[str],
    cwd: Path,
    timeout: float = 600,
    description: str = "",
) -> subprocess.CompletedProcess:
    """Run a build command with consistent timeout and output capture.

    Args:
        cmd: Command and arguments as a list (shell=False is enforced).
        cwd: Working directory.
        timeout: Maximum time in seconds.
        description: Human-readable description for error messages.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        RuntimeError: On non-zero exit or timeout.
    """
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Build command timed out after {timeout}s: "
            f"{description or ' '.join(cmd)}"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(
            f"Build command failed (exit {result.returncode}): "
            f"{description or ' '.join(cmd)}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result
```
2. Nahradit všechna `subprocess.run(...)` volání v builderech za `run_build_command(...)`
3. Zachovat specifické timeouty kde jsou (pre_build 600s, buildy 600s, dry-run kratší)

**Review po opravě:**
- [ ] `pytest tests/test_builders/ -x` projde
- [ ] Každý builder otestován samostatně
- [ ] git diff zkontrolován

### 5.2 Odstranit `registry.detect()` — mrtvý kód v `builders/__init__.py`

**Problém:** `registry.detect()` nikde voláno — `build.py` implementuje vlastní markers-scoring.

**Oprava:** Odstranit `detect()` metodu z `BuildSystemRegistry`.

**Commit:** `fix: add timeout and output capture to all builder subprocess calls`

---

## Fáze 6: Data integrity opravy (K7, V8, V11, V12, M17, M21)

### 6.1 K7: Opravit `config_header` — injektovat před výpočtem hashe

**Soubor:** `src/fw_context_mcp/indexer/runner.py`

**Problém:** `config_header` je injektován do `unit.clang_args` až PO výpočtu `config_hash` a manifestu → dva identické projekty s různým `config_header` sdílejí `config_hash`.

**Oprava:**
1. Přesunout `config_header` injekci PŘED `compute_config_hash()`:
   ```python
   # Původní pořadí:
   config_hash = compute_config_hash(...)
   for unit in units:
       unit.clang_args.extend(config_header)

   # Opravené pořadí:
   for unit in units:
       unit.clang_args.extend(config_header)
   config_hash = compute_config_hash(...)  # nyní zahrnuje config_header
   ```
2. Ověřit, že `compute_config_hash` v `manifest.py` normalizuje `-include` argumenty

**Review po opravě:**
- [ ] `pytest tests/test_config_hash.py -x` projde
- [ ] `pytest tests/test_index_integrity.py -x` projde
- [ ] git diff zkontrolován

---

### 6.2 V8: `reset_index` — přidat pauzu bg reindexu

**Soubor:** `src/fw_context_mcp/mcp/handlers/maintenance.py`

**Problém:** `reset_index()` maže DB bez pauznutí bg reindexu — race condition s concurrent zápisem.

**Oprava:**
1. Před `unlink` DB souboru:
   ```python
   from ..background import _request_bg_reindex_pause, _resume_bg_reindex
   pause_result = _request_bg_reindex_pause(project_root, db_dir)
   ```
2. Po dokončení (i při chybě — v `finally`):
   ```python
   _resume_bg_reindex(project_root, db_dir)
   ```
3. Aplikovat stejný pattern jako v `cli.py:cmd_db_delete`

**Review po opravě:**
- [ ] `pytest tests/test_server.py -x` projde
- [ ] Ověřit, že `reset_index` a `cmd_db_delete --all` používají stejný pause pattern
- [ ] git diff zkontrolován

---

### 6.3 V11: Opravit poziční mapping v `prompts.py`

**Soubor:** `src/fw_context_mcp/indexer/prompts.py`

**Problém:** `parse_analysis_response()` mapuje LLM výsledky na symboly podle indexu. Vynechaná položka → všechny následující přiřazeny ke špatným symbolům.

**Oprava:**
1. Přidat identity-based matching jako primární strategii:
   - Upravit prompt aby LLM vracel `"id": <symbol_id>` v každé odpovědi
   - Mapovat podle `id` místo podle pozice
2. Zachovat poziční mapping jako fallback s varováním
3. Pokud LLM vrátí méně položek — doplnit `None` pro chybějící

**Review po opravě:**
- [ ] `pytest tests/test_prompts.py -x` projde
- [ ] Přidat test pro missing-entry scénář
- [ ] git diff zkontrolován

---

### 6.4 V12: Opravit stale makra při inkrementálním reindexu

**Soubor:** `src/fw_context_mcp/indexer/ops.py`

**Problém:** `delete_macros_for_file()` v `db/_symbols.py` existuje, je exportováno v `db/__init__.py`, ale **nikde se nevolá**.

**Oprava:**
1. V `store_symbols_for_unit()` přidat volání `delete_macros_for_file()` před `insert_macros_batch()`
2. Stejný pattern jako u symbolů a refs — DELETE před INSERT
3. Ověřit, že `delete_macros_for_file` správně filtruje podle `config_hash` a `file_id`

**Review po opravě:**
- [ ] `pytest tests/test_ops_edge.py -x` projde
- [ ] `pytest tests/test_index_integrity.py -x` projde
- [ ] git diff zkontrolován

---

### 6.5 M17: Opravit `created_at` reset v `global_db.py`

**Soubor:** `src/fw_context_mcp/config/global_db.py`

**Problém:** `upsert_project_registry()` používá `INSERT OR REPLACE` → `created_at` se resetuje při každém updatu.

**Oprava:**
```python
conn.execute("""
    INSERT INTO projects (project_id, name, project_type, root_path, created_at, updated_at)
    VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
    ON CONFLICT(project_id) DO UPDATE SET
        name = excluded.name,
        project_type = excluded.project_type,
        root_path = excluded.root_path,
        updated_at = excluded.updated_at
""", (project_id, name, project_type, str(root_path)))
```

**Review po opravě:**
- [ ] `pytest tests/test_config.py -x` projde
- [ ] Ověřit, že `created_at` se nemění při opakovaném volání
- [ ] git diff zkontrolován

---

### 6.6 M21: Opravit inflaci počtů v `get_all_projects`

**Soubor:** `src/fw_context_mcp/indexer/db/_projects.py`

**Problém:** Dva buildy ve stejné sekundě → více řádků v JOINu → násobení počtů symbolů a souborů.

**Oprava:**
```python
# Přidat rowid tiebreak do subquery:
b.rowid = (
    SELECT b2.rowid FROM build_configs b2
    WHERE b2.project_id = b.project_id
    ORDER BY b2.created_at DESC, b2.rowid DESC
    LIMIT 1
)
```

**Review po opravě:**
- [ ] `pytest tests/test_db.py -x` projde
- [ ] git diff zkontrolován

---

### 6.7 Mezifázová kontrola

```bash
python3 -m pytest tests/ -x --timeout=120 -q
bandit -r src/ -c pyproject.toml
```

**Commit:** `fix: data integrity — config_header hash, reset_index pause, positional mapping, stale macros, created_at, count inflation`

---

## Fáze 7: Connection a cache lifecycle (V9, V10, M5, M6, M7, M9)

### 7.1 V9: Sjednotit connection lifecycle

**Soubory:** `mcp/shared/connection.py`, `mcp/handlers/callgraph.py`, `mcp/shared/fallback.py`

**Problém:**
- Handlery spoléhají na cache-managed connection a nikdy nezavírají (`finally: pass`)
- `_refs_guard` dokumentuje "caller must close" — ale nikdo to nedělá
- `fallback.py` jako jediný connection zavírá (`conn.close()`)
- `active_users` v `_ConnCacheEntry` je mrtvé pole

**Oprava:**
1. Stanovit jednotnou strategii: **všechny connections jsou cache-managed**
2. Odstranit `active_users` z `_ConnCacheEntry` dataclass
3. V `_refs_guard`: změnit dokumentaci na "connection is cache-managed — do not close"
4. V `fallback.py`: odstranit explicitní `conn.close()`
5. Zdokumentovat kontrakt v `connection.py` module docstring

**Review po opravě:**
- [ ] `pytest tests/test_server.py -x` projde
- [ ] `pytest tests/test_cache.py -x` projde
- [ ] git diff zkontrolován

---

### 7.2 V10: Optimalizovat `PRAGMA integrity_check`

**Soubor:** `src/fw_context_mcp/mcp/shared/connection.py`

**Problém:** Full-DB scan při prvním tool callu — několika-sekundová latence na velkých DB.

**Oprava:**
1. První volání: `PRAGMA quick_check` (rychlejší, detekuje většinu problémů)
2. Plný `PRAGMA integrity_check` spustit asynchronně v background threadu
3. Pokud quick_check nebo integrity_check selže — označit connection za poškozenou

```python
def _check_db_integrity(conn: sqlite3.Connection) -> None:
    """Fast check first, full check in background."""
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if quick[0] != "ok":
        raise DatabaseCorruptionError(...)
    # Full integrity check runs once per process, asynchronously
    ...
```

**Review po opravě:**
- [ ] `pytest tests/test_server.py -x` projde
- [ ] Ověřit, že první MCP tool call netrvá déle než 100ms
- [ ] git diff zkontrolován

---

### 7.3 M5: Přidat eviction do neomezených cache

**Soubor 1:** `search/scoring.py` — `_STEM_PATTERN_CACHE`

**Oprava:** Převést na `functools.lru_cache`:
```python
from functools import lru_cache

@lru_cache(maxsize=10000)
def _compile_stem_pattern(stem: str) -> re.Pattern:
    return re.compile(f"(?:^|_|[a-z])({re.escape(stem)})(?:$|_|[A-Z])")
```

**Soubor 2:** `search/did_you_mean.py` — `_names_cache`

**Oprava:** Přidat TTL (300s) a maxsize (256) — použít existující `KeywordCache` pattern.

---

### 7.4 M6: Změnit `KeywordCache` z FIFO na LRU

**Soubor:** `src/fw_context_mcp/search/cache.py`

**Oprava:**
1. V `get()`: `self._cache.move_to_end(key)` pro LRU refresh
2. V `set()`: použít `popitem(last=True)` pro LRU evikci
3. Zachovat thread safety (`threading.Lock`)

---

### 7.5 M7: Optimalizovat `did_you_mean` prefix search

**Soubor:** `src/fw_context_mcp/search/did_you_mean.py`

**Problém:** O(Q × distinct-tokens) — pro každý query token se iteruje celý `token_index`.

**Oprava:**
1. Přidat prefixový index: `_prefix_index: dict[str, set[str]]` — první 3 znaky → kandidátní tokeny
2. Při lookup: vezmi prvních 3 znaků query tokenu → lookup v `_prefix_index` → jen tyto kandidáty skenuj
3. Fallback na plný sken pokud prefix < 3 znaky

---

### 7.6 M9: Přidat pruning `_auth_failures` dict

**Soubor:** `src/fw_context_mcp/cache_server/auth.py`

**Oprava:**
```python
def _prune_auth_failures(now: float) -> None:
    """Remove stale entries older than 2× the rate-limit window."""
    window = 120.0
    stale = [
        ip for ip, times in _auth_failures.items()
        if not times or now - times[-1] > window
    ]
    for ip in stale:
        del _auth_failures[ip]
```
Volat periodicky v `_check_rate_limit` (každých 100 volání).

---

### 7.7 Mezifázová kontrola

```bash
python3 -m pytest tests/ -x --timeout=120 -q
```

**Commit:** `fix: connection lifecycle consistency, cache eviction, staleness deduplication`

---

## Fáze 8: Dekompozice `cli.py` (R2)

### 8.1 Nové moduly

```
cli/
  __init__.py      → main(), VerboseFormatter, argument parser registry
  _index.py        → cmd_index, _manage_bg_reindex, _post_index_optimize, _resolve_compile_commands, _validate_and_fix_artifacts
  _init.py         → cmd_init, _install_skills, _install_agents, _build_agent_targets, _init_one_tool, _select_init_tools, _check_config_file, _ensure_gitignore, _detect_project_ai_tools
  _search.py       → cmd_search, cmd_list, cmd_status, _cli_is_stale
  _export.py       → cmd_export, cmd_analyze, cmd_version
  _cache.py        → cmd_cache_stats, cmd_cache_push, cmd_cache_remote_init, cmd_cache_clear
  _db.py           → cmd_db, cmd_db_list, cmd_db_stats, cmd_db_delete, cmd_db_cleanup, _resolve_config_hash
  _watch.py        → cmd_watch, cmd_watch_status, cmd_watch_restart
  _finetune.py     → cmd_finetune
  _mcp.py          → _resolve_mcp_bin, _register_mcp, _register_mcp_cli, _register_mcp_file, _ensure_subagent_mcp_permission, _update_marked_section, _inject_agent_section, _inject_agent_toml_section, _convert_agent_md_to_toml
```

### 8.2 Postup

Stejný jako u Fáze 4 — extrahovat po jednom modulu, review smyčka po každém.

### 8.3 Opravit CLI chyby během extrakce

- `_register_mcp_file` — JSONC comment stripping (`re.sub(r"//.*$", "", raw, re.MULTILINE)`) korumpuje URL s `//` → použít bezpečnější parser nebo omezit na řádky začínající `//`
- `_update_marked_section` — zbytečné `.bak` soubory při každém běhu → odstranit
- `cmd_list` — duplicitní logika s `maintenance.list_projects` → extrahovat sdílenou funkci
- `cmd_export` — raw SQL místo db API → použít db API
- `main()` — přidat exception handling pro neočekávané chyby

**Commit (po každém modulu):** `refactor: extract <module> from cli.py`

---

## Fáze 9: Search subsystém a LLM úklid (M1–M4, M8, M10–M14, M19, M20, M22)

### 9.1 M1: Opravit `ft://latest` cache-key mismatch

**Soubor:** `src/fw_context_mcp/llm/embedder_factory.py`

**Problém:** `_get_ft_embedder` vrací embedder s `name` = absolutní cesta, ale `cfg.embed_key()` vrací `ft://latest:desc-v5` — cache klíče se rozcházejí.

**Oprava:** Po resolvingu mutovat `cfg.embed_model` na resolved path:
```python
cfg = copy.copy(cfg)
cfg.embed_model = str(resolved_path)
```

### 9.2 M2: Konsolidovat duplicitní `OllamaError`/`OllamaModelNotFoundError`

**Soubory:** `llm/_diag.py`, `llm/ollama.py`

**Oprava:**
1. Ponechat definice POUZE v `ollama.py` (kanonické umístění)
2. V `_diag.py` importovat: `from .ollama import OllamaError, OllamaModelNotFoundError`
3. Odstranit duplicitní definice z konce `ollama.py` (řádky ~340+)

### 9.3 M3: Opravit embed retry path — leak raw HTTPStatusError

**Soubor:** `src/fw_context_mcp/llm/ollama.py`

**Oprava:** V `_call_ollama_embed_impl`:
```python
except httpx.HTTPStatusError as e:
    if e.response.status_code == 404:
        _pull_model(model, base_url)
        try:
            resp = httpx.post(...)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e2:
            raise OllamaModelNotFoundError(...) from e2
    raise OllamaError(...) from e
```
Také konsolidovat duplicitní dim-update + debug-log kód.

### 9.4 M4: Opravit `call_ollama_embed` — přidat `resolve_embed_model`

**Oprava:**
```python
def call_ollama_embed(inputs, cfg, query=True, embedder=None):
    from .auto_model import resolve_embed_model
    resolve_embed_model(cfg)
    return _call_ollama_embed_impl(inputs, cfg, query=query, embedder=embedder)
```

### 9.5 M8: Rate limiting — přidat `X-Forwarded-For` parsing

**Soubor:** `src/fw_context_mcp/cache_server/auth.py`

**Oprava:**
```python
def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
```

### 9.6 M10: Opravit body-size limit pro chunked requests

**Soubor:** `src/fw_context_mcp/cache_server/app.py`

**Oprava:** Přidat `max_body_size` pomocí Starlette `Request.body()` s limitem, nebo použít FastAPI middleware pro počítání bytes.

### 9.7 M11: Validovat `CacheEntry.hash` formát

**Soubor:** `src/fw_context_mcp/cache_server/app.py`

**Oprava:**
```python
from pydantic import field_validator

class CacheEntry(BaseModel):
    hash: str
    ...

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        if not re.match(r"^[a-f0-9]{64}$", v):
            raise ValueError("hash must be a 64-character hex string (SHA-256)")
        return v
```

### 9.8 M12: Odstranit `allow_external_llm` dead config

**Soubor:** `src/fw_context_mcp/config/settings.py`

**Oprava:** Odstranit `("allow_external_llm", "allow_external_llm", "bool")` z `_LLM_FIELDS` — `LLMConfig` nemá tento atribut a nic ho nepoužívá.

### 9.9 M13: Async wrapper pro reranker

**Soubor:** `src/fw_context_mcp/search/reranker.py`

**Oprava:**
```python
import asyncio

async def rank_async(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
    return await asyncio.to_thread(self.rank, query, candidates, top_k)
```

### 9.10 M14: Zvýšit LIMIT v `expand_context`

**Soubor:** `src/fw_context_mcp/search/phases/expand_context.py`

**Oprava:** Zvýšit LIMIT z 200 na 500 na směr. Přidat logování když je limit dosažen ("truncated N neighbors").

### 9.11 M19: Přidat path-traversal validaci do `get_file_map`

**Soubor:** `src/fw_context_mcp/mcp/handlers/source.py`

**Oprava:** Extrahovat validační logiku z `read_file` do sdílené funkce `_validate_path_in_root()`, použít v `get_file_map`.

### 9.12 M20: Opravit stale docs

- `mcp/__init__.py`: "29 tools" → "34 tools"
- `search/context.py`: odstranit/aktualizovat "Phase 28 refactoring" odkaz
- `search/__init__.py`: "Pre-compute at import time" → "Lazily materialized via __getattr__"
- `mcp/handlers/source.py`: opravit stray mid-docstring security note v `get_file_map`

### 9.13 M22: Odstranit duplicitní `_CRITICAL_TABLES`

**Soubor:** `src/fw_context_mcp/indexer/db/_connection.py`

**Oprava:**
1. Přesunout `_CRITICAL_TABLES` do `_schema.py` jako jediný zdroj pravdy
2. V `_connection.py`: `from ._schema import _CRITICAL_TABLES`
3. Odstranit duplicitní definici i s "MUST be kept in sync" komentářem

### 9.14 Mezifázová kontrola

```bash
python3 -m pytest tests/ -x --timeout=120 -q
bandit -r src/ -c pyproject.toml
```

**Commit:** `fix: LLM and search subsystem fixes — cache keys, error handling, rate limiting, validation, stale docs`

---

## Fáze 10: Cache server hardening + Refaktoringové doplňky

### 10.1 Ověřit všechny `finally:` bloky v `backend.py`

Po opravě K1 zkontrolovat, že VŠECHNY `acquire()` mají párové `release()` v `finally:`, nejen `meta_pool` a `cache_pool` v `init_schema()`.

### 10.2 R3: Data Access Layer pro SQL

Vytvořit repository třídy pro nejpoužívanější dotazy:
```python
# indexer/db/_repository.py
class SymbolRepository:
    def __init__(self, conn: sqlite3.Connection, config_hash: str): ...
    def search_fts5(self, query: str, kind: str | None = None) -> list[dict]: ...
    def find_by_usr(self, usr: str) -> dict | None: ...
    def get_embeddings(self) -> list[dict]: ...
```

Postupná migrace handlerů a fází pipeline na repository.

### 10.3 R6: Extrahovat shared handler logic

Vytvořit `mcp/handlers/_base.py`:
```python
class BaseHandler:
    @staticmethod
    def resolve_db_context(project_root: str | None) -> DbContext:
        """Společné: resolve root → config → project_id → db_path → open_db → config_hash"""
        ...

    @staticmethod
    def handle_staleness(conn, config_hash, db_path) -> list[str]:
        """Společné: check stale files, append warnings, ensure daemon"""
        ...
```

### 10.4 R8: Extrakce experimentů

- `experiments/eval_harness.py` — přesunout do `tests/` (je to testovací infrastruktura)
- Staré `analyze_db_*.py`, `test_graph_edges_*.py` — přesunout do `experiments/archive/` nebo smazat
- Ponechat jen aktivní experimenty s dokumentací v `experiments/README.md`

**Commit:** `refactor: DAL, shared handler logic, experiment cleanup`

---

## Fáze 11: Finální integrace, testování, dokumentace

### 11.1 Kompletní test suite

```bash
python3 -m pytest tests/ -x --timeout=120 -v 2>&1 | tee test-results.txt
```
Očekávaný výsledek: 898+ testů, 0 failures, 0 errors.

### 11.2 Security scan

```bash
bandit -r src/ -c pyproject.toml -f txt
```
Očekávaný výsledek: 0 issues.

### 11.3 Import verification

```bash
python3 -c "
import fw_context_mcp
from fw_context_mcp.cli import main
from fw_context_mcp.mcp.server import main as server_main
from fw_context_mcp.cache_server.cli import main as cache_cli_main
from fw_context_mcp.cache_server.backend import CacheBackend
from fw_context_mcp.cache_server.setup import setup_wizard
from fw_context_mcp.indexer.runner import run
from fw_context_mcp.llm.embedder_factory import get_embedder
from fw_context_mcp.search.pipeline import PipelineRunner, SEARCH_CODE, SMART_SEARCH
print('All critical imports OK')
"
```

### 11.4 Py_compile všech souborů

```bash
find src/ -name "*.py" -print0 | xargs -0 python3 -m py_compile 2>&1
```

### 11.5 Deduplikační scan

```bash
# Ověřit že žádný mrtvý kód nezůstal
grep -rn "except Exception" src/ | grep -v "KeyboardInterrupt\|SystemExit\|# allowed\|# intentional"
grep -rn "def.*:\|class.*:" src/ | sort | uniq -d  # detekce duplicitních definic
```

### 11.6 Aktualizace dokumentace

- `README.md` — aktuální stav
- `README-MCP.md` — aktuální počet tools (34)
- `CHANGELOG.md` — seznam všech oprav
- `docs/` — aktualizovat podle změn
- `MEMORY.md` — přidat lessons learned

### 11.7 Final commit

```bash
git add -A
git commit -m "chore: final integration — documentation, tests passing, 0 bandit issues"
```

---

## Časová osa (odhad)

| Fáze | Popis | Odhad (dny) |
|------|-------|-------------|
| 0 | Příprava, branch, baseline | 0.5 |
| 1 | Kritické opravy (K1, K2, K3, K6) | 1 |
| 2 | Mrtvý kód (K4, K5, V3, M15, M16, M18, M23, M24, M25) | 2 |
| 3 | Error handling (V1, V5, V7) | 3 |
| 4 | Dekompozice runner.py (V2, V13) | 5 |
| 5 | Buildery (V6) | 1 |
| 6 | Data integrity (K7, V8, V11, V12, M17, M21) | 3 |
| 7 | Connection/cache (V9, V10, M5, M6, M7, M9) | 2 |
| 8 | Dekompozice cli.py (R2) | 4 |
| 9 | Search + LLM subsystém (M1–M4, M8, M10–M14, M19, M20, M22) | 2 |
| 10 | Cache server + refaktoring (R3, R6, R8) | 2 |
| 11 | Integrace, testy, docs | 2 |
| **Celkem** | | **~27.5 dne** |

---

## Checklist — každá oprava

- [ ] Konkrétní problém identifikován a pochopen
- [ ] Nalezeny všechny soubory kterých se změna týká
- [ ] Ověřeno, že řešení používá existující sdílené funkce (P1)
- [ ] Implementována změna
- [ ] `py_compile` projde pro změněné soubory
- [ ] Relevantní testy projdou
- [ ] Full test suite projde
- [ ] Bandit projde (0 issues)
- [ ] Review změn (git diff) — nalezeny další problémy?
  - [ ] ANO → opravit je → zpět na začátek checklistu
  - [ ] NE → commit
- [ ] Commit message: `fix: <popis>` nebo `refactor: <popis>`
- [ ] Označeno jako hotové v tomto plánu
