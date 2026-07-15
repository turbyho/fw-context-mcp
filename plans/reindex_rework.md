# Plán: Rework reindexace — per-file inkrementální indexace

**Stav: Návrh — čeká na schválení** (revize 3 — zapracováno hluboké review)

## Cíl

`fw-context index --build` musí být stejně rychlý jako `fw-context index` bez
`--build`. Nezměněné soubory se nesmí parsovat ani kopírovat — jejich data
zůstanou na místě. Doindexují se pouze změnené soubory. Stará data se po
úspěšné indexaci vyčistí.

## Analýza — proč je to teď pomalé

### Současný flow při `--build`

```
build_preliminary() → nový config_hash
  ↓
pro každý TU:
  ├─ Tier 1 (mtime) → skip (stejný config_hash) ← nefunguje, config_hash je nový
  ├─ Tier 2 (content_hash) → skip ← nefunguje, config_hash je nový
  ├─ Tier 2b (manifest check) → "reuse" ← funguje, ale:
  │     _migrate_symbols_for_file() ← kopíruje INSERT OR REPLACE:
  │       symbols   ~52000 řádků
  │       refs      ~1.3M řádků  ← TOHLE JE POMALÉ
  │       macros    ~77000 řádků
  │       + fp_assignments, indirect_call_sites, inheritance
  └─ "updated" → libclang parse + INSERT (správně, pro změněné soubory)
```

**Root cause 1:** `compute_structural_hash()` (manifest.py:205) hashuje celé
`arguments` pole pro každý TU. Když build systém změní pořadí flagů, přidá
warning flag, nebo změní cestu k `-include` headeru, vznikne nový `config_hash`
i když se symboly nezměnily.

V aktuální DB jsou 4 config_hashe pro stejný projekt — všechny odkazují na
stejný `compile_commands.json`, ale pokaždé byl vygenerován znovu s mírně
odlišnými parametry.

**Root cause 2:** Když už nový `config_hash` vznikne, `_migrate_symbols_for_file`
kopíruje data pomocí INSERT OR REPLACE. To znamená:
- Alokace nových řádků
- Přestavba indexů (`idx_symbols_file`, `idx_refs_fromfile`, …)
- Zápis do WALu
- Stejná I/O zátěž jako čerstvá indexace

Výsledek: `--build` trvá stejně dlouho jako první indexace, i když se změnil
jen jeden soubor.

## Scénáře reindexace

Je potřeba pokrýt tři různé scénáře:

### Scénář A: `config_hash` zůstal stejný (drtivá většina případů)

`--build` vygeneroval `compile_commands.json` se stejnými soubory a
sémanticky stejnými flagy. Po normalizaci (Vrstva 1) je `config_hash`
stejný jako předchozí.

→ **Tier 1/2 fast path** (mtime → content_hash) přeskočí nezměněné soubory.
Data zůstanou na místě, nic se nekopíruje, nic se neparsuje.

→ Změněné soubory projdou Tier 3 (libclang parse + DELETE starých dat +
INSERT nových).

→ Nové soubory se normálně naindexují.

**Cena: O(změněné_soubory)**

### Scénář B: `config_hash` se změnil, existuje předchozí build

Přibyly/ubily soubory, změnily se `-D` flagy apod. V DB je předchozí
`config_hash` s daty, která lze "přesunout".

**Detekce nezměněných vs změněných souborů** ve Scénáři B je řešena
existujícím Tier 2b checkem v `_check_and_parse_unit()` (runner.py:1508-1535):
manifest z předchozího buildu obsahuje `source_hash`, `flags_hash` a header
hashe. Když se flags_hash změnil (např. nové `-D`), check vrátí `"updated"`
a soubor jde do Tier 3. Když flags_hash souhlasí a zdrojáky/headery se
nezměnily, check vrátí `"reuse"`.

→ Nezměněné soubory: **UPDATE `config_hash`** na existujících řádcích
(Vrstva 2). Žádné kopírování, žádný INSERT.

→ Změněné soubory: Tier 3 — DELETE starých dat + libclang parse + INSERT.

→ Nové soubory: Tier 3 — libclang parse + INSERT.

→ Zmizelé soubory: smažou se cleanupem (Vrstva 3).

**Poznámka k Tier 3 ve Scénáři B:** `store_symbols_for_unit()` maže stará
data jen když soubor existuje pod aktuálním `config_hash`. Ve Scénáři B
(nový config_hash) soubor ještě nemá záznam → staré symboly zůstávají pod
starým config_hash. To je záměr — cleanup (Vrstva 3) je odstraní atomicky
na konci. Během indexace zůstává starý config_hash plně dotazovatelný,
nový ještě není vidět (díky `manifest_verification = 'indexing'`).

**Cena: UPDATE nezměněných + parse změněných**

### Scénář C: První indexace, žádný předchozí build

Není co UPDATEovat, všechny soubory musí projít libclang parsováním.

→ Každý soubor: Tier 3 — libclang parse + INSERT.

**Cena: plná indexace (jediný správný případ pro plnou indexaci)**

---

## Řešení — tři vrstvy

### Vrstva 1: Stabilizace `config_hash` (zabrání zbytečným změnám)

**Soubor:** `src/fw_context_mcp/indexer/manifest.py`

**Změna:** Místo počítání `config_hash` z manifestu ho odvodit přímo
z normalizovaného `compile_commands.json`. Normalizovaná verze se uloží
do `~/.fw-context/index/<project_id>/compile_commands.json` — slouží
zároveň jako debug artefakt ("co se změnilo mezi buildy?") i jako
deterministický vstup pro hash.

**Princip:** canonical → hash. Když dva `compile_commands.json` obsahují
stejné soubory se stejnými flagy (jen v jiném pořadí nebo s jinak
zapsanými cestami), jejich canonical forma je identická → stejný hash.

**Normalizace (jediné místo — `compute_config_hash()`):**

1. **Seřadit entries podle `file`** — odstraní vliv pořadí TU v JSONu
2. **Seřadit `arguments` každého entry abecedně** — odstraní vliv pořadí flagů
3. **Normalizovat cesty** v pathových argumentech — převést absolutní cesty
   uvnitř `project_root` na relativní:
   - `-I*`, `-isystem*`, `-idirafter*`, `-iquote*`
   - `--sysroot=*`
   - `-include*`, `-imacros*`
   - Cesty k SDK (mimo `project_root`) normalizovat přes `Path.resolve()`
     aby `/home/user/esp/../esp/esp-idf` → `/home/user/esp/esp-idf`

**Žádné stripování flagů.** `-W*`, `-g*`, `-O*`, `-fcolor-*` atd. zůstávají.
Pokud se změní, config_hash se změní a proběhne korektní reindexace
(Scénář B). To je jednorázová změna, ne recurring problém.

**Výpočet hashe (odstranění cirkulární závislosti):**

1. Sestavit normalizovaný JSON v paměti (string)
2. `config_hash = sha256(canonical_json_string)`
3. Zapsat na disk: `~/.fw-context/index/<project_id>/compile_commands.<config_hash>.json`

Tedy: **nejdřív hash, pak zápis**. Soubor s config_hash v názvu umožňuje
porovnání mezi buildy:
`diff compile_commands.<stary_hash>.json compile_commands.<novy_hash>.json`
ukáže jen sémantické rozdíly — bez šumu ze změn pořadí flagů nebo cest.

**Uložení:** Normalizovaný JSON se zapíše do
`~/.fw-context/index/<project_id>/compile_commands.<config_hash>.json`.
Staré soubory se mažou spolu s cleanupem starých config_hash (Vrstva 3).

**Dopad na manifest.json:** Manifest (`<project>/.fw-context/index/manifest.json`)
dál slouží pro per-TU staleness detection (source_hash, headers). Jeho
`config_hash` je teď převzatý z normalizovaného cc.json — manifest ho
dostane jako parametr při zápisu. `build_preliminary()` zachovává současné
chování zápisu manifest.json (přepíše jen degraded/chybějící manifest).

**Změna signatur — `config_hash` jako parametr:** `compute_config_hash()` mění
signaturu z `(manifest: dict)` na `(units, project_root, db_dir)`. `save()`
už `compute_config_hash()` nevolá — `config_hash` dostane jako parametr.
`_update_manifest_after_index()` musí `config_hash` dostat od volajícího
(`run()`) a předat ho do `save()`. `generate()` taktéž dostane `config_hash`
parametrem zvenčí. `compute_structural_hash()` se zjednoduší na volání
`compute_config_hash(units, project_root, db_dir)` — canonical, hash, hotovo.

**Výsledek:** Když `--build` vygeneruje `compile_commands.json` se stejnými
soubory a stejnými flagy (jen v jiném pořadí nebo s jinak zapsanými cestami),
`config_hash` zůstane stejný → Tier 1/2 fast path přeskočí nezměněné soubory
s nulovými náklady.

### Vrstva 2: Efektivní migrace při legitimní změně `config_hash`

Když se `config_hash` přesto změní (přibyl/ubyl soubor, změnilo se `-D`),
použít **UPDATE** místo INSERT OR REPLACE:

**Soubor:** `src/fw_context_mcp/indexer/runner.py`

**Nový přístup v `_reassign_symbols_for_file()`:**

Místo kopírování řádků (INSERT OR REPLACE) provést UPDATE `config_hash`
a `file_id` na existujících řádcích. **Celá operace běží v jedné transakci**
(volající kód už zajišťuje `with transaction(conn)`).

```python
def _reassign_symbols_for_file(conn, new_config_hash, new_file_id, file_path):
    """Přesune data souboru ze starého config_hash do nového pomocí UPDATE.

    Na rozdíl od _migrate_symbols_for_file NEKOPÍRUJE data — mění jen
    config_hash a file_id u existujících řádků.  Řádky, které by
    kolidovaly s už zaindexovanými symboly (sdílené headery), se
    nechají pod starým config_hash a smažou se cleanupem.

    Když neexistuje předchozí build (Scénář C — první indexace), vrací 0
    a volající spadne do normálního parsování.

    Běží v transakci volajícího — atomicita UPDATE napříč všemi tabulkami.
    """
    old = conn.execute(
        "SELECT config_hash, id FROM files WHERE path=? AND config_hash!=? ORDER BY rowid DESC LIMIT 1",
        (file_path, new_config_hash),
    ).fetchone()
    if old is None:
        return 0  # Scénář C — první indexace, není co reassignovat

    old_ch, old_fid = old

    # ── symbols ──
    # UPDATE jen ty, co nekolidují s už existujícími pod novým config_hash.
    # NOT IN subselect: symboly sdílené napříč TUs (headery) už mohly být
    # zaindexovány pod novým config_hash jiným TU.
    cur = conn.execute(
        """UPDATE symbols SET config_hash=?, file_id=?
           WHERE config_hash=? AND file_id=?
           AND usr NOT IN (SELECT usr FROM symbols WHERE config_hash=?)""",
        (new_config_hash, new_file_id, old_ch, old_fid, new_config_hash),
    )
    symbol_count = cur.rowcount
    # Reset pagerank — _build_pagerank() skipuje když pagerank > 0.
    # Hodnoty z předchozího buildu jsou neplatné, protože call graph
    # mohl být změněn v jiných (změněných) souborech.
    conn.execute(
        "UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ? AND file_id = ?",
        (new_config_hash, new_file_id),
    )

    # ── macros ──
    # UNIQUE(config_hash, file_id, line) — při kolizi nechat starou verzi.
    # NOT EXISTS: SQLite row-value NOT IN se poddotazem má problémy s NULL.
    conn.execute(
        """UPDATE macros SET config_hash=?, file_id=?
           WHERE config_hash=? AND file_id=?
           AND NOT EXISTS (
               SELECT 1 FROM macros m2
               WHERE m2.config_hash = ?
                 AND m2.file_id = ?
                 AND m2.line = macros.line
           )""",
        (new_config_hash, new_file_id, old_ch, old_fid, new_config_hash, new_file_id),
    )

    # ── refs ──
    # Pro jistotu smaž všechno, co pod novým config_hash pro tento soubor
    # už existuje (prevence duplicit — refs nemá UNIQUE constraint).
    conn.execute(
        "DELETE FROM refs WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE refs SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── fp_assignments ──
    conn.execute(
        "DELETE FROM fp_assignments WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE fp_assignments SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── indirect_call_sites ──
    conn.execute(
        "DELETE FROM indirect_call_sites WHERE config_hash=? AND from_file=?",
        (new_config_hash, file_path),
    )
    conn.execute(
        """UPDATE indirect_call_sites SET config_hash=?
           WHERE config_hash=? AND from_file=?""",
        (new_config_hash, old_ch, file_path),
    )

    # ── vec_symbols (sqlite-vec KNN) ──
    # Vec0 tabulka má config_hash sloupec pro filtrované KNN dotazy.
    # Po UPDATE symbolů musíme aktualizovat i config_hash v vec0.
    # DELETE před UPDATE: bezpečnější — vec0 virtual tables nemusí
    # podporovat UPDATE s WHERE subselect.
    try:
        conn.execute(
            """DELETE FROM vec_symbols WHERE symbol_id IN (
                   SELECT id FROM symbols WHERE config_hash=? AND file_id=?
               )""",
            (new_config_hash, new_file_id),
        )
        conn.execute(
            """UPDATE vec_symbols SET config_hash=?
               WHERE symbol_id IN (
                   SELECT id FROM symbols WHERE config_hash=? AND file_id=?
               )""",
            (new_config_hash, new_config_hash, new_file_id),
        )
    except Exception:
        pass  # sqlite-vec nemusí být dostupné

    # ── inheritance ──
    # UPDATE inheritance edges pro třídy definované v tomto souboru.
    # derived_usr musí patřit symbolům, které jsme právě reassignovali
    # (už mají nový config_hash).
    # NOT EXISTS: brání UNIQUE constraint violation, když jiný TU
    # (změněný, Tier 3) už vytvořil stejnou (derived_usr, base_usr)
    # hranu pod novým config_hash.
    conn.execute(
        """UPDATE inheritance SET config_hash=?
           WHERE config_hash=? AND derived_usr IN (
               SELECT usr FROM symbols WHERE config_hash=? AND file_id=?
           )
           AND NOT EXISTS (
               SELECT 1 FROM inheritance i2
               WHERE i2.config_hash = ?
                 AND i2.derived_usr = inheritance.derived_usr
                 AND i2.base_usr = inheritance.base_usr
           )""",
        (new_config_hash, old_ch, new_config_hash, new_file_id, new_config_hash),
    )

    # ── overrides ──
    # NEMIGRUJE se — _build_overrides() rebuildne od nuly pro nový
    # config_hash v post-processingu. Nepotřebuje reassignaci.

    return symbol_count
```

**Proč je to rychlejší:**
- UPDATE upravuje existující řádky na místě
- Nealokují se nové řádky
- Indexy se aktualizují jen pro změněnou hodnotu `config_hash`
- FTS5 triggery pro `symbols` a `files` jsou shozené před indexing loopem
- `macros_fts` triggery je taky potřeba shodit (aktuálně je `drop_fts_triggers()`
  nezahazuje — **nutná změna v `drop_fts_triggers()` a přidat `_rebuild_macros_fts()`
  do post-processingu**)
- Řádově: UPDATE ~50K symbolů trvá zlomky sekund, INSERT OR REPLACE ~50K
  symbolů trvá sekundy

**Poznámka k FK constraints:** `symbols`, `files`, `macros`, `inheritance`,
`overrides`, a `hotspot_cache` mají `REFERENCES build_configs(config_hash)`.
S `PRAGMA foreign_keys = ON` vyžaduje UPDATE cílový `config_hash` existující
v `build_configs`. Proto `upsert_build_config()` **musí běžet před** první
reassignací — viz Vrstva 3.

**Poznámka k `_build_filtered_file_content()`:** Pro "reuse" soubory se
stále spouští libclang tokenizace, která plní `files.content` pro nový
config_hash a sbírá `tu_headers` pro inkrementální update manifestu.
I když se zdrojový kód nezměnil, data v `files` tabulce jsou vázaná na
config_hash — starý záznam patří starému buildu. Tokenizace je tedy nutná,
není to wasted work.

**Tabulky bez migrace (řešeno post-processingem nebo explicitně):**
- `overrides` — `_build_overrides()` rebuildne od nuly pro nový config_hash
- `hotspot_cache` — `_build_hotspot_cache()` rebuildne od nuly
- `embeddings` — navázáno na `symbol_id` (nemění se), ON DELETE CASCADE
- `llm_analysis` — navázáno na `symbol_id` (nemění se), ON DELETE CASCADE
- `file_analysis` — navázáno na `file_id`, ON DELETE CASCADE (automaticky
  vyčištěno při `DELETE FROM files` v cleanupu)
- `vec_symbols` — **nutný explicitní UPDATE `config_hash`** (viz pseudokód
  výše); v cleanupu `DELETE FROM vec_symbols WHERE config_hash = ?`
- `files_fts`, `symbols_fts`, `macros_fts` — FTS5 triggery jsou během
  TU loopu shozené, rebuildnou se v post-processingu

### Vrstva 3: Cleanup starých dat

**Soubor:** `src/fw_context_mcp/indexer/db.py` + `runner.py`

Po úspěšné reindexaci smazat data starých `config_hash`:

```python
def delete_build_data(conn, config_hash: str) -> None:
    """Smaže všechna data pro daný config_hash.
    
    file_analysis a embeddings jsou řešeny ON DELETE CASCADE —
    nepotřebují explicitní DELETE.
    """
    conn.execute("DELETE FROM symbols WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM macros WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM refs WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM fp_assignments WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM indirect_call_sites WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM inheritance WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM overrides WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM hotspot_cache WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM files WHERE config_hash=?", (config_hash,))
    conn.execute("DELETE FROM build_configs WHERE config_hash=?", (config_hash,))
    # vec0 virtual table — není pokryto ON DELETE CASCADE
    try:
        conn.execute("DELETE FROM vec_symbols WHERE config_hash=?", (config_hash,))
    except Exception:
        pass  # sqlite-vec nemusí být dostupné
```

Volat na konci `run()`, po úspěšném dokončení všech fází (FTS5 rebuild,
embeddings, LLM analýza, PageRank, hotspot_cache).

**Poznámka k `upsert_build_config()`:** Plán původně navrhoval přesun na konec
`run()`, aby `get_active_config()` nevracel nekompletní data. To ale není možné
kvůli FK constraints (Vrstva 2). Místo toho:

1. `upsert_build_config()` zůstane na začátku (jako teď):
   - **Nový config_hash** (Scénář B/C, `old_row IS NULL`) → `manifest_verification = 'indexing'`
   - **Existující config_hash** (Scénář A, data se updatují in-place atomicky
     po jednotlivých TU) → zachovat současnou hodnotu (typicky `'full'`)
2. `get_active_config()` / `get_active_build()` dostanou filtr —
   `WHERE manifest_verification != 'indexing'`. Tím:
   - Během Scénáře B/C: MCP server padne zpátky na **předchozí dokončený build**
     (starý config_hash). Queries vrací kompletní data, ne poloviční.
   - Během Scénáře A: MCP server normálně funguje — stejný config_hash,
     atomické per-TU transakce, konzistentní data.
   - Po dokončení: MCP server vidí nový/aktualizovaný build.
   - **Edge case:** Pokud všechny buildy mají `manifest_verification='indexing'`
     (např. crash během první indexace), `get_active_config()` vrátí `None` a
     `get_active_build()` zahlásí "no build config indexed". To je správné
     chování — lepší než vrátit nekompletní data.
3. Na konci `run()` se `manifest_verification` přepne na `'full'` (když
   manifest.json existuje) nebo `'none'` (když ne). Tím se build stane
   viditelným pro `get_active_config()`.

## Seznam změn

### `src/fw_context_mcp/indexer/manifest.py`

1. **`compute_config_hash()`** — změní se z "hash manifestu" na "hash
   normalizovaného compile_commands.json":
   - Nová signatura: `compute_config_hash(units, project_root, db_dir)` —
     `units` (seznam `CompilationUnit`), `project_root`, `db_dir` (pro
     odvození `project_id` a cesty k `~/.fw-context/index/`)
   - Normalizace: seřadit podle `file`, seřadit `arguments` abecedně,
     normalizovat cesty na relativní, odfiltrovat build-output cesty
   - Vypočítat hash z canonical stringu v paměti
   - Uložit canonical JSON do `~/.fw-context/index/<project_id>/compile_commands.<hash>.json`
   - Vrátit `config_hash`
2. **`compute_structural_hash()`** — zjednoduší se na volání
   `compute_config_hash(units, project_root, db_dir)`.
3. **`build_preliminary()`** — použije `compute_config_hash(units, project_root, db_dir)`.
   Zápis manifest.json zůstává beze změny (přepíše jen degraded/chybějící).
4. **`save()`** — `config_hash` jako nový parametr, už nevolá `compute_config_hash()`.
5. **`generate()`** — `config_hash` jako nový parametr, předává se zvenčí.

### `src/fw_context_mcp/indexer/runner.py`

6. **`_reassign_symbols_for_file()`** — nahradí `_migrate_symbols_for_file()`:
   - UPDATE config_hash/file_id místo INSERT OR REPLACE
   - Ošetří kolize u sdílených headerů (NOT IN subselect)
   - Resetuje pagerank na 0.0
   - Ošetří UNIQUE constraint u macros
   - Prevence duplicit u refs/fp_assignments/indirect_call_sites (DELETE před UPDATE)
   - Zahrnuje inheritance a vec_symbols tabulky (DELETE+UPDATE pattern)
   - Komentář: overrides se nemigruje — řeší `_build_overrides()`
7. **"reuse" path** — použít `_reassign_symbols_for_file()` místo
   `_migrate_symbols_for_file()`
8. **Cleanup na konci `run()`** — najít staré config_hashe, zavolat
   `delete_build_data()`
9. **`upsert_build_config()`** — `'indexing'` jen pro nový config_hash
   (`old_row IS NULL`, tj. `initial_manifest_verification = "indexing"`),
   pro existující zachovat původní hodnotu (`old_row["manifest_verification"]`).
10. **`get_active_config()`** — přidat filtr `WHERE manifest_verification != 'indexing'`.
    Tím MCP server během reindexace vidí předchozí dokončený build (Scénář B/C)
    nebo aktuální data s atomickými per-TU updaty (Scénář A).
11. **`_refresh_header_mtimes_from_manifest()`** — přesunout volání až za
    `_update_manifest_after_index()`, aby pracovala s aktuálním manifestem
    (ne se starým z předchozího buildu).
12. **`_update_manifest_after_index()`** — přidat `config_hash` parametr,
    předat ho do `save()`. Před zavoláním získat aktuální manifest
    (`load_manifest()`) aby `_refresh_header_mtimes_from_manifest()`
    (volaná po návratu) pracovala s aktuálními daty.
13. **FTS5 rebuild** — přidat `_rebuild_macros_fts(conn)` za `rebuild_files_fts(conn)`
    (runner.py:2210), aby macros FTS byl taky rebuildnut po TU loopu.

### `src/fw_context_mcp/indexer/db.py`

14. **`delete_build_data(conn, config_hash)`** — nová funkce (včetně `vec_symbols`,
    `file_analysis` a `embeddings` řešeny ON DELETE CASCADE)
15. **`drop_fts_triggers()`** — přidat `macros_ai`, `macros_ad`, `macros_au`
16. **`get_active_config()`** — přidat `WHERE manifest_verification != 'indexing'`
    (umístění: db.py:2978)

### `src/fw_context_mcp/indexer/config_hash.py`

17. **Odstranit `compute()`** — mrtvý kód, konfliktní normalizační strategie
    (stripuje `-MD`, `-MP`, `-MF`, `-o` atd.)

### `src/fw_context_mcp/cli.py`

18. **Beze změny** — `cmd_index()` funguje transparentně

## Dopad na rychlost

| Scénář | Před | Po |
|---|---|---|
| **A:** `--build`, config_hash stejný, žádná změna v kódu | ~plná reindexace (migrace všech dat) | **pár sekund** (Tier 1/2 skip všeho) |
| **A:** `--build`, config_hash stejný, 1 změněný soubor | ~plná reindexace | **jen 1 TU** (ostatní skip) |
| **B:** `--build`, nový `config_hash`, 1 změněný `-D` flag | ~plná reindexace | UPDATE ~50K symbolů + reindex změněných TU |
| **C:** První indexace, žádný předchozí build | plná indexace | plná indexace (správně) |
| Bez `--build`, mtime změněn | Tier 1/2 skip (už funguje) | beze změny |

## Rizika

- **Normalizace cest:** Převod absolutních cest na relativní může být chybný,
  pokud cesta vede do symlinku nebo mount pointu. `Path.resolve()` se o to
  postará. Riziko je nízké — při chybné normalizaci se změní config_hash
  a proběhne plná reindexace (korektní, jen pomalejší).
- **Build direktoráře s dynamickými názvy:** Build systémy někdy generují
  unikátní build adresáře (`cmake-build-debug-12345/`). Když je build
  directory uvnitř `project_root`, normalizuje se na relativní cestu a stane
  se součástí config_hash. Při každém buildu tak vznikne nový config_hash →
  Scénář B zbytečně. Mitigace: build output adresáře (`BUILD/`, `build/`,
  `cmake-build-*/`) by měly být z normalizovaného cc.json **vyloučeny**
  (jejich cesty jsou build artefakty, ne zdrojový kód). Implementačně:
  před normalizací odfiltrovat argumenty, jejichž hodnota ukazuje do
  známého build-output adresáře.
- **Kolize při UPDATE:** `NOT IN (SELECT usr FROM symbols WHERE config_hash=?)`
  může být pomalý na velkých DB. Alternativa: dočasná tabulka s kolizními
  USR — pokud se ukáže jako bottleneck.
- **Selhání uprostřed indexace:** Stará data musí zůstat zachována do
  úspěšného konce `run()`. Transakce v `_reassign_symbols_for_file()` zajistí
  atomický UPDATE (všechny tabulky najednou). Když indexing spadne před
  cleanupem, staré config_hashe se zachovají.
- **PageRank s neplatnými hodnotami:** Reset `pagerank=0` v
  `_reassign_symbols_for_file()` zajistí přepočítání. `_build_pagerank()`
  dostane `force=True` když `run()` běží s `--force`.
- **`-O*` flagy:** Už se nestripují — zůstávají v hashovaných datech. Když
  se změní, config_hash se změní a proběhne korektní reindexace.
- **Jednorázová plná reindexace po nasazení:** Vrstva 1 mění způsob výpočtu
  `config_hash` — normalizované entries produkují jiný hash než původní
  neseřazené. Při prvním `--build` po nasazení se config_hash nebude shodovat
  s uloženým v manifestu → proběhne plná reindexace (Scénář B/C). Je to
  jednorázová daň za přechod na stabilní hash.
- **`config_hash.py::compute()`** — mrtvý kód, nikde se nevolá. Má vlastní
  stripování flagů (`-MD`, `-MP`, `-MF`, `-o`, …). Po nasazení Vrstvy 1 by
  měl být odstraněn, aby nevznikl zmatek dvou normalizačních strategií.
- **`reindex_file_impl` race condition:** Pokud během background reindexu
  (s novým config_hash) doběhne `reindex_file`, operuje nad starým
  config_hash. Po dokončení background reindexu Vrstva 3 smaže data starého
  config_hash — včetně změn z `reindex_file`. Toto je existující problém,
  není zaveden plánem, ale Vrstva 3 ho zhoršuje (data jsou smazána místo
  aby zůstala orphaned). Mitigace: před `delete_build_data()` zkontrolovat,
  že žádný `reindex_file` neběží (pause marker).
- **`_refresh_header_mtimes_from_manifest()` používá starý manifest:**
  Volá se před `_update_manifest_after_index()`, takže pracuje s manifest
  načteným na začátku `run()`. V Scénáři B (nový config_hash) je to manifest
  z předchozího buildu. Fix: přesunout volání až za `_update_manifest_after_index()`,
  nebo reloadovat manifest.
- **Všechny buildy s `manifest_verification='indexing'`:** Pokud indexing
  spadne během první indexace (Scénář C), build_config zůstane ve stavu
  `'indexing'` a `get_active_config()` ho odfiltruje. Při absenci
  předchozího buildu vrátí `None` — MCP server zahlásí "no index".
  Uživatel musí spustit `fw-context index` znovu. Není potřeba speciální
  handling — jde o korektní chování.

## Postup implementace

1. **Vrstva 1** — Upravit `compute_config_hash()` v `manifest.py`:
   - Nová signatura: `compute_config_hash(units, project_root, db_dir)`
   - Normalizovat entries (řazení, normalizace cest) → canonical string v paměti
   - Vypočítat hash → zapsat canonical JSON do `~/.fw-context/index/<project_id>/compile_commands.<hash>.json`
   - Upravit signaturu `save(manifest, db_dir, config_hash)`, `generate(..., config_hash)`,
     `compute_structural_hash()`, `build_preliminary()`
2. **Vrstva 2** — Přidat macros triggery do `drop_fts_triggers()` v `db.py`.
   Přidat `_rebuild_macros_fts(conn)` do `run()` za `rebuild_files_fts(conn)`
   (runner.py:2210).
3. **Vrstva 2** — Přidat `delete_build_data()` do `db.py` (včetně `vec_symbols`).
4. **Vrstva 2** — Nahradit `_migrate_symbols_for_file()` → `_reassign_symbols_for_file()`
   (UPDATE, vec_symbols DELETE+UPDATE, pagerank reset, inheritance, UNIQUE ošetření,
   komentář k overrides).
5. **Vrstva 3** — `upsert_build_config()`: `'indexing'` jen pro nový config_hash
   (`old_row IS NULL`), pro existující zachovat původní hodnotu.
   `get_active_config()`: přidat `WHERE manifest_verification != 'indexing'`.
   Na konci `run()` přepnout na `'full'`/`'none'`.
6. **Vrstva 3** — Přesunout `_refresh_header_mtimes_from_manifest()` až za
   `_update_manifest_after_index()`. Přidat `config_hash` parametr do
   `_update_manifest_after_index()` a předat ho do `save()`.
7. **Vrstva 3** — Přidat cleanup starých config_hash (včetně `vec_symbols`)
   na konec `run()`. Před cleanupem ověřit, že neběží `reindex_file`
   (pause marker).
8. Odstranit `config_hash.py::compute()` — mrtvý kód s konfliktní strategií.
9. Ověřit testy: `python3 -m pytest tests/ -x -q`
10. Ručně otestovat: `fw-context index --build` → změřit čas, zkontrolovat DB
