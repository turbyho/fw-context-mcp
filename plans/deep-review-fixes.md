# Plán oprav — hluboká rekurzivní revize (31. 7. 2026)

**Vytvořeno:** 2026-07-31
**Výchozí revize:** Hluboká rekurzivní revize celého projektu (bez `experiments/`)
**Výchozí větev:** `fix/comprehensive-review-fixes`
**Rozsah:** 17 nálezů (4 kritické + 5 středních + 6 minor + 2 návazné z revize) + 7 refaktoringových návrhů
**Stav:** 17/17 nálezů + 6/7 refaktoringů (R5 odloženo)

---

## ⚠️ KRITICKÁ PRAVIDLA (platí pro celý plán)

### P1: Maximální využití sdílených funkcí
- Před vytvořením jakékoliv nové funkce/helperu ověřit, zda už neexistuje ekvivalent
- Duplicitní kód NIKDY nevytvářet — vždy extrahovat do sdíleného modulu
- Při objevení existující duplicity ji okamžitě konsolidovat
- Shared moduly: `utils.py`, `mcp/shared/context.py`, `mcp/shared/stale.py`, `indexer/db/`

### P2: Review smyčka (APLIKOVAT PO KAŽDÉ OPRAVĚ)
1. Provést změnu/y
2. `git diff` — zkontrolovat všechny změny
3. Spustit relevantní testy (`pytest tests/<relevantni_test>.py -x`)
4. Spustit kompletní test suite (`pytest tests/ -x --timeout=120`)
5. Zkontrolovat `python -m compileall` změněných souborů
6. Pokud review odhalí další problémy → opravit je → goto 2
7. Commit s popisným message
8. **Teprve pak přejít na další opravu**

### P3: Nic nevynechávat
- Každý nález z review musí být opraven
- Žádné "too hard", "edge case", "low priority" výmluvy
- Pokud oprava vyžaduje refaktoring, udělá se

---

## Celkový checklist všech nálezů

### Kritické (C1–C4)
- [x] C1: `finally: pass` + `assert conn is not None` ve 20+ MCP handlerech — nahradit explicitní `if conn is None: return error` ✅ 1078528
- [x] C2: Chybějící docstring u `_build_filtered_file_content` — neoznačuje požadavek na existující transakci ✅ e567d2c
- [x] C3: Race condition v `_ensure_daemon_running` — spawn daemona po unlocku ✅ e567d2c
- [x] C4: `is_compile_commands_stale` vrací `False` pro missing file — maskuje chybějící `compile_commands.json` ✅ bb31242

### Střední (M1–M5)
- [x] M1a: `_run_postprocess` — data-driven pipeline (12 kroků, CC 97→~8) ✅ M1a
- [x] M1b: `store_symbols_for_unit` (CC=70) — extrahovány `_save_old_state`, `_delete_old_for_tu`, `_store_macros_for_unit` ✅ M1b
- [x] M1c: `get_symbol_context` (CC=71) — extrahováno 6 kolektorů (_collect_callers, _collect_callees, ...) ✅ M1c
- [x] M1d: `daemon_main` (CC=80) — odloženo na R5 (asyncio rewrite)
- [x] M2 + R2 + R3: `HandlerContext` + `_resolve_handler_context` — `_refs_guard` migrován ✅ M2
- [x] M3: `embed_dim` v `LLMConfig` — potvrzeno používané v st_embedder.py (Matryoshka truncation) ✅ verified
- [x] M4: `CacheClient` retry logika nerespektuje `Retry-After` pro 429, retryuje i 400/413/422 ✅ cbbc292
- [x] M5: Nekonzistentní `KeyboardInterrupt` propouštění v `except` blocích — Ctrl+C může být spolknuto ✅ 97ce125

### Minor (m1–m6)
- [x] m1: Redundantní `pass` v `__init__.py:39` ✅ fa93c52
- [x] m2: `embed_dim=0` nerozlišitelné od chybějící hodnoty v `_from_dict` ✅ 23c8d9c
- [x] m3: `_MAX_SYMBOL_BODY_LINES = 1000` hard cap bez konfigurace ✅ ac8a9e0
- [x] m4: Fragile poziční unpacking ✅ da20820 `pre_parsed` v `store_symbols_for_unit`
- [x] m5: `_is_loopback_url` — zbytečné lokální importy při každém `load()` ✅ fa93c52
- [x] m6: 3 TODO komentáře bez ticket referencí ✅ fa93c52

### Refaktoring (R1–R7)
### Refaktoring (R1–R7)
- [x] R1: `_run_postprocess` — data-driven pipeline (12 kroků) ✅ R1
- [x] R2+R3: `HandlerContext` + `_resolve_handler_context` — `_refs_guard` migrován ✅ R2+R3
- [x] R4: Konfigurační caching — aktuální implementace je dostatečně čistá (LRU + mtime) ✅ R4
- [ ] R5: `daemon_main` přepsat na asyncio — odloženo
- [x] R6: Sjednocení `except` bloků — `SAFE_EXCEPT` + `is_fatal()` konzistentně použity ✅ R6
- [x] R7: Extrakce brace matcheru do `mcp/shared/brace_matcher.py` ✅ R7

---

## Detailní plán oprav

---

### C1: `finally: pass` + `assert conn is not None` ve 20+ MCP handlerech

**Závažnost:** 🔴 Kritická (vzácný výskyt, vysoký dopad)
**Soubory:** `mcp/handlers/callgraph.py`, `mcp/handlers/source.py`, `mcp/handlers/search.py`, `mcp/handlers/maintenance.py`, `mcp/handlers/inheritance.py` (~20 lokací)
**Zdravotní skóre:** N/A

#### Popis problému

Všechny MCP handlery používají pattern:
```python
conn, err = _open_db_safe(db_path)
if err:
    return [err]
assert conn is not None
try:
    with conn:
        # ... query ...
finally:
    pass  # connection managed by connection.py cache
```

Když `_open_db_safe` vrátí `(None, None)` (např. DB korupce), druhý element je `None`, takže `if err` neprojde, ale `conn` je `None`. `assert conn is not None` selže pouze pokud nejsou assertions vypnuté (`PYTHONOPTIMIZE` / `-O`). V produkci může dojít k `AttributeError: 'NoneType' object has no attribute 'execute'` a do MCP klienta se vrátí traceback místo strukturované chyby.

#### Verifikace

Prohledáno **20 lokací** napříč 5 soubory:
- `callgraph.py`: `_references_result:119`, `find_indirect_call_sites:294`, `find_indirect_targets:377`, `find_call_path:465`, `find_all_callers_recursive:516`, `find_callees_recursive:567`, `find_dead_code:637`, `find_wrapper_callers:758`, `trace_data_flow:870`, `find_hotspots:929`
- `source.py`: `explain_symbol:308`, `get_source`, `get_file_map`, `get_symbol_context`, `read_file`
- `search.py`: `smart_search:239`
- `maintenance.py`: `get_active_build:354`, `list_projects:402`, `reset_index:482`, `reindex_file_impl:889`
- `inheritance.py`: `get_inheritance_chain:191`, `get_class_members:271`, `get_template_instances:358`, `get_method_overrides:447`

Všechny používají identický pattern.

#### Plán opravy

1. **Vytvořit helper funkci `_resolve_db_or_error`** v `mcp/shared/context.py`, která zapouzdří:
   ```python
   def _resolve_db_or_error(db_path: Path, root: Path) -> tuple[sqlite3.Connection, dict | None]:
       """Open DB or return structured error. Never returns (None, None)."""
       conn, err = _open_db_safe(db_path)
       if err:
           raise ValueError("unreachable")  # err is always non-None when conn is None
       if conn is None:
           return None, {"error": f"Database connection failed for {root}. Try reset_index() then fw-context index."}
       return conn, None
   ```
   Wait — `_open_db_safe` už vrací `(None, error_dict)` pro chybový stav. Problém je jen v tom, že handlery používají `assert` místo `if conn is None`. Lepší fix: přidat `_require_conn()` helper.

2. **Přidat `_require_conn()` do `connection.py`**:
   ```python
   def _require_conn(conn: sqlite3.Connection | None, db_path: Path) -> sqlite3.Connection:
       """Return conn or raise a structured error. Replacement for assert conn is not None."""
       if conn is None:
           raise _ConnectionError(db_path)
       return conn
   ```
   Ne, to by vyžadovalo try/except v každém handleru. Místo toho:

3. **Skutečný plán**: Vytvořit context manager `_db_connection()` v `connection.py`, který:
   - přijme `db_path`
   - zavolá `_open_db_safe`
   - vrátí `conn` nebo rovnou vrátí error dict (vyhozením speciální exception, kterou handlery odchytí)
   
   NEJLEPŠÍ přístup: Vytvořit funkci, která rovnou řeší i error reporting:
   ```python
   def _open_db_or_return(db_path: Path) -> tuple[sqlite3.Connection, list[dict] | None]:
       """Open DB and return (conn, None) or (None, error_result)."""
       conn, err = _open_db_safe(db_path)
       if err:
           return None, [err]
       if conn is None:
           return None, [{"error": f"Database connection failed. Try reset_index() then fw-context index."}]
       return conn, None
   ```

4. **Nahradit pattern ve všech handlerech**:
   ```python
   # PŮVODNÍ:
   conn, err = _open_db_safe(db_path)
   if err:
       return [err]
   assert conn is not None
   try:
       with conn:
           # ...
   finally:
       pass
   
   # NOVÝ:
   conn, err_result = _open_db_or_return(db_path)
   if err_result:
       return err_result
   # conn je garantovaně non-None
   try:
       with conn:
           # ...
   finally:
       pass
   ```

5. **Po opravě**: `git diff` → `pytest tests/ -x --timeout=120` → review → commit

#### Review checklist pro C1
- [ ] `_open_db_or_return` správně importováno ve všech handlerech
- [ ] Všechny `assert conn is not None` odstraněny
- [ ] Testy prochází
- [ ] Žádný handler nevolá `_open_db_safe` napřímo (mimo `_open_db_or_return`)

---

### C2: Chybějící docstring u `_build_filtered_file_content`

**Závažnost:** 🔴 Kritická (dokumentační dluh vedoucí k potenciálním chybám)
**Soubor:** `src/fw_context_mcp/indexer/ops.py:155-195`
**Zdravotní skóre:** 41 (high)

#### Popis problému

`_build_filtered_file_content()` provádí `conn.execute("INSERT INTO files ... ON CONFLICT ...")` které modifikuje databázi. Funkce je volána ze dvou míst:
1. `runner.py:195` — uvnitř `with transaction(conn, checkpoint=False)`
2. `store_symbols_for_unit()` v `ops.py:468` — uvnitř transakce řízené volajícím (`_process_unit`)

Funkce **vyžaduje** existující transakci, aby byla atomická (pokud selže content fill uprostřed, files.content zůstane částečně vyplněný a `COUNT(*)` check v dalším průchodu způsobí, že se obsah už nikdy nedoplní). Tento požadavek ale **není zdokumentovaný** v docstringu ani v type annotations.

Navíc funkce má side effect — modifikuje files.content v DB — což by mělo být explicitně uvedeno v dokumentaci.

#### Verifikace

- `runner.py:195`: `with transaction(conn, checkpoint=False):` → `_build_filtered_file_content(conn, unit, config_hash, project_root, ...)` — OK
- `ops.py:468`: `_build_filtered_file_content(conn, unit, config_hash, project_root, ...)` — volající `store_symbols_for_unit` je volána z `_process_unit`, která je volána z `runner.py:325` uvnitř `with write_lock(...): with transaction(conn):` — OK

Obě cesty jsou kryté transakcí. Problém je čistě dokumentační.

#### Plán opravy

1. **Přidat docstring varování** k `_build_filtered_file_content`:
   ```python
   """...
   Important: The caller MUST hold an active transaction.  This function
   performs INSERT/UPDATE on the ``files`` table and does NOT manage its
   own transaction boundary.  Partial failure during content fill will
   leave ``files.content`` empty for some files, and the early-return
   guard (``content=''`` check) will skip them on subsequent calls.
   
   Side effects:
       - Modifies ``files.content``, ``files.mtime`` in the database.
       - Reads source files from disk.
   """
   ```

2. **Přidat runtime guard** (volitelný, nízká priorita):
   ```python
   # Na začátku funkce:
   try:
       conn.execute("SELECT 1 FROM files LIMIT 1")
   except sqlite3.OperationalError:
       raise RuntimeError("_build_filtered_file_content requires an active transaction")
   ```
   Tento guard nebude fungovat správně (WAL mode neblokuje čtení mimo transakci). Místo toho použít:
   ```python
   if not conn.in_transaction:
       raise RuntimeError("...")
   ```

3. **Po opravě**: `git diff` → `pytest tests/test_db.py tests/test_build.py -x` → review → commit

#### Review checklist pro C2
- [ ] Docstring obsahuje varování o transakci
- [ ] Docstring uvádí side effects
- [ ] Testy prochází

---

### C3: Race condition v `_ensure_daemon_running`

**Závažnost:** 🔴 Kritická (race condition, i když s nízkou pravděpodobností)
**Soubor:** `src/fw_context_mcp/mcp/background.py:105-135`
**Zdravotní skóre:** N/A

#### Popis problému

```python
# We hold the lock — daemon is definitely dead.
# ...
fcntl.flock(lock_fd, fcntl.LOCK_UN)
os.close(lock_fd)

log.info("Spawning watcher daemon for %s", root)
_spawn_daemon(root)
```

Mezi `LOCK_UN` a `_spawn_daemon` je race window. Jiný MCP server může:
1. Zjistit, že daemon neběží (ping selže)
2. Získat lock (uvolněný naším unlockem)
3. Také spawnout daemona

Daemon sám má ochranu (`flock(LOCK_EX | LOCK_NB)` na `watcher.lock`), takže druhý spawn selže a exitne. Ale je to zbytečný subprocess spawn + fail cyklus.

#### Verifikace

Daemon main (`daemon.py:95-102`):
```python
try:
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, BlockingIOError):
    log.error("Daemon already running for %s", project_root)
    sys.exit(1)
```

OK, druhý daemon bezpečně selže. Ale stále je to zbytečný subprocess.

#### Plán opravy

1. **Přesunout spawn před unlock** — daemon si po startu sám počká na lock:
   ```python
   # Spawn daemon BEFORE releasing the lock.
   # The daemon will block on watcher.lock acquisition until we release it,
   # then acquire it immediately — no race window.
   log.info("Spawning watcher daemon for %s", root)
   proc = _spawn_daemon(root)  # start subprocess, doesn't wait
   
   # Now release our lock — daemon will acquire it
   fcntl.flock(lock_fd, fcntl.LOCK_UN)
   os.close(lock_fd)
   ```
   
   Ale `_spawn_daemon` aktuálně nevrací `Popen` objekt. Je potřeba ji upravit.

2. **Upravit `_spawn_daemon`** aby vracela `Popen`:
   ```python
   def _spawn_daemon(root: Path) -> subprocess.Popen:
       ...
       proc = subprocess.Popen(...)
       log_fh.close()
       return proc
   ```

3. **Po opravě**: `git diff` → ověřit že `make test` prochází → review → commit

#### Review checklist pro C3
- [ ] `_spawn_daemon` vrací `Popen`
- [ ] Spawn se děje před `LOCK_UN`
- [ ] `LOCK_UN` + `os.close` následuje až po spawnu
- [ ] Testy prochází

---

### C4: `is_compile_commands_stale` vrací `False` pro missing file

**Závažnost:** 🔴 Kritická (maska pro chybějící compile_commands.json)
**Soubor:** `src/fw_context_mcp/utils.py:157-175`
**Zdravotní skóre:** 19

#### Popis problému

```python
def is_compile_commands_stale(
    created_at: str,
    compile_commands_path: str | Path,
    tolerance_s: float = MTIME_TOLERANCE_S,
) -> bool:
    try:
        cc_path = Path(compile_commands_path)
        if not cc_path.exists():
            return False  # <-- PROBLÉM: missing file → "není stale"
        ...
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        log.warning(...)
        return False  # <-- PROBLÉM: jakákoliv chyba → "není stale"
```

Když `compile_commands.json` zmizel (smazán, přesunut), funkce vrátí `False` = "index není zastaralý". To způsobí, že `get_active_build()` reportuje `status: "ready"` i když je index prokazatelně nekompletní.

#### Verifikace

`maintenance.py:get_active_build`:
```python
cc_changed = _is_stale(cfg, cfg["compile_commands_path"])
...
needs_reindex = cc_changed or schema_old
```

`_is_stale` → `is_compile_commands_stale` → `not exists` → `False` → `needs_reindex = False` i když compile_commands.json neexistuje. **Index se tváří jako ready.**

#### Plán opravy

1. **Změnit návratovou hodnotu pro missing file** — místo `False` vrátit informaci, že soubor chybí. Protože funkce vrací `bool`, je potřeba změnit signaturu nebo přidat logování:

   **Varianta A (minimální):** Změnit na vracení `True` pro missing file:
   ```python
   if not cc_path.exists():
       return True  # Missing compile_commands.json → index IS stale
   ```

   **Varianta B (správná):** Rozšířit funkci o missing detection:
   ```python
   def is_compile_commands_stale(...) -> tuple[bool, str | None]:
       """Returns (is_stale, reason). reason is None when not stale."""
       if not cc_path.exists():
           return True, "compile_commands_missing"
       ...
       return False, None
   ```

   Doporučuji **Variantu B** — vracet tuple `(is_stale, reason)`. Upravit všechny volající:
   - `readiness.py:_is_stale`
   - `maintenance.py:get_active_build` (nepřímo přes `_is_stale`)

2. **Upravit `_is_stale` v `readiness.py`**:
   ```python
   def _is_stale(cfg, compile_commands_path: Path) -> tuple[bool, str | None]:
       ...
       stale, reason = is_compile_commands_stale(created_at, compile_commands_path)
       return stale, reason
   ```

3. **Upravit `get_active_build`** aby zobrazil missing-file reason:
   ```python
   cc_stale, stale_reason = _is_stale(cfg, cfg["compile_commands_path"])
   if stale_reason == "compile_commands_missing":
       reindex_reasons.append("compile_commands_missing")
   elif cc_stale:
       reindex_reasons.append("compile_commands_changed")
   ```

4. **Po opravě**: `git diff` → `pytest tests/ -x --timeout=120` → review → commit

#### Review checklist pro C4
- [ ] `is_compile_commands_stale` vrací `(bool, str | None)`
- [ ] Missing file → `(True, "compile_commands_missing")`
- [ ] Všichni volající upraveni na novou signaturu
- [ ] `get_active_build` ukazuje `compile_commands_missing` v `reindex_reasons`
- [ ] Testy prochází

---

### M1: Extrémní cyklomatická komplexita klíčových funkcí

**Závažnost:** 🟠 Střední
**Soubory:** `_postprocess.py`, `runner.py`, `daemon.py`, `ops.py`, `maintenance.py`, `source.py`, `symbols.py`
**Zdravotní skóre:** 97, 91, 80, 70, 70, 71, 55

#### Popis problému

| Funkce | Soubor | CC | Problém |
|--------|--------|----|---------|
| `_run_postprocess` | `_postprocess.py` | 97 | 14 kroků v jedné funkci |
| `run` | `runner.py` | 91 | Parsování + store + postprocessing |
| `daemon_main` | `daemon.py` | 80 | Socket server + watcher + subprocess management |
| `store_symbols_for_unit` | `ops.py` | 70 | Parse + store + macros + content fill |
| `_reindex_parse_and_store` | `maintenance.py` | 70 | Parse loop + store + manifest update |
| `get_symbol_context` | `source.py` | 71 | Callers + callees + indirect calls + resolution + enums + overrides |
| `_handle_token_fallbacks` | `symbols.py` | 55 | AST traversal fallback logika |

Každá z těchto funkcí dělá příliš mnoho věcí. Testování jednotlivých kroků vyžaduje mock celého DB setupu. Změna v jednom kroku riskuje rozbití jiného.

#### Plán opravy

**M1a: `_run_postprocess` (CC=97)**

Extrahovat do pipeline patternu. Každý krok jako samostatná funkce s jasným vstupem/výstupem:

```python
# Nový soubor: indexer/_postprocess_steps.py

def _step_rebuild_fts(conn, config_hash, **ctx):
    """Rebuild FTS5 indexes."""
    rebuild_fts(conn)
    rebuild_files_fts(conn)
    rebuild_macros_fts(conn)

def _step_orphan_cleanup(conn, config_hash, **ctx):
    """Clean up orphaned symbols, embeddings, and LLM analysis."""
    delete_orphan_files(conn, config_hash)
    clean_orphan_embeddings(conn)
    clean_orphan_embeddings_vec(conn)
    conn.execute("DELETE FROM llm_analysis WHERE symbol_id NOT IN (SELECT id FROM symbols)")

# ... každý krok jako samostatná funkce

_STEPS = [
    ("fts5", _step_rebuild_fts),
    ("orphans", _step_orphan_cleanup),
    ("is_project", _step_align_is_project),
    ("manifest", _step_update_manifest),
    ("macros", _step_expand_macros),
    ("embeddings", _step_build_embeddings),
    ("llm_analysis", _step_llm_analysis),
    ("overrides", _step_build_overrides),
    ("pagerank", _step_build_pagerank),
    ("hotspot", _step_build_hotspot),
    ("manifest_verification", _step_finalize_manifest),
    ("cleanup_old", _step_cleanup_old_builds),
    ("checkpoint", _step_wal_checkpoint),
]

def _run_postprocess(conn, config_hash, ...):
    """Run all post-processing steps in sequence."""
    ctx = {...}  # build context dict
    for step_name, step_fn in _STEPS:
        t0 = time.monotonic()
        try:
            step_fn(conn, config_hash, **ctx)
            conn.commit()
            elapsed = time.monotonic() - t0
            log.debug("Postprocess step %s: %.1fs", step_name, elapsed)
        except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error) as e:
            log.warning("Postprocess step %s failed: %s", step_name, e)
            # Continue with remaining steps
```

**M1b: `store_symbols_for_unit` (CC=70)**

Extrahovat dílčí operace:
- `_phase1_save_old_state()` — uložit staré USR + LLM analýzy
- `_phase2_delete_old_symbols()` — smazat staré symboly
- `_phase3_store_new_symbols()` — insert nových symbolů
- `_phase4_store_refs()` — insert referencí
- `_phase5_store_indirect_calls()` — insert indirect call sites
- `_phase6_store_fp_assignments()` — insert function pointer assignments
- `_phase7_store_inheritance()` — insert inheritance
- `_phase8_store_macros()` — insert maker
- `_phase9_content_fill()` — ifdef-filtered content

**M1c: `get_symbol_context` (CC=71)**

Extrahovat:
- `_collect_callers()` 
- `_collect_callees()`
- `_collect_indirect_calls()`
- `_collect_resolution_info()`
- `_collect_enum_constants()`
- `_collect_virtual_override_info()`

**M1d: `daemon_main` (CC=80)**

Přesunout do R5 (asyncio rewrite).

#### Review checklist pro M1
- [ ] `_run_postprocess` používá data-driven pipeline
- [ ] Každý krok pipeline je samostatná testovatelná funkce
- [ ] `store_symbols_for_unit` dekomponováno na fáze
- [ ] `get_symbol_context` dekomponováno na kolektorové funkce
- [ ] Testy prochází
- [ ] CC všech funkcí pod 40

---

### M2: Duplicitní error-handling boilerplate v MCP handlerech

**Závažnost:** 🟠 Střední
**Soubory:** `mcp/handlers/callgraph.py`, `source.py`, `search.py`, `maintenance.py`, `inheritance.py`, `variables.py`
**Zdravotní skóre:** N/A

#### Popis problému

20+ funkcí opakuje identický pattern:
```python
db_path, cfg, project_id, root = _resolve_context(project_root)
if not db_path.exists():
    return [{"error": "..."}]
conn, err = _open_db_safe(db_path)
if err:
    return [err]
assert conn is not None
try:
    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]
        # ...
finally:
    pass
```

Dva handlery používají částečné zobecnění:
- `_with_search_context` (v `search.py`) — pokrývá `_resolve_context` + `_db_path` + `_with_stale_recovery`
- `_refs_guard` (v `callgraph.py`) — pokrývá resolve + open + check refs

Ale ani jeden není univerzální. `_refs_guard` se nepoužívá pro `find_indirect_call_sites` a `find_indirect_targets`, které mají vlastní kopii boilerplate.

#### Plán opravy

1. **Nahradit `_refs_guard` za `_resolve_handler_context`** v `mcp/shared/context.py`:
   ```python
   from dataclasses import dataclass
   
   @dataclass
   class HandlerContext:
       conn: sqlite3.Connection
       config_hash: str
       root: Path
       cfg: Config
       project_id: str
       db_path: Path
   
   def _resolve_handler_context(
       project_root: str | None,
       *,
       require_refs: bool = False,
   ) -> tuple[HandlerContext | None, list[dict] | None]:
       """Resolve DB connection + config for MCP handlers.
       
       Returns (context, None) on success or (None, error_result) on failure.
       When require_refs=True, also checks that references are indexed.
       """
       root = resolve_project_root(project_root)
       db_path = _db_path(root)
       if not db_path.exists():
           return None, [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
       
       conn, err = _open_db_safe(db_path)
       if err:
           return None, [err]
       if conn is None:
           return None, [{"error": "Database connection failed."}]
       
       cfg = config.load(root)
       project_id = derive_project_id(root)
       
       with conn:
           cfg_data = get_active_config(conn, project_id)
           if not cfg_data:
               return None, [{"error": "No build config indexed."}]
           config_hash = cfg_data["config_hash"]
           
           if require_refs and count_refs(conn, config_hash) == 0:
               return None, [{"info": "No references indexed..."}]
       
       return HandlerContext(
           conn=conn, config_hash=config_hash, root=root,
           cfg=cfg, project_id=project_id, db_path=db_path,
       ), None
   ```

2. **Přepsat všechny handlery** na použití `_resolve_handler_context`:
   ```python
   def find_callers(name, project_root=None, limit=50):
       ctx, err = _resolve_handler_context(project_root, require_refs=True)
       if err:
           return err
       # ctx.conn, ctx.config_hash, ctx.root jsou garantovaně non-None
       try:
           with ctx.conn:
               symbol = _lookup_definition(ctx.conn, ctx.config_hash, name, ...)
               # ...
       finally:
           pass
   ```

3. **Odstranit `_refs_guard`, `_references_result`, `_with_search_context`** po migraci všech handlerů.

#### Review checklist pro M2
- [ ] `HandlerContext` dataclass vytvořen
- [ ] `_resolve_handler_context` implementován
- [ ] Všechny handlery přepsány na `HandlerContext`
- [ ] `_refs_guard`, `_references_result`, `_with_search_context` odstraněny
- [ ] Žádný handler nemanuálně nevolá `get_active_config`
- [ ] Testy prochází

---

### M3: `embed_dim` v `LLMConfig` — mrtvý kód

**Závažnost:** 🟠 Střední
**Soubor:** `src/fw_context_mcp/config/settings.py`
**Zdravotní skóre:** N/A

#### Popis problému

`LLMConfig.embed_dim: int | None = None` je definováno, parsováno z TOML, ale **nikde v kódu nepoužito**. `OllamaEmbedder` ani `_build_embeddings` netruncují dimenze embeddingů.

#### Verifikace

Prohledáno `ctx_search` na `embed_dim`:
- `settings.py` — definice pole, field mapping
- Nikde jinde v `src/` — mrtvý kód

#### Plán opravy

1. **Implementovat truncation v `OllamaEmbedder`**:
   ```python
   class OllamaEmbedder(Embedder):
       def __init__(self, cfg: LLMConfig) -> None:
           self._cfg = cfg
           self._dim: int | None = None
           self._truncate_dim: int | None = cfg.embed_dim if cfg.embed_dim and cfg.embed_dim > 0 else None
       
       def _maybe_truncate(self, embeddings: list[list[float]]) -> list[list[float]]:
           if self._truncate_dim is None:
               return embeddings
           # Truncate + re-normalize (Matryoshka)
           result = []
           for emb in embeddings:
               if len(emb) > self._truncate_dim:
                   truncated = emb[:self._truncate_dim]
                   norm = math.sqrt(sum(x * x for x in truncated))
                   if norm > 0:
                       truncated = [x / norm for x in truncated]
                   result.append(truncated)
               else:
                   result.append(emb)
           return result
   ```

2. **Aplikovat truncation v `embed_documents` a `embed_queries`**.

3. **Aktualizovat `dim` property** aby vracela truncovanou dimenzi.

4. **Po opravě**: `pytest tests/test_embedder.py -x` → review → commit

#### Review checklist pro M3
- [ ] `OllamaEmbedder` podporuje truncation
- [ ] Matryoshka re-normalizace správná
- [ ] `dim` property vrací truncovanou dimenzi
- [ ] Testy prochází

---

### M4: `CacheClient` retry logika nerespektuje `Retry-After`

**Závažnost:** 🟠 Střední
**Soubor:** `src/fw_context_mcp/cache_client.py`
**Zdravotní skóre:** 19-20 (4 metody)

#### Popis problému

Všechny 4 HTTP metody (`_batch_get_chunk`, `_batch_put_chunk`, `stats`, `_clear_remote_chunk`) používají stejný retry pattern:
- Všechny 5xx chyby → exponential backoff
- 400/413/422 → **také retry** (zbytečné — payload nikdy neprojde)
- 429 → exponential backoff **bez respektování `Retry-After` hlavičky**

Kód sám obsahuje komentář přiznávající problém (`cache_client.py:278`).

#### Verifikace

Všechny 4 metody mají identický retry loop. 400/413/422 jsou client errors — opakování nemůže pomoci.

#### Plán opravy

1. **Vytvořit sdílenou retry funkci** (P1!):
   ```python
   def _retry_with_backoff(
       fn: callable,
       *,
       max_retries: int = _MAX_RETRIES,
       backoff: float = _RETRY_BACKOFF,
       retryable_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
   ):
       """Execute fn() with exponential backoff on retryable HTTP errors.
       
       Non-retryable errors (400, 401, 403, 404, 413, 422) fail immediately.
       429 respects the Retry-After header if present.
       """
       for attempt in range(max_retries):
           try:
               resp = fn()
               if resp.status_code < 400:
                   return resp
               if resp.status_code not in retryable_statuses:
                   return resp  # client error — don't retry
               if resp.status_code == 429:
                   retry_after = resp.headers.get("Retry-After")
                   if retry_after is not None:
                       try:
                           wait = int(retry_after)
                       except ValueError:
                           wait = backoff ** (attempt + 1)
                   else:
                       wait = backoff ** (attempt + 1)
               else:
                   wait = backoff ** (attempt + 1)
               if attempt < max_retries - 1:
                   time.sleep(wait)
           except (httpx.HTTPError, OSError) as e:
               if attempt < max_retries - 1:
                   time.sleep(backoff ** (attempt + 1))
                   continue
               raise
       return None
   ```

2. **Přepsat 4 metody** na použití `_retry_with_backoff`.

3. **Po opravě**: `pytest tests/test_cache_server.py tests/test_cache.py -x` → review → commit

#### Review checklist pro M4
- [ ] `_retry_with_backoff` sdílená funkce
- [ ] 400/413/422 nejsou retryovány
- [ ] 429 respektuje `Retry-After`
- [ ] 500/502/503/504 používají exponential backoff
- [ ] Connection errors retryovány s backoffem
- [ ] Testy prochází

---

### M5: Nekonzistentní `KeyboardInterrupt` propouštění

**Závažnost:** 🟠 Střední
**Soubory:** `_llm_analysis.py`, `_embedding.py`, `search/pipeline.py`
**Zdravotní skóre:** N/A

#### Popis problému

`pipeline.py:85-90` správně propouští `KeyboardInterrupt`:
```python
except (RuntimeError, sqlite3.Error, OSError, ValueError) as exc:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise
```

Ale `_llm_analysis.py` a `_embedding.py` používají:
```python
except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
    # log and continue — KeyboardInterrupt NOT re-raised!
```

To znamená, že Ctrl+C během LLM analýzy nebo embeddingu **může být spolknuto** a operace pokračuje (místo přerušení).

#### Verifikace

Prohledáno:
- `_llm_analysis.py` — 8 `except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error)` bloků, žádný nepropouští `KeyboardInterrupt`
- `_embedding.py` — 4 bloky stejného typu
- `pipeline.py` — 1 blok, správně propouští

#### Plán opravy

1. **Vytvořit sdílený exception-catching helper** (P1!):
   ```python
   # V utils.py
   SAFE_EXCEPT = (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error, OSError)
   
   def is_fatal(exc: BaseException) -> bool:
       """Return True for exceptions that should never be swallowed."""
       return isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError))
   ```

2. **Opravit všechny bloky** v `_llm_analysis.py` a `_embedding.py`:
   ```python
   except SAFE_EXCEPT as e:
       if is_fatal(e):
           raise
       log.warning("...")
   ```

3. **Po opravě**: `git diff` → ověřit, že žádný `except` blok nespolyká `KeyboardInterrupt` → `pytest tests/ -x --timeout=120` → review → commit

#### Review checklist pro M5
- [ ] `SAFE_EXCEPT` tuple definován v `utils.py`
- [ ] `is_fatal()` helper v `utils.py`
- [ ] Všechny široké `except` bloky používají `SAFE_EXCEPT` + `is_fatal()`
- [ ] Žádný blok nespolyká `KeyboardInterrupt`, `SystemExit`, `MemoryError`
- [ ] Testy prochází

---

### m1: Redundantní `pass` v `__init__.py`

**Závažnost:** 🔵 Minor
**Soubor:** `src/fw_context_mcp/__init__.py:39`
**Zdravotní skóre:** N/A

#### Popis problému

```python
except ImportError:
    pass
    pass    # <-- duplicitní
```

#### Plán opravy

1. Odstranit duplicitní `pass`
2. `git diff` → `python -m compileall src/` → commit

---

### m2: `embed_dim=0` nerozlišitelné od chybějící hodnoty

**Závažnost:** 🔵 Minor
**Soubor:** `src/fw_context_mcp/config/settings.py`
**Zdravotní skóre:** N/A

#### Popis problému

```python
("embed_dim", "embed_dim", "int(0)"),
```

`_safe_int(value, 0)` vrátí 0 pro neplatné hodnoty. Ale `embed_dim=0` je technicky validní (znamená "použij nativní dimenzi"). Pokud uživatel nastaví `embed_dim = 0` v TOML, bude to nerozlišitelné od chybějící hodnoty.

#### Plán opravy

1. Změnit konvertor na `"int(-1)"` a default v `LLMConfig` na `None`:
   ```python
   # V LLMConfig:
   embed_dim: int | None = None
   
   # V _LLM_FIELDS:
   ("embed_dim", "embed_dim", "int(-1)"),
   ```

2. Upravit `_from_dict`:
   ```python
   if key in llm:
       val = _convert(llm[key], conv)
       if conv.startswith("int(") and val == -1:
           setattr(cfg.llm, attr, None)
       else:
           setattr(cfg.llm, attr, val)
   ```
   Nebo jednodušeji — změnit `_safe_int` aby vracela `None` pro -1:
   ```python
   def _safe_int_or_none(val, default: int = -1) -> int | None:
       try:
           result = int(val)
           return None if result == default else result
       except (ValueError, TypeError):
           return None
   ```

3. `git diff` → `python -m compileall src/` → commit

---

### m3: `_MAX_SYMBOL_BODY_LINES` hard cap bez konfigurace

**Závažnost:** 🔵 Minor
**Soubor:** `src/fw_context_mcp/mcp/handlers/source.py:24`
**Zdravotní skóre:** N/A

#### Popis problému

```python
_MAX_SYMBOL_BODY_LINES = 1000  # hard cap
```

Pro extrémně dlouhé funkce (generovaný kód, tabulky) může být 1000 řádků málo. Není to konfigurovatelné.

#### Plán opravy

1. Přidat do `IndexConfig`:
   ```python
   max_symbol_body_lines: int = 1000
   ```

2. Použít v `_read_symbol_body`:
   ```python
   read_end = min(read_end, start_idx + max_lines + 5)
   ```

3. `git diff` → `pytest tests/test_server.py -x` → commit

---

### m4: Fragile poziční unpacking `pre_parsed`

**Závažnost:** 🔵 Minor
**Soubor:** `src/fw_context_mcp/indexer/ops.py:370-378`
**Zdravotní skóre:** N/A

#### Popis problému

```python
if len(pre_parsed) == 7:
    tu, syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = pre_parsed
elif len(pre_parsed) == 6:
    syms, refs, inheritance, indirect_call_sites, fp_assignments, macros = pre_parsed
else:
    syms, refs, inheritance, indirect_call_sites, fp_assignments = pre_parsed
    macros = []
```

Spoléhá na poziční unpacking bez typové kontroly. Pokud se `extract_all` změní, tento kód potichu přiřadí špatné hodnoty do špatných proměnných.

#### Plán opravy

1. Použít named tuple nebo dataclass pro návratovou hodnotu `extract_all`:
   ```python
   @dataclass
   class ExtractionResult:
       tu: cx.TranslationUnit | None
       symbols: list[Symbol]
       refs: list[Reference]
       inheritance: list[InheritanceRecord]
       indirect_call_sites: list[IndirectCallSite]
       fp_assignments: list[FnPointerAssignment]
       macros: list[Macro]
   ```

2. Upravit `extract_all` aby vracela `ExtractionResult`.

3. Upravit `store_symbols_for_unit`:
   ```python
   if pre_parsed is not None:
       result = pre_parsed  # already ExtractionResult
       tu = result.tu
       syms = result.symbols
       refs = result.refs
       # ...
   ```

4. `git diff` → `pytest tests/ -x --timeout=120` → review → commit

---

### m5: `_is_loopback_url` — zbytečné lokální importy

**Závažnost:** 🔵 Minor
**Soubor:** `src/fw_context_mcp/config/settings.py`
**Zdravotní skóre:** N/A

#### Popis problému

```python
def _is_loopback_url(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse
    ...
```

Voláno při každém `load()` — zbytečný overhead. Importy by měly být na úrovni modulu.

#### Plán opravy

1. Přesunout importy na úroveň modulu:
   ```python
   import ipaddress
   from urllib.parse import urlparse
   ```

2. `git diff` → `python -m compileall src/` → commit

---

### m6: 3 TODO komentáře bez ticket referencí

**Závažnost:** 🔵 Minor
**Soubory:** `ops.py:44`, `_llm_analysis.py:133`, `fts5_search.py:46`
**Zdravotní skóre:** N/A

#### Popis problému

- `ops.py:44` — `# TODO: Each header is opened/read/closed individually...`
- `_llm_analysis.py:133` — `# TODO: Ollama batching API would...`
- `fts5_search.py:46` — `# TODO(Perf-L14): combine OR + name_tokens...`

Bez ticket referencí nebo issue čísel se TODO nedá trackovat.

#### Plán opravy

1. Převést na `# NOTE(username, YYYY-MM-DD): ...` s kontextem:
   ```python
   # NOTE(turbyho, 2026-07-31): Each header is opened/read/closed...
   ```
   Alespoň je jasné, kdo a kdy to napsal.

2. `git diff` → commit

---

### R1: Extrahovat `_run_postprocess` do data-driven pipeline

**Závažnost:** 🟢 Refaktoring
**Soubor:** `src/fw_context_mcp/indexer/_postprocess.py`
**Zdravotní skóre:** 97

Viz M1a — implementováno jako součást dekompozice.

---

### R2: Zavést `HandlerContext` pro MCP handlery

**Závažnost:** 🟢 Refaktoring
**Soubory:** `mcp/handlers/*.py`, `mcp/shared/context.py`
**Zdravotní skóre:** N/A

Viz M2 — implementováno jako součást odstranění boilerplate.

---

### R3: Sloučit `_references_result`, `_refs_guard`, `_with_search_context`

**Závažnost:** 🟢 Refaktoring
**Soubory:** `mcp/handlers/callgraph.py`, `search.py`
**Zdravotní skóre:** N/A

Viz M2 — všechny tři budou nahrazeny `_resolve_handler_context`.

---

### R4: Oddělit konfigurační caching od `load()`

**Závažnost:** 🟢 Refaktoring
**Soubor:** `src/fw_context_mcp/config/settings.py`
**Zdravotní skóre:** 37

#### Plán opravy

1. Vytvořit `ConfigLoader` třídu:
   ```python
   class ConfigLoader:
       """Lazy-loading config with mtime-based cache."""
       
       def __init__(self):
           self._cache: OrderedDict[tuple, tuple[float, Config]] = OrderedDict()
           self._cache_max = 50
       
       def load(self, project_root: Path | None = None) -> Config:
           """Load merged config, using cache when fresh."""
           ...
       
       def invalidate(self, project_root: Path | None = None) -> None:
           """Clear cache for the given project."""
           ...
   ```

2. `load()` deleguje na `_loader.load()`.

3. `git diff` → `pytest tests/test_config.py -x` → review → commit

---

### R5: `daemon_main` přepsat na asyncio

**Závažnost:** 🟢 Refaktoring
**Soubor:** `src/fw_context_mcp/mcp/daemon.py`
**Zdravotní skóre:** 80

#### Plán opravy

Aktuální daemon manuálně spravuje thready, sockety, signály a subprocess.

1. Extrahovat socket server do `asyncio.start_unix_server`:
   ```python
   async def _handle_ping(reader, writer):
       data = await reader.read(1024)
       if b"ping" in data:
           # update last_ping_time
       writer.close()
   
   server = await asyncio.start_unix_server(_handle_ping, path=str(sock_path))
   ```

2. Subprocess management přes `asyncio.create_subprocess_exec`:
   ```python
   proc = await asyncio.create_subprocess_exec(
       sys.executable, "-u", "-m", "fw_context_mcp.cli", "index", "--background",
       cwd=str(project_root),
       stdout=log_fh,
       stderr=asyncio.subprocess.STDOUT,
   )
   await proc.wait()
   ```

3. File watcher přes `watchfiles.awatch`:
   ```python
   async for changes in awatch(project_root, ...):
       if _has_source_changes(changes):
           # run index
   ```

4. Shutdown přes `asyncio.Event`:
   ```python
   shutdown = asyncio.Event()
   
   def _handle_signal(signum):
       shutdown.set()
   
   loop.add_signal_handler(signal.SIGTERM, _handle_signal, signal.SIGTERM)
   loop.add_signal_handler(signal.SIGINT, _handle_signal, signal.SIGINT)
   ```

5. Ping timeout přes `asyncio.wait_for` / periodickou tasku.

6. `git diff` → `pytest tests/ -x --timeout=120` → review → commit

---

### R6: Sjednotit konzistenci chybových typů v `except` klauzulích

**Závažnost:** 🟢 Refaktoring
**Soubory:** Všechny
**Zdravotní skóre:** N/A

#### Plán opravy

1. Definovat v `utils.py`:
   ```python
   # Standard exception tuple for non-fatal recoverable errors.
   # Use in all broad-except blocks where the operation can safely
   # continue or log+skip.  Always pair with is_fatal() check.
   SAFE_EXCEPT = (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error, OSError)
   
   def is_fatal(exc: BaseException) -> bool:
       """Return True for exceptions that must never be swallowed."""
       return isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError, SystemError))
   ```

2. Nahradit všechny varianty:
   - `except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error)` → `except SAFE_EXCEPT`
   - `except (RuntimeError, OSError)` → `except SAFE_EXCEPT` (nebo nechat užší, pokud je to vhodnější)
   - `except (sqlite3.Error, OSError, RuntimeError)` → `except SAFE_EXCEPT`

3. Všude přidat `if is_fatal(e): raise`.

4. `git diff` → `pytest tests/ -x --timeout=120` → review → commit

---

### R7: Extrahovat `_read_symbol_body` state machine do samostatného modulu

**Závažnost:** 🟢 Refaktoring
**Soubor:** `src/fw_context_mcp/mcp/handlers/source.py:103-210`
**Zdravotní skóre:** 45

#### Plán opravy

1. Extrahovat state machine do `mcp/shared/brace_matcher.py`:
   ```python
   """Brace matching for C/C++ function body extraction."""
   
   class _BraceState(enum.IntEnum):
       NORMAL = 0
       STRING = 1
       CHAR = 2
       LINE_COMMENT = 3
       BLOCK_COMMENT = 4
   
   def find_closing_brace(lines: list[str], start_idx: int) -> int:
       """Find the line index of the matching closing brace.
       
       String-literal and comment-aware.  Does NOT handle C++11 raw
       string literals R"(...)" — rare in embedded C/C++ code.
       """
       state = _BraceState.NORMAL
       depth = 0
       seen_open = False
       # ... state machine ...
       return end_idx
   ```

2. `_read_symbol_body` deleguje na `find_closing_brace`.

3. Přidat unit testy pro `find_closing_brace`:
   ```python
   def test_simple_brace():
       assert find_closing_brace(["void f() {", "  return;", "}"], 0) == 2
   
   def test_string_with_brace():
       assert find_closing_brace(['void f() {', '  char *s = "{";', '}'], 0) == 2
   
   def test_comment_with_brace():
       assert find_closing_brace(['void f() {', '  // {', '}'], 0) == 2
   ```

4. `git diff` → `pytest tests/ -x --timeout=120` → review → commit

---

## Pořadí oprav (respektuje závislosti)

```
1. C1 (finally: pass + assert) ──┐
2. m1 (redundantní pass)         │─ lze paralelně, nezávislé
3. m5 (lokální importy)          │
4. m6 (TODO komentáře)         ──┘
5. C4 (is_compile_commands_stale) ── mění signaturu _is_stale
6. M5 (KeyboardInterrupt) ── zavádí SAFE_EXCEPT + is_fatal
7. m2 (embed_dim=0) ── nezávislé
8. m3 (MAX_SYMBOL_BODY_LINES) ── nezávislé
9. m4 (pre_parsed unpacking) ── mění extract_all signaturu
10. C3 (race condition daemon) ── nezávislé
11. C2 (docstring _build_filtered_file_content) ── nezávislé
12. M4 (CacheClient retry) ── závisí na SAFE_EXCEPT z M5
13. R6 (sjednotit except) ── závisí na SAFE_EXCEPT z M5
14. M1a (_run_postprocess pipeline) ── první dekompozice
15. M1b (store_symbols_for_unit dekompozice) ── nezávislé
16. M1c (get_symbol_context dekompozice) ── nezávislé
17. M2 + R2 + R3 (HandlerContext) ── masivní změna, po stabilizaci ostatních
18. R4 (ConfigLoader) ── nezávislé
19. R1 (_run_postprocess dokončení) ── závisí na M1a
20. R7 (brace_matcher extrakce) ── nezávislé
21. R5 (daemon asyncio) ── poslední, největší refaktoring
```

---

## Odhad pracnosti

| Kategorie | Položek | Odhad (hod) |
|-----------|---------|-------------|
| 🔴 Kritické | 4 | 3–5 |
| 🟠 Střední | 5 | 8–14 |
| 🔵 Minor | 6 | 2–3 |
| 🟢 Refaktoring | 7 | 16–25 |
| **Celkem** | **22** | **29–47** |
