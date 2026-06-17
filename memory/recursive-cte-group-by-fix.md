---
name: recursive-cte-group-by-fix
description: Kritický bugfix — MIN(depth) bez GROUP BY v rekurzivních CTE dotazech vracel vždy jen 1 výsledek
metadata:
  type: project
---

# GROUP BY bug v rekurzivních call-graph funkcích

## Problem

`find_all_callers_recursive` a `find_callees_recursive` v `db.py` používaly `MIN(c.depth)` v SELECTu bez `GROUP BY c.usr`. SQLite bez GROUP BY zkolabuje celý výsledek do jediného řádku (s minimální hloubkou).

**Důsledek:** Obě funkce vracely vždy jen 1 výsledek, bez ohledu na `limit`. Např. `zdebug` s 4126 callery → vrácen jen 1 caller.

## Oprava (2026-06-17)

Přidána mezilehlá `dedup` CTE s `GROUP BY usr`:
```sql
dedup AS (
    SELECT usr, MIN(depth) AS depth
    FROM callers   -- nebo callees
    GROUP BY usr
)
```

SELECT pak čte z `dedup d` místo z `callers c`, používá `d.depth` místo `MIN(c.depth)`.

## Současně opravené problémy

1. **Rekurzivní funkce zahrnovaly enum_constant a fieldy** — přidán `AND ref_kind IN ('call', 'indirect')` do `extended_refs` CTE
2. **`find_hotspots` ukazoval nevolatelné symboly** — přidán filtr `r.ref_kind IN ('call', 'indirect')`
3. **`search_code` vracel lokální proměnné** — přidán parametr `exclude_variables`, `search_code` ho zapíná
4. **`get_symbol_context` limitoval na 5** — odstraněn LIMIT, vrací všechny direct callers/callees
5. **`find_call_path` používal všechny ref_kinds** — přidán filtr na call/indirect
6. **`find_dead_code` dokumentace** — doplněn docstring o false positives

## Soubory změněné

- `src/fw_context_mcp/indexer/db.py` — GROUP BY fix, ref_kind filtry, `exclude_variables` parametr
- `src/fw_context_mcp/mcp/server.py` — `get_symbol_context` bez limitu, `search_code` s `exclude_variables`, `find_dead_code` docstring

**Why:** Nejzávažnější bug v celém nástroji — rekurzivní call graph vracel vždy 1 výsledek místo stovek.
**How to apply:** Při jakékoliv změně SQL dotazů v db.py ověřit že GROUP BY je přítomný u všech agregací. Spustit `test_comprehensive_quality.py` pro regresi.
