"""Tests for the AdaptiveFusionPhase — replaces deleted test_rrf_fusion.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.search.phases.adaptive_fusion import (
    AdaptiveFusionPhase,
)


def _make_ctx(*, limit: int = 20, embedding_results=None, fts5_results=None, config: Config | None = None) -> PipelineContext:
    return PipelineContext(
        config_hash="test",
        project_root=Path("/tmp"),
        db_path=Path("/tmp/test.db"),
        query="test",
        original_query="test",
        config=config if config is not None else Config(),
        executor=MagicMock(),  # fusion phase never touches the DB
        limit=limit,
        embedding_results=embedding_results or [],
        fts5_results=fts5_results or [],
    )



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
