"""Tests for the AdaptiveFusionPhase — replaces deleted test_rrf_fusion.py."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.search.phases.adaptive_fusion import (
    AdaptiveFusionPhase,
)

# ── RRF boost constants (moved from adaptive_fusion.py — test-only) ──

PROJ_BOOST: float = 1.5
FUNC_BOOST: float = 1.2
PAGERANK_BOOST: float = 0.2


def _boost(row: dict) -> float:
    """Compute per-symbol RRF boost factor from project/kind/pagerank fields."""
    b = 1.0
    if row.get("is_project") == 1:
        b *= PROJ_BOOST
    kind = row.get("kind", "")
    if kind in ("function", "method", "constructor", "destructor", "varglobal"):
        b *= FUNC_BOOST
    pr = row.get("pagerank", 0.0) or 0.0
    if pr > 0:
        b *= 1.0 + pr * PAGERANK_BOOST
    return b


def _make_ctx(*, limit: int = 20, embedding_results=None, fts5_results=None, config: Config | None = None) -> PipelineContext:
    return PipelineContext(
        config_hash="test",
        project_root=Path("/tmp"),
        db_path=Path("/tmp/test.db"),
        query="test",
        original_query="test",
        config=config if config is not None else Config(),
        limit=limit,
        embedding_results=embedding_results or [],
        fts5_results=fts5_results or [],
    )


class TestBoostFunction:
    def test_identity_for_empty_row(self) -> None:
        assert _boost({}) == 1.0

    def test_project_boost(self) -> None:
        assert _boost({"is_project": 1}) == PROJ_BOOST

    def test_non_project_no_boost(self) -> None:
        assert _boost({"is_project": 0}) == 1.0

    def test_function_kind_boost(self) -> None:
        assert _boost({"kind": "function"}) == FUNC_BOOST

    @pytest.mark.parametrize("kind", ["function", "method", "constructor", "destructor", "varglobal"])
    def test_all_func_kinds_boosted(self, kind: str) -> None:
        assert _boost({"kind": kind}) == FUNC_BOOST

    @pytest.mark.parametrize("kind", ["class", "struct", "enum", "typedef", "variable", "field"])
    def test_non_func_kinds_not_boosted(self, kind: str) -> None:
        assert _boost({"kind": kind}) == 1.0

    def test_pagerank_boost(self) -> None:
        assert _boost({"pagerank": 1.0}) == 1.0 + PAGERANK_BOOST

    def test_pagerank_zero_no_boost(self) -> None:
        assert _boost({"pagerank": 0.0}) == 1.0

    def test_pagerank_none_no_boost(self) -> None:
        assert _boost({"pagerank": None}) == 1.0

    def test_combined_boosts_multiply(self) -> None:
        score = _boost({"is_project": 1, "kind": "function", "pagerank": 0.5})
        expected = PROJ_BOOST * FUNC_BOOST * (1.0 + 0.5 * PAGERANK_BOOST)
        assert score == expected

    def test_varglobal_combined(self) -> None:
        score = _boost({"is_project": 1, "kind": "varglobal"})
        assert score == PROJ_BOOST * FUNC_BOOST


class TestAdaptiveFusionPhase:
    def test_prefers_embedding_over_fts5(self) -> None:
        """With >= MIN_DENSE_COUNT embedding results, FTS5 is ignored."""
        from fw_context_mcp.search.phases.adaptive_fusion import MIN_DENSE_COUNT

        emb = [{"name": f"sym{i}"} for i in range(MIN_DENSE_COUNT)]
        fts = [{"name": "i2c_read"}]
        ctx = _make_ctx(embedding_results=emb, fts5_results=fts)
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        results = list(result.final_results)
        assert len(results) == MIN_DENSE_COUNT
        assert all(r["name"].startswith("sym") for r in results)

    def test_falls_back_to_fts5_when_no_embeddings(self) -> None:
        ctx = _make_ctx(
            embedding_results=[],
            fts5_results=[{"name": "i2c_read", "qualified_name": "i2c_read"}],
        )
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        results = list(result.final_results)
        assert len(results) == 1
        assert results[0]["name"] == "i2c_read"

    def test_empty_when_both_empty(self) -> None:
        ctx = _make_ctx(embedding_results=[], fts5_results=[])
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        assert len(list(result.final_results)) == 0

    def test_respects_limit(self) -> None:
        ctx = _make_ctx(
            limit=3,
            embedding_results=[{"name": f"sym{i}"} for i in range(10)],
        )
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        assert len(list(result.final_results)) == 3

    def test_should_run_with_fts5_only(self) -> None:
        ctx = _make_ctx(embedding_results=[], fts5_results=[{"name": "x"}])
        phase = AdaptiveFusionPhase()
        assert phase.should_run(ctx)

    def test_should_run_false_when_both_empty(self) -> None:
        ctx = _make_ctx(embedding_results=[], fts5_results=[])
        phase = AdaptiveFusionPhase()
        assert not phase.should_run(ctx)

    def test_fts5_fallback_when_dense_below_min(self) -> None:
        """Dense results (1) < MIN_DENSE_COUNT (3) → FTS5 fallback."""
        ctx = _make_ctx(
            embedding_results=[{"name": "only_one"}],
            fts5_results=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        )
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        results = list(result.final_results)
        assert all(r["name"] in ("a", "b", "c") for r in results)

    def test_embeds_ignored_when_few_and_fts5_exists(self) -> None:
        """2 dense results (< MIN_DENSE_COUNT) — FTS5 used, dense ignored."""
        ctx = _make_ctx(
            embedding_results=[{"name": "x"}, {"name": "y"}],
            fts5_results=[{"name": "fts_result"}],
        )
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        results = list(result.final_results)
        assert results[0]["name"] == "fts_result"

    def test_limits_dense_results_to_ctx_limit(self) -> None:
        """Dense results are truncated to ctx.limit even when many available."""
        emb = [{"name": f"sym{i}"} for i in range(20)]
        ctx = _make_ctx(embedding_results=emb, limit=5)
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        assert len(list(result.final_results)) == 5

    def test_no_fts5_when_dense_meets_threshold(self) -> None:
        """FTS5 results are completely ignored when dense >= MIN_DENSE_COUNT."""
        from fw_context_mcp.search.phases.adaptive_fusion import MIN_DENSE_COUNT

        emb = [{"name": f"sym{i}"} for i in range(MIN_DENSE_COUNT)]
        fts = [{"name": "should_be_ignored"}]
        ctx = _make_ctx(embedding_results=emb, fts5_results=fts, limit=MIN_DENSE_COUNT)
        phase = AdaptiveFusionPhase()
        result = asyncio.run(phase.run(ctx))
        results = list(result.final_results)
        assert all(r["name"].startswith("sym") for r in results)
        assert not any(r["name"] == "should_be_ignored" for r in results)

    def test_min_dense_count_from_config(self) -> None:
        """Config index.min_dense_count overrides module default."""
        from fw_context_mcp.config.settings import Config, IndexConfig

        cfg = Config()
        cfg.index = IndexConfig(min_dense_count=5)
        ctx = _make_ctx(config=cfg, embedding_results=[{"name": f"x{i}"} for i in range(4)])
        # 4 < config threshold 5 → fallback
        phase = AdaptiveFusionPhase()
        threshold = phase._get_threshold(ctx)
        assert threshold == 5
