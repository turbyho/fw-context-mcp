# Shared Index — Centrální PostgreSQL backend

**Stav:** Návrh — čeká na mentální review

## Problém

### Primární — opakovaná LLM analýza velkých symbolů (VYŘEŠENO)

Velké structy (100+ fields) generují prompt který se nevejde do kontextu
modelu. LLM vrací `unparseable response`, ale protože se neuloží žádný
záznam do `llm_analysis`, symbol se při každém `fw-context index --analyze`
znovu a znovu posílá do Ollama → nekonečná smyčka.

**Vyřešeno v `runner.py` (`_build_llm_analysis`):**
- Před Ollama callem se odhadnou tokeny promptu a porovnají s velikostí
  kontextu modelu (získanou z Ollama `/api/tags` → `details.context_length`,
  fallback na `num_ctx` z configu)
- Pokud prompt + odpověď překročí kontext → uloží se sentinel row
  `model="skip:toolarge:{ctx_size}"` a symbol se přeskočí
- Při přechodu na model s větším kontextem se staré sentinely automaticky
  smažou → symbol se znovu pokusí analyzovat

### Sekundární — sdílení LLM analýzy mezi vývojáři (TENTO PLÁN)

**Situace:**
- Projekt zbox: `fw-context index --analyze` trvá 8–12 h (jen CPU)
- Někteří devové nemají GPU → analýzu nespouští vůbec, nebo jim trvá desítky hodin
- Pracovní stanice (včetně GPU stroje) se vypínají — index nemůže běžet lokálně 24/7
- Server běží 24/7, už na něm běží nginx s HTTPS
- Mezi devy není VPN → NFS nepřipadá v úvahu
- Potřebujeme síťové rozhraní přístupné přes internet

## Zvažované alternativy

### 1. NFS / sdílený disk (ZAMÍTNUTO)
- Nejsme na stejné síti, nemáme VPN
- SQLite WAL přes NFS není doporučený pro zápis

### 2. MCP server proxy (ZAMÍTNUTO)
- GPU stanice se vypíná — nemůže hostit MCP server 24/7
- I kdyby běžela, jeden MCP server obsluhující všechny devy by byl bottleneck

### 3. rqlite (ZAMÍTNUTO)
- SQLite-kompatibilní s HTTP API, Raft konsensus
- **Problém:** žádná správa uživatelů na úrovni databáze
- Nelze říct "dev A vidí jen projekt X, dev B jen projekt Y"
- Auth musí řešit nginx → manuální správa uživatelů v htpasswd, žádné per-project
  oprávnění
- Pro více projektů a více vývojářů s různými právy nedostačující

### 4. PostgreSQL (VYBRÁNO)
- Síťový přístup odkudkoliv (TCP)
- Plná správa uživatelů a oprávnění (role, GRANT, row-level security)
- Per-project izolace přes schema nebo row-level security
- Paralelní zápisy (více lidí může indexovat současně — MVCC)
- Connection pooling (pgBouncer)
- Streamovací replikace, point-in-time recovery
- `pgvector` místo `sqlite-vec`, `tsvector` místo FTS5
- Audit log (`pgaudit`), monitoring (`pg_stat_statements`)

## Co získáme

| Vlastnost | Předtím (SQLite) | Potom (PostgreSQL) |
|-----------|-----------------|-------------------|
| Přístup | Lokální soubor | Síťový (TCP, kdekoliv) |
| Uživatelé | Žádní | Role, GRANT, row-level security |
| Projekty | Jeden na DB | Více schemat / RLS per-project |
| Souběžný zápis | `write_lock` (fcntl) | MVCC — kdokoliv může indexovat |
| Full-text | FTS5 (`modem*`) | tsvector (`modem:*`) |
| Vektory | sqlite-vec | pgvector |
| Replikace | manuální rsync | Streaming replication |
| Monitoring | Žádný | pg_stat_statements, pgaudit |
| Cache persistence | `llm_analysis_cache` (SQLite) | Stejná tabulka v PostgreSQL |
| Závislosti | Žádné (SQLite embedded) | PostgreSQL server, psycopg3/asyncpg |

## Co ztratíme

- **Jednoduchost jednoho souboru** — `index.db` je jeden file, PostgreSQL je
  běžící služba s konfigurací, autentizací, connection poolem
- **Zero-dependency deployment** — předtím stačilo `pip install fw-context-mcp`
  a `fw-context index`, teď potřebuješ PostgreSQL server někde na síti
- **Rychlost lokálních dotazů** — i "lokální" dotazy jdou přes TCP (ale na
  LAN <1ms, na WAN <10-50ms — pro MCP tools akceptovatelné)
- **FTS5 prefix syntax** — `modem*` se mění na `modem:*` (tsquery), drobná
  změna v parseru query
- **Content-sync triggery** — FTS5 se aktualizuje automaticky při INSERT/DELETE
  přes triggery. tsvector vyžaduje explicitní `REFRESH` nebo generovaný sloupec
- **write_lock / fcntl.flock** — celý locking mechanizmus padá, nahradí se
  transakční izolací (MVCC)
- **BLOB embedding formát** — `sqlite-vec` ukládá vektory jako BLOB s custom
  binárním formátem, `pgvector` používá `vector(1024)` typ — nutná konverze
  při migraci existujících indexů

## Rozsah změn

### Nutné přepsání

| Soubor | Rozsah | Popis |
|--------|--------|-------|
| `indexer/db.py` | ~2700 ř. → celé přepsání | Schéma, indexy, triggery, migrace, všechny query funkce |
| `indexer/ops.py` | ~200 ř. | INSERT/UPDATE logika — `INSERT ... ON CONFLICT` → `INSERT ... ON CONFLICT` (PG to má taky, jen jiná syntax) |
| `config/settings.py` | ~30 ř. | Nový `[database]` section: `url`, `pool_size`, read-only flag |
| `llm/ollama.py` | ~5 ř. | Beze změny (jen Ollama klient) |
| `indexer/runner.py` | ~30 ř. | `write_lock` → transakce, connection management |

### Beze změny

| Soubor | Důvod |
|--------|-------|
| `indexer/symbols.py` | Libclang parsování — nezávislé na storage |
| `indexer/prompts.py` | LLM prompty a response parsing — beze změny |
| `mcp/handlers/source.py` | MCP handlery — pokud zůstane stejné DB API |
| `mcp/handlers/callgraph.py` | dtto |
| `mcp/handlers/maintenance.py` | dtto (kromě `reset_index` — to se změní na `TRUNCATE`) |
| `search/pipeline.py` | Search pipeline — beze změny |
| `search/phases/*.py` | Search fáze — beze změny |

### Nové soubory

| Soubor | Popis |
|--------|-------|
| `indexer/db_pg.py` | PostgreSQL implementace storage vrstvy |
| `migrations/` | Alembic migrace (náhrada za `_MIGRATION_ADD_COLUMNS`) |
| `config/pgconfig.py` | PostgreSQL connection config, pool management |

## Architektura po migraci

```
┌─────────────────────────────────────────────────────────┐
│ Server (24/7)                                           │
│                                                         │
│ PostgreSQL 16+                                          │
│   ├─ pgvector extension                                │
│   ├─ pg_trgm / tsvector (full-text)                    │
│   ├─ pgaudit (audit log)                               │
│   ├─ role-based auth:                                   │
│   │   gpu_writer — SELECT, INSERT, UPDATE, DELETE      │
│   │   dev_reader — SELECT only                         │
│   └─ pgBouncer (connection pooling)                    │
│                                                         │
│ nginx:443 → pgBouncer:6432 (už existuje)               │
└─────────────────────────────────────────────────────────┘
        ▲                             ▲
        │ TCP (LAN/internet)          │ TCP
   ┌────┴──────────┐            ┌────┴──────────┐
   │ Dev A (GPU)   │            │ Dev B (CPU)    │
   │               │            │               │
   │ fw-context    │            │ fw-context     │
   │ index         │            │ (read-only)    │
   │ --analyze     │            │               │
   │               │            │ jen query      │
   │ zapisuje přes │            │ čte přes       │
   │ PG connection │            │ PG connection  │
   └───────────────┘            └───────────────┘
```

## Schéma databáze (návrh)

PostgreSQL schéma kopíruje SQLite schéma s minimálními změnami:

```sql
-- Projekty (stejné)
CREATE TABLE projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root_path    TEXT NOT NULL
);

-- Build konfigurace (stejné)
CREATE TABLE build_configs (
    config_hash TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(project_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    compile_commands_path TEXT NOT NULL
);

-- Soubory (stejné)
CREATE TABLE files (
    id          BIGSERIAL PRIMARY KEY,
    config_hash TEXT NOT NULL REFERENCES build_configs(config_hash),
    path        TEXT NOT NULL,
    language    TEXT NOT NULL,
    generated   BOOLEAN NOT NULL DEFAULT false,
    mtime       DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(config_hash, path)
);

-- Symboly (téměř stejné, přidán tsvector)
CREATE TABLE symbols (
    id              BIGSERIAL PRIMARY KEY,
    config_hash     TEXT NOT NULL,
    file_id         BIGINT NOT NULL REFERENCES files(id),
    file_path       TEXT NOT NULL DEFAULT '',
    name_tokens     TEXT NOT NULL DEFAULT '',
    usr             TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    line            INTEGER NOT NULL,
    col             INTEGER NOT NULL,
    end_line        INTEGER NOT NULL DEFAULT 0,
    is_definition   BOOLEAN NOT NULL DEFAULT false,
    is_virtual      BOOLEAN NOT NULL DEFAULT false,
    is_pure_virtual BOOLEAN NOT NULL DEFAULT false,
    is_template     BOOLEAN NOT NULL DEFAULT false,
    is_project      BOOLEAN NOT NULL DEFAULT false,
    signature       TEXT NOT NULL DEFAULT '',
    docstring       TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    inputs          TEXT NOT NULL DEFAULT '',
    outputs         TEXT NOT NULL DEFAULT '',
    parent_usr      TEXT NOT NULL DEFAULT '',
    template_usr    TEXT NOT NULL DEFAULT '',
    pagerank        REAL NOT NULL DEFAULT 0.0,
    enum_value      INTEGER,
    -- Full-text search (náhrada FTS5)
    fts_vector      TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(name, '') || ' ' ||
                    coalesce(qualified_name, '') || ' ' ||
                    coalesce(signature, '') || ' ' ||
                    coalesce(docstring, '') || ' ' ||
                    coalesce(file_path, '') || ' ' ||
                    coalesce(name_tokens, '') || ' ' ||
                    coalesce(summary, '') || ' ' ||
                    coalesce(inputs, '') || ' ' ||
                    coalesce(outputs, ''))
    ) STORED,
    UNIQUE(config_hash, usr)
);
CREATE INDEX idx_symbols_fts ON symbols USING GIN(fts_vector);

-- Reference / call graph (stejné)
CREATE TABLE refs (
    id          BIGSERIAL PRIMARY KEY,
    config_hash TEXT NOT NULL,
    to_usr      TEXT NOT NULL,
    from_file   TEXT NOT NULL,
    from_line   INTEGER NOT NULL,
    from_usr    TEXT,
    ref_kind    TEXT NOT NULL
);

-- Embeddings (pgvector místo BLOB)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE embeddings (
    symbol_id   BIGINT PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    embedding   vector(1024) NOT NULL,
    model       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LLM analýza (stejné, akorát TIMESTAMP → TIMESTAMPTZ)
CREATE TABLE llm_analysis (
    symbol_id   BIGINT PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    summary     TEXT NOT NULL DEFAULT '',
    inputs      TEXT NOT NULL DEFAULT '',
    outputs     TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL,
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content-addressable cache (stejné)
CREATE TABLE llm_analysis_cache (
    content_hash TEXT PRIMARY KEY,
    summary      TEXT NOT NULL DEFAULT '',
    inputs       TEXT NOT NULL DEFAULT '',
    outputs      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL,
    analyzed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ostatní tabulky (inheritance, overrides, hotspot_cache, indirect_call_sites,
-- fp_assignments) — identické schéma, jen SERIAL → BIGSERIAL, BOOLEAN
```

## Návrh oprávnění

```sql
-- Writer role (GPU dev, CI)
CREATE ROLE gpu_writer WITH LOGIN PASSWORD '...';
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gpu_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO gpu_writer;

-- Reader role (všichni devové)
CREATE ROLE dev_reader WITH LOGIN PASSWORD '...';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dev_reader;

-- Row-level security pro multi-tenant (volitelné)
ALTER TABLE symbols ENABLE ROW LEVEL SECURITY;
CREATE POLICY project_isolation ON symbols
    USING (config_hash IN (
        SELECT config_hash FROM build_configs
        WHERE project_id = current_setting('app.project_id', true)
    ));
```

## Otevřené otázky pro zítřejší review

1. **Je PostgreSQL správná volba?** Získáme hodně, ale ztrácíme tu "one file"
   jednoduchost — stačí `pip install` a jedeš. S PostgreSQL je deployment
   složitější (musí běžet server, musí být dostupný po síti).

2. **Nestačilo by lokální SQLite + `llm_analysis_cache` sdílená přes síť?**
   Cache je content-addressable — kdyby se sdílela (např. jako samostatný
   rqlite/shared SQLite), devové bez GPU by měli instantní analýzy pro
   nezměněné symboly. Nové/změněné by stále museli analyzovat (nebo počkat
   až to udělá GPU dev).

3. **Co `compile_commands.json` cesty?** PostgreSQL je síťový, ale zdrojáky
   zůstávají lokální. Symboly v DB obsahují `file_path` — čtení body
   (`get_source`) vyžaduje aby cesty seděly. Řešení: relativní cesty vůči
   project root, nebo Docker s fixními mount pointy (`/workspace`).

4. **Connection pooling** — psycopg3 s connection poolem, nebo pgBouncer?
   Pro MCP server (jeden klient, stdio) stačí jedno connection. Pro paralelní
   indexování je potřeba pool.

5. **Fázování migrace** — udělat PostgreSQL jako optional backend (vedle
   SQLite), nebo rovnou migrovat a SQLite zahodit? Druhá varianta je
   jednodušší na kód (jeden storage backend), ale znamená breaking change.

6. **Odhad práce** — ~2 týdny na storage vrstvu, ~1 týden testování, ~1 týden
   deployment/docs. Celkem ~měsíc.

---

_Review: zítra_
