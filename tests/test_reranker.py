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
