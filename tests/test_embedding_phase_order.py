"""EmbeddingPhase must return its rows in the order of the vector search.

The phase gets the ranked symbol ids from the KNN query (vec0) or from the
brute-force scan (BLOB table), and then reads the symbol rows with
``WHERE id IN (...)``.  SQLite returns such rows in its own order, not in
the order of the id list.  Before the fix, SMART_SEARCH thus returned its
embedding results in symbol id order.  On 1292 evaluation queries, the
mean reciprocal rank was 0.048 in that order and 0.512 in similarity order.

The fixture inserts the symbols with ids in the opposite order to their
similarity, thus a result in id order fails the test.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

CONFIG_HASH = "hash-deadbeef"
QUERY_VEC = [1.0, 0.0, 0.0, 0.0]
# Inserted in this order, thus the ids ascend from "far" to "near".  The
# cosine similarity to QUERY_VEC ascends in the same direction: 0.6, 0.8, 0.995.
SYMBOLS = [
    ("far", [0.6, 0.8, 0.0, 0.0]),
    ("middle", [0.8, 0.6, 0.0, 0.0]),
    ("near", [1.0, 0.1, 0.0, 0.0]),
]
EXPECTED = ["near", "middle", "far"]


class _FakeEmbedder:
    """Stand-in embedder: every query gets QUERY_VEC."""

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [list(QUERY_VEC) for _ in texts]


def _seed_symbols(conn: sqlite3.Connection) -> dict[str, int]:
    from fw_context_mcp.indexer.db._files import upsert_file

    upsert_file(conn, CONFIG_HASH, "src/a.c", "c", False, 0.0)
    file_id = conn.execute(
        "SELECT id FROM files WHERE path = ? AND config_hash = ?", ("src/a.c", CONFIG_HASH)
    ).fetchone()["id"]
    ids = {}
    for line, (name, _vec) in enumerate(SYMBOLS, start=1):
        cur = conn.execute(
            """INSERT INTO symbols
               (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name,
                kind, line, col, end_line, is_definition, signature, is_project, source)
               VALUES (?, ?, 'src/a.c', ?, ?, ?, ?, 'function', ?, 1, ?, 1, ?, 1, ?)""",
            (CONFIG_HASH, file_id, name, f"c:@F@{name}", name, name, line, line,
             f"void {name}(void)", f"void {name}(void) {{}}"),
        )
        ids[name] = cur.lastrowid
    conn.commit()
    return ids


def _context(db_path: Path):
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.mcp.shared.executor import SyncQueryExecutor
    from fw_context_mcp.search.context import PipelineContext

    return PipelineContext(
        config_hash=CONFIG_HASH,
        project_root=db_path.parent,
        db_path=db_path,
        query="q",
        original_query="q",
        limit=20,
        config=Config(),
        executor=SyncQueryExecutor(str(db_path.resolve()), db_path),
    )


def _run_phase(db_path: Path, source_boost: bool = False):
    from fw_context_mcp.search.phases.embedding import EmbeddingPhase

    phase = EmbeddingPhase(independent=True, threshold=0.5, overfetch=50, source_boost=source_boost)
    with mock.patch("fw_context_mcp.search.phases.embedding.get_embedder", return_value=_FakeEmbedder()):
        return asyncio.run(phase.run(_context(db_path)))


@pytest.fixture
def db_path(populated_db, tmpdir) -> Path:
    return Path(str(tmpdir)) / "test.db"


def test_knn_path_keeps_the_vector_order(populated_db, db_path):
    _seed_vec0(populated_db)

    ctx = _run_phase(db_path)

    assert [r["name"] for r in ctx.embedding_results] == EXPECTED
    assert [r["name"] for r in ctx.final_results] == EXPECTED
    sims = [r["_similarity"] for r in ctx.embedding_results]
    assert sims == sorted(sims, reverse=True), "rows must come in descending similarity"


def _seed_vec0(conn: sqlite3.Connection) -> dict[str, int]:
    from fw_context_mcp.indexer.db._embeddings import init_vec_table, upsert_embeddings_vec

    ids = _seed_symbols(conn)
    init_vec_table(conn, dim=len(QUERY_VEC), recreate=True)
    upsert_embeddings_vec(conn, [(ids[name], 0, CONFIG_HASH, vec) for name, vec in SYMBOLS])
    conn.commit()
    return ids


def test_source_boost_path_sorts_by_boosted_similarity(populated_db, db_path):
    """semantic_search path: project x1.2, vendor x0.85, then descending.

    "near" is vendor code here: 0.995 x 0.85 = 0.846 goes below "middle"
    (0.8 x 1.2 = 0.96), and stays above "far" (0.6 x 1.2 = 0.72).  The
    boosted order thus differs from the vector order, and a phase that
    ignores the boost fails.
    """
    ids = _seed_vec0(populated_db)
    populated_db.execute("UPDATE symbols SET is_project = 0 WHERE id = ?", (ids["near"],))
    populated_db.commit()

    ctx = _run_phase(db_path, source_boost=True)

    assert [r["name"] for r in ctx.embedding_results] == ["middle", "near", "far"]


def test_a_declaration_is_left_out_and_the_order_stays(populated_db, db_path):
    """The phase reads definitions only; a row it drops must not disturb the rest."""
    ids = _seed_vec0(populated_db)
    populated_db.execute("UPDATE symbols SET is_definition = 0 WHERE id = ?", (ids["middle"],))
    populated_db.commit()

    ctx = _run_phase(db_path)

    assert [r["name"] for r in ctx.embedding_results] == ["near", "far"]


def test_blob_path_keeps_the_vector_order(populated_db, db_path):
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.indexer.db._embeddings import _vec_to_blob, upsert_embeddings

    ids = _seed_symbols(populated_db)
    # No vec0 table: the phase must use the brute-force scan of the BLOB table.
    populated_db.execute("DROP TABLE IF EXISTS vec_symbols")
    model = Config().llm.embed_key()
    upsert_embeddings(populated_db, [(ids[name], 0, _vec_to_blob(vec), model, "h") for name, vec in SYMBOLS])
    populated_db.commit()

    ctx = _run_phase(db_path)

    assert [r["name"] for r in ctx.embedding_results] == EXPECTED
    assert [r["name"] for r in ctx.final_results] == EXPECTED
