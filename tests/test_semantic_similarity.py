"""semantic_search must keep the result contract of its docstring on both embedding paths.

The docstring gives ``_similarity`` (the raw cosine similarity) and
``_method`` as result fields.  When semantic_search moved to the search
pipeline, these defects came in:

- ``FormatPhase`` copied a fixed set of fields and dropped ``_similarity``,
  thus no result had the field.
- No result had ``_method: "embedding"``.
- When a phase failed, the fallback told the user to lower the threshold,
  and not the error of the phase.

The relevance floor reads the raw similarity, as ``threshold`` does.  The
source boost (project x1.2, other code x0.85) only sets the order.

The fixture and the helpers come from ``test_embedding_phase_order``:
three symbols with a cosine similarity of 0.6, 0.8 and 0.995 to the query.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from tests._paging import NOTICE_KEYS
from tests.test_embedding_phase_order import (
    SYMBOLS,
    _context,
    _FakeEmbedder,
    _run_phase,
    _seed_symbols,
    _seed_vec0,
)

# Cosine similarity of each symbol in SYMBOLS to QUERY_VEC.
RAW_SIMILARITY = {"far": 0.6, "middle": 0.8, "near": 0.995}


def _seed_blob(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed the BLOB table only, thus the phase must use the brute-force scan."""
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.indexer.db._embeddings import _vec_to_blob, upsert_embeddings

    ids = _seed_symbols(conn)
    conn.execute("DROP TABLE IF EXISTS vec_symbols")
    model = Config().llm.embed_key()
    upsert_embeddings(conn, [(ids[name], 0, _vec_to_blob(vec), model, "h") for name, vec in SYMBOLS])
    conn.commit()
    return ids


SEEDERS = {"knn": _seed_vec0, "blob": _seed_blob}


@pytest.fixture
def db_path(populated_db, tmpdir) -> Path:
    return Path(str(tmpdir)) / "test.db"


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_each_path_gives_the_cosine_similarity(populated_db, db_path, path):
    SEEDERS[path](populated_db)

    ctx = _run_phase(db_path)

    by_name = {r["name"]: r["_similarity"] for r in ctx.embedding_results}
    assert by_name == pytest.approx(RAW_SIMILARITY, abs=1e-3)


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_each_path_applies_the_source_boost(populated_db, db_path, path):
    """"near" is vendor code: 0.995 x 0.85 = 0.846 goes below "middle" (0.8 x 1.2 = 0.96).

    Thus the boost changes the order, and a path that ignores it fails.
    ``_similarity`` keeps the raw value: the boost does not change it.
    """
    ids = SEEDERS[path](populated_db)
    populated_db.execute("UPDATE symbols SET is_project = 0 WHERE id = ?", (ids["near"],))
    populated_db.commit()

    ctx = _run_phase(db_path, source_boost=True)

    assert [r["name"] for r in ctx.embedding_results] == ["middle", "near", "far"]
    by_name = {r["name"]: r["_similarity"] for r in ctx.embedding_results}
    assert by_name == pytest.approx(RAW_SIMILARITY, abs=1e-3)


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_semantic_pipeline_gives_the_similarity(populated_db, db_path, path):
    """The handler gives formatted_results, thus the value must pass FormatPhase."""
    from fw_context_mcp.search.pipeline import PipelineRunner, _build_semantic_search

    SEEDERS[path](populated_db)
    runner = PipelineRunner(_build_semantic_search(threshold=0.5))
    with mock.patch("fw_context_mcp.search.phases.embedding.get_embedder", return_value=_FakeEmbedder()):
        ctx = asyncio.run(runner.run(_context(db_path)))

    symbols = [r for r in ctx.formatted_results if "name" in r]
    assert [r["name"] for r in symbols] == ["near", "middle", "far"]
    assert [r["_similarity"] for r in symbols] == pytest.approx([0.995, 0.8, 0.6], abs=1e-3)


class _OffAxisEmbedder:
    """Query vector with a cosine similarity of 0.7 ("far"), 0.7 ("middle") and 0.547 ("near").

    The best raw score, 0.7, is above the floor of 0.68.  For vendor code
    (x 0.85) the best boosted score is 0.595, below the floor.
    """

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [[0.5, 0.5, 0.5**0.5, 0.0] for _ in texts]


class _WeakEmbedder:
    """Query vector with a cosine similarity of 0.588 ("far"), 0.588 ("middle") and 0.46 ("near").

    "near" is below the threshold of 0.5.  The best raw score, 0.588, is
    below the floor of 0.68.  For project code (x 1.2) the best boosted
    score is 0.706, above the floor.
    """

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [[0.42, 0.42, (1.0 - 2 * 0.42**2) ** 0.5, 0.0] for _ in texts]


FALLBACK = [{"warning": "fallback", "_method": "search_code_fallback"}]


def _run_handler(
    db_path: Path, embedder: object, threshold: float = 0.5, config: object | None = None,
    limit: int = 20, context_limit: int = 20, offset: int = 0,
) -> tuple[list[dict], mock.MagicMock]:
    """Call the semantic_search handler with the index, the LLM and the fallback replaced.

    *context_limit* is the limit of the pipeline context, which the
    embedding phase cuts to.  ``PipelineContext.create`` is replaced, thus
    the context does not follow *limit*.
    """
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.mcp.handlers import search as handler
    from fw_context_mcp.search.context import PipelineContext

    fallback = mock.MagicMock(return_value=FALLBACK)
    context = _context(db_path).evolve(limit=context_limit)
    with (
        mock.patch.object(handler, "resolve_project_root", return_value=db_path.parent),
        mock.patch.object(handler, "_db_path", return_value=db_path),
        mock.patch.object(handler, "load_config", return_value=config or Config()),
        mock.patch.object(handler, "check_setup", return_value={"ollama_running": True}),
        mock.patch.object(handler, "_fallback_to_search_code", fallback),
        mock.patch.object(handler, "_append_staleness_warning", side_effect=lambda r, *_: r),
        mock.patch.object(PipelineContext, "create", return_value=context),
        mock.patch("fw_context_mcp.search.phases.embedding.get_embedder", return_value=embedder),
    ):
        rows = asyncio.run(handler.semantic_search("q", threshold=threshold, limit=limit, offset=offset))
    # The page notice is a row of its own; the tests below read the answer.
    results = [r for r in rows if not NOTICE_KEYS <= set(r)]
    return results, fallback


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_relevance_floor_sees_a_match_that_the_boosted_cut_drops(populated_db, db_path, path):
    """Raw scores: far 0.7 and middle 0.7 (vendor), near 0.547 (project).

    The boost puts "near" first (0.656 against 0.595), and the cut to one
    row keeps only "near".  The best raw score of all matches, 0.7, is above
    the floor, thus the answer is "near" with no warning.  A floor that
    reads only the rows after the cut sees 0.547 and gives the warning.
    """
    ids = SEEDERS[path](populated_db)
    populated_db.execute(
        "UPDATE symbols SET is_project = 0 WHERE id IN (?, ?)", (ids["far"], ids["middle"])
    )
    populated_db.commit()

    results, fallback = _run_handler(db_path, _OffAxisEmbedder(), limit=1, context_limit=1)

    fallback.assert_not_called()
    assert [r.get("name") for r in results] == ["near"]
    assert "warning" not in results[0]


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_handler_gives_no_more_than_the_limit(populated_db, db_path, path):
    """PipelineContext.create lifts a limit below 5 to 5 for smart_search.

    semantic_search accepts a limit of 1 and more, thus the handler must
    cut to the limit that the caller gave.
    """
    SEEDERS[path](populated_db)

    results, fallback = _run_handler(db_path, _FakeEmbedder(), limit=2)

    fallback.assert_not_called()
    assert [r["name"] for r in results] == ["near", "middle"]


def test_the_relevance_floor_keeps_the_limit(populated_db, db_path):
    _seed_vec0(populated_db)

    results, _ = _run_handler(db_path, _WeakEmbedder(), limit=1)

    assert len(results) == 1
    assert len(results[0]["_results"]) == 1


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_handler_marks_each_result_as_embedding(populated_db, db_path, path):
    SEEDERS[path](populated_db)

    results, fallback = _run_handler(db_path, _FakeEmbedder())

    fallback.assert_not_called()
    assert [r["name"] for r in results] == ["near", "middle", "far"]
    assert {r["_method"] for r in results} == {"embedding"}
    assert results[0]["_similarity"] == pytest.approx(0.995, abs=1e-3)


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_relevance_floor_reads_the_raw_similarity(populated_db, db_path, path):
    """Project code: the raw best (0.588) is below the floor, the boosted best (0.706) is not."""
    SEEDERS[path](populated_db)

    results, fallback = _run_handler(db_path, _WeakEmbedder())

    fallback.assert_not_called()
    assert len(results) == 1
    assert results[0]["_fallback_suggestion"] == "search_code"
    assert results[0]["_best_similarity"] == pytest.approx(0.588, abs=1e-3)
    # "far" and "middle" have the same score, thus their order is not fixed.
    assert sorted(r["name"] for r in results[0]["_results"]) == ["far", "middle"]


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_the_boost_does_not_push_vendor_code_below_the_floor(populated_db, db_path, path):
    """Vendor code: the raw best (0.7) is above the floor, the boosted best (0.595) is not."""
    SEEDERS[path](populated_db)
    populated_db.execute("UPDATE symbols SET is_project = 0")
    populated_db.commit()

    results, fallback = _run_handler(db_path, _OffAxisEmbedder())

    fallback.assert_not_called()
    assert sorted(r["name"] for r in results) == ["far", "middle", "near"]
    assert all("warning" not in r for r in results)


@pytest.mark.parametrize("path", sorted(SEEDERS))
def test_no_match_goes_to_the_search_code_fallback(populated_db, db_path, path):
    """FormatPhase gives an "info" entry for no match, thus the list is not empty."""
    SEEDERS[path](populated_db)

    results, fallback = _run_handler(db_path, _FakeEmbedder(), threshold=0.999)

    assert results == FALLBACK
    fallback.assert_called_once()
    assert "No symbols matched" in fallback.call_args.kwargs["warning"]


class _ReversingReranker:
    """Stand-in cross-encoder: puts the last result first."""

    def rank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        return list(reversed(candidates))[:top_k]


def test_the_floor_reads_the_best_score_and_not_the_reranked_first(populated_db, db_path):
    """Raw scores: far 0.7, middle 0.7, near 0.547.  The reranker puts near first.

    The best score is above the floor, thus the answer is the reranked list.
    A floor that reads the first result after the rerank sees 0.547 and fails.
    """
    from fw_context_mcp.config.settings import Config

    _seed_vec0(populated_db)
    config = Config()
    config.llm.reranker_model = "stand-in"

    with mock.patch("fw_context_mcp.search.reranker.get_reranker", return_value=_ReversingReranker()):
        results, fallback = _run_handler(db_path, _OffAxisEmbedder(), config=config)

    fallback.assert_not_called()
    assert results[0]["name"] == "near"
    assert sorted(r["name"] for r in results[1:]) == ["far", "middle"]
    assert all("warning" not in r for r in results)


def test_the_reranker_orders_inside_one_page(populated_db, db_path):
    """The pages are a slice of the vector order; the reranker moves no symbol to another page.

    Vector order: near, middle, far.  Page one of two holds near and middle,
    and the reversing reranker turns them around.  far stays on page two.
    """
    from fw_context_mcp.config.settings import Config

    _seed_vec0(populated_db)
    config = Config()
    config.llm.reranker_model = "stand-in"

    with mock.patch("fw_context_mcp.search.reranker.get_reranker", return_value=_ReversingReranker()):
        first, _ = _run_handler(db_path, _FakeEmbedder(), config=config, limit=2)
        second, _ = _run_handler(db_path, _FakeEmbedder(), config=config, limit=2, offset=2)

    assert [r["name"] for r in first] == ["middle", "near"]
    assert [r["name"] for r in second] == ["far"]


class _FailingEmbedder:
    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding dimension mismatch")


def test_a_failed_phase_gives_its_error_to_the_fallback(populated_db, db_path):
    """The runner records the failure in ctx.warnings and the result is empty.

    The fallback must give that error, not the advice to lower the threshold.
    """
    _seed_vec0(populated_db)

    results, fallback = _run_handler(db_path, _FailingEmbedder())

    assert results == FALLBACK
    warning = fallback.call_args.kwargs["warning"]
    assert "embedding dimension mismatch" in warning
    assert "lowering the threshold" not in warning


def test_the_default_format_gives_no_similarity(db_path, populated_db):
    """smart_search mixes embedding rows with call-graph neighbors that have no score.

    Thus its default format keeps the output it had, without ``_similarity``.
    """
    from fw_context_mcp.search.phases.format import FormatPhase

    ctx = _context(db_path).evolve(final_results=[{"name": "near", "_similarity": 0.9}])

    formatted = asyncio.run(FormatPhase().run(ctx)).formatted_results

    assert formatted == [{
        "name": "near", "qualified_name": "", "kind": "", "file": formatted[0]["file"],
        "line": 0, "is_definition": False, "signature": "", "docstring": "",
    }]
