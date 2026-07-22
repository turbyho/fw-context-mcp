# Vylepšení embeddingové vrstvy: těla funkcí + BGE-M3 cesta

**Status:** Návrh — čeká na schválení (rev. 6: zapracované opravy z review — query-side model filtr, vec0 dim-mismatch korekce, vec0 CASCADE mezera, 2026-07-22)
**Datum:** 2026-07-21

---

## Cíl

Zvýšit recall sémantického vyhledávání nad C/C++ kódem. Dnes `semantic_search` embeduje pouze popisy symbolů (signatura + docstring + LLM summary, ~30–200 tokenů), nikoliv implementaci. `search_bodies` (FTS5) prohledává těla, ale jen lexikálně — `malloc()` nenajde na dotaz "allocate buffer".

## Analýza současného stavu (ověřeno proti kódu 2026-07-22)

### Co se embeduje dnes

`src/fw_context_mcp/indexer/runner.py:161-196` — `_build_embeddings()`:

```python
parts = [f"{kind} {name}"]
if class_:
    parts.append(f"in {class_}")
if path:
    parts.append(f"in {path}")
if sig:
    parts.append(sig)
if doc:
    parts.append(doc)
if llm:
    parts.append(llm)
descriptions.append(" : ".join(parts))
```

Výsledek: `"function uart_init : in UART_DRIVER : void uart_init(UART_DRIVER *uart) : Initialize UART hardware"`

### Klíčová fakta z review kódu

- **Description se skládá na jediném místě** v celém projektu (`runner.py:161-196`). Komentář "same format as embed phase" je zastaralý — query-time embeduje surový text dotazu. Změna formátu = jediná editace.
- **SQL dotaz (`runner.py:143-155`) neselectuje `source`** — pro těla funkcí je nutné ho přidat do SELECT.
- **Embedding cache je klíčovaná `(symbol_id, model)`** (`... NOT IN (SELECT symbol_id FROM embeddings WHERE model = ?)`) — změna formátu description by bez verzování klíče existující embeddingy nezregenerovala.
- **Query strana `semantic_search` NEFILTRUJE podle `model`** (`mcp/handlers/search.py:616-650`) — `_do_semantic` skenuje celou BLOB tabulku `embeddings` pro daný `config_hash` bez `WHERE e.model = ?`. Jakékoli koexistující model/desc-verze klíče v jedné DB se na query straně promítnou jako duplicitní/smíchané kandidáty. vec0 cesta (`search_similar_vec`) tím netrpí — 1 řádek na `symbol_id`.
- **vec0 tabulka NEMÁ ON DELETE CASCADE** (`db.py:1327`) — cleanup existuje jen v `delete_build_data`; `reindex_file_impl` (`maintenance.py:616-622`) vec0 nečistí a sirotčí řádky přežijí.
- **`call_ollama_embed()` má 4 call sites:** `indexer/runner.py:208` (index-time), `mcp/handlers/search.py:602` (`semantic_search`), `search/phases/embedding.py:94` a `search/phases/rough_search.py:137` (pipeline). Abstrakce embedderu musí pokrýt všechny.
- **RRF fúze** (`search/phases/rrf_fusion.py`): `K=30`, `W_FTS=1.8`, `W_VEC=0.2`, boosty `PROJ=1.5`, `FUNC=1.2`, `PAGERANK=0.2`, `OVERFETCH=50`. Parametry potvrzené experimenty 8.8–8.10.
- **Vendor handling:** `runner.py:173` potlačuje docstring pro mbed-os (`is_os = "mbed-os" in fp.lower()`).
- **vec0 tabulka má fixní dimenzi** detekovanou při prvním batchi (`init_vec_table`) — změna dimenze embeddingu (Matryoshka) = rebuild tabulky.
- **`semantic_search` je čistý dense KNN** — žádné RRF, žádný rerank. `smart_search` má plnou pipeline.
- **Retrieval cesty nástrojů:** `search_code`/`search_bodies`/`search_content` → čisté FTS5 (embeddingové změny je neovlivní); `semantic_search` → dense KNN; `smart_search` → LLM → FTS5 + embedding → RRF.

### Co to znamená v praxi

| Dotaz | Kde je odpověď | Najde se dnes? |
|-------|---------------|----------------|
| `"DMA timeout handler"` | `if (ch->status & DMA_TEIF)` v těle `dma_irq()` | Ne — description o timeoutu neví |
| `"allocate buffer for packet"` | `buf = malloc(PKT_SIZE)` v těle `prepare_packet()` | Ne — description o malloc neví |
| `"I2C error recovery"` | `i2c_reset(); i2c_reinit()` v těle `i2c_xfer()` | Ne — leda by to popsalo LLM summary |

---

## Implementační bloky

Graf závislostí:

```
Blok 0 (benchmark) ──► gate pro 2,3,4,5
Blok 1 (Embedder ABC) ──► Blok 6 (FT), Blok 7 (Granite), Blok 8 (LateOn)
Blok 2 (těla + cache verzování) ──► Blok 3 (contextual), Blok 6 (FT)
Blok 4 (adaptive RRF) ──► nezávislý (jen benchmark z Bloku 0)
Blok 5 (reranker) ──► nezávislý (runtime only), synergie s Blokem 0
```

### Blok 0: Benchmark infrastruktura

**Proč:** Bez měření se nedá rozhodnout, jestli Bloky 2–5 pomáhají nebo škodí (riziko diluce signálu u těl je reálné). Dnes `tests/quality_eval.py` je unit-level (in-memory, bez DB); dotazové sady s relevance judgments existují jen rozptýleně v `experiments/`. Tento blok je gate — dokud neumíme změřit baseline, neimplementujeme nic dalšího.

**Co se mění:**

- **`tests/quality_eval.py`** (nebo nový `experiments/eval_harness.py`) — sjednotit evaluaci nad reálnou indexovou DB:
  - Dotazová sada A: odpověď je **jen v implementaci** (ne v signatuře/docstringu) — cílová pro Blok 2.
  - Dotazová sada B: odpověď je v signatuře (kontrolní — nesmí se zhoršit).
  - Metriky: recall@1/5/10, MRR, NDCG@10.
- **Baselines k změření:** FTS5-only, dense-only (`mxbai-embed-large`), hybrid (RRF, produkční parametry).
- **A/B army description formátu** (příprava Bloků 2–3):
  1. současný formát (tokeny za dvojtečkami),
  2. token-prefix + body,
  3. contextual-věta + body (Anthropic formát).
  - Caveat z researchu: Anthropic měřil document retrieval, ne symbol-level code search — funkce mají silný lexikální signál v názvu, relativní zisk kontextu může být menší než 35 %. Proto měřit, ne věřit.
- **Rozhodovací kritéria (doplněno po review):** definovat předem, např. "Blok 2 se adoptuje, pokud recall@10 na sadě A stoupne aspoň o 10 % a sada B neklesne o víc než 2 %". Bez kritérií hrozí nekonečné ladění.

**Závislosti:** žádné. **Blokuje:** adopční rozhodnutí Bloků 2–5.

**Implementační kroky:**

1. **`experiments/eval_harness.py`** (nový) — DB-backed harness: načte indexovou DB projektu, spustí dotazovou sadu přes zvolenou retrieval variantu (FTS5-only / dense-only / RRF-hybrid), výsledky předá existující `tests/quality_eval.py:evaluate()`. Unit-level API (`EvalMetrics`, `EvalReport`) se nemění — harness je tenká vrstva nad ním.
2. **Dotazové sady** jako JSON v `experiments/datasets/<projekt>/queries.json` — formát `{query, relevant: [[name, file], ...], split: "impl" | "sig"}`. Zdroj: sjednotit stávající sady rozptýlené v `experiments/test_rrf_fusion.py`, `test_adaptive_rrf.py`, `weight_grid_search.py`; ručně doplnit ≥20 impl-only dotazů na referenčním projektu (mbed-os firmware) — dotazy typu "DMA timeout handler", "allocate buffer for packet".
3. **Baseline měření:** 3 varianty × sada A/B → výsledky do `tests/results/` (JSON summary, konvence projektu).
4. **A/B army description formátu:** 3 varianty embeddingů v jedné DB pod různými `model` klíči (např. `mxbai:fmt-token`, `mxbai:fmt-body`, `mxbai:fmt-context`) — levnější než 3 indexy; dense-only recall@10 / NDCG@10 na sadě A i B. **Předpoklad (odhaleno review):** produkční `semantic_search` nefiltruje podle `model` (`search.py:616-650`) — harness proto nesmí měřit přes `_do_semantic`, ale přes vlastní dotaz s explicitním `WHERE e.model = ?` (tenká obálka v `eval_harness.py`), jinak by viděl každý symbol 3× se smíchanými formáty. Query-side filtr pro produkci řeší Blok 2, krok 3.
5. **Rozhodovací kritéria jako prahy v harnessu** (exit code 0/1), ne jen v textu plánu.

**Testy:** `tests/test_eval_harness.py` — harness nad in-memory DB (fixture: pár symbolů + embeddings), metriky ověřené proti ručně spočítaným hodnotám.

**Akceptační kritéria:** jedním příkazem (`python experiments/eval_harness.py --project X --variant baseline`) reprodukovatelné číslo; sada A ≥ 20 dotazů, sada B ≥ 20 dotazů; prahy vynucují exit code.

**Odhad:** S (1–2 dny).

### Blok 1: Embedder abstrakce

**Proč:** Granite R2 a LateOn-Code neběží přes Ollama (ModernBERT nepodporován). Dnes existuje jediná cesta `call_ollama_embed()` (`llm/ollama.py:145`) volaná ze 4 míst. Bez abstrakce by každý nový backend znamenal vetvat podmínky do 4 call sites.

**Co se mění:**

- **`src/fw_context_mcp/llm/embedder.py`** (nový) — `Embedder` ABC: `embed_documents(texts) -> list[list[float]]`, `embed_queries(texts) -> list[list[float]]`, vlastnosti `name` (pro cache klíč), `dim` (po prvním volání), `max_tokens`.
- **`src/fw_context_mcp/llm/ollama.py`** — `OllamaEmbedder(Embedder)`; `call_ollama_embed()` zůstane jako thin wrapper (zpětná kompatibilita), nebo se call sites přepíší.
- **`src/fw_context_mcp/llm/st_embedder.py`** (nový) — `SentenceTransformerEmbedder` (lazy import `sentence_transformers` — heavy dep, podle konvencí projektu import uvnitř funkce/metody).
- **Call sites k refaktoru (4):** `indexer/runner.py:208`, `mcp/handlers/search.py:602`, `search/phases/embedding.py:94`, `search/phases/rough_search.py:137`. Kromě toho `call_ollama_embed` importuje ~10 skriptů v `experiments/` — díky zachovanému wrapperu dál fungují beze změny; žádná akce, jen neodstranit wrapper.
- **`config/settings.py`** — `llm.embed_backend = "ollama" | "sentence_transformers"` s auto-detekcí podle názvu modelu.
- **Query/doc prompty:** stávající `embed_query_prompt`/`embed_doc_prompt` se přesunou do implementací embedderu (každý backend má jinou instruction syntaxi).

**Approval required:** nová dependency `sentence-transformers` v `pyproject.toml`.

**Závislosti:** žádné. **Blokuje:** Bloky 6, 7, 8.

**Implementační kroky:**

1. **`src/fw_context_mcp/llm/embedder.py`** (nový) — `Embedder` ABC: `embed_documents(texts)`, `embed_queries(texts)`, read-only properties `name` (pro cache klíč), `dim` (lazy po prvním volání), `max_tokens`. Factory `get_embedder(cfg) -> Embedder`.
2. **`llm/ollama.py`** — `OllamaEmbedder(Embedder)`: přesun logiky z `call_ollama_embed()` (`ollama.py:145-224`, včetně auto-pull a prompt prefixů). `call_ollama_embed()` zůstane jako delegující wrapper — zpětná kompatibilita, testy se nerozbijí.
3. **`llm/st_embedder.py`** (nový) — `SentenceTransformerEmbedder`: lazy import `sentence_transformers` uvnitř `__init__` (heavy-dep konvence projektu); query/doc prompty mapované na ST instruction API.
4. **Refaktor 4 call sites** na `get_embedder(cfg)`: `indexer/runner.py:208`, `mcp/handlers/search.py:602`, `search/phases/embedding.py:94`, `search/phases/rough_search.py:137`. Embedder instance se v pipeline předá přes `PipelineContext`/config, v runneru se vytvoří jednou na embed fázi.
5. **`config/settings.py`** — `llm.embed_backend = "auto" | "ollama" | "sentence_transformers"`; autodetekce z prefixu modelu (`ibm-granite/`, `lightonai/`, `BAAI/` → ST, jinak Ollama). `_apply_embed_prompt_defaults` rozšířit o ST modely.
6. **`pyproject.toml`** — `sentence-transformers` jako **optional extra** (`[project.optional-dependencies] st = [...]`), ne povinná dependency — minimální intrusion, Ollama-only instalace se nezmění.

**Testy:** `tests/test_embedder.py` — fake Embedder; `OllamaEmbedder` přes mock httpx (stejný pattern jako existující testy ollama klienta); autodetekce backendu; wrapper `call_ollama_embed` beze změny chování. Celá suite musí projít bez nainstalovaného `sentence-transformers` (skip marker).

**Akceptační kritéria:** `python3 -m pytest tests/ -x -q` zelené; `semantic_search` i indexace fungují přes obě implementace; žádný call site nevolá `call_ollama_embed` přímo (jen wrapper nebo `get_embedder`).

**Odhad:** S–M (1–2 dny).

### Blok 2: Těla funkcí do embeddings + verzování cache

**Proč:** Odstraňuje slepotu `semantic_search` k implementaci (tabulka výše). Žádný nový model — jen delší description. Description se skládá na jediném místě, takže změna je chirurgická. Verzování cache klíče je blokující: bez něj by se po změně formátu staré embeddingy tiše používaly dál a benchmark by měřil mix formátů.

**Co se mění:**

- **`indexer/runner.py:_build_embeddings()`**:
  - SQL: přidat `s.source` do SELECT (`runner.py:143-155`).
  - Description: stávající prefix (kind, name, class, path, sig, doc, llm) + `body: <tělo>`.
  - **Scope kindů:** těla jen pro `function, method, constructor, destructor`; pro `class, struct, union, typedef, enum` ponechat dnešní formát (tělo třídy by bylo příliš dlouhé).
  - **Truncace — tělo, ne description:** `mxbai-embed-large` má kontext ~512 tokenů; Ollama truncuje zprava a usekla by konec těla, kde často je klíčová logika (position bias, -15.6 %). Strategie head+tail: prvních ~1200 + posledních ~800 znaků těla, metadata prefix vždy celý. Limit parametrizovat podle `Embedder.max_tokens` (u Granite R2 limit zvednout/odstranit).
  - **Vendor politika (doplněno po review):** `runner.py:173` dnes potlačuje docstring pro mbed-os. Rozhodnout: vendor těla embedovat nebo ne. Doporučení: vendor těla ano (vendor funkce se hledají taky), ale měřit náklady — vendor symbolů je většina. Případný opt-out přes config.
- **`indexer/db.py`** — `source` sloupec existuje, žádná schema změna.
- **`config/settings.py`** — `llm.embed_bodies = true` (default), `llm.embed_body_max_chars = 2000`.
- **Verzování cache klíče:** ukládat `model = "<embed_model>:desc-v2"` při `embed_bodies=true`; verzi inkrementovat při každé změně formátu (Bloky 3, 6). Staré embeddingy se automaticky přeskočí a zregenerují. Ošetřit i v `reindex_file` cestě (`reindex_file_impl`) — doplněno po review, plán to původně nezmiňoval.

**Náklady:** ~2000 znaků navíc na symbol → vstupy ~30–200 → ~500–600 tokenů. Embed fáze řádově 3–5× delší (dominantní část indexace). Storage beze změny (fixní dimenze); delší je jen index-time, ne query-time.

**Rizika:** diluce signálu kombinací metadata+kód → proto A/B arm v Bloku 0 a rozhodovací kritéria.

**Závislosti:** benchmark z Bloku 0 pro adopci. **Blokuje:** Bloky 3, 6.

**Implementační kroky:**

1. **`config/settings.py`** — `llm.embed_bodies: bool = True`, `llm.embed_body_max_chars: int = 2000`, `llm.embed_vendor_bodies: bool = True`.
2. **`indexer/runner.py:_build_embeddings()`**:
   - SQL (`runner.py:143-155`): přidat `s.source` do SELECT.
   - Nový helper `_truncate_body(source: str, head: int, tail: int) -> str` — čistá funkce (snadno testovatelná): krátké tělo beze změny, dlouhé → `head + "\n// ...\n" + tail`. Head/tail odvozeny z `embed_body_max_chars` (default 1200/800).
   - Description builder: pro kindy `function, method, constructor, destructor` připojit `body: <tělo>`; pro `class, struct, union, typedef, enum` beze změny.
   - **Vendor gate:** místo hardcoded `is_os = "mbed-os" in fp.lower()` (`runner.py:173`) použít `is_project` sloupec z `symbols` (už existuje, RRF ho čte) — `embed_vendor_bodies=False` → vendor funkce se embedují bez těla. **Pozor — záměrná změna chování, ne ekvivalent:** gate se tím rozšíří z mbed-os na všechny vendor cesty (docstring gate dnes platí jen pro mbed-os). Zdokumentovat v commit zprávě a ověřit na sadě B (vendor dotazy).
   - **Cache klíč:** konstanta `DESCRIPTION_VERSION = "desc-v2"`; `model_key = f"{embed_model}:{DESCRIPTION_VERSION}"` při `embed_bodies=True`, jinak `f"{embed_model}:desc-v1"`. Použít v SELECT NOT IN i v INSERT (`upsert_embeddings`, `upsert_embeddings_vec`).
3. **Query-side model filtr (povinný, odhaleno review):** `_do_semantic` (`mcp/handlers/search.py:616-650`) skenuje BLOB tabulku `embeddings` bez `WHERE e.model = ?` — po zavedení verzovaných klíčů by query strana viděla staré `:desc-v1` i nové `:desc-v2` vektory duplicitně (mix formátů, kterému má verzování zabránit). Změna: oba dotazy (COUNT i SELECT v `_do_semantic`) filtrovat `e.model = ?` s aktuálním klíčem (`f"{embed_model}:{DESCRIPTION_VERSION}"` podle `embed_bodies`). Zkontrolovat i BLOB fallback `get_embeddings` v `search/phases/embedding.py`. Bez tohoto kroku je verzování klíče na index straně nedostatečné.
4. **`mcp/handlers/maintenance.py:reindex_file_impl`** — ověřeno: při reindexu souboru se symboly smažou a embeddingy (BLOB) zruší přes ON DELETE CASCADE (`maintenance.py:618`) → zregenerují se při příštím indexu pod aktuálním klíčem. **Mezera (odhalena review):** vec0 tabulka (`vec_symbols`) CASCADE nemá (`db.py:1327` — explicitní DELETE jen v `delete_build_data`); `reindex_file_impl` vec0 nečistí → sirotčí vec řádky přežijí. Většinou benigní (KNN filtruje přes `config_hash`), ale při reuse `symbol_id` může starý vektor nalepit na nový symbol — pre-existing issue, nezhoršené tímto blokem. Součástí bloku: explicitní `DELETE FROM vec_symbols WHERE symbol_id IN (...)` v `reindex_file_impl` (stejný pattern jako `db.py:1330`, try/except OperationalError) + regresní test, že skip-logika s verzovaným klíčem funguje i po reindexu souboru.
5. **Cleanup starých embeddingů** (na konci indexu): `DELETE FROM embeddings WHERE model NOT LIKE '%:desc-v%'` — uvolní storage a eliminuje duplicity na query straně. Ponechat za config flagem `index.prune_stale_embeddings` (default **true** — po zavedení query-side filtru (krok 3) je prune už jen storage optimalizace, ale bez filtru by byl jedinou obranou; default true je bezpečnější pro uživatele, kteří filtr minou při cherry-picku).

**Testy:** `tests/test_embed_bodies.py` — `_truncate_body` (krátké tělo beze změny; dlouhé → head+tail; metadata prefix nedotčen); description builder s/bez body podle kindu; vendor gate podle `is_project`; verzovaný klíč — symbol embedovaný pod `:desc-v1` se při `:desc-v2` znovu zařadí do fronty; **query-side filtr — `_do_semantic` vrací jen vektory aktuálního klíče (v1+v2 v DB → žádné duplicity)**; reindex souboru → embeddingy pryč (BLOB CASCADE + explicitní vec0 DELETE) → další index je zregeneruje.

**Akceptační kritéria:** embed fáze doběhne na referenčním projektu; harness (Blok 0): sada A recall@10 **+≥10 %**, sada B **−≤2 %** — jinak `embed_bodies` default přepnout na `false` a iterovat formát přes A/B army.

**Odhad:** S (1 den implementace + čas indexace na měření).

### Blok 3: Contextual description formát (sloučené Fáze 1-context + Fáze 3)

**Proč:** 2026 evidence (Anthropic Contextual Retrieval + reprodukce): -35 až -67 % retrieval failures za cenu jiné formulace description. Klíč: kontext musí být chunk-specific a souvislý, ne seznam tokenů. fw-context má výchozí pozici lepší než typický RAG: kontext **netřeba generovat LLMem** — `symbols.summary` z `--analyze` je hotový chunk-specific kontext a deterministické fakty (path, modul, callers z `refs`) jsou zdarma a bez halucinací. Graph augmentation (původní Fáze 3, SpIDER +13 %) je stejná změna na stejném místě — sloučeno do jednoho bloku.

**Co se mění:**

- **`indexer/runner.py:_build_embeddings()`** — description z tokenového seznamu na souvislý kontext:

  ```
  uart_irq_handler is an interrupt handler in drivers/uart (UART2 driver).
  Called from the UART2 IRQ vector; calls dma_complete, ringbuf_push.
  Handles DMA transfer completion and error recovery.
  <body...>
  ```

  - **Zdroje kontextu:** deterministické vždy (path/modul, top-N callers/callees z `refs`), `symbols.summary` kde existuje. **Žádný nový LLM pass** (local-first strategie).
  - **Cap:** max ~10 callerů/callees (hub funkce mají stovky callerů — description by explodovala).
- **Contextual BM25 analog:** stejný kontextový text přidat do FTS5 indexu (dnes FTS5 vidí jen name/signature/docstring). Rebuild FTS5, ne schema změna.
- **Cache:** inkrementovat `desc-vN` klíč (mechanismus z Bloku 2).
- **Explicitně mimo scope:** žádný GraphRAG engine — 2026 evidence: zřídka stojí za cenu; textová augmentace stačí.

**Rizika:** relativní zisk na symbol-level search může být menší než 35 % (funkce mají silný lexikální signál v názvu) → A/B arm 3 v Bloku 0.

**Závislosti:** Blok 2 (mechanismus verzování cache).

**Implementační kroky:**

1. **`indexer/runner.py`** — nový helper `_build_context_prefix(symbol_row, conn) -> str`:
   - Deterministické fakty: modul (z `file_path`), top-N callers/callees z `refs` — SQL s `LIMIT 10`; řazení podle pagerank skóre, pokud je v `symbols` k dispozici (RRF ho čte jako `pagerank`).
   - `symbols.summary` připojit jako větu, kde existuje. **Žádné nové LLM volání.**
2. **Description builder přepsat na souvislý text** (šablona z bloku výše); první řádek vždy `<kind> <name> in <path>` — zachovat lexikální signál názvu na začátku (position bias).
3. **Contextual BM25 analog:** context text přidat do FTS5 obsahu symbolů — úprava naplňování FTS5 v `indexer/db.py` (`rebuild_fts`) + triggery; **bez schema změny** (FTS5 tabulka se dropne a znovu naplní). Zvážit za config flagem `index.fts_context` — měřitelné odděleně.
4. **Cache:** `DESCRIPTION_VERSION` → `desc-v3` (mechanismus z Bloku 2).
5. **Měření:** A/B arm 3 v harnessu (Blok 0, krok 4) — `mxbai:fmt-context` vs `mxbai:fmt-body`.

**Testy:** `_build_context_prefix` — cap 10 callerů u hub funkce; chybějící `refs` → prefix jen z path; summary absent → vynechá se; FTS5 rebuild obsahuje context text (dotaz na caller jméno najde callee).

**Akceptační kritéria:** arm 3 ≥ arm 2 na sadě A (jinak zůstane formát z Bloku 2); FTS5 s kontextem nesníží precision sad B (šum z cizích jmen callerů).

**Odhad:** M (2 dny včetně FTS5 rebuild a měření).

### Blok 4: Adaptive RRF

**Proč:** vstash paper: per-query IDF váhy místo fixního RRF → +21.4 % NDCG jen změnou fúzní strategie. Nejlevnější implementace ze všech bloků (jeden soubor). Lokální kalibrace očekávání: fúzní váhy jsou vyladěné grid searchem (experimenty 8.8–8.10), čekat jednotky % NDCG, ne desítky.

**Co se mění:**

- **`search/phases/rrf_fusion.py` (`RRFFusionPhase`)** — nahradit fixní `W_FTS/W_VEC` per-query IDF-weighted RRF; `K`, boosty a `OVERFETCH` zachovat.
- **IDF váhy:** z FTS5 term statistik.
- **Východisko (doplněno po review):** `experiments/test_per_query_idf.py` už existuje — blok začíná vyhodnocením jeho výsledků, ne od nuly.
- **Benchmark:** stávající RRF vs adaptive RRF na harnessu z Bloku 0.

**Závislosti:** Blok 0 (měření).

**Implementační kroky:**

1. **Vyhodnotit `experiments/test_per_query_idf.py`** — replay na harnessu z Bloku 0; rozhodnout variantu IDF zdroje: FTS5 `fts5vocab` virtual table (vestavěná, žádná schema změna) vs vlastní term-statistiky.
2. **`search/phases/rrf_fusion.py`** — nepřepisovat `RRFFusionPhase`; přidat parametrizaci `weights: "fixed" | "adaptive"` (default `fixed`, přepínač v pipeline config). Adaptive větev: per-query váhy `w_fts/w_vec` odvozené z průměrného IDF termů dotazu (vzorec podle vstash); `K`, boosty, `OVERFETCH` beze změny.
3. **Benchmark:** fixed vs adaptive na harnessu (sady A i B, hybrid varianta).
4. **Produkční default** podle výsledku — adaptive jen pokud vyhraje; jinak zůstane za feature flagem.

**Testy:** unit test váhového výpočtu (syntetická IDF → očekávané váhy); pipeline test s `weights="adaptive"` (deterministické pořadí na fixture datech); regrese: `fixed` mód produkuje identické výsledky jako dnes.

**Akceptační kritéria:** NDCG@10 adaptive ≥ fixed na harnessu — jinak se neadoptuje (očekávání jsou jednotky %, takže reálný výsledek může být "nezavádět").

**Odhad:** S (1 den).

### Blok 5: Cross-encoder reranker

**Proč:** Jediná vrstva pipeline, která ve fw-context chybí úplně. ReSIM: +27.8 % Recall, +21.7 % nDCG. "Beyond the Reranker": většina kvality pipeline je v rerankeru. 2026 konsenzus: cross-encoder rerank nad top-50–200 kandidáty je de facto standard. Runtime-only změna, žádná schema změna, CPU stačí.

**Co se mění:**

- **`src/fw_context_mcp/search/reranker.py`** (nový modul) — cross-encoder inference (lazy import).
- **Pipeline:** first-stage (FTS5+dense+RRF) → top-50 → cross-encoder → top-N.
  - Sladit s `OVERFETCH_FTS/OVERFETCH_VEC=50` v `RRFFusionPhase` — reranker konzumuje top-50 **výstupu RRF**, ne raw fází.
- **`semantic_search` povinně zahrnout (zpřísněno po review):** dnes čistý KNN; bez rerankeru by zůstal trvale horší než `smart_search`. Rerank fáze se zapne i do `semantic_search` cesty (`mcp/handlers/search.py:514`).
- **Model:** ModernBERT-base (149M) fine-tuned na code retrieval, nebo Granite R2 cross-encoder.
- **`config/settings.py`** — `llm.reranker_model`, `search.rerank_top_k`.
- **Kalibrace očekávání:** vstash měřil, že po fine-tuningu bi-encoderu (Blok 6) reranker přestává přidávat — hodnota Bloku 5 je nejvyšší teď, před FT. Nebrání implementaci.

**Závislosti:** Blok 0 (měření). Runtime-only — žádná schema změna.

**Implementační kroky:**

1. **`src/fw_context_mcp/search/reranker.py`** (nový) — `Reranker` protocol + `CrossEncoderReranker` (sentence-transformers `CrossEncoder`, lazy import); metoda `rank(query, candidates, top_k) -> list` vrací přeskládané kandidáty se skóre.
2. **Pipeline změna (nutná, odhaleno review):** `RRFFusionPhase.run()` dnes truncuje na `ctx.limit` (`ranked[: ctx.limit]`) — reranker potřebuje top-50. Změna: RRF uloží top-50 do nového pole `PipelineContext.ranked_candidates`, `final_results` naplní až rerank fáze (nebo fallback truncate, když reranker off). Zpětně kompatibilní: bez rerankeru `final_results = ranked_candidates[:limit]`.
3. **`search/phases/rerank.py`** (nový) — `RerankPhase(Phase)`: `should_run` = `ranked_candidates` non-empty ∧ `reranker_model` nastaven; konzumuje top-`rerank_top_k` kandidátů, produkuje `final_results`. Registrace v `pipeline.py:_build_registry()` + pořadí v SMART_SEARCH: za `RRFFusionPhase`, před `ExpandContextPhase`.
4. **`mcp/handlers/search.py:semantic_search`** — po KNN kroku volat stejný reranker (sdílená funkce, ne duplikát logiky fáze); `semantic_search` tak přestane být trvale horší než `smart_search`.
5. **`config/settings.py`** — `llm.reranker_model: str | None = None` (off by default), `search.rerank_top_k: int = 50` (sladěno s `OVERFETCH_*=50` v RRF).
6. **Model:** `cross-encoder/ms-marco-MiniLM-L6-v2` jako levný start nebo ModernBERT-base fine-tuned na CoIR; stažení z HF automaticky přes ST (~100–600 MB) — zdokumentovat v CLAUDE.md.

**Testy:** `tests/test_rerank.py` — fake reranker (identity → pořadí beze změny; reverse → obrácené pořadí); `should_run` logika (off model, prázdní kandidáti); RRF → `ranked_candidates` (top-50) → fallback truncate bez rerankeru; `semantic_search` integrace s mockem.

**Akceptační kritéria:** precision@5 na harnessu **+≥15 %**; latence `smart_search` **+<200 ms** navíc na CPU (50 párů); bez `reranker_model` se pipeline chová bitově stejně jako dnes.

**Odhad:** M (2–3 dny).

### Blok 6: Self-supervised fine-tuning

**Proč:** vstash (stejný stack: sqlite-vec + FTS5 + RRF): 74.5 % dotazů má top-10 neshodu dense vs FTS = bezplatný trénovací signál bez labelů. Fine-tune BGE-small (33M) → +19.5 % NDCG, přebíjí ColBERTv2 (110M). Jediný blok adaptivní na konkrétní projekt. Referenční baseline: `Stffens/bge-small-rrf-v2` (HF).

**Co se mění:**

- **`src/fw_context_mcp/indexer/finetune.py`** (nový modul) — disagreement pipeline:
  1. Spustit sadu dotazů (z docstringů, LLM summary, nebo syntetických).
  2. Top-10 dense vs top-10 FTS5 → disagreement pairs = trénovací data.
  3. Fine-tune s `MultipleNegativesRankingLoss`.
  4. Uložit do `~/.fw-context/models/<project_id>/`.
- **Verzování vůči description formátu:** při změně `desc-vN` (Bloky 2/3) se fine-tuned model invaliduje a trénuje znovu.
- **Náklady:** ~5–30 minut na projekt (33M model, CPU/MPS).

**Závislosti:** Blok 1 (fine-tuned model se načítá přes sentence-transformers), Blok 2 (stabilní description formát).

**Implementační kroky:**

1. **`src/fw_context_mcp/indexer/finetune.py`** (nový modul) — tři části:
   - `generate_queries(conn) -> list[str]` — syntetické dotazy z docstringů, `symbols.summary` a signatur (žádný LLM pass; šablony typu "code that <summary>").
   - `mine_disagreements(conn, embedder) -> triples` — pro každý dotaz top-10 dense vs top-10 FTS5; neshody → trénovací triples `(query, positive, negative)` pro `MultipleNegativesRankingLoss`.
   - `train(triples, base_model, out_dir)` — sentence-transformers training loop (lazy import); epochs/batch size v configu.
2. **`cli.py`** — nový příkaz `fw-context finetune` (spustí mining + train + eval report).
3. **Model storage:** `~/.fw-context/models/<project_id>/<base>-ft-<desc-vN>/` + `metadata.json` (base model, desc verze, datum, harness metriky před/po).
4. **Embedder factory rozšíření:** `embed_model = "ft://latest"` → resolve na nejnovější FT model s kompatibilní `desc-vN` (nesedí verze → warning + fallback na base model).
5. **Eval:** harness sady A/B před/po FT (Blok 0), výsledek do `metadata.json`.

**Testy:** mining logika na in-memory DB (syntetické neshody → správné triples); resolve `ft://latest` (výběr podle desc verze, fallback); train smoke test s tiny modelem — marker `slow`, skip na CI.

**Akceptační kritéria:** NDCG@10 **+≥10 %** vůči base modelu na harnessu; FT doběhne **< 30 min** na CPU na referenčním projektu; invalidace při změně desc verze ověřená testem.

**Odhad:** L (3–5 dnů).

### Blok 7: Granite R2 dense backend

**Proč:** 32K context = odstranění head+tail truncace z Bloku 2 (signatura + komentář + tělo v jednom vektoru bez useknutí). MTEB Code 63.8 (311M) vs 48.5 (R1). Matryoshka: indexovat 768-dim, vyhledávat v 128-dim → 6× menší storage, rychlejší KNN, ztráta jen 1.6 bodu.

**Co se mění:**

- Přes `SentenceTransformerEmbedder` (Blok 1) — Ollama ModernBERT nepodporuje.
- **`config/settings.py`** — `llm.embed_model = "ibm-granite/granite-embedding-311m-multilingual-r2"`, `search.embed_search_dim = 128`.
- **vec0 rebuild (doplněno po review):** vec0 tabulka má fixní dimenzi, ale `init_vec_table(conn, dim)` při předaném `dim` už dnes dělá `DROP TABLE IF EXISTS vec_symbols` + recreate (`db.py:2133-2136`) a `_build_embeddings` ho volá s detekovanou dimenzí při každém embed běhu (`runner.py:220`) — rebuild při změně dimenze (Matryoshka 768→128) tedy funguje automaticky. **Edge case k ošetření:** recreate se spustí jen když existují symboly k embedování; při prázdné frontě (vše už embedované pod novým klíčem) zůstane stará dimenze a KNN dotaz s jinou dimenzí selže — přidat guard (při startu embed fáze porovnat `build_configs.embedding_dim` s dimenzí aktuálního modelu a při mismatchu provést recreate i bez fronty).
- 97M varianta bez MRL — méně zajímavá (LateOn-Code-edge 17M má lepší Code score za zlomek velikosti).

**Závislosti:** Blok 1.

**Implementační kroky:**

1. **`config/settings.py`** — `embed_model = "ibm-granite/granite-embedding-311m-multilingual-r2"`; backend autodetekce ST přes Blok 1. Nový klíč `search.embed_dim: int | None = None` (None = nativní dimenze modelu).
2. **Matryoshka rozhodnutí (doporučeno):** ukládat rovnou **128-dim** vektory (truncace na straně embedderu před uložením) — jednodušší než duální dimenze, ztráta jen -1.6 bodu MTEB Code. vec0 tabulka tak má jednu dimenzi jako dnes.
3. **`indexer/db.py:init_vec_table`** — ~~detekce dimenze-mismatchu~~ **opraveno po review:** drop+create při změně dimenze už funguje (`db.py:2133-2136`). Zbývá ošetřit edge case prázdné embed fronty (guard výše) + regresní test stávajícího drop+create chování (768→128).
4. **Truncace z Bloku 2 vypnout** pro tento model: gate na `Embedder.max_tokens` (32 768 → žádný head+tail, celé tělo + komentář + signatura v jednom vektoru).
5. **Reindex embeddingů** — nový model klíč → automatická regenerace (mechanismus z Bloku 2); na referenčním projektu změřit i čas embed fáze na CPU.
6. **Benchmark:** Granite R2 vs mxbai na harnessu (sady A i B, dense-only i hybrid).

**Testy:** dim-mismatch rebuild vec0 (768→128); truncace gate (max_tokens > body → žádná truncace); autodetekce backendu pro `ibm-granite/*`.

**Akceptační kritéria:** recall@10 / NDCG@10 ≥ mxbai baseline na obou sadách; embed fáze na CPU **≤ 2× pomalejší** než mxbai přes Ollama (jinak zvážit MPS/ONNX variantu inference).

**Odhad:** S–M (1–2 dny, většina času je reindex + měření).

### Blok 8: LateOn-Code ColBERT

**Proč:** 17M model, 66.64 MTEB Code (+50 % vs BM25 baseline), běží v procesu přes PyLate (žádný Ollama), CPU. Token-level MaxSim je přesnější pro dlouhá těla. Kalibrace: +50 % je vůči BM25, ne vůči hybridu fw-context — zajímavé až po vyčerpání Bloků 2/5/6.

**Co se mění:**

- **`indexer/db.py`** — nová tabulka `colbert_embeddings`: `symbol_id, token_position, vector BLOB[128]`.
- **MaxSim vyhledávání:** PLAID index nebo LEMUR redukce → sqlite-vec (LEMUR umožní reuse single-vector KNN).
- Embedder přes Blok 1 (`SentenceTransformerEmbedder` + PyLate).

**Approval required:** schema změna v `indexer/db.py` + nová dependency `pylate`.

**Závislosti:** Blok 1.

**Implementační kroky:**

1. **Schema migrace (`indexer/db.py`):** nová tabulka `colbert_embeddings(symbol_id INTEGER, token_position INTEGER, vector BLOB)` + index na `symbol_id`; bump `CURRENT_SCHEMA_VERSION` + migrační cesta pro existující DB.
2. **`pyproject.toml`** — `pylate` jako optional extra (stejný pattern jako `st` extra z Bloku 1).
3. **`llm/st_embedder.py`** — `ColBERTEmbedder` (PyLate `models.ColBERT`): `embed_documents` vrací seznam token-vektorů na symbol (ne 1 vektor); `dim = 128` nativně.
4. **`indexer/runner.py`** — index-time větev pro multi-vector: detekce `ColBERTEmbedder` → zápis do `colbert_embeddings` místo `embeddings`/vec0.
5. **Vyhledávání — dvě varianty k vyhodnocení:**
   - **LEMUR redukce (preferováno):** multi-vektory → single-vector prostor → reuse sqlite-vec KNN (žádný vlastní MaxSim engine).
   - **Fallback:** vlastní MaxSim v Pythonu nad top-N kandidáty z first-stage (FTS5+dense) — late-stage rescoring, ne full index scan.
6. **Benchmark:** LateOn-Code vs dense pipeline (Bloky 2+5) na harnessu.

**Testy:** schema migrace (stará DB → nová verze); index-time zápis token-vektorů; MaxSim na fixture vektorech (ručně spočítané skóre); end-to-end smoke — marker `slow`.

**Akceptační kritéria:** NDCG@10 **> dense pipeline o ≥5 %** — jinak se nezavádí (storage cena N vektorů/symbol se musí vyplatit).

**Odhad:** L (4–5 dnů).

### Blok 9: SPLADE-Code (podmíněný)

**Proč:** Learned sparse retrieval pro kód — 75.4 MTEB Code (0.6B), naučené query expansion ("allocate" → malloc/calloc/realloc). Koncepčně nahrazuje FTS5 i dense. Modely publikované (`naver/splade-code-06B`, `naver/splade-code-8B`). **Implementovat jen pokud je lexikální přesnost FTS5 po Blocích 0–8 pořád bottleneck.** Jediný blok vyžadující GPU — proto na konci (local-first strategie).

**Co se mění (pokud trigger nastane):** tabulka `sparse_embeddings` (`symbol_id, term_indices, term_weights`), nahrazuje FTS5 fázi v pipeline. **Approval required:** schema změna v `indexer/db.py`.

**Implementační kroky (kostra — detail až při triggeri):**

1. **Trigger podmínka:** po dokončení Bloků 0–8 ukazuje harness, že lexikální dotazy (přesná jména symbolů, error kódy) mají systematicky nižší recall než koncept-dotazy — jinak se blok neotevírá.
2. Schema `sparse_embeddings` + migrace; inference přes `transformers` (MLM head — Ollama neumí, GPU prakticky nutné).
3. Search fáze `sparse_search.py` jako náhrada `fts5_search.py` v pipeline (za feature flagem, A/B).
4. Porovnání learned sparse vs FTS5 na sadě B (signaturové dotazy — doména sparse retrievalu).

**Odhad:** L (4+ dnů, GPU infrastruktura navíc).

### Blok 10: MetaEmbed (mimo scope)

**Proč:** Matryoshka multi-vector (4–8 meta-vektorů místo N token-vektorů) řeší storage explozi ColBERTu. Vyžaduje vlastní fine-tuning, spekulativní. **Doporučení: neimplementovat** — pokud storage Bloku 8 bude problém, řeší ho LEMUR redukce (už v Bloku 8).

---

## Doporučené pořadí

**Podle závislostí a dopadu:** Blok 0 → Blok 1 → Blok 2 → Blok 5 → Blok 4 (levný, přidat cestou) → Blok 3 → Blok 6 → Blok 7 → Blok 8 → (Blok 9 podmíněně). Blok 10 mimo scope.

### Prioritizace podle dopadu (analýza 2026-07-22)

**Strategie: local-first, žádné nové LLM passy.** Celý plán je proveditelný na lokálních modelech — fw-context už běží na Ollama (chat `qwen2.5-coder:14b` + embeddings). Klíčové rozhodnutí pro Bloky 2/3: **kontext nevyrábět novým LLM voláním** — deterministické fakty (path, modul, callers z `refs`) jsou zdarma a bez halucinací, LLM summary se reuses z existující analyze fáze. Nové modely vstupují jen tam, kde mají jasný poměr přínos/cena: reranker (149M, CPU), LateOn-Code (17M, CPU), Granite R2 (311M, sentence-transformers), fine-tuning (33M, CPU). Jediný blok vyžadující GPU je SPLADE-Code — proto je na konci.

| # | Blok | Očekávaný dopad | Zdůvodnění |
|---|------|----------------|------------|
| 1 | **Blok 5 — reranker** | Vysoký (+20–30 % precision dle ReSIM) | Jediná vrstva, která chybí úplně. Zlepší `smart_search` i `semantic_search` bez ohledu na kvalitu first-stage. |
| 2 | **Blok 2 — těla funkcí** | Vysoký na koncept-dotazy | Řeší slepotu `semantic_search` k implementaci. Žádný nový model. Riziko: diluce signálu — proto Blok 0. |
| 3 | **Blok 6 — fine-tuning** | Vysoký, největší náklady | vstash: +19.5 % NDCG; 74.5 % disagreement = free trénovací signál. Jediný blok adaptivní na projekt. |
| 4 | **Blok 7 — Granite R2** | Střední, synergie s Blokem 2 | 32K context = odstranění truncace z Bloku 2. Matryoshka je storage/rychlost win, ne kvalita. |
| 5 | **Blok 4 — adaptive RRF** | Nízký až střední | Fúzní váhy už jsou vyladěné; čekat jednotky % NDCG. Nejlevnější implementace. |
| 6 | **Blok 3 — contextual formát** | Nízký až vysoký (nejistý) | Anthropic -35 %, ale měřeno na document retrieval, ne symbol-level. Proto A/B arm. Graph signál už částečně běží (`PAGERANK_BOOST`). |
| 7 | **Blok 8 — LateOn-Code** | Střední, vysoká cena | +50 % je vůči BM25, ne vůči hybridu. MaxSim v sqlite = vlastní engine nebo LEMUR. |
| 8 | **Blok 9 — SPLADE** | Nízký poměr přínos/cena | Nahrazuje funkční FTS5, 600M+ model, GPU, schema změna. |
| 9 | **Blok 10 — MetaEmbed** | Mimo scope | — |

**Strukturální postřehy:**

1. **`semantic_search` je pod-dotázaný** — čistý KNN bez fúze. Bloky 2 a 6 ho zlepší přímo; Blok 5 ho povinně protáhne rerankerem, jinak zůstane trvale horší než `smart_search`.
2. **Blok 0 je reálně blokující a dnes chybí data** — bez sjednoceného datasetu a rozhodovacích kritérií se dopady Bloků 2–5 nedají věrohodně změřit.
3. **Contextual Retrieval mění implementaci Bloků 2/3, ne jejich prioritu** — je to forma description, ne nová fáze.
4. **Reference ověřeny 2026-07-22:** LateOn-Code-edge (HF model card), Granite R2 (32K, MRL, ONNX/OpenVINO potvrzeno), vstash (74.5 % / +19.5 % / +21.4 % / 20.9 ms sedí s abstraktem; FT model publikován jako `Stffens/bge-small-rrf-v2`), SPLADE-Code (modely `naver/splade-code-06B`, `naver/splade-code-8B` na HF). Reranker jako 2026 de facto standard potvrzen napříč zdroji.

---

## Research findings (podklad pro bloky)

### LateOn-Code: ColBERT pro kód (nejslibnější směr)

**lightonai/LateOn-Code-edge** (17M parametrů), červenec 2025

- MaxSim token-level late interaction, 128-dim výstup, 2048 token context
- **66.64 MTEB Code avg** — přebíjí 300M single-vector modely, +50 % vs BM25 baseline (44.41)
- 17M model je menší než mxbai-embed-large (~70 MB vs ~670 MB), běží na CPU
- 149M varianta: 74.12 MTEB Code, blízko Qwen3-Embedding-0.6B
- Trénováno na CoIR datasetech (CodeSearchNet, CosQA, StackOverflow QA, atd.)
- Existující nástroj: **colgrep** — CLI grep-like sémantické vyhledávání s LateOn-Code
- Knihovna: **PyLate** — Sentence Transformers kompatibilní, PLAID index pro MaxSim
- Reference: [HuggingFace model card](https://huggingface.co/lightonai/LateOn-Code), [PyLate na GitHub](https://github.com/lightonai/pylate)

**Pro fw-context:** Při 17M jde model embeddovat do procesu (žádný Ollama!). Token-level matching je přesnější než dense, ale storage je vyšší než 1 vektor/symbol. PLAID index nebo LEMUR redukce by řešily vyhledávání.

### SPLADE-Code: Learned Sparse Retrieval pro kód

**arXiv:2603.22008** (březen 2026), Simon Lupart et al.

- První large-scale learned sparse retrieval specializovaný na kód
- 600M–8B parametry, **75.4 MTEB Code** (pod 1B), **79.0** (8B)
- **Modely publikovány:** `naver/splade-code-06B` a `naver/splade-code-8B` na HuggingFace (duben–květen 2026) — není nutné trénovat, stačí integrovat
- Sub-milisekundová latence na 1M dokumentech
- Klíč: *learned expansion tokens* — model se naučí expandovat dotaz o relevantní tokeny (např. `"allocate"` → `malloc`, `calloc`, `realloc`)
- Jednofázový trénink, žádné složité pipeline

**Pro fw-context:** Koncepčně nejčistší — jeden model nahradí FTS5 (lexikální) i dense (sémantické) vyhledávání. Problém: 600M je velký model vyžadující GPU.

### Granite Embedding R2: Matryoshka dense + 32K context

**IBM, květen 2026** (arXiv:2605.13521), Apache 2.0

Dvě velikosti na ModernBERT architektuře:

| Vlastnost | 311M (full) | 97M (compact) |
|-----------|------------|---------------|
| Embedding dim | 768 (MRL) | 384 |
| Tokenizer slovník | 262K (Gemma3) | 180K (GPT-OSS pruned) |
| Context length | **32 768 tokenů** | **32 768 tokenů** |
| Tokenizer C++ | 3.38 tok/slovo | 2.74 tok/slovo |
| Tokenizer C | 3.18 tok/slovo | 2.54 tok/slovo |

**Výkon na code retrieval (MTEB Code v1):**

| Model | Params | Code avg |
|-------|--------|----------|
| granite-embedding-311m-multilingual-r2 | 311M | **63.8** |
| granite-embedding-97m-multilingual-r2 | 97M | **60.4** |
| granite-embedding-278m-multilingual (R1) | 278M | 48.5 |

**Matryoshka dimenze — klíčové číslo pro fw-context:**

| Dimenze | MTEB Code | Ztráta vs full |
|---------|-----------|----------------|
| 768 (full) | 63.9 | — |
| 384 | 63.7 | **-0.2** |
| 256 | 63.4 | -0.5 |
| 128 | 62.3 | -1.6 |

Ukládat 768-dim, vyhledávat v 128-dim = 6× menší storage v sqlite-vec, KNN za zlomek času, kvalita skoro stejná (-1.6 bodu).

**Blokující problém: Ollama nepodporuje ModernBERT.** Model jde použít přes `sentence-transformers` (CPU/MPS), `llama.cpp`/GGUF, nebo ONNX/OpenVINO.

### MetaEmbed: Matryoshka Multi-Vector

**Meta, ICLR 2026 Oral** (arXiv:2509.18095) — learnable Meta Tokens (fixní počet, např. 4–8) místo N token-vektorů; Matryoshka napříč Meta tokeny. Řeší storage explozi multi-vectoru. Vyžaduje fine-tuning — mimo scope (viz Blok 10).

### LEMUR: Multi-Vector → Single-Vector redukce

**ICML 2026** (arXiv:2601.21853) — redukuje MaxSim similarity search na single-vector dot product; o řád rychlejší než nativní multi-vector search; funguje s existujícími single-vector indexy (sqlite-vec). Kandidát pro Blok 8 místo vlastního MaxSim enginu.

### SpIDER: Graficky obohacené code retrieval

**arXiv:2512.16956** (prosinec 2025) — dense retrieval + codebase structure graph → **+13 %**. Základ pro graph augmentaci v Bloku 3 — fw-context už má libclang call graph (`refs` tabulka), augmentace je čistě softwarová změna v `_build_embeddings()`.

### SLIM: Sparse Late Interaction

**SIGIR 2023** (arXiv:2302.06587) — ColBERT token vektory mapované do sparse lexikálního prostoru, kompatibilní s inverted indexy. Zajímavý hybrid, zatím bez bloku.

### Position Bias (na co si dát pozor)

**EMNLP 2025** (arXiv:2505.13950) — dense a ColBERT modely ztrácí **15.6 %**, když relevantní informace je na konci textu. BM25 je vůči pozici robustní. Důvod pro head+tail truncaci v Bloku 2.

### vstash: Self-supervised fine-tuning na vlastním kódu

**arXiv:2604.15484** (duben 2026), Jayson Steffens

- **Stejný stack jako fw-context:** sqlite-vec + FTS5 + RRF
- Klíčový objev: **74.5 % dotazů má top-10 neshodu mezi dense a FTS**
- Tato neshoda = **bezplatný trénovací signál bez lidských labelů**
- Fine-tune BGE-small (33M) na 76K disagreement triples → **+19.5 % NDCG@10**
- 33M model po fine-tuningu **přebíjí ColBERTv2 (110M)** na 3 z 5 BEIR datasetů
- **Adaptive RRF:** per-query IDF váhy místo fixního k=60 → **+21.4 % NDCG**
- Latence: 20.9 ms medián na 50K chuncích
- Negativní výsledek: cross-encoder reranker na konci pipeline už nepřidal — fine-tuned bi-encoder byl dost dobrý
- Fine-tuned model publikován jako `Stffens/bge-small-rrf-v2` (HF) — referenční baseline pro Blok 6

### Contextual Retrieval — prefixovat chunky kontextem

**Anthropic Engineering, září 2024** ([anthropic.com/engineering/contextual-retrieval](https://www.anthropic.com/engineering/contextual-retrieval)) + reprodukce (Milvus/LlamaIndex) — ověřeno v [Retrieval Layer Research Summary 2026](https://lin-guanguo.github.io/llm-memory-research/retrieval.summary/)

Dvě sub-techniky aplikované **před indexací**: Contextual Embeddings (chunku se předřadí 50–100 tokenů kontextu, který ho situujé do celého dokumentu) a Contextual BM25 (stejný prefix do BM25 indexu). Kontext generuje LLM (Claude Haiku, ~$1/1M tokenů s prompt cachingem).

**Přesná čísla** (metrika 1 − recall@20, průměr přes domény včetně codebases):

| Konfigurace | Failure rate | Redukce |
|---|---|---|
| Baseline (embeddings + BM25) | 5.7 % | — |
| + Contextual Embeddings | 3.7 % | **-35 %** |
| + Contextual BM25 | 2.9 % | **-49 %** |
| + Reranker (top-150 → top-20) | 1.9 % | **-67 %** |

Zlepšilo to **každou** testovanou kombinaci embedding modelu a datasetu; benefity se **sčítají** s rerankerem. Negativní výsledky: generické document summary (minimální zisk), HyDE, summary-based indexing — klíč je, že kontext musí být **chunk-specific**, ne document-generic.

**Mapování na fw-context — výchozí pozice je lepší než u typického RAG:**

1. "Chunk" = funkce, ne náhodný řez textu — funkce je přirozeně self-contained (jméno, signatura). Problém ztráty kontextu je menší, ale existuje: `handle_irq()` samo neříká, že je to DMA handler UART2 driveru.
2. **Není potřeba LLM na výrobu kontextu** — `symbols.summary` z `--analyze` je přesně ten chunk-specific kontext. Deterministické fakty (path, modul, callers z `refs`) doplní strukturální roli bez halucinací.
3. **Contextual BM25 analog** = dát stejný kontextový text i do FTS5 indexu (rebuild FTS5, ne schema změna).
4. Stacking potvrzen: Bloky 2/3 (contextual) + Blok 5 (reranker) = přesně Anthropic -67 % konfigurace.

**Lokální modely a jejich omezení (proveditelnost contextual cesty):**

| Role | Lokálně? | Omezení |
|---|---|---|
| Kontext pro Bloky 2/3 | ✅ reuse `symbols.summary` | 14B model (qwen2.5-coder) < Claude Haiku — riziko halucinace modulové role. Mitigace: deterministický kontext z call graphu jako základ, LLM summary jen jako doplněk (už generované s `temperature=0.1`) |
| Granite R2 (Blok 7) | ⚠️ ne přes Ollama | ModernBERT v Ollama nepodporován → sentence-transformers (CPU/MPS) nebo ONNX |
| LateOn-Code (Blok 8) | ✅ | 17M, CPU, v procesu přes PyLate |
| Reranker (Blok 5) | ✅ | ModernBERT-base 149M, CPU stačí na top-50 párů |
| Fine-tuning (Blok 6) | ✅ | BGE-small 33M, minuty na CPU/MPS |
| SPLADE-Code (Blok 9) | ❌ prakticky ne | 0.6B+, MLM head — Ollama neumí, potřeba transformers + GPU |

Skutečné limitace lokální cesty: čas indexace (14B = sekundy/symbol, mitigováno `analyze_vendor=False` + cache podle code hashe), `num_ctx=16384` (velké TU se do promptu nevejdou → chudší kontext), žádný prompt caching v Ollama (stateless API — další důvod preferovat deterministický kontext z indexu před LLM přegenerováním). **Doporučená strategie: hybrid — deterministický kontext vždy, LLM summary jen kde už existuje. Žádný nový model, žádný nový LLM pass, plně lokální.**

### Co 2026 evidence vyvrací (na co nepoužít úsilí)

Z [Retrieval Layer Research Summary 2026](https://lin-guanguo.github.io/llm-memory-research/retrieval.summary/):

- **HyDE query rewriting** — "2023 artifact", moderní supervised embeddery zisk ruší. Nerozvíjet.
- **Semantic chunking** — tři nezávislé benchmarky ukazují paritu nebo ztrátu proti naivnímu chunkingu. Držet se 1 vektor / funkci, chunkovat jen velmi dlouhé funkce.
- **GraphRAG** — zřídka stojí za cenu mimo global sensemaking. Blok 3 není GraphRAG (jen textová augmentace) — neeskalovat do plného graph-retrieval enginu.

### Cross-encoder reranker (podklad pro Blok 5)

**ReSIM** (arXiv:2602.09548): cross-encoder pro binary function similarity → **+27.8 % Recall, +21.7 % nDCG**. Bi-encoder vidí query a kandidáta nezávisle — cross-encoder je zpracuje společně a vidí jejich vztah.

**"Beyond the Reranker"** (arXiv:2606.28367): *"A strong cross-encoder reranker accounts for most of the pipeline's quality."*

**Granite Embedding R2** (IBM): release obsahuje i cross-encoder variantu pro reranking.

**2026 konsenzus:** cross-encoder rerank nad top-50–200 kandidáty je de facto standard produkčních RAG pipeline (shoda napří zdroji).

### Instruction-tuned embeddings

Qwen3-Embedding (už podporovaný) a Granite R2 podporují task-specific instrukce v promptu:

- Index time: `"Represent the C/C++ function for code retrieval: <signature + body>"`
- Query time: `"Find firmware code that handles DMA timeout and error recovery"`

Drobné zlepšení bez změny modelu — jen jiné prompty. Zapracováno do Bloku 1 (prompty jako zodpovědnost embedder implementace).

### Shrnutí: celkový obrázek vyhledávací pipeline

| Vrstva | Dnes ve fw-context | Kam jít | Blok |
|--------|-------------------|---------|------|
| **Sparse retrieval** | FTS5 (BM25) | SPLADE-Code (podmíněně) | 9 |
| **Dense retrieval** | mxbai / qwen3 (512 tok) | Granite R2 311M (32K, MRL) nebo LateOn-Code | 7, 8 |
| **Fúze** | RRF k=30, váhy 1.8/0.2 + boosty | Adaptive per-query RRF | 4 |
| **Reranker** | **Chybí** | Cross-encoder (ModernBERT) | 5 |
| **Self-supervised FT** | **Chybí** | vstash disagreement → fine-tune | 6 |
| **Query processing** | LLM → FTS5 termy | Instruction-tuned embedding (HyDE ne) | 1 |
| **Contextual prefix** | Metadata jako tokeny | Contextual Retrieval (Anthropic) | 3 |
| **Embedder abstrakce** | Jen Ollama | `Embedder` ABC (Ollama + ST + PyLate) | 1 |
| **Call graph v embeddingu** | Jen pro traversal | Augmentovat embeddingy strukturou | 3 |
| **Embedding units** | Jen descriptions | Těla funkcí | 2 |

---

## Verifikace

- `tests/quality_eval.py` — existující benchmark kvality vyhledávání (rozšířit v Bloku 0)
- Nový test: `search_bodies` vs `semantic_search` recall na dotazech, kde odpověď je jen v implementaci
- Manuální testy na reálném projektu (např. mbed-os firmware)
