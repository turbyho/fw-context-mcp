---
name: comprehensive-quality-tests
description: Sada 96 generických testů kvality nad všemi oindexovanými projekty v ~/.fw-context/index/
metadata:
  type: project
---

# Comprehensive quality test suite

Umístění: `tests/test_comprehensive_quality.py`

Automaticky objevuje všechny oindexované projekty v `~/.fw-context/index/` a nad každým spustí 20-30 generických dotazů.

## Co testuje

- **search_code**: topic queries s `exclude_variables=True`, kind-filtrované, FTS5 wildcardy, prázdný dotaz
- **lookup_symbol/find_refs**: podle jména i qualified_name, neexistující symbol
- **Call graph** (jen pokud projekt má refs): `find_all_callers_recursive`, `find_callees_recursive` v hloubkách 1-2, `find_call_path`
- **find_hotspots**: bez enum_constant/field pollution, sestupně seřazené
- **find_dead_code**: jen volatelný druhy
- **get_file_map**: plnou cestou, suffix matchem, neexistující soubor
- **Edge cases**: prázdný dotaz, FTS5 wildcardy

## Baseline (2026-06-17)

| Projekt | Symboly | Reference |
|---------|---------|-----------|
| zbox-ecb-fw | 52409 | 1241641 |
| HA_Boiler | 14976 | 101307 |
| birdie1-v2-fw-v3 | 30861 | 0 (bez --refs) |
| fw-test | 34 | 0 |

Všechny projekty: 96/96 testů prošlo.

## Spuštění

```
cd ~/dev/sw/work/tools/fw-context-mcp
python3 tests/test_comprehensive_quality.py
```

**Why:** Regresní testovací sada — spustit po každé změně v db.py nebo server.py pro ověření že nedošlo k degradaci.
**How to apply:** Spustit před commit/push. Porovnat výstup s baseline. Přidávat assertions když přibudou nové featury.
