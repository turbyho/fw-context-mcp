#!/usr/bin/env python3
"""Context expansion experiment — test what call-graph expansion looks like on top results.

Fetches callers/callees for top-ranked symbols and estimates token budget.

Usage:
    python3 experiments/test_context_expansion.py [--project PROJECT_ROOT]
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fw_context_mcp.config import load as load_config, derive_project_id
from fw_context_mcp.indexer.db import open_db, get_active_config, search_symbols, search_similar_vec
from fw_context_mcp.llm.ollama import call_ollama_embed

# Queries that returned interesting results in the first experiment
QUERIES = [
    "how does the modem send data",
    "BLE pairing failure handling",
]


def get_callers(conn, config_hash: str, symbol_name: str, limit: int = 5, project_only: bool = True) -> list[dict]:
    """Get direct callers for a symbol by name.

    refs.to_usr = target, refs.from_usr = caller — joined via symbols.usr.
    """
    rows = conn.execute(
        """SELECT r.from_file AS caller_file, r.from_line AS caller_line,
                  caller.name AS caller_name, caller.kind AS caller_kind,
                  caller.file_path AS caller_file_path
           FROM refs r
           JOIN symbols target ON r.to_usr = target.usr
           JOIN symbols caller ON r.from_usr = caller.usr
           WHERE target.config_hash = ?
             AND caller.config_hash = ?
             AND target.name = ?
             AND r.ref_kind = 'call'
           ORDER BY r.from_file""",
        (config_hash, config_hash, symbol_name),
    ).fetchall()
    results = [dict(r) for r in rows]
    if project_only:
        results = [r for r in results if not _is_sdk((r.get("caller_file_path") or r.get("caller_file") or ""))]
    return results[:limit]


def get_callees(conn, config_hash: str, symbol_name: str, limit: int = 5, project_only: bool = True) -> list[dict]:
    """Get direct callees for a symbol by name.

    refs.from_usr = caller, refs.to_usr = callee — joined via symbols.usr.
    """
    rows = conn.execute(
        """SELECT r.from_file AS call_file, r.from_line AS call_line,
                  callee.name AS callee_name, callee.kind AS callee_kind,
                  callee.file_path AS callee_file_path
           FROM refs r
           JOIN symbols caller ON r.from_usr = caller.usr
           JOIN symbols callee ON r.to_usr = callee.usr
           WHERE caller.config_hash = ?
             AND callee.config_hash = ?
             AND caller.name = ?
             AND r.ref_kind = 'call'
           ORDER BY r.from_file""",
        (config_hash, config_hash, symbol_name),
    ).fetchall()
    results = [dict(r) for r in rows]
    if project_only:
        results = [r for r in results if not _is_sdk((r.get("callee_file_path") or r.get("call_file") or ""))]
    return results[:limit]


def get_source(conn, config_hash: str, symbol_name: str) -> str | None:
    """Get function body source."""
    row = conn.execute(
        """SELECT s.signature, s.source, s.file_path, s.line
           FROM symbols s
           WHERE s.config_hash = ? AND s.name = ? AND s.is_definition = 1
           LIMIT 1""",
        (config_hash, symbol_name),
    ).fetchone()
    if row:
        sig = row["signature"] or ""
        src = row["source"] or ""
        return f"{sig}\n{src[:200]}" if src else sig
    return None


def _is_sdk(file_path: str) -> bool:
    sdk_markers = ["mbed-os/", ".pio/", ".platformio/", "zephyr/", "build/", "modules/",
                   "framework-arduino", "tools/sdk"]
    return any(m in file_path for m in sdk_markers)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for C/C++."""
    return len(text) // 4


def run(project_path: str, limit: int = 5):
    project_root = Path(project_path).resolve()
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        config_hash = build_cfg["config_hash"]
    finally:
        conn.close()

    print(f"Project: {project_root.name} | Top-N: {limit}\n")

    for query in QUERIES:
        print("=" * 70)
        print(f"QUERY: '{query}'")
        print("=" * 70)

        # Get embedding
        try:
            query_vec = call_ollama_embed([query], cfg.llm)[0]
        except Exception as e:
            print(f"  SKIP: {e}")
            continue

        # FTS5 retrieval
        conn = open_db(db_path)
        with conn:
            fts5_rows = [dict(r) for r in search_symbols(conn, query, config_hash, limit=20)]
        conn.close()

        # Vector retrieval
        conn = open_db(db_path)
        with conn:
            vec_raw = search_similar_vec(conn, query_vec, config_hash, threshold=0.50, limit=20)
        sym_ids = [r["symbol_id"] for r in vec_raw]
        vec_rows = []
        if sym_ids:
            with conn:
                placeholders = ",".join("?" * len(sym_ids))
                emb_rows = conn.execute(
                    f"SELECT * FROM symbols WHERE config_hash = ? AND id IN ({placeholders}) AND is_definition = 1",
                    (config_hash, *sym_ids),
                ).fetchall()
                vec_rows = [dict(r) for r in emb_rows]
        conn.close()

        # Simple RRF fusion (default params)
        # Just take top-N from FTS5 for this experiment
        top = fts5_rows[:limit]

        print(f"\nTop-{limit} results with context expansion:\n")

        for i, r in enumerate(top, 1):
            name = r.get("name", "?")
            kind = r.get("kind", "?")
            file = r.get("file_path", "?")
            line = r.get("line", "?")
            sig = (r.get("signature") or "")[:80]
            summary = (r.get("summary") or "")[:120]

            print(f"--- #{i}: {name} ({kind}) — {file}:{line}")
            if sig:
                print(f"    Signature: {sig}")
            if summary:
                print(f"    Summary: {summary}")

            # Expansion: callers + callees (project only)
            conn = open_db(db_path)
            with conn:
                callers = get_callers(conn, config_hash, name, limit=5, project_only=True)
                callees = get_callees(conn, config_hash, name, limit=5, project_only=True)
            conn.close()

            if callers:
                print(f"    Called by ({len(callers)}):")
                for c in callers[:5]:
                    f = c.get('caller_file_path') or c.get('caller_file', '?')
                    print(f"      - {c['caller_name']} ({c['caller_kind']}) — {f}:{c.get('caller_line', '?')}")

            if callees:
                print(f"    Calls ({len(callees)}):")
                for c in callees[:5]:
                    f = c.get('callee_file_path') or c.get('call_file', '?')
                    print(f"      → {c['callee_name']} ({c['callee_kind']}) — {f}:{c.get('call_line', '?')}")

            # Estimate token usage
            ctx_text = f"{name} {sig or ''} {summary or ''}"
            caller_text = " ".join(c["caller_name"] for c in callers)
            callee_text = " ".join(c["callee_name"] for c in callees)
            total = estimate_tokens(f"{ctx_text} {caller_text} {callee_text}")
            print(f"    [~{total} tokens]")

            if not callers and not callees:
                print(f"    (no project-level callers or callees)")

        print()

    print("✓ Done.")


if __name__ == "__main__":
    parser = ArgumentParser(description="Context expansion experiment")
    parser.add_argument("--project", default="/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    run(args.project, args.limit)
