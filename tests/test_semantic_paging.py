"""semantic_search pages the symbols above its threshold.

The tool gave the top ``limit`` symbols and nothing more.  It now ranks
every symbol above the threshold, as far as one KNN query reaches, and a
page is a slice of that order.  sqlite-vec refuses a k above 4096, thus
the set stops there; ``total_capped`` in the notice says so when the last
row of the KNN was still above the threshold.

The k of the older KNN helper grew with the limit, and a limit of 1000
asked for k=5000: the query failed, and the phase fell back to the slow
scan of the BLOB table.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from fw_context_mcp.indexer.db import (
    init_vec_table,
    search_similar_vec,
    search_similar_vec_all,
    upsert_embeddings_vec,
)
from fw_context_mcp.indexer.db._embeddings import VEC_KNN_MAX_K
from tests._paging import NOTICE_KEYS, assert_whole_and_once
from tests.test_embedding_phase_order import CONFIG_HASH, QUERY_VEC, _FakeEmbedder, _seed_vec0


@pytest.fixture
def db_path(populated_db, tmpdir) -> Path:
    return Path(str(tmpdir)) / "test.db"


def _fill_vectors(conn, count: int) -> None:
    """*count* vectors in the vec0 table, all close to QUERY_VEC."""
    init_vec_table(conn, dim=len(QUERY_VEC), recreate=True)
    upsert_embeddings_vec(conn, [
        (i + 1, 0, CONFIG_HASH, [1.0, 0.001 * (i % 97), 0.0, 0.0]) for i in range(count)
    ])
    conn.commit()


def test_a_large_limit_does_not_break_the_knn(populated_db):
    """limit 1000 asked for k=5000, and sqlite-vec refused it."""
    _fill_vectors(populated_db, 50)
    rows = search_similar_vec(populated_db, QUERY_VEC, CONFIG_HASH, threshold=0.5, limit=1000)
    assert len(rows) == 50


def test_the_whole_set_says_when_it_stops_at_the_cap(populated_db):
    _fill_vectors(populated_db, VEC_KNN_MAX_K + 10)
    rows, capped = search_similar_vec_all(populated_db, QUERY_VEC, CONFIG_HASH, threshold=0.5)
    assert len(rows) == VEC_KNN_MAX_K
    assert capped is True


def test_a_set_below_the_cap_is_whole(populated_db):
    _fill_vectors(populated_db, 30)
    rows, capped = search_similar_vec_all(populated_db, QUERY_VEC, CONFIG_HASH, threshold=0.5)
    assert len(rows) == 30
    assert capped is False


def _page(path: Path, offset: int, limit: int = 1, threshold: float = 0.5, config=None):
    """One page of the handler, the notice included."""
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.mcp.handlers import search as handler
    from fw_context_mcp.search.context import PipelineContext
    from tests.test_embedding_phase_order import _context

    with (
        mock.patch.object(handler, "resolve_project_root", return_value=path.parent),
        mock.patch.object(handler, "_db_path", return_value=path),
        mock.patch.object(handler, "load_config", return_value=config or Config()),
        mock.patch.object(handler, "check_setup", return_value={"ollama_running": True}),
        mock.patch.object(handler, "_append_staleness_warning", side_effect=lambda r, *_: r),
        mock.patch.object(PipelineContext, "create", return_value=_context(path)),
        mock.patch("fw_context_mcp.search.phases.embedding.get_embedder", return_value=_FakeEmbedder()),
    ):
        import asyncio

        return asyncio.run(handler.semantic_search("q", threshold=threshold, limit=limit, offset=offset))


def _notice(rows):
    return next(r for r in rows if NOTICE_KEYS <= set(r))


def test_the_pages_walk_whole_and_once(populated_db, db_path):
    _seed_vec0(populated_db)
    seen: list[str] = []
    offset = 0
    while True:
        rows = _page(db_path, offset)
        notice = _notice(rows)
        names = [r["name"] for r in rows if "name" in r]
        assert notice["shown"] == len(names)
        seen += names
        offset += len(names)
        if not notice["more"]:
            break
    assert seen == ["near", "middle", "far"]
    assert_whole_and_once(seen, notice["total"])


def test_the_hint_repeats_a_threshold_that_is_not_the_default(populated_db, db_path):
    _seed_vec0(populated_db)
    assert _notice(_page(db_path, 0))["hint"] == (
        "semantic_search('q', threshold=0.5, offset=1) reads the next page."
    )


def test_a_page_after_the_end_names_the_total(populated_db, db_path):
    _seed_vec0(populated_db)
    assert _page(db_path, 9) == [{"info": "No symbol at offset 9; the answer holds 3."}]


def test_a_capped_set_says_that_the_total_is_a_lower_bound(populated_db, db_path):
    from tests.test_embedding_phase_order import _seed_symbols

    _seed_symbols(populated_db)
    with mock.patch(
        "fw_context_mcp.search.phases.embedding.search_similar_vec_all",
        side_effect=lambda conn, vec, ch, threshold: (
            search_similar_vec_all(conn, vec, ch, threshold)[0], True,
        ),
    ):
        _fill_vectors(populated_db, 3)
        notice = _notice(_page(db_path, 0))
    assert notice["total_capped"] is True


def test_the_lexical_fallback_pages_too(populated_db, db_path, tmp_path):
    from fw_context_mcp.mcp.shared.fallback import _fallback_to_search_code_inner
    from tests.test_embedding_phase_order import _seed_symbols

    _seed_symbols(populated_db)
    first = _fallback_to_search_code_inner(populated_db, tmp_path, "far middle near", CONFIG_HASH,
                                           2, "LLM is not running.")
    assert first[0]["warning"] == "LLM is not running."
    notice = first[1]
    assert (notice["total"], notice["shown"], notice["more"]) == (3, 2, True)
    assert notice["hint"] == "semantic_search('far middle near', offset=2) reads the next page."
    whole = _fallback_to_search_code_inner(populated_db, tmp_path, "far middle near", CONFIG_HASH,
                                           3, "LLM is not running.")
    last = _fallback_to_search_code_inner(populated_db, tmp_path, "far middle near", CONFIG_HASH,
                                          2, "LLM is not running.", offset=2)
    names = [r["name"] for r in whole if "name" in r]
    assert [r["name"] for r in last if "name" in r] == names[2:]


def test_the_fallback_hint_keeps_the_threshold(populated_db, tmp_path):
    """Without it, the next call ran at 0.60 and could answer with the embedding set."""
    from fw_context_mcp.mcp.shared.fallback import _fallback_to_search_code_inner
    from tests.test_embedding_phase_order import _seed_symbols

    _seed_symbols(populated_db)
    rows = _fallback_to_search_code_inner(populated_db, tmp_path, "far middle near", CONFIG_HASH,
                                          2, "No match.", hint_args={"threshold": 0.9})
    assert rows[1]["hint"] == (
        "semantic_search('far middle near', threshold=0.9, offset=2) reads the next page."
    )


def test_a_fallback_page_after_the_end_names_the_total(populated_db, tmp_path):
    from fw_context_mcp.mcp.shared.fallback import _fallback_to_search_code_inner
    from tests.test_embedding_phase_order import _seed_symbols

    _seed_symbols(populated_db)
    rows = _fallback_to_search_code_inner(populated_db, tmp_path, "far middle near", CONFIG_HASH,
                                          2, "No match.", offset=9)
    assert rows[1] == {"info": "No symbol at offset 9; the answer holds 3."}


def test_two_symbols_with_one_score_keep_the_order_of_their_id():
    """The page is a slice of this order; a tie in the order of id IN (...) moved between calls."""
    from fw_context_mcp.search.phases.embedding import EmbeddingPhase

    phase = EmbeddingPhase(independent=True, source_boost=True, whole_set=True)
    rows = [{"id": 9, "name": "b", "is_project": 1}, {"id": 3, "name": "a", "is_project": 1}]
    ranked, _ = phase._rank(rows, {9: 0.7, 3: 0.7}, limit=1)
    assert [r["name"] for r in ranked] == ["a", "b"], "whole_set must not cut, and id breaks the tie"


def test_the_boost_orders_within_its_window_of_the_raw_rank():
    """A project symbol deep in the raw order does not jump over a vendor symbol near the top.

    The boost over the whole set cost the first page 0.014 MRR@20 on 1207
    evaluation queries; inside a window of 200 raw ranks the first page
    is the one that semantic_search gave before it paged.
    """
    from fw_context_mcp.search.phases.embedding import _BOOST_WINDOW, EmbeddingPhase

    phase = EmbeddingPhase(independent=True, source_boost=True, whole_set=True)
    # Raw order: vendor (0.80) at rank 0, filler ranks 1.._BOOST_WINDOW, project (0.70) after them.
    similarity = {1: 0.80}
    similarity.update({100 + i: 0.79 for i in range(_BOOST_WINDOW)})
    similarity[2] = 0.70
    rows = [{"id": 1, "name": "vendor", "is_project": 0}, {"id": 2, "name": "project", "is_project": 1}]
    ranked, _ = phase._rank(rows, similarity, limit=20)
    # 0.70 x 1.2 = 0.84 beats 0.80 x 0.85 = 0.68, but the project row is one window later.
    assert [r["name"] for r in ranked] == ["vendor", "project"]
    # Inside one window the boost still decides.
    near = {1: 0.80, 2: 0.70}
    ranked, _ = phase._rank(rows, near, limit=20)
    assert [r["name"] for r in ranked] == ["project", "vendor"]


def test_a_symbol_of_several_chunks_counts_once(populated_db):
    init_vec_table(populated_db, dim=len(QUERY_VEC), recreate=True)
    upsert_embeddings_vec(populated_db, [
        (1, 0, CONFIG_HASH, [1.0, 0.0, 0.0, 0.0]),
        (1, 1, CONFIG_HASH, [1.0, 0.1, 0.0, 0.0]),
        (2, 0, CONFIG_HASH, [1.0, 0.2, 0.0, 0.0]),
    ])
    populated_db.commit()
    rows, capped = search_similar_vec_all(populated_db, QUERY_VEC, CONFIG_HASH, threshold=0.5)
    assert [r["symbol_id"] for r in rows] == [1, 2]
    assert capped is False


def test_a_reranker_that_drops_a_row_does_not_move_the_next_page(populated_db, db_path):
    """A page that lost a row would move the next offset back and overlap the next page."""
    from fw_context_mcp.config.settings import Config

    class _DropsOne:
        def rank(self, query, candidates, top_k):
            return list(candidates)[1:]

    _seed_vec0(populated_db)
    config = Config()
    config.llm.reranker_model = "stand-in"
    with \
            mock.patch("fw_context_mcp.search.reranker.get_reranker", return_value=_DropsOne()):
        rows = _page(db_path, 0, limit=2, config=config)
    assert [r["name"] for r in rows if "name" in r] == ["near", "middle"]
    assert _notice(rows)["shown"] == 2
