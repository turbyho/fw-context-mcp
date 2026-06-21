"""Test LLM analysis quality across prompt variants.

Compares 4 prompt variants for describing large C/C++ functions:
  CURRENT — metadata only (name, signature, docstring, file path) — current behavior
  A       — metadata + full function body
  B       — metadata + callee names
  A_B     — metadata + body + callee names

Usage:
  python3 plans/test_llm_analysis_variants.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
from pathlib import Path

DB_PATH = "/home/turbyho/.fw-context/index/452361ffbf84f774/index.db"
OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5-coder:14b"

# Test symbols (qualified_name) — pick interesting functions from different domains
TEST_SYMBOLS = [
    "zbox::ModemMsgManager::process_cons",     # 284 lines, src/, consumer message processing
    "zbox::BleCmd::write",                     # 223 lines, src/, BLE command writing
    "zbox::AppManager::_periodic_app_task",    # 281 lines, src/, periodic task
    "zbox::LbKeyboard::check_buffer",          # 258 lines, src/, keyboard buffer check
]

ANALYSIS_SYSTEM = """You are a senior C/C++ embedded engineer writing comprehensive documentation.

For this symbol, output a JSON object with EXACTLY these three string fields:
  "summary": a detailed description (3-6 sentences). Explain the purpose, behavior, key design patterns, and embedded-system context.
  "inputs": a single string describing all inputs. For functions: list each parameter with type and role. For classes: describe dependencies and configuration. Format as "param1 (type): role; param2 (type): role". Write everything inline as ONE STRING, not a JSON object.
  "outputs": a single string describing all outputs and side effects. For functions: return value, error codes, and all side effects. For classes: what the class provides and manages. Format as ONE STRING, not a JSON object.

CRITICAL: "inputs" and "outputs" MUST be plain text strings. DO NOT use nested JSON objects like {"param": "desc"}. Write everything as a single string separated by semicolons or newlines.

Output ONLY the JSON object. No markdown fences, no commentary.
"""


def get_symbol_info(conn, qualified_name: str) -> dict | None:
    """Get symbol metadata from the index."""
    row = conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                  s.line, s.end_line, s.signature, s.docstring
           FROM symbols s
           WHERE s.qualified_name = ?
             AND s.is_definition = 1
           ORDER BY (s.end_line - s.line) DESC
           LIMIT 1""",
        (qualified_name,),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def get_function_body(symbol: dict) -> str:
    """Read the function body from the source file using line numbers."""
    file_path = symbol["file_path"]
    start_line = symbol["line"]
    end_line = symbol["end_line"]

    if not Path(file_path).exists():
        # Try relative to zbox project root
        file_path = f"/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw/{file_path}"

    try:
        with open(file_path) as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError) as e:
        return f"[CHYBA: nelze načíst soubor: {e}]"

    # Extract the function body
    if end_line > start_line and end_line <= len(lines):
        body_lines = lines[start_line - 1:end_line]
        return "".join(body_lines)
    return "[CHYBA: neplatné řádkové rozsahy]"


def get_callees(conn, symbol: dict) -> list[str]:
    """Get direct callee names for a function from the refs table."""
    rows = conn.execute(
        """SELECT DISTINCT s.qualified_name
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.from_usr = (SELECT usr FROM symbols WHERE id = ?)
             AND r.ref_kind = 'call'
             AND s.qualified_name != ''
           ORDER BY s.qualified_name
           LIMIT 80""",
        (symbol["id"],),
    ).fetchall()
    return [r["qualified_name"] for r in rows]


def build_prompt_current(symbol: dict) -> str:
    """CURRENT: metadata only (name, signature, docstring, file path)."""
    kind = symbol["kind"]
    qname = symbol["qualified_name"]
    sig = symbol.get("signature") or "—"
    file_path = symbol.get("file_path") or "?"
    doc = (symbol.get("docstring") or "").strip()
    doc_display = doc[:400] if doc else "(NO DOCSTRING — infer from name, signature, and file path)"

    user = textwrap.dedent(f"""\
        [{kind}] {qname}
        file: {file_path}
        sig: {sig}
        doc: {doc_display}""")

    return ANALYSIS_SYSTEM + "\nSymbol:\n\n" + user + "\n\nJSON:"


def build_prompt_a(symbol: dict) -> str:
    """A: metadata + full function body."""
    base = build_prompt_current(symbol)
    body = get_function_body(symbol)

    # Truncate body if it would exceed ~5000 chars (keep some room for response)
    if len(body) > 5000:
        body = body[:5000] + "\n// ... (zkráceno)"

    return base.rstrip() + f"\n\nFunction body:\n```cpp\n{body}\n```\n\nJSON:"


def build_prompt_b(symbol: dict) -> str:
    """B: metadata + callee names."""
    base = build_prompt_current(symbol)
    return base.rstrip() + f"\n\nCalled functions:\n  " + "\n  ".join(callee_names) + "\n\nJSON:"


def build_prompt_ab(symbol: dict) -> str:
    """A+B: metadata + function body + callee names."""
    base = build_prompt_current(symbol)
    body = get_function_body(symbol)

    if len(body) > 4000:
        body = body[:4000] + "\n// ... (zkráceno)"

    return (
        base.rstrip()
        + f"\n\nFunction body:\n```cpp\n{body}\n```"
        + f"\n\nCalled functions:\n  " + "\n  ".join(callee_names)
        + "\n\nJSON:"
    )


def call_ollama(prompt: str) -> str:
    """Call Ollama chat API and return the response text."""
    import httpx

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 3000,
        },
    }
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=180.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")
    except Exception as e:
        return f"CHYBA: {e}"


def parse_response(response: str) -> dict:
    """Parse JSON from Ollama response."""
    text = response.strip()
    # Strip markdown fences
    if text.startswith("```"):
        for fence in ("```json", "```", "``"):
            if text.startswith(fence):
                text = text[len(fence):].strip()
                break
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"_error": f"Nepodařilo se naparsovat JSON. Odpověď:\n{text[:500]}"}


def print_result(variant: str, result: dict, indent: str = "    "):
    """Pretty-print one test result."""
    print(f"{indent}┌─ {variant} ──────────────────────────────────────")
    summary = result.get("summary", "(chybí)")
    inputs = result.get("inputs", "(chybí)")
    outputs = result.get("outputs", "(chybí)")
    _error = result.get("_error", "")

    if _error:
        print(f"{indent}│ CHYBA: {_error[:200]}")
    else:
        print(f"{indent}│ summary: {summary}")
        print(f"{indent}│ inputs:  {inputs}")
        print(f"{indent}│ outputs: {outputs}")
    print(f"{indent}└{'─' * 50}")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 80)
    print("TEST LLM ANALÝZY — porovnání prompt variant")
    print(f"Model: {MODEL} | Ollama: {OLLAMA_URL}")
    print("=" * 80)

    for sym_name in TEST_SYMBOLS:
        symbol = get_symbol_info(conn, sym_name)
        if not symbol:
            print(f"\n❌ Symbol nenalezen: {sym_name}")
            continue

        global callee_names
        callee_names = get_callees(conn, symbol)

        print(f"\n{'─' * 80}")
        print(f"🔬 {symbol['qualified_name']}")
        print(f"   {symbol['file_path']}:{symbol['line']}-{symbol['end_line']} "
              f"({symbol['end_line'] - symbol['line']} ř.)")
        print(f"   sig: {symbol['signature']}")
        doc_preview = (symbol.get('docstring') or '(žádný)')[:100]
        print(f"   doc: {doc_preview}")
        print(f"   callees: {len(callee_names)} unikátních")
        if callee_names:
            print(f"   prvních 10 callees: {', '.join(callee_names[:10])}")

        # Run all 4 variants
        variants = {
            "CURRENT": build_prompt_current,
            "A (tělo)": build_prompt_a,
            "B (callees)": build_prompt_b,
            "A+B (tělo+callees)": build_prompt_ab,
        }

        results = {}
        for name, builder in variants.items():
            print(f"\n   ⏳ {name} — posílám do Ollama...")
            prompt = builder(symbol)
            prompt_chars = len(prompt)
            prompt_lines = prompt.count("\n")
            print(f"      prompt: {prompt_chars} znaků, ~{prompt_lines} řádků")

            response = call_ollama(prompt)
            parsed = parse_response(response)
            results[name] = parsed
            print_result(name, parsed)

        # Summary comparison
        print(f"\n   📊 POROVNÁNÍ:")
        for name, r in results.items():
            summary_len = len(r.get("summary", ""))
            inputs_len = len(r.get("inputs", ""))
            total = summary_len + inputs_len + len(r.get("outputs", ""))
            has_error = "_error" in r
            status = "❌ CHYBA" if has_error else f"✅ {total} znaků"
            print(f"   {name:<25} {status}")

    conn.close()
    print(f"\n{'=' * 80}")
    print("Hotovo.")


if __name__ == "__main__":
    main()
