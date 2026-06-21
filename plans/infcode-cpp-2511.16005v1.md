# InfCode-C++: Intent-Guided Semantic Retrieval and AST-Structured Search for C++ Issue Resolution

arXiv: 2511.16005v1 — 2025
Source: https://arxiv.org/html/2511.16005v1

## Relevance to fw-context-mcp

This paper demonstrates the value of **semantic code understanding** for C++ codebases.
Key findings relevant to our LLM symbol analysis feature:

1. **Dual-index architecture**: AST structural index + semantic embedding index.
   fw-context already has both (symbols table + embeddings). LLM-generated
   descriptions would add a third layer — explicit natural language descriptions
   searchable via FTS5.

2. **Semantic retrieval matters**: Ablation shows removing semantic code-intent
   retrieval drops resolution from 25.58% → 19.37% (‑6.21 pp). LLM descriptions
   would enrich our semantic search quality.

3. **AST-structured queries are essential**: Removing AST querying drops to
   17.05% (‑8.53 pp). Our existing `lookup_symbol`, `find_callers`,
   `get_symbol_context` tools are the AST-structured layer.

4. **C++ ambiguity is the core problem**: Identifier overloading, namespace
   shadowing, template instantiation — lexical search fails. LLM-generated
   descriptions help disambiguate by describing *what* a symbol does, not just
   its name.

## Key metrics

- 25.58% resolution rate on MultiSWE-bench-CPP (GPT-5)
- 129 C++ issues from 5 repositories (Catch2, fmt, nlohmann/json, simdjson, cpp-httplib)
- File-level localization: 55.10%, Function-level: 42.10%
- AST querying removes 8.53 pp, semantic retrieval removes 6.21 pp
- Without both retrieval mechanisms: turn count inflates from 28.1 → 45.3

## Architecture

Three agents: Reproducer → Patch (50-turn budget, 10 candidates) → Selector

Two pre-built indices:
- **AST-Based Structural Index**: T_C = (N, E) — nodes for classes/methods/templates,
  edges for inheritance/call/containment
- **Semantic Code-Intent Index**: I_intent: Intent → {A_1, ..., A_k} — maps
  feature descriptions to files/classes/functions

Tools: QueryCodeIntent, FindClass, FindFunction, GetInheritanceChain, GetFunctionCalls

---

## Mapping to fw-context-mcp: gap analysis

### What we already have (✅)

| InfCode-C++ tool / capability | fw-context equivalent | Status |
|-------------------------------|----------------------|--------|
| **AST Structural Index** (N, E) | `symbols` + `refs` tables, edges via call references | ✅ Silné — libclang-based parsing, FTS5 search |
| **Semantic Intent Index** | `embeddings` table + `llm_analysis` (summary/inputs/outputs) | ✅ Silné — embeddingy generované během indexu, LLM popisy |
| **QueryCodeIntent** | `smart_search`, `semantic_search`, `search_code` | ✅ — 3 úrovně: FTS5, embedding, LLM-řízené |
| **FindFunction** | `lookup_symbol(name, kind="function")` | ✅ |
| **FindClass** | `lookup_symbol(name, kind="class")` | ✅ |
| **GetFunctionCalls** | `find_callers`, `find_callees_recursive`, `find_call_path` | ✅ Silné — přímé i tranzitivní, BFS |
| **Symbol-level intent** | `llm_analysis` — structured descriptions per symbol | ✅ Od 2026-06 — summary/inputs/outputs |

### What's missing (❌ → příležitosti)

#### 1. ❌ `GetInheritanceChain` — chybí indexace dědičnosti tříd

**Problém:** libclang při parsování vidí `CXCursor_CXXBaseSpecifier` — ukazatel na
bázovou třídu. Tuhle informaci momentálně **zahazujeme**. Neumíme odpovědět na
otázky typu:
- "Jakou hierarchii má třída `UART_DRIVER`?"
- "Které třídy dědí z `SerialBase`?"
- "Která metoda je virtuální a kde je přetížená?"

**libclang API — co je k dispozici:**
- `CXCursor_CXXBaseSpecifier` — cursor child třídy, referencuje bázovou třídu
- `cursor.is_virtual_method` — True pro `virtual` metody
- `cursor.is_pure_virtual_method` — True pro `= 0`
- `cursor.is_abstract_record` — True pro abstraktní třídy
- `cursor.get_children()` — projde base specifiery, metody, fieldy

**Návrh implementace:**

```sql
-- Nová tabulka pro dědičnost
CREATE TABLE inheritance (
    derived_usr  TEXT NOT NULL,   -- USR odvozené třídy
    base_usr     TEXT NOT NULL,   -- USR bázové třídy
    access       TEXT NOT NULL,   -- 'public' | 'protected' | 'private'
    is_virtual   INTEGER DEFAULT 0,
    UNIQUE(derived_usr, base_usr)
);
CREATE INDEX idx_inheritance_derived ON inheritance(derived_usr);
CREATE INDEX idx_inheritance_base    ON inheritance(base_usr);

-- Rozšíření symbols o virtual flag
ALTER TABLE symbols ADD COLUMN is_virtual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE symbols ADD COLUMN is_pure_virtual INTEGER NOT NULL DEFAULT 0;
```

**Nový MCP tool `get_inheritance_chain`:**
- Vstup: `class_name` (název třídy)
- Výstup: přímí rodiče (base classes) + přímí potomci (derived classes)
- Volitelně: tranzitivní BFS nahoru/dolů (podobně jako `find_all_callers_recursive`)

**Přínos:** Vysoký. InfCode-C++ paper potvrzuje že AST-strukturované dotazy
jsou nejcennější komponenta (-8.53 pp bez nich). Dědičnost je základní C++
struktura a momentálně jsme k ní slepí.

**Náročnost:** Střední. ~150-200 řádků v `symbols.py` (extrakce), ~80 v `db.py`
(schéma + dotazy), ~100 v `server.py` (tool handler).

#### 2. ❌ Class member grouping — chybí vazba metoda → třída

**Problém:** Metody a fieldy indexujeme jako samostatné symboly, ale
nezaznamenáváme jejich parent class. `get_file_map` je seskupuje podle souboru,
ale neumíme říct "všechny metody třídy `ModemManager`".

**Řešení:** Přidat `parent_usr` sloupec do `symbols` — pro metody, fieldy,
konstruktory atd. odkazuje na USR třídy/struktury.

```sql
ALTER TABLE symbols ADD COLUMN parent_usr TEXT;
CREATE INDEX idx_symbols_parent ON symbols(parent_usr);
```

**Nový / rozšířený tool:** `get_class_methods(class_name)` — vrátí všechny
metody a fieldy dané třídy.

**Přínos:** Střední. Užitečné pro pochopení struktury třídy, zvlášť pro
velké "god class" které jsou v embedded kódu běžné.

**Náročnost:** Nízká. ~50 řádků v `symbols.py` (parent tracking při AST walku),
migrace, jednoduchý dotaz.

#### 3. ❌ Template instantiation tracking — nevíme které šablony jsou instanciované

**Problém:** `CXCursor_CLASS_TEMPLATE` a `CXCursor_FUNCTION_TEMPLATE` jsou
deklarace šablon. libclang ale vidí i konkrétní instanciace (přes spelling
s konkretními typy). Momentálně je neodlišujeme.

**libclang API:**
- `cursor.kind == CXCursor_CLASS_TEMPLATE` — šablona
- `cursor.kind == CXCursor_CLASS_TEMPLATE_PARTIAL_SPECIALIZATION` — parciální specializace
- `cursor.specialized_template` — referencuje původní template (pokud je cursor instanciace)
- `TEMPLATE_REF` — reference na template v kódu

**Návrh:** Přidat `template_usr` sloupec do `symbols` — pro instanciace
odkazuje na USR původní šablony. Umožní to:
1. Najít všechny instance dané šablony
2. Rozlišit template vs. konkrétní typ v `lookup_symbol`

**Přínos:** Střední. Důležité pro C++ projekty s těžkým využitím šablon
(např. knihovny typu fmt, simdjson — přímo z paperu).

**Náročnost:** Střední. ~100 řádků. Hlavní složitost je správně rozlišit
deklaraci šablony od její instance napříč různými verzemi libclangu.

#### 4. ❌ File-level intent — chybí popis účelu souborů

**Problém:** Máme per-symbol `llm_analysis`, ale nemáme per-file popisy.
InfCode-C++ mapuje intent na **soubory** i na symboly.

**Návrh:** Vygenerovat během `_build_llm_analysis()` i krátké shrnutí pro
každý soubor na základě symbolů které obsahuje:

```sql
CREATE TABLE file_analysis (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    summary      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL,
    analyzed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Prompt:** "Here are symbols in file X. Write a 2-3 sentence summary of
what this file is responsible for." — batch po 5 souborů.

**Přínos:** Nízký-Střední. Zlepšilo by orientaci v neznámém kódu, ale
symbol-level descriptions + `get_file_map` už teď dávají dobrou orientaci.

**Náročnost:** Nízká. ~100 řádků, hlavně prompt + DB + jeden nový tool.

#### 5. ❌ Method override tracking

**Problém:** U virtuálních metod nevíme která metoda přetěžuje kterou
bázovou. To je klíčové pro pochopení polymorfního chování.

**libclang API:** `cursor.is_virtual_method`, `cursor.is_pure_virtual_method`,
ale přímá detekce "tahle metoda overrideuje tamtu" vyžaduje porovnání signatur
napříč hierarchií dědičnosti.

**Návrh:** Po zaindexování dědičnosti (bod 1) — pro každou virtuální metodu
najít metodu se stejnou signaturou v bázové třídě a uložit vazbu.

**Přínos:** Střední (navazuje na bod 1).

**Náročnost:** Vyšší. Vyžaduje mít hotovou dědičnost a implementovat
signature-matching. ~200 řádků.

### 🤖 Agent architecture — jiná úroveň než nástroje

Paper popisuje tří-agentní architekturu (Reproducer → Patch → Selector)
která **používá** tyhle nástroje k řešení issue. To je vrstva nad MCP
tools — patří do AI asistenta (Claude Code, Cursor), ne do fw-context-mcp.

fw-context-mcp poskytuje **nástroje**, paper popisuje **agenta který je
používá**. Naše nástroje by měly být natolik dobré aby umožnily podobnou
agentní architekturu.

### 📊 Shrnutí priorit

| # | Vylepšení | Přínos | Náročnost | Priorita |
|---|-----------|--------|-----------|----------|
| 1 | **Inheritance chain** | 🔴 Vysoký | Střední (~330 ř.) | **P0** ✅ hotovo (f779b25) |
| 2 | **Class member grouping** | 🟡 Střední | Nízká (~150 ř.) | **P1** ✅ hotovo (ac339e7) |
| 3 | **Template tracking** | 🟡 Střední | Střední (~100 ř.) | **P2** ✅ hotovo (2026-06-21) |
| 4 | **File-level intent** | 🟢 Nízký-Střední | Nízká (~100 ř.) | **P3** ✅ hotovo (2026-06-21) |
| 5 | **Method override** | 🟡 Střední | Vyšší (~200 ř.) | **P3** ✅ hotovo (2026-06-21) |

### 🔬 Paperem potvrzené směry které už máme

Paper validuje naši současnou strategii:

1. **Dual-index je správně** — AST strukturální + sémantický embedding.
   Naše `symbols`+`refs` + `embeddings`+`llm_analysis` kopíruje tenhle vzor.

2. **AST nástroje jsou nejcennější** — paper ukazuje -8.53 pp bez nich.
   Našich 8 call-graph nástrojů (`find_callers`, `find_call_path`,
   `find_all_callers_recursive`, `find_callees_recursive`, `find_hotspots`,
   `find_dead_code`, `find_wrapper_callers`, `trace_data_flow`) je naše
   nejsilnější stránka.

3. **Sémantické vyhledávání je druhý pilíř** — paper ukazuje -6.21 pp.
   Naše `llm_analysis` + `semantic_search` + `smart_search` jdou tímhle
   směrem. LLM-generované popisy symbolů (summary/inputs/outputs) přímo
   obohacují embeddingy.

4. **Popisy účelu, ne jen jmen** — paper zdůrazňuje že C++ ambiguita
   (přetěžování, namespace shadowing, šablony) dělá lexikální search
   nespolehlivým. Naše `llm_analysis` generuje popisy *co symbol dělá*,
   ne jen parsuje jeho jméno. Přesně to paper doporučuje.

### 🎯 Doporučený postup

1. ✅ ~~**Teď:** Implementovat inheritance chain (#1)~~ — hotovo (f779b25)
2. ✅ ~~**Brzy:** Class member grouping (#2)~~ — hotovo (ac339e7)
3. ✅ ~~**Později:** Template tracking (#3)~~ — hotovo (2026-06-21)
4. ✅ ~~**Možná:** File-level intent (#4)~~ — hotovo (2026-06-21)
5. ✅ ~~**Později:** Method override tracking (#5)~~ — hotovo (2026-06-21)

### ⚠️ Co z paperu nedává smysl přebírat

- **Reproducer/Patch/Selector agenti** — to je architektura konzumenta
  nástrojů, ne poskytovatele indexu. fw-context je index a sada nástrojů.
- **50-turn budget s 10 kandidáty** — paper řeší issue-resolution pipeline
  s rozpočtem, ne indexaci kódu.
- **File-level localization jako separátní krok** — my poskytujeme nástroje
  pro obě úrovně (file-level přes `get_file_map`, function-level přes
  `get_source`).
