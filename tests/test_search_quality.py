"""Evaluace kvality search variants — camelCase, query expansion, prompt.

Testuje cesty A, B, C a jejich kombinace na reálném indexu zbox-ecb-fw.
Kvalita se hodnotí podle:
- recall: kolik relevantních symbolů dotaz najde
- precision: poměr relevantních výsledků mezi prvními 10
- zbox_ratio: poměr výsledků z našeho kódu (src/, lib/) vs mbed-os
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


# ─── camelCase splitting ────────────────────────────────────────────

def split_camel(s: str) -> str:
    """Split camelCase/PascalCase/snake_case into space-separated tokens.

    onConnectionComplete  → "on Connection Complete"
    _connectionSupervisionTimeout → "_connection Supervision Timeout"
    ModemMsgManager       → "Modem Msg Manager"
    HTTPResponse          → "HTTP Response"
    modem_parser_oob_init → "modem parser oob init"
    """
    # vložit mezeru před velké písmeno, kterému předchází malé nebo číslice
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    # akronymy následované dalším velkým + malým: "HTTPResponse" → "HTTP Response"
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    # podtržítka taky mezery
    s = s.replace("_", " ")
    return s


def tokenize_camel(name: str) -> list[str]:
    """Vrátí seznam camelCase-splitnutých tokenů."""
    raw = split_camel(name).lower().split()
    # odfiltrovat jednopísmenné tokeny (písmena po akronymech apod.)
    return [t for t in raw if len(t) > 1]


def make_wildcard_infix(name: str) -> list[str]:
    """Generuje FTS5 prefixové dotazy, které pokryjí tokeny uvnitř camelCase jména.

    Pro jméno 'onConnectionComplete':
    - standardní token: 'onConnectionComplete' (jeden FTS5 token)
    - prefixy podle split: 'connection*', 'complete*'  (začátky split tokenů)
      (ale FTS5 je nenajde, protože token začíná 'onConnection...')

    Vrací alternativní FTS5 dotazy, které se dají použít.
    """
    tokens = tokenize_camel(name)
    # každý split token jako wildcard — ale pozor, FTS5 prefix musí odpovídat
    # ZAČÁTKU FTS5 tokenu. Tohle funguje jen pro tokeny, které začínají
    # na správném místě (např. "connection*" pro "ConnectionHandle").
    return [f"{t}*" for t in tokens]


# ─── query expansion strategie ───────────────────────────────────────

def expand_query_naive(query: str) -> str:
    """Původní dotaz — žádné rozšiřování."""
    return query


def expand_query_split(query: str) -> str:
    """Rozdělí camelCase slova v dotazu.

    'bleConnect' → 'ble Connect*'
    """
    parts = query.split()
    expanded = []
    for p in parts:
        tokens = split_camel(p).split()
        if len(tokens) > 1:
            expanded.append(" OR ".join(f"{t}*" for t in tokens))
        else:
            expanded.append(p)
    return " AND ".join(f"({e})" for e in expanded) if len(expanded) > 1 else expanded[0]


def expand_query_prefix(query: str) -> str:
    """Přidá prefix wildcard ke každému slovu.

    'connect' → 'connect*'
    'ble connect' → 'ble* connect*'
    """
    parts = query.split()
    return " ".join(f"{p}*" for p in parts)


def expand_query_hybrid(query: str) -> str:
    """Kombinuje prefix + split: každé query slovo expanduje na
    (word* OR split_variant1* OR split_variant2*).

    'connect' → '(connect* OR connection*)'
    """
    parts = query.split()
    expanded_parts = []
    for p in parts:
        alternatives = {f"{p}*"}
        tokens = split_camel(p).split()
        for t in tokens:
            if t.lower() != p.lower():
                alternatives.add(f"{t}*")
        # Generování camelCase variant: "connect" → "connection*"
        if p.lower() == "connect":
            alternatives.add("connection*")
        if p.lower() == "init":
            alternatives.add("init*")
            alternatives.add("initialization*")
        if p.lower() == "ble":
            alternatives.add("ble*")
        if p.lower() == "setup":
            alternatives.add("setup*")
            alternatives.add("set*")
        expanded_parts.append("(" + " OR ".join(sorted(alternatives)) + ")")
    return " AND ".join(expanded_parts)


# ─── smart_search prompt varianty ────────────────────────────────────

BASELINE_PROMPT = """\
You are a C/C++ code search assistant for an embedded firmware project.
Generate 3-5 FTS5 keyword search terms based on the description below.
Study the real symbol names to learn the project's naming conventions
(prefixes, word order, abbreviations). Match them.

Real symbols from this project:
{context}

Return a JSON array of snake_case strings, e.g.: ["modem_init", "conn_open"]
Rules:
- Output MUST be a valid JSON array and nothing else
- Use snake_case identifiers matching the project's naming patterns
- STRONGLY prefer short stems with a trailing wildcard over exact
  full names — stems find more symbols ("ble_conn*" > "ble_connection_handler")
- You may use a trailing wildcard: "ble_gap*" matches ble_gap_init, etc.
- No asterisks anywhere else — FTS5 only supports trailing wildcards
- Cover different naming patterns found in the context — e.g. if
  the project has "ble_*", "on_*", and "connection_*" prefixes,
  generate one stem per pattern

Description: {description}"""


CAMELCASE_AWARE_PROMPT = """\
You are a C/C++ code search assistant for an embedded firmware project.
Generate 3-5 FTS5 keyword search terms based on the description below.
Study the real symbol names to learn the project's naming conventions
(prefixes, word order, abbreviations). Match them.

CRITICAL: Embedded code uses camelCase AND snake_case. The FTS5 tokenizer
treats each camelCase word as ONE token — e.g. "onConnectionComplete" is
a single token, NOT split into "on", "Connection", "Complete".
Therefore your queries MUST match token BEGINNINGS:
- For "connect" → generate "connection*" (prefix of "ConnectionComplete", "ConnectionHandle")
- For "init" → generate "init*" OR "Init*" (prefix of "Init", "Initialize")
- For "handle" → generate "handle*" OR "*handle*" won't work! Only prefix: "handle*" works for "handleConnection"
- Generate BOTH snake_case AND camelCase prefix variants

Real symbols from this project:
{context}

Return a JSON array of strings, e.g.: ["ble_conn*", "connection*", "on_connect*"]
Rules:
- Output MUST be a valid JSON array and nothing else
- Mix snake_case and camelCase prefixes: "modem_init*", "Connection*", "on_conn*"
- STRONGLY prefer short stems with a trailing wildcard
- You may use a trailing wildcard: "ble_gap*" matches ble_gap_init, etc.
- No asterisks anywhere else — FTS5 only supports trailing wildcards
- Cover different naming patterns found in the context
- For each concept in the description, generate the MOST LIKELY prefix
  that matches real symbol beginnings (not substrings!)

Description: {description}"""


# ─── testovací sada ───────────────────────────────────────────────────

@dataclass
class TestCase:
    query: str
    description: str
    expected_names: list[str] = field(default_factory=list)  # symboly, které by měly být nalezeny


TEST_CASES = [
    TestCase(
        query="ble connect",
        description="BLE connection establishment and handling",
        expected_names=[
            "onConnectionComplete",
            "onDisconnectionComplete",
            "_last_ble_connected",
            "connectionHandle",
        ],
    ),
    TestCase(
        query="modem init",
        description="inicializace a nastavení modemu",
        expected_names=[
            "modem_parser_oob_init",
            "modem_connect",
            "init",
            "power_on",
        ],
    ),
    TestCase(
        query="key encrypt decrypt",
        description="key storage encryption decryption",
        expected_names=[
            "set_key",
            "get_key",
            "_decrypt_key",
            "_encrypt_key",
        ],
    ),
    TestCase(
        query="watchdog refresh",
        description="watchdog timer refresh and management",
        expected_names=[
            "wdt_refresh",
            "wdt_init",
            "wdt_kick",
        ],
    ),
    TestCase(
        query="flash write read",
        description="flash memory write and read operations",
        expected_names=[
            "flash_write",
            "flash_read",
            "flash_erase",
        ],
    ),
]


# ─── evaluace ─────────────────────────────────────────────────────────

@dataclass
class EvalResult:
    name: str
    total_results: int
    zbox_count: int
    mbedos_count: int
    other_count: int
    expected_found: list[str]
    expected_missed: list[str]
    top5: list[str]
    top10_names: list[str]


def is_zbox(path: str) -> bool:
    """True pokud soubor patří do našeho kódu (ne mbed-os)."""
    return ("/src/" in path or "/lib/" in path) and "/mbed-os/" not in path


def is_mbedos(path: str) -> bool:
    return "/mbed-os/" in path


def evaluate(
    label: str,
    conn: sqlite3.Connection,
    config_hash: str,
    query_str: str,
    expected: list[str],
) -> EvalResult:
    """Spustí FTS5 dotaz a vyhodnotí výsledky."""
    try:
        rows = conn.execute(
            """SELECT s.name, f.path as file_path
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               JOIN files f ON f.id = s.file_id
               WHERE symbols_fts MATCH ? AND s.config_hash = ?
               ORDER BY rank
               LIMIT 10""",
            (query_str, config_hash),
        ).fetchall()
    except Exception:
        rows = []

    names = [r["name"] for r in rows]
    paths = [r["file_path"] for r in rows]

    zbox = sum(1 for p in paths if is_zbox(p))
    mbedos = sum(1 for p in paths if is_mbedos(p))
    other = len(rows) - zbox - mbedos

    expected_lower = {e.lower() for e in expected}
    found_expected = []
    missed_expected = []
    for e in expected:
        if any(e.lower() in n.lower() for n in names):
            found_expected.append(e)
        else:
            missed_expected.append(e)

    # Fuzzy: najít i bez přesné shody jména — kontrolujeme, zda výsledek
    # obsahuje alespoň část očekávaného názvu
    for e in list(missed_expected):
        el = e.lower().replace("_", "")
        for n in names:
            nl = n.lower().replace("_", "")
            if el in nl or nl in el:
                found_expected.append(e)
                missed_expected.remove(e)
                break

    return EvalResult(
        name=label,
        total_results=len(rows),
        zbox_count=zbox,
        mbedos_count=mbedos,
        other_count=other,
        expected_found=found_expected,
        expected_missed=missed_expected,
        top5=names[:5],
        top10_names=names,
    )


# ─── main ─────────────────────────────────────────────────────────────

def main():
    db_path = Path.home() / ".fw-context/index/452361ffbf84f774/index.db"
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cfg = conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    config_hash = cfg["config_hash"]
    print(f"config_hash: {config_hash}")

    strategies = {
        "A-naive": lambda q: q,
        "B-split": lambda q: expand_query_split(q),
        "C-prefix": lambda q: expand_query_prefix(q),
        "D-hybrid": lambda q: expand_query_hybrid(q),
    }

    print("\n" + "=" * 80)
    print("QUERY STRATEGY EVALUATION")
    print("=" * 80)

    for tc in TEST_CASES:
        print(f"\n── Query: '{tc.query}' — {tc.description} ──")
        print(f"   Očekávané symboly: {tc.expected_names}")

        best = None
        all_results = []
        for sname, sfn in strategies.items():
            expanded = sfn(tc.query)
            r = evaluate(
                f"{sname} [{expanded[:50]}]",
                conn, config_hash, expanded, tc.expected_names,
            )
            all_results.append(r)
            z_ratio = r.zbox_count / max(r.total_results, 1)
            score = len(r.expected_found) * 2 + r.zbox_count
            if best is None or score > best[0]:
                best = (score, r.name)

        for r in sorted(all_results, key=lambda x: len(x.expected_found) * 2 + x.zbox_count, reverse=True):
            z_ratio = r.zbox_count / max(r.total_results, 1)
            m_ratio = r.mbedos_count / max(r.total_results, 1)
            print(f"\n   {r.name}:")
            print(f"      výsledků: {r.total_results} (zbox: {r.zbox_count} [{z_ratio:.0%}], mbed-os: {r.mbedos_count} [{m_ratio:.0%}])")
            print(f"      nalezeno: {r.expected_found}")
            if r.expected_missed:
                print(f"      chybí:    {r.expected_missed}")
            print(f"      top 5:    {r.top5}")

    # --- Test camelCase splitter ---
    print("\n" + "=" * 80)
    print("CAMELCASE SPLITTER TEST")
    print("=" * 80)
    test_cases = [
        "onConnectionComplete",
        "_connectionSupervisionTimeout",
        "ModemMsgManager",
        "HTTPResponse",
        "modem_parser_oob_init",
        "onDisconnectionComplete",
        "connectionHandle",
        "_last_ble_connected",
        "ZCfgDataManager",
        "validate_inv",
        "get_key",
        "_decrypt_key",
        "wdt_refresh",
        "flash_write",
    ]
    for tc in test_cases:
        tokens = tokenize_camel(tc)
        wildcards = make_wildcard_infix(tc)
        print(f"  {tc:40s} → tokens: {tokens}")
        if wildcards and wildcards != [f"{t}*" for t in tokens]:
            print(f"  {'':40s}   wildcards: {wildcards}")

    # --- Simulace přínosu pre-split indexu ---
    print("\n" + "=" * 80)
    print("PRE-SPLIT INDEX SIMULATION (simulace, co by nasel index s camelCase tokeny)")
    print("=" * 80)
    for tc in TEST_CASES:
        print(f"\n── Query: '{tc.query}' — {tc.description} ──")
        # Simulace: query slova expandujeme na camelCase-split varianty
        # a hledáme v existujícím indexu, zda by split varianta něco našla
        query_tokens = tc.query.lower().split()
        found_names = set()
        for qt in query_tokens:
            # Hledáme FTS5 MATCH na qt* — co najde?
            try:
                rows = conn.execute(
                    """SELECT s.name FROM symbols_fts
                       JOIN symbols s ON s.id = symbols_fts.rowid
                       WHERE symbols_fts MATCH ?
                       AND s.config_hash = ?
                       ORDER BY rank LIMIT 5""",
                    (f"{qt}*", config_hash),
                ).fetchall()
                for r in rows:
                    found_names.add(r["name"])
            except Exception:
                pass

        # Najdi symboly, které MATCHUJÍ splitnutý dotaz
        found_via_split = {}
        for qt in query_tokens:
            split_vars = tokenize_camel(qt)
            for sv in set(split_vars):
                if sv == qt:
                    continue
                try:
                    rows = conn.execute(
                        """SELECT s.name, f.path FROM symbols_fts
                           JOIN symbols s ON s.id = symbols_fts.rowid
                           JOIN files f ON f.id = s.file_id
                           WHERE symbols_fts MATCH ? AND s.config_hash = ?
                           ORDER BY rank LIMIT 5""",
                        (f"{sv}*", config_hash),
                    ).fetchall()
                    for r in rows:
                        # Ověř, že by to split index skutečně trefil
                        name_tokens = tokenize_camel(r["name"])
                        if sv in name_tokens:
                            key = (r["name"], r["path"])
                            if key not in found_via_split:
                                found_via_split[key] = sv
                except Exception:
                    pass

        print(f"   Nalezeno přes přímé tokeny:    {sorted(found_names)[:8]}")
        if found_via_split:
            print(f"   NAVÍC přes camelCase split:    ")
            for (name, path), token in sorted(found_via_split.items(), key=lambda x: x[0][0])[:8]:
                src = "zbox" if is_zbox(path) else "mbed-os" if is_mbedos(path) else "other"
                print(f"      {name:45s} [{src}] via '{token}*'")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
