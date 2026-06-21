# P2: Template instantiation tracking

## Cíl

Sledovat vazbu mezi C++ šablonami a jejich instancemi. Umožnit:
1. Najít všechny instance dané šablony
2. Rozlišit template deklaraci od konkrétní instance v `lookup_symbol`

## Analýza

### libclang API
- `cursor.kind == CXCursor_CLASS_TEMPLATE` — deklarace class template
- `cursor.kind == CXCursor_FUNCTION_TEMPLATE` — deklarace function template  
- `cursor.specialized_template` — pro instanciaci vrací cursor původní šablony
  (funguje pro CLASS_DECL/STRUCT_DECL instancované z CLASS_TEMPLATE,
   FUNCTION_DECL instancované z FUNCTION_TEMPLATE,
   CXX_METHOD v rámci template class, CONSTRUCTOR/DESTRUCTOR)
- `cursor.kind == CXCursor_CLASS_TEMPLATE_PARTIAL_SPECIALIZATION` — parciální specializace

### Současný stav
- `_cursor_kind_label` mapuje CLASS_TEMPLATE → "class", FUNCTION_TEMPLATE → "function"
  (nerozlišuje template od konkrétního typu)
- `Symbol` nemá `template_usr` ani `is_template`
- `specialized_template` se nikde nevolá

## Změny

### 1. `indexer/symbols.py`
- `Symbol`: přidat `template_usr: str = ""` — pro instance odkazuje na USR šablony
- `Symbol`: přehodit `is_template: bool = False` — True pro CLASS_TEMPLATE, FUNCTION_TEMPLATE
- `extract_all()`: po extrakci parent_usr volat `cursor.specialized_template`
- `_SYMBOL_KINDS`: přidat `CLASS_TEMPLATE_PARTIAL_SPECIALIZATION`

### 2. `indexer/db.py`
- Migrace: `ALTER TABLE symbols ADD COLUMN template_usr TEXT NOT NULL DEFAULT ''`
- Schéma: přidat `template_usr` do CREATE TABLE symbols
- `insert_symbols_batch`: přidat `template_usr` do INSERT/UPDATE
- Nová funkce `get_template_instances()` — vrátí všechny symboly s daným `template_usr`
- Index: `CREATE INDEX idx_symbols_template ON symbols(config_hash, template_usr)`

### 3. `indexer/ops.py`
- `store_symbols_for_unit`: přidat `s.template_usr` do row tuples

### 4. `mcp/server.py`
- Nový tool `get_template_instances(template_name)` — najde všechny instance šablony
- `lookup_symbol`: přidat `template_usr` a `is_template` do výstupu
- `get_source`, `get_symbol_context`: přidat `is_template` a `template_usr` do výstupu

### 5. `search_code` / `search_symbols`
- `search_symbols` DB funkce už vrací celý řádek (SELECT *), takže `template_usr` se objeví automaticky
- Není potřeba měnit

## Co se NEDĚLÁ
- Parciální specializace jako samostatná entita (ukládá se jako template s `is_template=True`)
- `template_usr` u parciálních specializací — `specialized_template` vrací primary template
- Metody template tříd — `specialized_template` funguje, ale vazba je na template metody, ne na template třídu
  (složitější případ, nestojí za to v P2)

## Rizika
- `cursor.specialized_template` může vracet cursor z jiné TU (např. `std::vector<int>` → `std::vector`
  z headeru). USR bude validní, ale template možná není v indexu.
- Změna schématu vyžaduje re-index (automaticky detekováno přes `CURRENT_SCHEMA_VERSION`)

## Verifikace
- `python3 -m pytest tests/ -x -q`
- `python3 -m pytest tests/test_db.py -x -q`
- `ruff check src/ && mypy src/`

## Status
Dokončeno — 2026-06-21
