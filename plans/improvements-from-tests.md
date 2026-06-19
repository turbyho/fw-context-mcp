# Plan: fw-context vylepšení na základě testů

**Projekt:** fw-context-mcp (`~/dev/sw/work/tools/fw-context-mcp`)
**Větev:** `main`
**Status:** P0.1 hotovo (cd79a56), P0.2 neaplikovatelné, P1–P3 hotovo dříve

## Výsledek implementace (2026-06-19)

### P0.1 — Root cause a oprava

Diagnostika na reálných projektech (zbox-ecb-fw, HA_Boiler) odhalila:

1. **Reference v indexu JSOU** — `find_callees_recursive("ZMODEM::thread_app")` vrací
   `ZMODEM_DRIVER::send`, takže indexer ukládá field-access volání správně.

2. **Problém byl v `find_refs` name resolution** — dotaz `name = ? OR qualified_name = ?`
   nerozpoznal částečně kvalifikovaná jména jako `ZMODEM_DRIVER::send`:
   - `name` sloupec obsahuje jen `send`
   - `qualified_name` obsahuje `zbox::ZMODEM_DRIVER::send`
   - `ZMODEM_DRIVER::send` neodpovídá ani jednomu

3. **Oprava:** Přidán suffix LIKE fallback (`qualified_name LIKE '%::<name>'`)
   do `find_refs` (obě větve) a `_resolve_target_usr`. Tím se pokryjí všechna
   částečně kvalifikovaná jména napříč všemi call-graph tooly.

### P0.2 — Field-access calls

Není potřeba opravovat indexer — reference se ukládají správně. Problém byl
čistě v query vrstvě (P0.1).

### Co se implementovalo jinak než plán navrhoval

- Místo debug logování a lepších error messages (plán P0.1) jsme provedli
  přímou diagnostiku SQL dotazy na reálných indexech
- Místo `find_refs` restrukturalizace (plán navrhoval změnu na `to_usr IN`)
  jsme přidali suffix LIKE — minimální změna se stejnou funkčností
- `_did_you_mean` (P1.4) už byl hotový dříve, stejně jako ostatní P1–P3 featury

## Cíl

Zlepšit kvalitu výsledků fw-context nástrojů na základě 26 testů (21 základních + 5 komplexních scénářů) provedených 2026-06-18 proti indexu zbox-ecb-fw (52 409 symbolů, 1 241 641 referencí).

## Analýza

Testy odhalily 6 limitů. Tento plán adresuje 5 z nich (mimo explain_symbol cache/multi-model):

### Kořenové příčiny

#### A. Field-access calls nejsou v call graphu (zodpovědný za limity #1, #2, #6)

`_zmodem_driver.send()` → `find_callers(ZMODEM_DRIVER::send)` = 0.

**Příčina:** V `symbols.py:extract_all()` se reference sbírají přes `cursor.referenced` na `CALL_EXPR` a `MEMBER_REF_EXPR`. Libclang vrací `cursor.referenced` pro přímá volání (`send(...)`) i pro volání přes field (`obj.send(...)`) — obojí by mělo fungovat.

**Hypotéza:** Problém je v jednom z:
- (a) `ref_loc.file` je `None` pro některé definice (macro expanze)
- (b) `_in_roots(ref_loc.file.name)` selže — cesta k definici nespadá pod `source_roots`
- (c) `find_refs()` (`db.py:1061`) resolvuje špatný USR — `LIMIT 1` s `ORDER BY is_definition DESC, qualified_name = ? DESC` může pro běžná jména jako `send`, `init`, `connect` vrátit jiný symbol se stejným jménem

**Ověření:** Je potřeba nasadit logování do `_references_result` a `find_refs`, zjistit který USR se resolvuje, a porovnat s USR v `refs` tabulce. Alternativně přidat test, který přímo ověří počet referencí pro známé symboly.

**Dopad:** Opraví to `find_callers`, `find_references`, `find_all_callers_recursive`, `find_call_path` a `find_callees_recursive` pro všechny wrapper→driver vztahy. Týká se to stovek symbolů v projektovém kódu.

#### B. FTS5 tokenizace + AND sémantika (limity #3, #4)

`search_code("socket state")` nenajde `socket_state_t`. `search_code("shutdown reboot reset")` = 0.

**Příčina:** `split_tokens()` (`db.py:28`) rozděluje `socket_state_t` na tokeny `["socket", "state"]`. FTS5 s `_expand_query()` vytvoří `"socket* state*"`. To je AND — musí se trefit oba tokeny. Ale `socket_state_t` v názvu má FTS5 tokeny `["socket", "state"]` (po split_tokens). Takže `socket* state*` by MĚLO najít `socket_state_t`.

Problém je spíš v tom, že FTS5 rank upřednostní jiné výsledky (např. `_mbed_error_code` s obřím docstringem obsahujícím slovo "socket"). A pro delší dotazy AND sémantika zahodí všechno.

#### C. `find_dead_code` zahlceno (limit #5)

Vrací hlavně mbed-os konstruktory. Projektový kód je utopený v šumu.

**Příčina:** `find_dead_code()` (`db.py:990`) nemá žádný filtr na cesty (kromě `config_hash`).

---

## Seznam změn

### P0 — Kritické opravy

#### 1. Debug a oprava `find_refs` / `_references_result`

**Soubor:** `src/fw_context_mcp/indexer/db.py:1061-1132` (`find_refs`)

**Změna:**
- Přidat debug logování: který USR se resolvuje pro dané jméno, kolik referencí v `refs` tabulce pro tento USR existuje
- Pokud se resolvuje špatný USR (např. pro `ZMODEM_DRIVER::send` se najde USR `mbed::FileHandle::send`), přidat fallback: pokud `LIMIT 1` vrátí symbol, který má 0 referencí, zkusit další symbol se stejným jménem
- Alternativně: změnit `find_refs` aby queryovala `refs.to_usr IN (SELECT usr FROM symbols WHERE ...)` místo resolvování jediného USR — tím se pokryjí všechny varianty jména

**Soubor:** `src/fw_context_mcp/mcp/server.py:911-948` (`_references_result`)

**Změna:**
- Přidat detailnější error message: když je 0 výsledků, vypsat kolik symbolů se daným jménem našlo a jaké mají USR (pro debugging)

**Soubor:** `tests/test_search_quality.py` nebo nový `tests/test_refs.py`

**Změna:**
- Přidat test: pro známý symbol (`zbox::WDT::swdt_kick`) ověřit že `find_refs` vrací > 0 výsledků
- Přidat regresní test: pro `zbox::ZMODEM_DRIVER::send` (nebo jiný známý problematický symbol) ověřit počet referencí

#### 2. Field-access type resolution v indexeru

**Soubor:** `src/fw_context_mcp/indexer/symbols.py:350-465` (`extract_all` reference sběr)

**Analýza před implementací:**
Nejdřív ověřit, zda problém s field-access calls je skutečně v indexeru (reference se neukládají) nebo v query vrstvě (`find_refs` resolvuje špatný USR). Postup:
1. Spustit SQL dotaz: `SELECT COUNT(*) FROM refs WHERE to_usr = (SELECT usr FROM symbols WHERE qualified_name = 'zbox::ZMODEM_DRIVER::send' AND config_hash = ?)` 
2. Pokud COUNT = 0 → problém je v indexeru → implementovat field-access resolution
3. Pokud COUNT > 0 → problém je v `find_refs` → opravit pouze query logiku (položka 1)

**Pokud je potřeba opravit indexer:**

Princip: Při průchodu AST, když narazíme na `CALL_EXPR` kde `cursor.referenced` je None (libclang nerozpoznal cíl), prohledat děti:
- Pokud CALL_EXPR dítě je `MEMBER_REF_EXPR`, vzít `cursor.referenced` z MEMBER_REF_EXPR (to by měl být cílový method)
- Alternativně: pokud CALL_EXPR má `MEMBER_REF_EXPR` v dětech a `cursor.referenced` je nastavené, ověřit že `ref_loc.file` není None (možná root cause)

Konkrétně v `symbols.py:362-381`:
```python
# ř. 362-363
ref_kind = _REF_KINDS.get(cursor.kind)
if ref_kind is not None:
    referenced = cursor.referenced
    loc = cursor.location
    if referenced is not None and loc.file and ...
```

Přidat explicitní handling pro `CALL_EXPR` jehož `referenced` je nastavené:
- Ověřit, že `referenced.location.file` není None
- Pokud je None, zkusit najít cíl přes children (MEMBER_REF_EXPR → referenced)
- Přidat fallback: hledat v dětech CALL_EXPR uzel `MEMBER_REF_EXPR` a použít jeho `referenced`

**Soubor:** `tests/test_refs.py`

**Změna:**
- Přidat test s mock AST: `obj.method()` → ověřit že se vygeneruje reference s `to_usr = method_USR`

### P1 — UX vylepšení

#### 3. `search_code` fallback na `lookup_symbol`

**Soubor:** `src/fw_context_mcp/mcp/server.py:1141-1205` (`search_code`)

**Změna:**
- Po FTS5 vyhledávání: pokud `len(results) == 0`, provést `lookup_symbol` s každým slovem dotazu jako prefixem
- Spojit výsledky, deduplikovat podle USR
- Přidat parametr `fallback_to_lookup: bool = True` (default zapnuto)
- Do odpovědi přidat metadata: `{"_fallback_used": true, "_fallback_terms": ["socket", "state"]}`

**Pseudokód:**
```python
rows = search_symbols(conn, query, config_hash, limit=limit, kind=kind, exclude_variables=True)
if not rows and fallback_to_lookup:
    fallback_results = []
    for term in query.split():
        symbols = lookup_in_db(conn, config_hash, term, exact=False, limit=5)
        fallback_results.extend(symbols)
    # Deduplikovat podle USR
    seen = set()
    rows = []
    for s in fallback_results:
        if s["usr"] not in seen:
            seen.add(s["usr"])
            rows.append(s)
    rows = rows[:limit]
```

#### 4. Levenshtein "Did you mean?"

**Soubor:** Nový `src/fw_context_mcp/search/did_you_mean.py`

**Změna:**
- Implementovat Levenshtein vzdálenost (nebo Python `difflib.get_close_matches`)
- Prohledávat proti `symbols.name` a `symbols.qualified_name` sloupci (omezeno na definice, `is_definition=1`)
- Threshold: max vzdálenost 3 znaky nebo ratio >= 0.6
- Cache výsledků pro opakované dotazy (jednoduchý dict, max 100 položek)

**Soubor:** `src/fw_context_mcp/mcp/server.py:361-455` (`lookup_symbol`)

**Změna:**
- Když lookup_symbol vrátí prázdný seznam, zavolat `did_you_mean(name)` 
- Do výsledku přidat: `{"_did_you_mean": ["swdt_kick", "swdt_check", ...]}`

**Soubor:** `src/fw_context_mcp/indexer/db.py`

**Změna:**
- Přidat pomocnou funkci `get_all_definition_names(conn, config_hash) -> list[str]` — vrátí všechny `name` pro `is_definition=1`, `kind IN ('function', 'method', 'constructor', 'destructor')`

### P2 — Nové featury

#### 5. `find_wrapper_callers` — resolvuje field-access calls

**Soubor:** Nový nástroj v `src/fw_context_mcp/mcp/server.py`

**Princip:** 
1. Najít všechny třídy/struktury, které mají field typu cílové třídy
2. Prohledat všechny metody těchto wrapper tříd, zda volají metody přes field
3. Reportovat jako nepřímé callery

**Implementace:**
- SQL dotaz: najít fieldy, jejichž typ obsahuje hledaný symbol v qualified_name
- Projít `refs` hledáním patternu: wrapper_method → (přes field) → target_method
- Protože field-access reference NEJSOU v indexu (to je ten problém), použít hybridní přístup:
  - Najít fieldy typu `ZMODEM_DRIVER` → identifikovat třídy které je vlastní
  - Pro každou metodu těchto tříd zkontrolovat `refs` s `from_usr = metoda` a `ref_kind = 'call'`
  - Pokud metoda volá cokoliv, co má stejné jméno jako cílová metoda, je to kandidát
  - Alternativně: parsovat zdrojový kód metod pomocí regex pro pattern `_field_name.method_name(`

**Alternativní jednodušší přístup:**
Místo nového nástroje přidat do `find_callers` parametr `resolve_fields: bool = False`.
Když je zapnutý, po normálním `find_callers` dotazu:
1. Najít symbol, jehož USR byl resolvován
2. Najít všechny fieldy, jejichž typ se jmenuje stejně jako třída obsahující cílový symbol
3. Najít metody wrapper tříd, které volají metody se stejným jménem
4. Přidat je do výsledků s `ref_kind: "wrapper"`

**Soubor:** `tests/test_refs.py`

**Změna:**
- Přidat test: pro známý wrapper→driver vztah ověřit, že `find_wrapper_callers` najde volání

#### 6. Filtry pro `find_dead_code`

**Soubor:** `src/fw_context_mcp/indexer/db.py:990-1013` (`find_dead_code`)

**Změna:**
- Přidat parametr `exclude_paths: list[str] | None = None`
- Přidat do SQL: `AND s.file_path NOT LIKE ?` pro každou excluded path
- Defaultní exclude: `["mbed-os/%", "cmsis/%", "connectivity/%"]` (dají se override)

**Soubor:** `src/fw_context_mcp/mcp/server.py:1089-1114` (`find_dead_code` tool)

**Změna:**
- Přidat MCP parametr `exclude_paths: list[str] | None` s rozumným defaultem
- Předat do `index_db.find_dead_code()`

### P3 — Ambiciózní featury

#### 7. `trace_data_flow` — sleduje cestu dat systémem

**Soubor:** Nový `src/fw_context_mcp/search/data_flow.py`

**Princip:**
1. Uživatel zadá výchozí datovou strukturu (např. `InventorySlot`) a cílový endpoint (např. `ZMODEM_DRIVER::send`)
2. Nástroj prohledá call graph od funkcí, které čtou/zapisují `InventorySlot`, až k `send`
3. Využívá typovou informaci z libclang signatur

**Implementace:**
- Najít všechny funkce, které mají v parametrech nebo návratovém typu `InventorySlot`
- Z nich spustit BFS po call graphu směrem k cíli
- V každém kroku kontrolovat, zda se typ přenáší (přes parametry, návratové hodnoty, fieldy)
- Využít existující `find_call_path` jako základ
- Výstup: sekvence kroků s vysvětlením transformace dat

**Omezení první verze:**
- Sleduje pouze přímé předávání parametrů (názvem)
- Neresolvuje transformace (CBOR encode/decode)
- Jen pro orientační pochopení data flow

**Soubor:** `src/fw_context_mcp/mcp/server.py`

**Změna:**
- Přidat MCP tool `trace_data_flow(from_type: str, to_symbol: str, max_depth: int = 8)`
- Volá `data_flow.trace()`

---

## Rizika

| Riziko | Pravděpodobnost | Dopad | Zmírnění |
|--------|----------------|-------|----------|
| Root cause `find_refs` je v libclang, ne v query vrstvě | Střední | Vysoký — oprava indexeru je složitější | Nejdřív diagnostikovat SQL dotazem, pak rozhodnout |
| Levenshtein nad 52k symboly je pomalý | Nízká | Střední — použitelnost | Omezit na definice (menší množina), cache, threshold |
| `trace_data_flow` nedává smysluplné výsledky pro složité chainy | Vysoká | Nízká — je to experimentální feat | Označit jako "experimental", iterovat podle feedbacku |
| Field-access fix nevyřeší všechny případy (templaty, makra) | Střední | Střední | Akceptovat částečné pokrytí, dokumentovat limity |

## Verifikace

### Manuální testy (na zbox-ecb-fw indexu)

1. **Po P0.1:** `find_callers(zbox::ZMODEM_DRIVER::send)` musí vrátit > 0 výsledků
2. **Po P0.1:** `find_references(zbox::ZMODEM_DRIVER::init)` musí vrátit > 0 výsledků
3. **Po P0.2:** `find_callers(zbox::ZMODEM_DRIVER::socket_open)` musí obsahovat `ZMODEM::socket_open` a `ZMODEM::thread_app`
4. **Po P1.3:** `search_code("socket state", kind="enum")` musí vrátit `socket_state_t`
5. **Po P1.3:** `search_code("shutdown reboot reset", kind="method")` musí vrátit relevantní výsledky (ne prázdno)
6. **Po P1.4:** `lookup_symbol("WDT::kick")` musí vrátit `{"_did_you_mean": ["swdt_kick", ...]}`
7. **Po P2.6:** `find_dead_code(exclude_paths=["mbed-os/%", "cmsis/%", "connectivity/%"], limit=20)` musí vrátit primárně symboly z `src/` a `lib/`

### Automatické testy

- Regresní testy v `tests/test_refs.py` pro P0
- Unit testy pro `did_you_mean.py`
- Integrační test pro `search_code` fallback

### Spuštění

```bash
cd ~/dev/sw/work/tools/fw-context-mcp
.venv/bin/python -m pytest tests/ -q
```

---

## Pořadí implementace

1. **P0.1 — Diagnostika a oprava `find_refs`** (1 den)
   - Diagnostický SQL dotaz
   - Oprava query logiky nebo indexeru
   - Regresní testy

2. **P0.2 — Field-access resolution** (1 den, závisí na výsledku P0.1)
   - Pokud P0.1 odhalí problém v indexeru → opravit indexer
   - Pokud P0.1 odhalí problém jen v query → field-access resolution přesunout do P2.5

3. **P1.3 — search_code fallback** (0.5 dne)
   - Implementace + test

4. **P1.4 — Did you mean?** (0.5 dne)
   - Implementace + test

5. **P2.5 — find_wrapper_callers** (1 den)
   - Implementace + test

6. **P2.6 — find_dead_code filtry** (0.5 dne)
   - Implementace + test

7. **P3.7 — trace_data_flow** (1-2 dny)
   - Experimentální implementace

**Celkový odhad: 5.5–6.5 dne**
