import sqlite3


def upsert_llm_analysis_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str, str, str, str, str]],
) -> int:
    """Insert or replace LLM analysis rows.

    Each row: (symbol_id, summary, inputs, outputs, model, content_hash).
    Uses INSERT OR REPLACE so re-analysis is idempotent.
    Returns number of rows inserted.
    Orphan cleanup is handled separately by clean_orphan_llm_analysis()
    called once after indexing completes — see _run_postprocess in runner.py.
    """
    cur = conn.executemany(
        """INSERT OR REPLACE INTO llm_analysis(symbol_id, summary, inputs, outputs, model, content_hash, analyzed_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        rows,
    )
    # Sync denormalized columns on symbols table for FTS5 indexing
    conn.executemany(
        """UPDATE symbols SET summary = ?, inputs = ?, outputs = ? WHERE id = ?""",
        [(r[1], r[2], r[3], r[0]) for r in rows],
    )
    return cur.rowcount


def get_llm_analysis_for_symbol(
    conn: sqlite3.Connection,
    symbol_id: int,
) -> dict | None:
    """Return the LLM analysis row for a single symbol, or None."""
    row = conn.execute(
        """SELECT summary, inputs, outputs, model, analyzed_at
           FROM llm_analysis
           WHERE symbol_id = ?""",
        (symbol_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "summary": row["summary"],
        "inputs": row["inputs"],
        "outputs": row["outputs"],
        "model": row["model"],
        "analyzed_at": row["analyzed_at"],
    }


def count_llm_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
) -> int:
    """Return how many symbols in a config have pre-computed LLM analysis."""
    return conn.execute(
        """SELECT COUNT(*) FROM llm_analysis a
           JOIN symbols s ON s.id = a.symbol_id
           WHERE s.config_hash = ?""",
        (config_hash,),
    ).fetchone()[0]
