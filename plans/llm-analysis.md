# Dokumentace změn — LLM analýza symbolů a vyhledávání

**Autor:** Claude (turbyho)
**Datum:** 2026-06-21
**Stav:** ✅ Implementováno — čeká na otestování

---

## Přehled

K fw-context-mcp přibyla schopnost automaticky generovat strukturované popisy funkcí, metod,
tříd, struktur a konstruktorů pomocí Ollama LLM. Popisy se ukládají do SQLite, denormalizují
do `symbols` tabulky pro FTS5 full-text search a používají se pro obohacení embeddingů.
Výsledkem je výrazně lepší vyhledávání — najde symboly podle účelu, ne jen podle názvu.

---

## Změněné soubory

| Soubor | Co se změnilo |
|--------|---------------|
| `indexer/db.py` | Nová tabulka `llm_analysis`; migrace přidávající `summary`, `inputs`, `outputs` do `symbols`; FTS5 rebuild s novými sloupci; helper funkce; retry při `database is locked` |
| `indexer/runner.py` | `_build_llm_analysis()` — dvoufázová analýza (batch + individuální retry); `_build_embeddings()` — `summary[:200]` v embedding textu |
| `indexer/prompts.py` | **Nový soubor** — prompt template (ověřený na qwen2.5-coder:14b a qwen3-coder:480b-cloud), parser s flatteningem |
| `config/settings.py` | `LLMConfig.analyze_symbols = True` (výchozí); doporučené modely (lokální i cloud); `summary`/`inputs`/`outputs` pole |
| `cli.py` | `--analyze`/`--no-analyze` flagy v `fw-context index`; `fw-context analyze` standalone příkaz |
| `mcp/server.py` | `summary`/`inputs`/`outputs` ve výstupu `search_code`, `lookup_symbol`, `semantic_search`, `get_symbol_context`, `explain_symbol`; auto-analýza po `reindex_file`; statistiky v `get_active_build` |
| `search/phases/format.py` | `summary`/`inputs`/`outputs` ve výstupu `smart_search` pipeline |
| `llm/ollama.py` | `call_ollama` přijímá `temperature` a `num_predict`; timeout zvýšen na 120s |

---

## Architektura

### 1. Generování analýzy

```
fw-context index --analyze  (nebo automaticky — výchozí)
  ├─ indexace symbolů (paralelní)
  ├─ _build_embeddings()  — obohacený text: name + sig + docstring + summary[:200]
  └─ _build_llm_analysis()
       ├─ Fáze 1: dávky po 10, num_predict=3000
       │   └─ Selhané → zaznamená failed_ids
       └─ Fáze 2: individuální retry s num_predict=4000
```

### 2. Automatická regenerace při změně kódu

```
reindex_file / fw-context index
  └─ store_symbols_for_unit()
       ├─ delete_symbols_for_file()  → CASCADE maže staré llm_analysis
       └─ insert_symbols_batch()     → nové symboly (nová ID)
  └─ _build_llm_analysis()
       └─ WHERE id NOT IN (SELECT symbol_id FROM llm_analysis)
          → najde všechny symboly bez analýzy → přegeneruje
```

Žádné flagy, hashe, ani retry counter — absence řádku v `llm_analysis` **je** ten flag.

### 3. Databázové schéma

```sql
-- Samostatná tabulka pro LLM analýzu
CREATE TABLE llm_analysis (
    symbol_id   INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    summary     TEXT NOT NULL DEFAULT '',   -- co symbol dělá
    inputs      TEXT NOT NULL DEFAULT '',   -- parametry / závislosti
    outputs     TEXT NOT NULL DEFAULT '',   -- návratová hodnota / efekty
    model       TEXT NOT NULL,              -- který model generoval
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Denormalizované sloupce v symbols (pro FTS5)
ALTER TABLE symbols ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE symbols ADD COLUMN inputs  TEXT NOT NULL DEFAULT '';
ALTER TABLE symbols ADD COLUMN outputs TEXT NOT NULL DEFAULT '';

-- FTS5 rozšířen o nové sloupce
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, qualified_name, signature, docstring, file_path, name_tokens,
    summary, inputs, outputs,  -- ← NOVÉ
    content='symbols', content_rowid='id'
);
```

### 4. API výstup — nová pole

Všechny nástroje vracející symboly (`search_code`, `lookup_symbol`, `semantic_search`,
`smart_search`, `get_symbol_context`) nově obsahují:

| Pole | Význam |
|------|--------|
| `summary` | Strukturovaný popis účelu — co symbol dělá, jak funguje, proč existuje |
| `inputs` | Popis parametrů (pro funkce) nebo závislostí (pro třídy) |
| `outputs` | Popis návratové hodnoty, vedlejších efektů, nebo co typ poskytuje |

`explain_symbol` vrací instantní odpověď z předpočítané analýzy (bez volání Ollama).

---

## Konfigurace

```toml
[llm]
model = "qwen2.5-coder:14b"        # výchozí (minimum 14B)
# analyze_symbols = true           # výchozí — automatická analýza

# Alternativní modely:
#   Lokální:  deepseek-coder-v2:16b, qwen2.5-coder:32b
#   Cloud:    qwen3-coder:480b-cloud, deepseek-v4-flash:cloud,
#             gemini-3-flash-preview:latest-cloud
```

---

## Ověřené chování

### Experimenty (zbox-ecb-fw, 2 672 symbolů)

| Test | Výsledek |
|------|----------|
| FTS5 "abstracts complexity protocol" | ✅ Vrací `SENSORS`, `ZBLE`, `FRAM` (abstraktní vrstvy) |
| FTS5 "encapsulates state" | ✅ Vrací `TimeoutModem` a další relevantní struktury |
| FTS5 "RAII cleanup resource" | ✅ Vylepšený ranking — `Component::~Component`, `ZBLE::~ZBLE` |
| Lokální model (14B) | 90 min, ~190 zn/popis, 98 % úspěšnost JSON |
| Cloud model (480B) | ~35 min, ~800 zn/popis, 100 % úspěšnost JSON |
| Retry mechanismus | 10 selhaných → 10 samostatných requestů → 100 % |
| Migrace při paralelním indexu | Retry po 2s na novém spojení |

### Testy

```
python3 -m pytest tests/ -x -q -k "not comprehensive"
175 passed ✅
```

---

## Plán testování na zbox-ecb-fw

Po dokončení `fw-context index`:

1. **Migrace DB** — ověřit že `symbols` má sloupce `summary`, `inputs`, `outputs`
2. **Backfill** — `SELECT COUNT(*) FROM symbols WHERE summary != ''` → 2 672
3. **FTS5** — `search_code("abstracts complexity")` → `SENSORS`, `ZBLE`, `FRAM`
4. **Embeddingy** — `semantic_search("battery management")` → `BattManager`
5. **Explain** — `explain_symbol("MutexSectionLock")` → instantní odpověď
6. **get_symbol_context** — ověřit `llm_analysis` klíč s `summary`/`inputs`/`outputs`
7. **get_active_build** — ověřit `analyzed_symbols` > 0
8. **Reindex souboru** — `reindex_file()` → `analysis_updated` v odpovědi
