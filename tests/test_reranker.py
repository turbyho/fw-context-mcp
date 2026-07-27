"""Tests for the cross-encoder reranker."""

from __future__ import annotations

import pytest


class TestReranker:
    def test_no_candidates(self) -> None:
        from fw_context_mcp.search.reranker import get_reranker
        r = get_reranker(None)
        assert r is None

    def test_disabled_model(self) -> None:
        from fw_context_mcp.search.reranker import get_reranker
        r = get_reranker("")
        assert r is None

    def test_describe_candidate(self) -> None:
        from fw_context_mcp.search.reranker import CrossEncoderReranker
        c = {
            "kind": "function",
            "name": "uart_init",
            "signature": "int uart_init(int baudrate)",
            "summary": "Initialize UART hardware",
            "source": "int uart_init(int baudrate) { return 0; }",
        }
        desc = CrossEncoderReranker._describe(c)
        assert "uart_init" in desc
        assert "int uart_init(int baudrate)" in desc
        assert "Initialize UART hardware" in desc
        assert "return 0" in desc

    def test_describe_minimal(self) -> None:
        from fw_context_mcp.search.reranker import CrossEncoderReranker
        c = {"kind": "enum", "name": "OperationMode"}
        desc = CrossEncoderReranker._describe(c)
        assert "enum" in desc and "OperationMode" in desc


class TestRerankPhase:
    def test_no_candidates(self) -> None:
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.search.context import PipelineContext
        from fw_context_mcp.search.phases.rerank import RerankPhase
        phase = RerankPhase()
        ctx = PipelineContext(
            config_hash="test",
            project_root=None, db_path=None, query="test",
            original_query="test", config=Config(),
        )
        import asyncio
        result = asyncio.run(phase.run(ctx))
        assert result.final_results == []

    def test_fallback_truncation(self) -> None:
        """Without a reranker model, phase truncates to limit."""
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.search.context import PipelineContext
        from fw_context_mcp.search.phases.rerank import RerankPhase
        phase = RerankPhase()
        candidates = [{"name": f"sym_{i}"} for i in range(20)]
        ctx = PipelineContext(
            config_hash="test",
            project_root=None, db_path=None, query="test",
            original_query="test", config=Config(),
            limit=5,
            ranked_candidates=candidates,
        )
        import asyncio
        result = asyncio.run(phase.run(ctx))
        assert len(result.final_results) == 5
        assert result.final_results[0]["name"] == "sym_0"


class TestRRFWithRankedCandidates:
    def test_rrf_produces_ranked_candidates(self) -> None:
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.search.context import PipelineContext
        from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase
        phase = RRFFusionPhase()
        ctx = PipelineContext(
            config_hash="test",
            project_root=None, db_path=None, query="test",
            original_query="test", config=Config(),
            fts5_results=[
                {"name": "a", "file_path": "f1.c", "kind": "function", "is_project": 1},
                {"name": "b", "file_path": "f2.c", "kind": "function", "is_project": 1},
            ],
            embedding_results=[
                {"name": "a", "file_path": "f1.c", "kind": "function", "is_project": 1},
                {"name": "c", "file_path": "f3.c", "kind": "function", "is_project": 1},
            ],
            limit=5,
        )
        import asyncio
        result = asyncio.run(phase.run(ctx))
        assert result.ranked_candidates
        assert len(result.ranked_candidates) >= 3
        assert result.final_results


class TestAdaptiveRRF:
    def test_fixed_is_default(self) -> None:
        from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase
        phase = RRFFusionPhase()
        assert phase.W_FTS == 1.8
        assert phase.W_VEC == 0.2
        assert phase._weights_mode == "fixed"

    def test_adaptive_constructor(self) -> None:
        from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase
        phase = RRFFusionPhase(weights="adaptive")
        assert phase._weights_mode == "adaptive"

    def test_adaptive_weights_fallback(self) -> None:
        from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase
        import sqlite3
        conn = sqlite3.connect(":memory:")
        phase = RRFFusionPhase(weights="adaptive")
        w_fts, w_vec = phase._adaptive_weights(conn, "hash", "uart init")
        assert w_fts == 1.8
        assert w_vec == 0.2

    def test_adaptive_weights_with_data(self) -> None:
        from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE symbols (config_hash TEXT, name TEXT, is_definition INT)")
        for name in ["uart_init", "modem_send_at", "ble_connection_handler"]:
            conn.execute("INSERT INTO symbols VALUES ('hash', ?, 1)", (name,))
        conn.commit()

        phase = RRFFusionPhase(weights="adaptive")
        w_fts, w_vec = phase._adaptive_weights(conn, "hash", "uart")
        assert w_fts != 1.8
        assert w_fts + w_vec == pytest.approx(2.0, rel=0.01)
