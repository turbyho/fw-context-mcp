"""Tests for the cross-encoder reranker."""

from __future__ import annotations


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


class TestRerankerRank:
    """Integration tests for CrossEncoderReranker.rank()."""

    def test_rank_empty_candidates(self) -> None:
        from fw_context_mcp.search.reranker import CrossEncoderReranker

        r = CrossEncoderReranker("cross-encoder/ms-marco-MiniLM-L6-v2")
        # Empty candidates — should return empty without loading model
        result = r.rank("query", [], top_k=10)
        assert result == []

    def test_rank_returns_correct_number(self, monkeypatch) -> None:
        """rank() returns exactly top_k results."""
        from fw_context_mcp.search.reranker import CrossEncoderReranker

        r = CrossEncoderReranker("cross-encoder/ms-marco-MiniLM-L6-v2")

        # Mock the model to avoid actual loading
        class FakeModel:
            def predict(self, pairs):
                return [0.8, 0.3, 0.9, 0.5, 0.1]

        r._model = FakeModel()

        candidates = [
            {"kind": "function", "name": f"fn{i}", "signature": f"void fn{i}()"}
            for i in range(5)
        ]
        result = r.rank("test query", candidates, top_k=3)
        assert len(result) == 3
        # Best score (0.9) should be first
        assert result[0]["_rerank_score"] == 0.9
        assert result[0]["name"] == "fn2"

    def test_rank_attaches_rerank_score(self, monkeypatch) -> None:
        """Each returned candidate has _rerank_score."""
        from fw_context_mcp.search.reranker import CrossEncoderReranker

        r = CrossEncoderReranker("cross-encoder/ms-marco-MiniLM-L6-v2")

        class FakeModel:
            def predict(self, pairs):
                return [0.5, 0.8]

        r._model = FakeModel()

        candidates = [
            {"kind": "function", "name": "a"},
            {"kind": "function", "name": "b"},
        ]
        result = r.rank("query", candidates, top_k=2)
        assert len(result) == 2
        for item in result:
            assert "_rerank_score" in item
            assert isinstance(item["_rerank_score"], float)

    def test_rank_mismatch_count_handled(self, monkeypatch) -> None:
        """Score/candidate count mismatch logged, not crashed."""
        from fw_context_mcp.search.reranker import CrossEncoderReranker

        r = CrossEncoderReranker("cross-encoder/ms-marco-MiniLM-L6-v2")

        class FakeModel:
            def predict(self, pairs):
                return [0.5]  # Only 1 score for 3 candidates

        r._model = FakeModel()

        candidates = [
            {"kind": "function", "name": f"fn{i}"}
            for i in range(3)
        ]
        result = r.rank("query", candidates, top_k=3)
        # Should handle gracefully — truncates to min length
        assert len(result) == 1
