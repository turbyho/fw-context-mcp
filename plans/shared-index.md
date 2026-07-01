# Shared LLM Analysis Cache — Centrální cache pro analýzu symbolů

**Stav:** Návrh — čeká na mentální review

## Problém

### Primární — opakovaná LLM analýza velkých symbolů (VYŘEŠENO)

Velké structy (100+ fields) generují prompt který se nevejde do kontextu
modelu. LLM vrací `unparseable response`, ale protože se neuloží žádný
záznam do `llm_analysis`, symbol se při každém `fw-context index --analyze`
znovu a znovu posílá do Ollama → nekonečná smyčka.

**Vyřešeno v `runner.py` (`_build_llm_analysis`):** prompt-fit check +
sentinel row `model="skip:toolarge:{ctx_size}"`.

### Sekundární — sdílení LLM analýzy mezi vývojáři (TENTO PLÁN)

**Situace:**
- Projekt zbox: `fw-context index --analyze` trvá 8–12 h na CPU (jen LLM
  analýza symbolů — samotné parsování a indexování je rychlé, minuty)
- Někteří devové nemají GPU → analýzu nespouští, nebo trvá desítky hodin
- GPU stanice se vypíná, server běží 24/7

**Klíčový vhled:** Jediné co je pomalé a potřebuje sdílení je **LLM analýza
symbolů**. Parsování, indexování, reference, embeddingy — to všechno jsou
minuty. Jen Ollama volání (tisíce symbolů, každý 10-30s) trvá hodiny.

`llm_analysis_cache` už existuje — je to content-addressable cache kde
klíčem je hash těla + jména + signatury + docstringu. Identický symbol
v různých projektech nebo po re-indexu dostane stejný hash → cache hit.

## Řešení

**Centralizovat jen `llm_analysis_cache`, zbytek zůstává lokální SQLite.**

```
┌─────────────────────────────────────────────┐
│ Server (24/7)                               │
│                                             │
│ fw-context-cache-server                     │
│   ├─ HTTP API (fastapi/starlette)           │
│   ├─ SQLite (jen llm_analysis_cache)        │
│   ├─ nginx HTTPS + auth (už existuje)       │
│   └─ GET  /cache/{content_hash}            │
│       PUT  /cache/{content_hash}            │
└─────────────────────────────────────────────┘
        ▲                        ▲
        │ HTTPS                  │ HTTPS
   ┌────┴──────────┐       ┌────┴──────────┐
   │ Dev A (GPU)   │       │ Dev B (CPU)    │
   │               │       │               │
   │ Lokální       │       │ Lokální        │
   │ index.db      │       │ index.db       │
   │               │       │               │
   │ fw-context    │       │ fw-context     │
   │ index         │       │ index          │
   │ --analyze     │       │ --analyze      │
   │               │       │               │
   │ Cache miss →  │       │ Cache miss →   │
   │ Ollama (GPU)  │       │ GET /cache/    │
   │ → PUT /cache  │       │   → hit!       │
   │               │       │   (uloží se    │
   │               │       │    lokálně)    │
   └───────────────┘       └───────────────┘
```

**Flow:**

1. GPU dev spustí `fw-context index --analyze`
   - Pro každý symbol: `compute_content_hash()` → dotaz na cache server
   - Cache hit → okamžitě uloženo do lokální `llm_analysis` (bez Ollama)
   - Cache miss → Ollama (GPU) → výsledek se uloží lokálně **a zároveň
     PUT na cache server**
2. CPU dev spustí `fw-context index --analyze`
   - Pro každý symbol: `compute_content_hash()` → dotaz na cache server
   - Cache hit → okamžitě uloženo lokálně (naprostá většina — GPU dev už
     analýzu vygeneroval)
   - Cache miss → Ollama (CPU) — ale jen pro nové/změněné symboly

**Efekt:**
- CPU dev analyzuje jen symboly které ještě nikdo neanalyzoval
- Jakmile GPU dev jednou projet celý projekt, všichni ostatní mají cache hit
  pro všechny nezměněné symboly → `fw-context index --analyze` trvá minuty
  místo hodin
- Cache je content-addressable → přežije re-index, změnu configu, přechod
  na jiný projekt se stejným SDK kódem

## Co se musí změnit

### 1. Cache server (`fw-context-cache-server`)

Nový subcommand, samostatný proces na serveru:

```python
# Nový soubor: src/fw_context_mcp/cache_server.py
# Spouští se jako: fw-context-cache-server --port 8080

from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.get("/cache/{content_hash}")
def get_cache(content_hash: str):
    row = db.execute("SELECT * FROM llm_analysis_cache WHERE content_hash = ?",
                     (content_hash,)).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)

@app.put("/cache/{content_hash}")
def put_cache(content_hash: str, entry: CacheEntry):
    db.execute("INSERT OR REPLACE INTO llm_analysis_cache VALUES (?,?,?,?,?,?)",
               (content_hash, entry.summary, entry.inputs,
                entry.outputs, entry.model, datetime.now()))
    return {"status": "ok"}
```

**Auth:** API key v headeru, ověřený nginx (`auth_request`).

**Storage backend:** Pluggable — rozhraní `CacheStorageBackend` se dvěma
metodami (`get_hash`, `put_hash`). Při startu se vybere podle configu:

```toml
[cache_server]
backend = "sqlite"           # nebo "postgresql"
# SQLite varianta (výchozí):
db_path = "/srv/fw-context/cache.db"
# PostgreSQL varianta:
# db_url = "postgresql://fw-cache:pass@localhost:5432/cache"
```

SQLite je výchozí — jeden soubor, žádná závislost navíc. PostgreSQL je
alternativa — pokud už na serveru běží, nebo chcete mít cache persistentní
napříč restarty se sdíleným connection poolem. Oba implementují stejné
rozhraní:

```python
class CacheStorageBackend(ABC):
    @abstractmethod
    def get(self, content_hash: str) -> dict | None: ...
    @abstractmethod
    def put(self, content_hash: str, summary: str, inputs: str,
            outputs: str, model: str) -> None: ...
```

Implementace jsou triviální (jeden SELECT/INSERT), rozdíl je jen v
connection managementu. Pro SQLite: `sqlite3.connect()`. Pro PostgreSQL:
`asyncpg` nebo `psycopg3` s connection poolem.

### 2. `llm_analysis_cache` klient

V `runner.py:_build_llm_analysis` přidat remote cache lookup:

```python
# Místo:
cached = lookup_llm_analysis_cache(conn, h)

# Nově:
cached = lookup_llm_analysis_cache(conn, h)
if not cached and cache_client:
    cached = cache_client.get(h)
    if cached:
        # Uložit lokálně pro příští použití
        upsert_llm_analysis_cache(conn, [(h, cached["summary"],
            cached["inputs"], cached["outputs"], cached["model"])])
```

A po úspěšné Ollama analýze navíc PUT na cache server:

```python
# Po upsert_llm_analysis_cache lokálně:
if cache_client:
    cache_client.put(h, r["summary"], r["inputs"], r["outputs"], model)
```

### 3. Konfigurace

```toml
# .fw-context/local.toml
[llm]
enabled = true
model = "qwen2.5-coder:14b"
ollama_url = "http://localhost:11434"

[cache_server]
url = "https://fw-cache.example.com"
api_key = "${FW_CACHE_API_KEY}"
# write = true   # GPU dev — zapisuje do cache
# write = false  # CPU dev — jen čte
```

### 4. CLI

```bash
# Spuštění cache serveru (na serveru 24/7)
fw-context-cache-server --db /srv/fw-context/cache.db --port 8080

# Dev s GPU (zapisuje do cache)
fw-context index --analyze  # použije [cache_server] url z configu

# Dev bez GPU (čte z cache)
fw-context index --analyze  # cache hity pokryjí 95% symbolů
```

## Co se nemění

- **SQLite zůstává** — každý dev má svůj lokální `index.db`
- **Parsování, reference, embeddingy** — vše lokální, rychlé (minuty)
- **FTS5, vec0** — lokální, žádná změna
- **MCP server** — lokální, stdio, žádná změna
- **Celá architektura** — jen se přidá optional cache vrstva
- **Offline režim** — bez cache serveru to funguje jako dřív (pomalejší, ale funkční)

## Výhody oproti PostgreSQL

| Vlastnost | PostgreSQL backend | Cache server |
|-----------|-------------------|--------------|
| Změna kódu | ~2700 ř. (celý `db.py`) | ~100 ř. (cache lookup/put) |
| Deployment | PostgreSQL server + migrace | Jeden Python proces + SQLite |
| Fallback | Žádný (requires PG) | Offline režim bez cache |
| Lokální výkon | Síťový round-trip na vše | Lokální SQLite |
| Složitost | Vysoká | Nízká |
| Riziko | Breaking change | Zero-risk (optional) |

## Nevýhody

- Nový běžící proces na serveru (ale triviální — FastAPI + SQLite)
- Cache server je single point of failure pro nové analýzy (ale ne pro
  existující — ty jsou uložené lokálně)
- Nutná správa API klíčů (ale to řeší nginx)

## Plán implementace (odhad: 2 dny)

1. **Cache server** (`cache_server.py`) — FastAPI, 2 endpointy, SQLite
2. **Klient** v `runner.py` — `CacheClient` třída s `get()` a `put()`
3. **Konfigurace** — `[cache_server]` section v `Config`
4. **Systemd unit** — `fw-context-cache-server.service`
5. **Testy** — unit testy na cache client, integrační test na server

## Otevřené otázky pro zítřejší review

1. **Je to dostatečné?** Cache server řeší jen LLM analýzu — ne sdílení
   embeddingů nebo referencí. Embeddingy se počítají rychle (batch po 100,
   jeden Ollama call). Reference se počítají lokálně při indexování.

2. **Cache eviction?** `llm_analysis_cache` roste s každým unikátním
   symbolem. Po roce to může být pár set MB. Limitovat LRU? Nebo neřešit
   (disk je levný)?

3. **Cache warming?** GPU dev by měl po každé změně kódu spustit
   `fw-context index --analyze` aby naplnil cache pro ostatní. Automatizovat
   přes CI?

4. **Race condition?** Dva GPU devové zapíšou stejný hash současně.
   `INSERT OR REPLACE` v SQLite je atomický (stejný hash = stejná data,
   content-addressable) — v pohodě.

---

_Review: zítra_
