# Vylepšení embeddingové vrstvy: těla funkcí + BGE-M3 cesta

**Status:** Návrh — čeká na schválení  
**Datum:** 2026-07-21

---

## Cíl

Zvýšit recall sémantického vyhledávání nad C/C++ kódem. Dnes `semantic_search` embeduje pouze popisy symbolů (signatura + docstring + LLM summary, ~30–200 tokenů), nikoliv implementaci. `search_bodies` (FTS5) prohledává těla, ale jen lexikálně — `malloc()` nenajde na dotaz "allocate buffer".

## Analýza současného stavu

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

### Co to znamená v praxi

| Dotaz | Kde je odpověď | Najde se dnes? |
|-------|---------------|----------------|
| `"DMA timeout handler"` | `if (ch->status & DMA_TEIF)` v těle `dma_irq()` | Ne — description o timeoutu neví |
| `"allocate buffer for packet"` | `buf = malloc(PKT_SIZE)` v těle `prepare_packet()` | Ne — description o malloc neví |
| `"I2C error recovery"` | `i2c_reset(); i2c_reinit()` v těle `i2c_xfer()` | Ne — leda by to popsalo LLM summary |

Současné řešení: `search_bodies("malloc")` najde lexikálně. `semantic_search` je slepé k implementaci.

### Co BGE-M3 nabízí navíc

| Schopnost | Stav ve fw-context | BGE-M3 alternativa |
|-----------|-------------------|-------------------|
| Dense embeddings | `mxbai-embed-large` / `qwen3-embedding` | Stejné, 8192 token context (vs ~512) |
| Sparse retrieval | FTS5 (BM25) — lexikální, nenaučené váhy | SPLADE-style — naučené váhování termů |
| Multi-vector | Nemá | ColBERT — token-level MaxSim, přesnější pro dlouhá těla |

## Návrh změn (fázovaný)

### Fáze 1: Embedovat těla funkcí (nízké riziko, potenciálně velký přínos)

**Změny:**

1. **`src/fw_context_mcp/indexer/runner.py:_build_embeddings()`** — rozšířit description o prvních ~2000 znaků těla funkce (`source` sloupec z `symbols`). Zachovat stávající formát jako prefix (kind, name, path, sig, doc, llm), připojit `body: <tělo>`.

2. **`src/fw_context_mcp/indexer/db.py`** — `source` sloupec už existuje. Žádná schema změna.

3. **`src/fw_context_mcp/config/settings.py`** — nový config klíč `llm.embed_bodies = true` (default).

4. **Model context:** `mxbai-embed-large` má kontext ~512 tokenů. Delší těla by bylo třeba truncate. Pokud se použije model s delším kontextem (BGE-M3, qwen3-embedding), truncate se zvedne.

**Rizika:**
- Embedding se stane "špinavější" — kombinace metadat + kódu může rozmazat signál
- Delší description → víc tokenů → pomalejší embedding generation
- Nutné ověřit benchmarkem: recall `semantic_search` před a po

### Fáze 2: BGE-M3 pro sparse + dense (střední riziko)

**Změny:**

1. **`src/fw_context_mcp/config/settings.py`** — přidat podporu pro `bge-m3` jako `embed_model` (stejně jako dnes `mxbai-embed-large` a `qwen3-embedding`).

2. **`src/fw_context_mcp/llm/ollama.py:call_ollama_embed()`** — BGE-M3 potřebuje extra parametr `"return_sparse": true` pro získání sparse vektorů z jednoho forward passu.

3. **`src/fw_context_mcp/indexer/db.py`** — nová tabulka `sparse_embeddings` pro uložení sparse vektorů (formát: `symbol_id`, `term_indices` BLOB, `term_weights` BLOB). Alternativně rozšířit FTS5 o naučené váhy.

4. **`src/fw_context_mcp/search/`** — nová fáze nebo modifikace `fts5_search.py` → hybridní sparse/dense retrieval s BGE-M3 sparse vektory místo (nebo vedle) FTS5.

5. **`src/fw_context_mcp/search/pipeline.py`** — parametrizovat, zda sparse fáze použije FTS5 nebo BGE-M3 sparse.

**Rizika:**
- BGE-M3 ~2.2 GB — vyžaduje GPU pro rozumnou rychlost
- Sparse vektory jsou dimenze ~vocab_size — storage náročnější než dense
- Schema change (`sparse_embeddings` tabulka) — vyžaduje migraci
- Přínos oproti FTS5 nutné ověřit benchmarkem

### Fáze 3: Multi-vector / ColBERT (vysoké riziko, nejistý přínos)

**Změny:**

1. **Indexace:** každá funkce → N vektorů (jeden na token těla). Uložit jako `symbol_id, token_position, vector BLOB`.

2. **Vyhledávání:** MaxSim — pro každý token dotazu najdi nejbližší token v každém dokumentu, sečti skóre.

3. **Storage:** pro 10k funkcí × průměrně 200 tokenů = 2M vektorů. Při 1024 dimenzích × 4 bytes = ~8 GB raw (net压缩). Lze použít quantization.

**Rizika:**
- Masivní nárůst storage a výpočetní náročnosti
- `sqlite-vec` nepodporuje MaxSim nativně — nutná vlastní implementace nebo externí knihovna
- Přínos pro code search pravděpodobně marginální — funkce jsou krátké (průměr ~20–50 řádků)

**Alternativa:** Místo plného multi-vectoru vyzkoušet sliding-window chunking těl (každá funkce → 2–3 overlapping chunky po 256 tokenech). Kompromis mezi 1 vektorem a N token-vektory.

## Doporučený postup

1. **Implementovat Fázi 1** (těla do dense embeddings) — nízké riziko, schema beze změny, okamžitý přínos pro `semantic_search`.

2. **Benchmark** — změřit recall před/po na sadě reálných dotazů (např. `tests/quality_eval.py`).

3. **Fáze 2** (BGE-M3 sparse) jen pokud Fáze 1 nestačí a benchmark ukáže, že FTS5 je bottleneck.

4. **Fáze 3** (multi-vector) jen pokud existuje konkrétní use-case s dlouhými funkcemi, kde dense + sparse selhává.

## Verifikace

- `tests/quality_eval.py` — existující benchmark kvality vyhledávání
- Nový test: `search_bodies` vs `semantic_search` recall na dotazech, kde odpověď je jen v implementaci
- Manuální testy na reálném projektu (např. mbed-os firmware)
