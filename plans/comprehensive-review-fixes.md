# Plán oprav — Hloubkové review (2026-08-01)

**Vytvořeno:** 2026-08-01
**Větev:** `fix/comprehensive-review-fixes`
**Rozsah:** Všechny nálezy F1–F6 + refaktoringové návrhy R1–R6 z aktuálního review

---

## ⚠️ KRITICKÁ PRAVIDLA (platí pro všechny fáze)

### Pravidlo 1: Maximální sdílení kódu

**Každá oprava MUSÍ maximálně využívat existující sdílené funkce projektu.**
Před vytvořením jakékoli nové funkce/utility:
1. Prohledat codebase pro existující helpery, context managery, dekorátory
2. Ověřit v těchto sdílených modulech, zda stejná logika již neexistuje:
   - `utils.py` — `SAFE_EXCEPT`, `is_fatal`, `is_db_exception`, `abs_path`,
     `resolve_project_root`, `read_file_lines`, `compute_content_hash`, `fmt_count`
   - `mcp/shared/context.py` — `_open_db_safe`, `_open_db_or_return`,
     `_resolve_handler_context`, `_db_path`, `_check_server_ready`
   - `mcp/shared/stale.py` — `_stale_files`, `_with_stale_recovery`,
     `_count_modified_files`, `check_structural_staleness`
   - `mcp/shared/pid_file.py` — `PidFile` (context manager, `is_active`, `read_pid`)
   - `mcp/shared/fallback.py` — `_fallback_to_search_code`
   - `mcp/shared/filtering.py` — `detect_sdk_exclude_like`, `compute_exclude_like`
   - `mcp/handlers/_base.py` — `BaseHandler.resolve_db_context`,
     `BaseHandler.handle_staleness`, `BaseHandler.with_stale_recovery`
   - `indexer/ops.py` — `store_symbols_for_unit`, `_normalize_file_path`
3. Pokud objevíš duplicitní logiku, vždy refaktoruj do sdílené funkce
4. **Nikdy nekopíruj kód mezi soubory**

### Pravidlo 2: Review smyčka po každé opravě

**Po každé jednotlivé opravě (nebo těsně svázané skupině):**
1. `git diff` — vizuální kontrola VŠECH změn, ověřit že:
   - Žádný mrtvý kód
   - Žádné zbytečné `type: ignore` / `noqa` / `pragma: no cover`
   - Žádná duplicita s existujícím kódem
   - Všechny nové funkce mají docstring
   - Importy jsou správně organizované (lazy importy kde potřeba)
2. `make lint` — ruff + mypy MUSÍ projít čistě
3. `make test` — minimálně, ideálně `make test-all`
4. Pokud review odhalí další problémy → **opravit je**
5. Opakovat review → oprava → review **dokud review není čisté**
6. **Teprve pak přejít na další opravu**

### Pravidlo 3: Nic nevynechávat

Všechny níže uvedené položky MUSÍ být opraveny. Žádné přeskakování s
odůvodněním "je to moc složité" nebo "není to kritické". Každý nález
v review byl potvrzen a musí být adresován.

---

## Přehled všech nálezů k opravě

| # | Nález | Soubor | Typ | Priorita | Náročnost |
|---|-------|--------|-----|----------|-----------|
| ✅ F1+R3 | Dead code `semantic_search` | `mcp/handlers/search.py:365-373` | bug | 🔴 HIGH | Nízká |
| ✅ F5+R5 | `None` sentinel `did_you_mean` | `search/did_you_mean.py` | style | 🟡 MED | Nízká |
| ✅ F6 | Prefix fallback krátké tokeny | `search/did_you_mean.py` | perf | 🟢 LOW | Nízká |
| ✅ F4+R4 | Side effect `_from_dict` | `config/settings.py` | design | 🟡 MED | Střední |
| ✅ F3+R2 | Context manager pause/resume | `mcp/handlers/maintenance.py`, `mcp/background.py` | design | 🟡 MED | Střední |
| ✅ F2+R1 | BFS walker extrakce | `mcp/handlers/inheritance.py` | design | 🟡 MED | Vysoká |
| ✅ R6 | Rozbití `_reindex_post_write_phases` | `mcp/handlers/maintenance.py` | design | 🟢 LOW | Střední |

---

## Fáze 1: F1 + R3 — Dead code v `semantic_search`

### Soubor
`src/fw_context_mcp/mcp/handlers/search.py`, řádky 365–373

### Detailní popis
```python
# ŘÁDKY 355-373 (aktuální stav):
results: list[dict] = list(ctx.formatted_results) if ctx.formatted_results else []

if ctx.ollama_warning is not None:
    return _fallback_to_search_code(...)

if not results:
    # Filter out metadata entries and check for real results
    real_results = [r for r in results if not r.get("_meta")]
    if not real_results:
        return _fallback_to_search_code(...)
```

**Problém:** Když `results` je prázdný list (`[]`), podmínka `if not results`
je True. Uvnitř: `[r for r in results if ...]` kde `results` je `[]` → vždy
vrátí `[]`. Takže `if not real_results` je VŽDY True. Vnitřní filtrování je
**mrtvý kód** — výsledek je vždy stejný (fallback).

### Kroky opravy

#### Krok 1.1: Analyzovat `ctx.formatted_results`
Přečíst zdrojový kód, který plní `formatted_results`:
- `search/phases/format.py` — `FormatPhase.run()`
- `search/context.py` — `PipelineContext.formatted_results`

**Cíl:** Zjistit, zda `formatted_results` může obsahovat POUZE metadata
entries (např. dicty s klíčem `_meta`) bez reálných výsledků. Pokud ano,
opravit logiku. Pokud ne, odstranit mrtvý kód.

#### Krok 1.2: Implementovat opravu

**Varianta A — metadata-only results nejsou možné (pravděpodobná):**
```python
if not results:
    return _fallback_to_search_code(
        root, db_path, query, limit,
        warning=f"No symbols matched with similarity > {threshold}. "
                "Try lowering the threshold or rephrasing the query.",
    )
```
Odstranit celý blok `real_results = ...` a `if not real_results: ...`.

**Varianta B — metadata-only results JSOU možné:**
```python
# Check if results contain only metadata, no real symbol matches
real_results = [r for r in results if not r.get("_meta")]
if not real_results:
    return _fallback_to_search_code(...)
```
Odstranit vnější `if not results:` — filtr sám zkontroluje, zda jsou reálné výsledky.

#### Krok 1.3: Verifikace
1. `git diff` — vizuální kontrola
2. `make lint` — ruff + mypy
3. `make test` — rychlé testy
4. Spustit `fw-context index` na testovacím projektu (např. HA_Boiler)
5. Ověřit `semantic_search` funguje (pokud je Ollama dostupná)

#### Krok 1.4: Review smyčka
- Zkontrolovat diff → opravit případné problémy → opakovat
- Až review čisté → commit s message:
  `fix: F1 — odstranění dead code v semantic_search`

---

## Fáze 2: F5 + R5 — `None` sentinel v `did_you_mean.py`

### Soubor
`src/fw_context_mcp/search/did_you_mean.py`

### Detailní popis
```python
# Aktuální stav — funkce suggest():
_cache: dict[str, tuple[float, list[str]]] = {}
...
with _cache_lock:
    if cache_key in _cache:
        cached_val = _cache[cache_key]
        if cached_val is not None:  # guard against stampede sentinel
            ts, cached = cached_val
            ...
    _cache[cache_key] = None  # type: ignore[assignment]  # stampede sentinel
```

**Problémy:**
1. `None` je použit jako stampede sentinel, ale typově je `None` nekompatibilní
   s `tuple[float, list[str]]` → nutné `type: ignore[assignment]`
2. Pokud by výpočet spadl, `None` zůstane v cache jako "prázdný výsledek"
3. Mixuje se sémantika: `None` = "in flight" vs `None` = "není v cache"

### Kroky opravy

#### Krok 2.1: Analyzovat existující patterny v projektu
- Prohledat `mcp/shared/`, `search/`, `indexer/` pro podobné cache patterny
- Zkontrolovat `_modified_cache` v `stale.py`, `_config_cache` v `settings.py`

#### Krok 2.2: Vybrat a implementovat řešení

**Varianta A — Dedikovaný sentinel (doporučená):**
```python
_SENTINEL: object = object()
_cache: dict[str, tuple[float, list[str]] | object] = {}

# Při zápisu:
_cache[cache_key] = _SENTINEL

# Při čtení:
cached_val = _cache[cache_key]
if cached_val is _SENTINEL:
    return []  # jiný thread počítá
```

**Varianta B — Samostatný `_in_flight` set:**
```python
_in_flight: set[str] = set()

# Před výpočtem:
with _cache_lock:
    if cache_key in _in_flight:
        return []  # stampede — jiný thread počítá
    _in_flight.add(cache_key)

# Po výpočtu:
with _cache_lock:
    _in_flight.discard(cache_key)
    _cache[cache_key] = (time.monotonic(), matches)
```

Varianta B je čistší — odděluje concerns. Zvážit podle existujících patternů.

#### Krok 2.3: Verifikace
1. `git diff`
2. `make lint` — OVĚŘIT odstranění `type: ignore[assignment]`
3. `make test` — testy pro `lookup_symbol` s neexistujícím jménem
4. Ověřit `_did_you_mean` v odpovědi

#### Krok 2.4: Review smyčka
- Zkontrolovat diff → opravit → opakovat
- Commit: `fix: F5 — dedikovaný sentinel pro cache stampede v did_you_mean`

---

## Fáze 3: F6 — Prefix fallback pro krátké tokeny

### Soubor
`src/fw_context_mcp/search/did_you_mean.py`, funkce `suggest()`

### Detailní popis
```python
candidate_tokens = prefix_index.get(qt[:3], set()) if len(qt) >= 3 else set(token_index.keys())
```
Pro tokeny kratší než 3 znaky se iteruje přes **všechny** unikátní tokeny
v indexu (O(n) scan). Pro velké projekty (50K+ tokenů) problematické.

### Kroky opravy

#### Krok 3.1: Analyzovat reálný dopad
1. Zjistit, jaké tokeny typicky produkuje `_tokenize()` pro C/C++ symboly
2. Kolik tokenů má délku < 3 v typickém projektu?
3. Je `did_you_mean` vůbec voláno pro krátké query?

#### Krok 3.2: Implementovat opravu

**Doporučená varianta — Omezit prefix matching pro krátké tokeny:**
```python
if len(qt) >= 3:
    candidate_tokens = prefix_index.get(qt[:3], set())
elif len(qt) == 2:
    candidate_tokens = prefix_index_short.get(qt, set())
else:
    candidate_tokens = set()  # 1-char: exact matches only
```
Přidat `prefix_index_short` pro tokeny délky 2.

#### Krok 3.3: Verifikace
1. `git diff`
2. `make lint`
3. `make test`
4. Volitelně: test s velkým množstvím tokenů

#### Krok 3.4: Review smyčka
- Commit: `fix: F6 — prefix fallback limit pro krátké tokeny v did_you_mean`

---

## Fáze 4: F4 + R4 — Side effect v `_from_dict`

### Soubory
- `src/fw_context_mcp/config/settings.py` — `_from_dict()`, `load()`
- `src/fw_context_mcp/llm/auto_model.py` — `resolve_embed_model()`

### Detailní popis
```python
# V _from_dict() — settings.py:
def _from_dict(data: dict) -> Config:
    ...
    resolve_embed_model(cfg.llm)       # ← I/O SIDE EFFECT (GPU detect, Ollama API)
    _apply_embed_prompt_defaults(cfg.llm)  # ← čistá transformace
    ...
```

`resolve_embed_model` je I/O operace uvnitř funkce, která se tváří jako čistá
transformace dat. Voláno z `load()`, která je cachovaná podle mtime.

### Kroky opravy

#### Krok 4.1: Detailně analyzovat `resolve_embed_model`
- Přečíst `src/fw_context_mcp/llm/auto_model.py`
- Zjistit přesný rozsah I/O operací a zda je idempotentní
- Zjistit, zda se volá i odjinud

#### Krok 4.2: Přesunout volání do `load()`
```python
# V load() — settings.py:
cfg = _from_dict(data)

# Side effects — expected at this level
from ..llm.auto_model import resolve_embed_model
resolve_embed_model(cfg.llm)

cfg.index.db_dir = cfg.index.db_dir.expanduser().resolve()
...
```
Ponechat `_apply_embed_prompt_defaults` v `_from_dict` (čistá transformace).

#### Krok 4.3: Verifikace
1. `git diff`
2. `make lint`
3. `make test`
4. Ověřit `fw-context init` detekuje embed model
5. Ověřit mtime-based config cache funguje

#### Krok 4.4: Review smyčka
- Commit: `fix: F4 — přesun resolve_embed_model z _from_dict do load()`

---

## Fáze 5: F3 + R2 — Context manager pro pause/resume

### Soubory
- `src/fw_context_mcp/mcp/background.py` — `_request_bg_reindex_pause()`, `_resume_bg_reindex()`
- `src/fw_context_mcp/mcp/handlers/maintenance.py` — `_reindex_parse_and_store()`,
  `_reindex_cleanup_deleted_file()`, `reset_index()`

### Detailní popis

**Problém 1:** V `_reindex_parse_and_store` (maintenance.py:638-643):
```python
except sqlite3.Error as exc:
    return 0, {"error": f"DB error during parse of {unit.file.name}: {exc}"}
    # NOTE: _request_bg_reindex_pause is called AFTER this loop —
    # early return here is safe (pause was never requested).
```
Správné, ale křehké — spoléhá na pořadí volání. Při refaktoringu risk.

**Problém 2:** Nekonzistentní použití pause/resume napříč 3 místy —
všechna implementují stejný `try/finally` pattern manuálně.

### Kroky opravy

#### Krok 5.1: Vytvořit context manager v `background.py`
```python
from contextlib import contextmanager

@contextmanager
def bg_reindex_pause(root: Path):
    """Context manager: pause background reindex, auto-resume on exit.
    
    Safe for early returns, exceptions — resume always runs in finally.
    """
    _request_bg_reindex_pause(root)
    try:
        yield
    finally:
        _resume_bg_reindex(root)
```

#### Krok 5.2: Nahradit manuální pause/resume na všech 3 místech

**V `_reindex_parse_and_store`:**
```python
# PŘED:
_request_bg_reindex_pause(root)
try:
    with db_write_lock(...):
        ...
finally:
    _resume_bg_reindex(root)
    _invalidate_modified_cache(config_hash)

# PO:
with bg_reindex_pause(root):
    with db_write_lock(...):
        ...
_invalidate_modified_cache(config_hash)  # mimo CM — záměrně
```
Odstranit komentář o bezpečnosti early returnu.

**V `_reindex_cleanup_deleted_file`:**
```python
# PŘED:
_request_bg_reindex_pause(root)
try:
    with _db_write_lock(...):
        ...
finally:
    _resume_bg_reindex(root)

# PO:
with bg_reindex_pause(root):
    with _db_write_lock(...):
        ...
```

**V `reset_index`:**
```python
# PŘED:
_request_bg_reindex_pause(root)
try:
    db_path.unlink()
    ...
finally:
    _resume_bg_reindex(root)

# PO:
with bg_reindex_pause(root):
    db_path.unlink()
    ...
```

#### Krok 5.3: Verifikace
1. `git diff` — zkontrolovat všechna 3 místa
2. `make lint`
3. `make test`
4. Manuálně otestovat `reindex_file` a `reset_index` (dry-run)

#### Krok 5.4: Review smyčka
- Commit: `fix: F3 — context manager bg_reindex_pause pro bezpečné pause/resume`

---

## Fáze 6: F2 + R1 — Extrakce BFS walkeru z `get_inheritance_chain`

### Soubor
`src/fw_context_mcp/mcp/handlers/inheritance.py` — funkce `get_inheritance_chain()`

### Detailní popis
Funkce má complexity weight 92. Dvě téměř identické BFS smyčky po ~80 řádcích:
- **Walk up (ancestors):** `get_direct_bases_batch()`, klíč `base_usr`
- **Walk down (descendants):** `get_direct_derived_batch()`, klíč `derived_usr`

Obě implementují identickou logiku: deduplikace, batch lookup symbolů,
batch lookup vztahů, stavba resultů, příprava next levelu.

### Kroky opravy

#### Krok 6.1: Přečíst a analyzovat batch funkce
- `get_direct_bases_batch(conn, config_hash, usrs)` → `dict[str, list[dict]]`
- `get_direct_derived_batch(conn, config_hash, usrs)` → `dict[str, list[dict]]`
- Oba v `src/fw_context_mcp/indexer/db/`

#### Krok 6.2: Vytvořit generický BFS walker
```python
def _bfs_inheritance_walk(
    conn: sqlite3.Connection,
    config_hash: str,
    root: Path,
    start_edges: list[tuple[str, str, bool]],  # (usr, access, is_virtual)
    batch_fn,                                   # (conn, ch, usrs) -> dict[str, list[dict]]
    edge_usr_key: str,                          # "base_usr" | "derived_usr"
    visited: set[str],
    max_depth: int,
) -> list[dict]:
    """Generic BFS for inheritance graph traversal.
    
    Walks the inheritance graph level by level, using *batch_fn* to
    fetch edges for each level's USRs in a single SQL query.
    Returns list of dicts with: name, usr, access, is_virtual,
    depth, file, kind. Sorted by depth ascending.
    """
    all_results: list[dict] = []
    current_level = start_edges[:]
    
    for depth in range(1, max_depth + 1):
        if not current_level:
            break
        
        # 1. Deduplicate within level (diamond inheritance)
        seen_level: set[str] = set()
        unique_level: list[tuple[str, str, bool]] = []
        for cur_usr, access, is_virtual in current_level:
            if cur_usr not in seen_level and cur_usr not in visited:
                seen_level.add(cur_usr)
                visited.add(cur_usr)
                unique_level.append((cur_usr, access, is_virtual))
        current_level = unique_level
        if not current_level:
            break

        level_usrs = [u for u, _, _ in current_level]
        
        # 2. Batch lookup symbols
        placeholders = ",".join("?" * len(level_usrs))
        symbol_rows = conn.execute(
            f"SELECT usr, name, kind, file_path FROM symbols "
            f"WHERE config_hash=? AND usr IN ({placeholders})",
            (config_hash, *level_usrs),
        ).fetchall()
        symbol_map = {r["usr"]: r for r in symbol_rows}
        
        # 3. Batch lookup edges
        edges_batch = batch_fn(conn, config_hash, level_usrs)
        
        # 4. Build results and next level
        next_level: list[tuple[str, str, bool]] = []
        for cur_usr, access, is_virtual in current_level:
            cur_row = symbol_map.get(cur_usr)
            all_results.append({
                "name": cur_row["name"] if cur_row else "<unknown>",
                "usr": cur_usr,
                "access": access,
                "is_virtual": is_virtual,
                "depth": depth,
                "file": abs_path(root, cur_row["file_path"])
                        if cur_row and cur_row["file_path"] else None,
                "kind": cur_row["kind"] if cur_row else None,
            })
            for edge in edges_batch.get(cur_usr, []):
                next_usr = edge[edge_usr_key]
                if next_usr not in visited:
                    next_level.append(
                        (next_usr, edge["access"], bool(edge["is_virtual"]))
                    )
        current_level = next_level
    
    return all_results
```

#### Krok 6.3: Nahradit obě BFS smyčky voláním walkeru
```python
if transitive:
    visited_up: set[str] = {usr}
    start_up = [(b["base_usr"], b["access"], bool(b["is_virtual"]))
                for b in bases if b["base_usr"] not in visited_up]
    result["all_bases"] = _bfs_inheritance_walk(
        conn, config_hash, root,
        start_edges=start_up,
        batch_fn=get_direct_bases_batch,
        edge_usr_key="base_usr",
        visited=visited_up,
        max_depth=max_depth,
    )
    
    visited_down: set[str] = {usr}
    start_down = [(d["derived_usr"], d["access"], bool(d["is_virtual"]))
                  for d in derived if d["derived_usr"] not in visited_down]
    result["all_derived"] = _bfs_inheritance_walk(
        conn, config_hash, root,
        start_edges=start_down,
        batch_fn=get_direct_derived_batch,
        edge_usr_key="derived_usr",
        visited=visited_down,
        max_depth=max_depth,
    )
```

#### Krok 6.4: Umístění
- Jako privátní funkce `_bfs_inheritance_walk` v `inheritance.py`
- Není vhodný do `indexer/db/` — používá `abs_path` a MCP formátování

#### Krok 6.5: Verifikace
1. `git diff` — ~140 řádků odebráno, ~50 přidáno
2. `make lint`
3. `make test`
4. **KRITICKÉ:** Otestovat na Z-Box ECB (bohatá Mbed OS class hierarchie)
5. Ověřit diamond inheritance (cycle detection)

#### Krok 6.6: Review smyčka
- Commit: `fix: F2 — extrakce BFS walkeru z get_inheritance_chain`

---

## Fáze 7: R6 — Rozbití `_reindex_post_write_phases`

### Soubor
`src/fw_context_mcp/mcp/handlers/maintenance.py` — `_reindex_post_write_phases()`

### Detailní popis
Funkce (complexity weight 47) obsahuje 5 nezávislých enrichment kroků,
každý s vlastním try/except a result dict mutací.

### Kroky opravy

#### Krok 7.1: Vytvořit samostatné funkce

**`_reindex_llm_analysis(conn, config_hash, cfg, db_dir, result)`:**
LLM analysis regenerace — CacheClient, _build_llm_analysis, error handling.

**`_reindex_overrides(conn, config_hash, cfg, db_dir, result)`:**
Method override rebuild — _build_overrides z runner.py.

**`_reindex_pagerank(conn, config_hash, cfg, result)`:**
PageRank + hotspot cache — _build_pagerank, _build_hotspot_cache.

**`_reindex_embeddings(conn, config_hash, cfg, db_dir, target, root, result)`:**
Embedding regenerace — _build_embeddings, file_symbol_ids lookup.

#### Krok 7.2: Přepsat orchestrační funkci
```python
def _reindex_post_write_phases(
    conn, config_hash, cfg, total_symbols, db_dir, target, matching, result, root
) -> dict:
    """Run post-write enrichment phases after reindex_file.
    
    Each phase is independent — failure in one does not prevent others.
    """
    if total_symbols <= 0:
        return result
    
    _reindex_llm_analysis(conn, config_hash, cfg, db_dir, result)
    _reindex_overrides(conn, config_hash, cfg, db_dir, result)
    _reindex_pagerank(conn, config_hash, cfg, result)
    
    if len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"}:
        result["warning"] = (
            "Header re-indexed via one TU. Other TUs including this "
            "header may still have stale symbols."
        )
    
    _reindex_embeddings(conn, config_hash, cfg, db_dir, target, root, result)
    return result
```

#### Krok 7.3: Verifikace
1. `git diff`
2. `make lint`
3. `make test`
4. Ověřit `reindex_file` na testovacím projektu

#### Krok 7.4: Review smyčka
- Commit: `fix: R6 — rozbití _reindex_post_write_phases na samostatné funkce`

---

## Fáze 8: Finální verifikace

### 8.1: Automatické kontroly
- [x] `make test` — 857 passed, 30 skipped ✅
- [ ] `make test-all` — netestováno (vyžaduje ollama + libclang)
- [x] `make lint` — ruff ✅, mypy ✅
- [x] `make lint-security` — 0 High, 46 Medium (pre-existující subprocess volání)

### 8.2: Manuální testy
- [ ] `fw-context init` na čistém testovacím projektu
- [ ] `fw-context index --build` — úspěšné zaindexování
- [ ] `fw-context index --force` — reindexace
- [ ] `fw-context search "modem"` — výsledky
- [ ] `fw-context status` — index status
- [ ] MCP tools:
  - [ ] `get_active_build` — `status: "ready"`
  - [ ] `search_code("modem")` — výsledky
  - [ ] `semantic_search("uart init")` — výsledky nebo fallback
  - [ ] `get_inheritance_chain("SomeClass", transitive=True)` — ancestors + descendants
  - [ ] `reindex_file("src/main.cpp")` — úspěšná reindexace
  - [ ] `lookup_symbol("nonexistent_func")` — `_did_you_mean`

### 8.3: Finální diff review
- [x] `git diff main...HEAD --stat` — zkontrolováno
- [x] `git diff main...HEAD` — detailní kontrola provedena
- [x] Žádné neodůvodněné `type: ignore` / `noqa` / `pragma: no cover`
- [x] Žádný duplicitní kód

### 8.4: Commit a push
- [x] Všech 7 commitů vytvořeno (jeden na fázi)
- [ ] `git push`

---

## Harmonogram

| Fáze | Nález(y) | Náročnost | Odhad | Závisí na |
|------|----------|-----------|-------|-----------|
| ✅ F1 | F1+R3 — Dead code `semantic_search` | Nízká | 30 min | — |
| ✅ F2 | F5+R5 — `None` sentinel | Nízká | 30 min | — |
| ✅ F3 | F6 — Prefix fallback | Nízká | 30 min | — |
| ✅ F4 | F4+R4 — Side effect `_from_dict` | Střední | 1 h | — |
| ✅ F5 | F3+R2 — Context manager pause/resume | Střední | 1.5 h | — |
| ✅ F6 | F2+R1 — BFS walker extrakce | Vysoká | 2.5 h | — |
| ✅ F7 | R6 — Rozbití post_write phases | Střední | 1.5 h | F5 (sdílí soubor) |
| ✅ F8 | Finální verifikace (auto) | Střední | — | Všechny |
| ✅ **Celkem** | **7 oprav** | — | **~9 h** | |

---

## Rychlý odkaz: Sdílené moduly

| Modul | Klíčové exporty |
|-------|-----------------|
| `utils.py` | `SAFE_EXCEPT`, `is_fatal`, `is_db_exception`, `abs_path`, `resolve_project_root`, `read_file_lines`, `compute_content_hash`, `fmt_count`, `run_build_command` |
| `mcp/shared/context.py` | `_open_db_safe`, `_open_db_or_return`, `_resolve_handler_context`, `_db_path`, `_check_server_ready`, `_resolve_context`, `HandlerContext` |
| `mcp/shared/stale.py` | `_stale_files`, `_with_stale_recovery`, `_count_modified_files`, `check_structural_staleness`, `_check_header_staleness` |
| `mcp/shared/pid_file.py` | `PidFile` (context manager, `is_active()`, `read_pid()`, `_pid_exists()`) |
| `mcp/shared/fallback.py` | `_fallback_to_search_code`, `_fallback_to_search_code_inner` |
| `mcp/shared/filtering.py` | `detect_sdk_exclude_like`, `compute_exclude_like` |
| `mcp/shared/connection.py` | `_open_and_cache`, `_open_db_safe`, `_invalidate_conn_cache`, `HandlerContext` |
| `mcp/shared/readiness.py` | `_check_server_ready`, `_db_path`, `_resolve_context`, `_is_stale`, `_detect_build_system` |
| `mcp/handlers/_base.py` | `BaseHandler.resolve_db_context`, `BaseHandler.handle_staleness`, `BaseHandler.with_stale_recovery`, `DbContext` |
| `indexer/ops.py` | `store_symbols_for_unit`, `_normalize_file_path` |
| `indexer/db/__init__.py` | `open_db`, `get_active_config`, `transaction`, `write_lock` |
| `config/settings.py` | `Config`, `LLMConfig`, `IndexConfig`, `load`, `derive_project_id` |

## Rychlý odkaz: Review smyčka checklist

```
[ ] git diff — vizuální kontrola VŠECH změn
[ ] make lint — ruff + mypy (MUSÍ projít čistě)
[ ] make test — rychlé testy (MUSÍ projít)
[ ] Opravit review nálezy
[ ] Opakovat dokud review čisté
[ ] git commit
```
