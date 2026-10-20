"""Tests for streaming support: Plan A (SSE auto-detection) and Plan B (active streaming).

Covers:
- Plan A: SSE response auto-detection in OpenAIChatClient.generate (stream=false, API returns SSE)
- Plan A: SSE response auto-detection in call_llm (Ollama-native path)
- Plan B: Active streaming in OpenAIChatClient.generate (stream=true, httpx.stream)
- Plan B: Active streaming in call_llm (Ollama-native path, stream=true)
- Config: stream key of an LLM profile of the global config
- configure_llm: the test call uses the stream key of the selected profile
- Error handling in streaming paths
- Debug log entries for streaming responses
- SSE parsing edge cases (empty chunks, malformed lines, [DONE] marker)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig, _update_local_toml
from fw_context_mcp.llm.chat_router import (
    OpenAIChatClient,
    _accumulate_openai_sse,
    _extract_openai_text,
    _is_sse_response,
)
from fw_context_mcp.llm.client import call_llm
from fw_context_mcp.llm.errors import ChatApiError
from fw_context_mcp.llm.ollama import (
    OllamaError,
    _accumulate_ollama_sse,
    _extract_ollama_chunk,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(
    chat_api_base: str | None = "https://api.example.com/v1",
    chat_api_key: str | None = None,
    model: str = "test-model",
    timeout: float = 30.0,
    stream: bool = False,
    debug_log: Path | None = None,
    ollama_url: str = "http://localhost:11434",
    auto_pull: bool = False,
) -> LLMConfig:
    return LLMConfig(
        chat_api_base=chat_api_base,
        chat_api_key=chat_api_key,
        model=model,
        timeout=timeout,
        stream=stream,
        debug_log=debug_log,
        ollama_url=ollama_url,
        auto_pull=auto_pull,
    )


def _mock_json_response(
    status_code: int = 200,
    json_body: dict | None = None,
) -> httpx.Response:
    if json_body is None:
        json_body = {"choices": [{"message": {"content": "test response"}}]}
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "https://example.com"),
    )


def _mock_sse_response(
    status_code: int = 200,
    sse_lines: list[str] | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response with SSE content-type and body."""
    if sse_lines is None:
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]
    body = "\n".join(sse_lines)
    return httpx.Response(
        status_code=status_code,
        text=body,
        headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://example.com"),
    )


def _mock_ollama_ndjson_response(
    status_code: int = 200,
    lines: list[str] | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response with NDJSON content (Ollama streaming format)."""
    if lines is None:
        lines = [
            '{"response":"Hello","done":false}',
            '{"response":" world","done":false}',
            '{"response":"","done":true}',
        ]
    body = "\n".join(lines)
    return httpx.Response(
        status_code=status_code,
        text=body,
        headers={"content-type": "application/x-ndjson"},
        request=httpx.Request("POST", "http://localhost:11434/api/generate"),
    )


class _MockStreamResponse:
    """Mock for httpx.stream() context manager that yields lines from iter_lines()."""

    def __init__(self, lines: list[str], status_code: int = 200, headers: dict | None = None):
        self._lines = lines
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "\n".join(lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "https://example.com"),
                response=httpx.Response(self.status_code, text=self.text, request=httpx.Request("POST", "https://example.com")),
            )

    def iter_lines(self):
        yield from self._lines

    def read(self) -> bytes:
        """As httpx.Response.read: the clients read the body of an error answer before the error."""
        return self.text.encode()


def _mock_httpx_stream(lines: list[str], status_code: int = 200):
    """Return a mock for httpx.stream that yields the given lines."""
    mock_resp = _MockStreamResponse(lines, status_code)
    return MagicMock(return_value=mock_resp)


# ── Plan A: SSE response detection helpers ───────────────────────────────────


class TestSseDetection:
    def test_is_sse_response_true_for_event_stream(self):
        resp = _mock_sse_response()
        assert _is_sse_response(resp) is True

    def test_is_sse_response_false_for_json(self):
        resp = _mock_json_response()
        assert _is_sse_response(resp) is False

    def test_is_sse_response_false_for_no_content_type(self):
        resp = httpx.Response(
            200,
            text="data: test",
            request=httpx.Request("POST", "https://example.com"),
        )
        assert _is_sse_response(resp) is False

    def test_is_sse_response_true_for_ndjson(self):
        """Ollama uses application/x-ndjson for streaming."""
        resp = httpx.Response(
            200,
            text='{"response":"hi"}',
            headers={"content-type": "application/x-ndjson"},
            request=httpx.Request("POST", "http://localhost:11434"),
        )
        # ndjson is NOT SSE — _is_sse_response should return False
        # (the Ollama path checks for ndjson separately)
        assert _is_sse_response(resp) is False


# ── Plan A: OpenAI SSE accumulation ──────────────────────────────────────────


class TestAccumulateOpenaiSse:
    def test_basic_accumulation(self):
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "Hello world"

    def test_empty_delta_content_skipped(self):
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":""}}]}',
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            'data: {"choices":[{"delta":{}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "OK"

    def test_no_content_key_in_delta(self):
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"text"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "text"

    def test_done_marker_stops_parsing(self):
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"before"}}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":"after"}}]}',
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "before"

    def test_malformed_json_lines_skipped(self):
        resp = _mock_sse_response(sse_lines=[
            "data: not json",
            'data: {"choices":[{"delta":{"content":"good"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "good"

    def test_empty_stream_raises(self):
        resp = _mock_sse_response(sse_lines=[
            "data: [DONE]",
        ])
        with pytest.raises(ChatApiError, match="no content chunks"):
            _accumulate_openai_sse(resp)

    def test_non_data_lines_ignored(self):
        resp = _mock_sse_response(sse_lines=[
            ": comment line",
            "event: message",
            'data: {"choices":[{"delta":{"content":"text"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "text"


# ── Plan A: Ollama SSE accumulation ──────────────────────────────────────────


class TestAccumulateOllamaSse:
    def test_basic_accumulation(self):
        resp = _mock_ollama_ndjson_response(lines=[
            '{"response":"Hello","done":false}',
            '{"response":" world","done":false}',
            '{"response":"","done":true}',
        ])
        result = _accumulate_ollama_sse(resp)
        assert result == "Hello world"

    def test_empty_response_field_skipped(self):
        resp = _mock_ollama_ndjson_response(lines=[
            '{"response":"","done":false}',
            '{"response":"data","done":false}',
            '{"response":"","done":true}',
        ])
        result = _accumulate_ollama_sse(resp)
        assert result == "data"

    def test_no_response_key_skipped(self):
        resp = _mock_ollama_ndjson_response(lines=[
            '{"done":false,"model":"test"}',
            '{"response":"content","done":false}',
            '{"done":true}',
        ])
        result = _accumulate_ollama_sse(resp)
        assert result == "content"

    def test_malformed_json_skipped(self):
        resp = _mock_ollama_ndjson_response(lines=[
            "not json",
            '{"response":"good","done":false}',
            '{"response":"","done":true}',
        ])
        result = _accumulate_ollama_sse(resp)
        assert result == "good"

    def test_empty_stream_raises(self):
        resp = _mock_ollama_ndjson_response(lines=[
            '{"response":"","done":true}',
        ])
        with pytest.raises(OllamaError, match="no content"):
            _accumulate_ollama_sse(resp)

    def test_extract_ollama_chunk_helper(self):
        chunks: list[str] = []
        _extract_ollama_chunk('{"response":"hi","done":false}', chunks, {})
        assert chunks == ["hi"]

    def test_extract_ollama_chunk_empty_line(self):
        chunks: list[str] = []
        _extract_ollama_chunk("", chunks, {})
        assert chunks == []

    def test_extract_ollama_chunk_none(self):
        chunks: list[str] = []
        _extract_ollama_chunk(None, chunks, {})
        assert chunks == []


# ── Plan A: OpenAIChatClient.generate with SSE response (stream=false) ────────────────


class TestPlanAOpenaiSseFallback:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sse_response_parsed_when_stream_false(self, mock_post):
        """API returns SSE despite stream:false — should parse transparently."""
        mock_post.return_value = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"streamed response"}}]}',
            "data: [DONE]",
        ])
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "https://api.example.com/v1/chat/completions").generate("prompt").text
        assert result == "streamed response"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sse_response_with_multiple_chunks(self, mock_post):
        mock_post.return_value = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"part1"}}]}',
            'data: {"choices":[{"delta":{"content":" part2"}}]}',
            'data: {"choices":[{"delta":{"content":" part3"}}]}',
            "data: [DONE]",
        ])
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "endpoint").generate("p").text
        assert result == "part1 part2 part3"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_json_response_still_works(self, mock_post):
        """Normal JSON response path must not break."""
        mock_post.return_value = _mock_json_response(
            json_body={"choices": [{"message": {"content": "json response"}}]}
        )
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "endpoint").generate("p").text
        assert result == "json response"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sse_response_payload_still_has_stream_false(self, mock_post):
        """Even with SSE fallback, the request payload should have stream:false."""
        mock_post.return_value = _mock_sse_response()
        cfg = _cfg(stream=False)
        OpenAIChatClient(cfg, "endpoint").generate("p")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["stream"] is False

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sse_debug_log_written(self, mock_post, tmp_path):
        mock_post.return_value = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"logged"}}]}',
            "data: [DONE]",
        ])
        log_path = tmp_path / "debug.jsonl"
        cfg = _cfg(stream=False, debug_log=log_path)
        OpenAIChatClient(cfg, "endpoint").generate("prompt")
        assert log_path.exists()
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert entry["response"] == "logged"
        assert entry["prompt"] == "prompt"


# ── Plan A: call_llm with SSE/NDJSON response (stream=false) ──────────────


class TestPlanAOllamaSseFallback:
    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_ndjson_response_parsed_when_stream_false(self, _mock_guard, mock_post):
        """Ollama returns NDJSON despite stream:false — should parse transparently."""
        mock_post.return_value = _mock_ollama_ndjson_response(lines=[
            '{"response":"ollama streamed","done":false}',
            '{"response":"","done":true}',
        ])
        cfg = _cfg(chat_api_base=None, stream=False)
        result = call_llm("prompt", cfg)
        assert result == "ollama streamed"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_json_response_still_works(self, _mock_guard, mock_post):
        mock_post.return_value = httpx.Response(
            200,
            json={"response": "normal ollama"},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )
        cfg = _cfg(chat_api_base=None, stream=False)
        result = call_llm("prompt", cfg)
        assert result == "normal ollama"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_payload_has_stream_false(self, _mock_guard, mock_post):
        mock_post.return_value = httpx.Response(
            200,
            json={"response": "ok"},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )
        cfg = _cfg(chat_api_base=None, stream=False)
        call_llm("prompt", cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["stream"] is False


# ── Plan B: OpenAIChatClient.generate with active streaming (stream=true) ─────────────


class TestPlanBOpenaiStreaming:
    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_true_uses_httpx_stream(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "https://api.example.com/v1/chat/completions").generate("prompt").text
        assert result == "Hello world"
        mock_stream.assert_called_once()

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_true_does_not_use_post(self, mock_stream):
        """When stream=true, httpx.post should NOT be called."""
        lines = [
            'data: {"choices":[{"delta":{"content":"text"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        with patch("fw_context_mcp.llm.chat_router.httpx.post") as mock_post:
            OpenAIChatClient(cfg, "endpoint").generate("p")
            mock_post.assert_not_called()

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_payload_has_stream_true(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        OpenAIChatClient(cfg, "endpoint").generate("p")
        _, kwargs = mock_stream.call_args
        assert kwargs["json"]["stream"] is True

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_empty_chunks_accumulated(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":""}}]}',
            'data: {"choices":[{"delta":{"content":"real"}}]}',
            'data: {"choices":[{"delta":{}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "endpoint").generate("p").text
        assert result == "real"

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_malformed_lines_skipped(self, mock_stream):
        lines = [
            "data: not json",
            'data: {"choices":[{"delta":{"content":"good"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "endpoint").generate("p").text
        assert result == "good"

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_no_content_raises(self, mock_stream):
        lines = ["data: [DONE]"]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        with pytest.raises(ChatApiError, match="no content chunks"):
            OpenAIChatClient(cfg, "endpoint").generate("p")

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_connect_error_wrapped(self, mock_stream):
        mock_stream.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(stream=True)
        with pytest.raises(ChatApiError, match="Cannot connect"):
            OpenAIChatClient(cfg, "endpoint").generate("p")

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_timeout_wrapped(self, mock_stream):
        mock_stream.side_effect = httpx.TimeoutException("slow")
        cfg = _cfg(stream=True)
        with pytest.raises(ChatApiError, match="timed out"):
            OpenAIChatClient(cfg, "endpoint").generate("p")

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_404_error(self, mock_stream):
        mock_resp = _MockStreamResponse([], status_code=404)
        mock_stream.return_value = mock_resp
        cfg = _cfg(stream=True, model="missing-model")
        with pytest.raises(ChatApiError, match="not found"):
            OpenAIChatClient(cfg, "endpoint").generate("p")

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_debug_log_has_streamed_flag(self, mock_stream, tmp_path):
        lines = [
            'data: {"choices":[{"delta":{"content":"logged"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        log_path = tmp_path / "debug.jsonl"
        cfg = _cfg(stream=True, debug_log=log_path)
        OpenAIChatClient(cfg, "endpoint").generate("prompt")
        assert log_path.exists()
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert entry["response"] == "logged"
        assert entry.get("streamed") is True

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_sends_authorization_header(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True, chat_api_key="sk-secret")
        OpenAIChatClient(cfg, "endpoint").generate("p")
        _, kwargs = mock_stream.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-secret"

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_uses_cfg_timeout(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True, timeout=99.0)
        OpenAIChatClient(cfg, "endpoint").generate("p")
        _, kwargs = mock_stream.call_args
        assert kwargs["timeout"] == 99.0


# ── Plan B: call_llm with active streaming (stream=true) ──────────────────


class TestPlanBOllamaStreaming:
    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_true_uses_httpx_stream(self, _mock_guard, mock_stream):
        lines = [
            '{"response":"Hello","done":false}',
            '{"response":" world","done":false}',
            '{"response":"","done":true}',
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base=None, stream=True)
        result = call_llm("prompt", cfg)
        assert result == "Hello world"

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_true_does_not_use_post(self, _mock_guard, mock_stream):
        lines = [
            '{"response":"text","done":false}',
            '{"response":"","done":true}',
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base=None, stream=True)
        with patch("fw_context_mcp.llm.ollama.httpx.post") as mock_post:
            call_llm("prompt", cfg)
            mock_post.assert_not_called()

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_payload_has_stream_true(self, _mock_guard, mock_stream):
        lines = [
            '{"response":"x","done":false}',
            '{"response":"","done":true}',
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base=None, stream=True)
        call_llm("prompt", cfg)
        _, kwargs = mock_stream.call_args
        assert kwargs["json"]["stream"] is True

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_no_content_raises(self, _mock_guard, mock_stream):
        lines = ['{"response":"","done":true}']
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base=None, stream=True)
        with pytest.raises(OllamaError, match="no content"):
            call_llm("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_debug_log_has_streamed_flag(self, _mock_guard, mock_stream, tmp_path):
        lines = [
            '{"response":"logged","done":false}',
            '{"response":"","done":true}',
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        log_path = tmp_path / "debug.jsonl"
        cfg = _cfg(chat_api_base=None, stream=True, debug_log=log_path)
        call_llm("prompt", cfg)
        assert log_path.exists()
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert entry["response"] == "logged"
        assert entry.get("streamed") is True

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_connect_error_wrapped(self, _mock_guard, mock_stream):
        mock_stream.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(chat_api_base=None, stream=True)
        with pytest.raises(OllamaError, match="Cannot connect"):
            call_llm("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_timeout_wrapped(self, _mock_guard, mock_stream):
        mock_stream.side_effect = httpx.TimeoutException("slow")
        cfg = _cfg(chat_api_base=None, stream=True)
        with pytest.raises(OllamaError, match="timed out"):
            call_llm("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.stream")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_stream_keeps_keep_alive_and_options(self, _mock_guard, mock_stream):
        """Streaming Ollama path should still send keep_alive and options."""
        lines = [
            '{"response":"x","done":false}',
            '{"response":"","done":true}',
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base=None, stream=True)
        call_llm("prompt", cfg)
        _, kwargs = mock_stream.call_args
        assert kwargs["json"]["keep_alive"] == "10m"
        assert "options" in kwargs["json"]
        assert kwargs["json"]["options"]["num_ctx"] == 16384


# ── Plan B: call_llm routes to streaming OpenAI when chat_api_base + stream ─


class TestPlanBRoutingOpenaiStream:
    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_true_routes_to_openai_streaming(self, mock_stream):
        """When chat_api_base is OpenAI-compatible and stream=true,
        call_llm should delegate to OpenAIChatClient.generate which uses httpx.stream."""
        lines = [
            'data: {"choices":[{"delta":{"content":"routed"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base="https://api.deepseek.com/v1", stream=True)
        result = call_llm("prompt", cfg)
        assert result == "routed"
        mock_stream.assert_called_once()

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_stream_true_openai_path_not_using_post(self, mock_stream):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(chat_api_base="https://api.example.com/v1", stream=True)
        with patch("fw_context_mcp.llm.chat_router.httpx.post") as mock_post:
            call_llm("prompt", cfg)
            mock_post.assert_not_called()


# ── Config: stream field parsing ─────────────────────────────────────────────


def _write_global_profiles(tmp_path: Path, monkeypatch, profiles: str) -> Path:
    """Point the global config to a file with ``[llm] profile = "p"`` and *profiles*.

    WHY each test writes its own global config: ``stream`` is a key of a
    profile, and the profiles are in the global config only.  The session
    fixture copies the global config of the operator, and its profiles
    would decide the result.  ``embed_model`` is set, thus ``load`` does
    not probe the GPU or Ollama for the automatic model.
    """
    from fw_context_mcp.config import settings

    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text(
        '[llm]\nprofile = "p"\nenabled = true\nembed_model = "test-embed:1b"\n\n' + profiles, encoding="utf-8",
    )
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    settings._config_cache.clear()
    return path


def _make_stream_project(tmp_path: Path) -> Path:
    """Create an initialized project with an empty local.toml."""
    root = tmp_path / "proj"
    cfg_dir = root / ".fw-context"
    cfg_dir.mkdir(parents=True)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        "[project]\n"
        'id = "abc123"\n\n'
        "[index]\n"
        f'db_dir = "{str(index_dir).replace(chr(92), "/")}"\n'
        'compile_commands = "compile_commands.json"\n',
        encoding="utf-8",
    )
    (cfg_dir / "local.toml").write_text("", encoding="utf-8")
    return root


class TestStreamConfig:
    """``stream`` is a key of an LLM profile: ``load`` copies it from the selected profile."""

    _OLLAMA = '[llm.profiles.p]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "m:1b"\n'

    def test_stream_defaults_to_false(self):
        cfg = LLMConfig()
        assert cfg.stream is False

    def test_stream_can_be_set_true(self):
        cfg = LLMConfig(stream=True)
        assert cfg.stream is True

    def test_stream_parsed_from_toml_true(self, tmp_path, monkeypatch):
        from fw_context_mcp.config import load as load_config

        _write_global_profiles(tmp_path, monkeypatch, self._OLLAMA + "stream = true\n")
        cfg = load_config(project_root=_make_stream_project(tmp_path))
        assert cfg.llm.stream is True

    def test_stream_parsed_from_toml_false(self, tmp_path, monkeypatch):
        from fw_context_mcp.config import load as load_config

        _write_global_profiles(tmp_path, monkeypatch, self._OLLAMA + "stream = false\n")
        cfg = load_config(project_root=_make_stream_project(tmp_path))
        assert cfg.llm.stream is False

    def test_stream_parsed_from_an_openai_compatible_profile(self, tmp_path, monkeypatch):
        """``stream`` is a key of each provider, not only of Ollama."""
        from fw_context_mcp.config import load as load_config

        _write_global_profiles(
            tmp_path, monkeypatch,
            '[llm.profiles.p]\nprovider = "openai_compatible"\nurl = "https://api.example.com/v1"\n'
            'model = "m"\nstream = true\n',
        )
        cfg = load_config(project_root=_make_stream_project(tmp_path))
        assert cfg.llm.stream is True

    def test_stream_not_in_toml_defaults_false(self, tmp_path, monkeypatch):
        from fw_context_mcp.config import load as load_config

        _write_global_profiles(tmp_path, monkeypatch, self._OLLAMA)
        cfg = load_config(project_root=_make_stream_project(tmp_path))
        assert cfg.llm.stream is False


class TestStreamLocalToml:
    """A project file cannot set ``stream``: it is a key of a profile of the global config."""

    @pytest.mark.parametrize("value", ["true", "false"])
    def test_stream_in_local_toml_is_refused(self, tmp_path, value):
        """A moved key gives an error that tells where it goes, not a silent default."""
        from fw_context_mcp.config import ConfigError
        from fw_context_mcp.config import load as load_config

        root = _make_stream_project(tmp_path)
        (root / ".fw-context" / "local.toml").write_text(f"[llm]\nstream = {value}\n", encoding="utf-8")

        with pytest.raises(ConfigError, match=r"\[llm\] stream is a key of an LLM profile now"):
            load_config(project_root=root)

    def test_the_template_offers_no_stream_key(self, tmp_path):
        """The template must not offer a key that the loader refuses: an uncommented line breaks the config."""
        root = tmp_path / "proj"
        cfg_dir = root / ".fw-context"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.toml").write_text('[project]\n# id = ""\n', encoding="utf-8")
        # Create local.toml from template
        _update_local_toml(root, {"embed_model": "x"})
        local = (cfg_dir / "local.toml").read_text(encoding="utf-8")
        assert not re.search(r"^#?\s*stream\s*=", local, re.MULTILINE), local


# ── configure_llm: the stream key of the selected profile ────────────────────


class TestConfigureLlmStream:
    """The test call of ``configure_llm`` uses the ``stream`` key of the profile it selects.

    The tool has no ``stream`` parameter any more: the key is in the
    profile, and the tool never writes it to local.toml.
    """

    _PROFILES = (
        '[llm.profiles.p]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "m:1b"\n\n'
        '[llm.profiles.streaming]\nprovider = "openai_compatible"\nurl = "https://api.example.com/v1"\n'
        'model = "m"\nstream = true\n\n'
        '[llm.profiles.posting]\nprovider = "openai_compatible"\nurl = "https://api.example.com/v1"\n'
        'model = "m"\nstream = false\n'
    )

    def _mock_response(self) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "OK"}}]},
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_a_profile_with_stream_true_streams_the_test_call(self, mock_stream, mock_post, tmp_path, monkeypatch):
        lines = [
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        _write_global_profiles(tmp_path, monkeypatch, self._PROFILES)
        root = _make_stream_project(tmp_path)
        from fw_context_mcp.mcp.handlers.maintenance import configure_llm

        result = configure_llm(project_root=str(root), profile="streaming")

        assert result["status"] == "ok", result
        assert result["stream"] is True
        mock_stream.assert_called_once()
        mock_post.assert_not_called()
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert 'profile = "streaming"' in local
        assert "stream = " not in local  # the key belongs to the profile

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_a_profile_with_stream_false_posts_the_test_call(self, mock_post, mock_stream, tmp_path, monkeypatch):
        mock_post.return_value = self._mock_response()
        _write_global_profiles(tmp_path, monkeypatch, self._PROFILES)
        root = _make_stream_project(tmp_path)
        from fw_context_mcp.mcp.handlers.maintenance import configure_llm

        result = configure_llm(project_root=str(root), profile="posting")

        assert result["status"] == "ok", result
        assert result["stream"] is False
        mock_post.assert_called_once()
        mock_stream.assert_not_called()
        local = (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")
        assert "stream = " not in local  # the key belongs to the profile


# ── Regression: existing non-streaming behavior unchanged ────────────────────


class TestRegressionNonStreaming:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_non_streaming_uses_post(self, mock_post):
        """When stream=False (default), httpx.post is used, not httpx.stream."""
        mock_post.return_value = _mock_json_response()
        cfg = _cfg(stream=False)
        with patch("fw_context_mcp.llm.chat_router.httpx.stream") as mock_stream:
            result = OpenAIChatClient(cfg, "endpoint").generate("p").text
            mock_stream.assert_not_called()
        assert result == "test response"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama.ollama_guard")
    def test_ollama_non_streaming_uses_post(self, _mock_guard, mock_post):
        mock_post.return_value = httpx.Response(
            200,
            json={"response": "ok"},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )
        cfg = _cfg(chat_api_base=None, stream=False)
        with patch("fw_context_mcp.llm.ollama.httpx.stream") as mock_stream:
            result = call_llm("prompt", cfg)
            mock_stream.assert_not_called()
        assert result == "ok"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_non_streaming_existing_tests_still_pass(self, mock_post):
        """Replicate test_payload_has_stream_false to ensure no regression."""
        mock_post.return_value = _mock_json_response()
        cfg = _cfg(stream=False)
        OpenAIChatClient(cfg, "endpoint").generate("p")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["stream"] is False


# ── reasoning_content fallback tests ───────────────────────────────────────
# GLM, DeepSeek-R1, and other reasoning models emit the answer in
# ``reasoning_content`` when ``content`` is empty or truncated.
# These tests verify the fallback logic in _extract_openai_text.


class TestExtractOpenaiText:
    """Unit tests for the _extract_openai_text helper."""

    def test_content_present_returns_content(self):
        msg = {"content": "answer", "reasoning_content": "thinking"}
        assert _extract_openai_text(msg) == "answer"

    def test_content_empty_returns_reasoning(self):
        msg = {"content": "", "reasoning_content": "thinking"}
        assert _extract_openai_text(msg) == "thinking"

    def test_content_absent_returns_reasoning(self):
        msg = {"reasoning_content": "thinking"}
        assert _extract_openai_text(msg) == "thinking"

    def test_both_absent_returns_empty(self):
        msg = {"role": "assistant"}
        assert _extract_openai_text(msg) == ""

    def test_both_empty_returns_empty(self):
        msg = {"content": "", "reasoning_content": ""}
        assert _extract_openai_text(msg) == ""

    def test_content_none_returns_reasoning(self):
        msg = {"content": None, "reasoning_content": "thinking"}
        assert _extract_openai_text(msg) == "thinking"

    def test_reasoning_none_returns_empty(self):
        msg = {"content": None, "reasoning_content": None}
        assert _extract_openai_text(msg) == ""


class TestReasoningContentInSseAccumulation:
    """Plan A SSE fallback: reasoning_content extracted when content is empty."""

    def test_only_reasoning_content_in_sse(self):
        """SSE chunks with only reasoning_content are extracted."""
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"reasoning_content":"step 1"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":" step 2"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "step 1 step 2"

    def test_content_preferred_over_reasoning_in_sse(self):
        """When both content and reasoning_content present, content is used."""
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"content":"answer","reasoning_content":"thinking"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "answer"

    def test_mixed_content_and_reasoning_in_sse(self):
        """Some chunks have content, others have reasoning_content."""
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}',
            'data: {"choices":[{"delta":{"content":"answer"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"more thinking"}}]}',
            'data: {"choices":[{"delta":{"content":" done"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "thinking...answermore thinking done"

    def test_reasoning_only_no_empty_error(self):
        """Pure reasoning_content stream does not raise 'no content chunks'."""
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"reasoning_content":"reasoning"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "reasoning"

    def test_empty_reasoning_skipped(self):
        """Empty reasoning_content strings are skipped."""
        resp = _mock_sse_response(sse_lines=[
            'data: {"choices":[{"delta":{"reasoning_content":""}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"text"}}]}',
            "data: [DONE]",
        ])
        result = _accumulate_openai_sse(resp)
        assert result == "text"


class TestReasoningContentInPlanBStreaming:
    """Plan B active streaming: reasoning_content fallback in OpenAIChatClient._stream."""

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_streaming_reasoning_only(self, mock_stream):
        """Streaming with only reasoning_content chunks returns text."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":" more"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "https://api.example.com/v1/chat/completions").generate("prompt").text
        assert result == "thinking more"

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_streaming_content_preferred_over_reasoning(self, mock_stream):
        """Streaming: content takes priority over reasoning_content."""
        lines = [
            'data: {"choices":[{"delta":{"content":"answer","reasoning_content":"ignore"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "https://api.example.com/v1/chat/completions").generate("prompt").text
        assert result == "answer"

    @patch("fw_context_mcp.llm.chat_router.httpx.stream")
    def test_streaming_reasoning_then_content(self, mock_stream):
        """Realistic GLM pattern: reasoning_content chunks followed by content."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"Let me calculate..."}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"2+3=5"}}]}',
            'data: {"choices":[{"delta":{"content":"5"}}]}',
            "data: [DONE]",
        ]
        mock_stream.side_effect = _mock_httpx_stream(lines)
        cfg = _cfg(stream=True)
        result = OpenAIChatClient(cfg, "https://api.example.com/v1/chat/completions").generate("prompt").text
        assert "5" in result
        assert "Let me calculate" in result


class TestReasoningContentInNonStreaming:
    """Non-streaming JSON path: reasoning_content fallback."""

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_non_streaming_reasoning_fallback(self, mock_post):
        """Non-streaming: empty content falls back to reasoning_content."""
        mock_post.return_value = _mock_json_response(json_body={
            "choices": [{"message": {"content": "", "reasoning_content": "the answer"}}]
        })
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "endpoint").generate("prompt").text
        assert result == "the answer"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_non_streaming_content_preferred(self, mock_post):
        """Non-streaming: content preferred over reasoning_content."""
        mock_post.return_value = _mock_json_response(json_body={
            "choices": [{"message": {"content": "final", "reasoning_content": "draft"}}]
        })
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "endpoint").generate("prompt").text
        assert result == "final"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_non_streaming_content_absent_reasoning_present(self, mock_post):
        """Non-streaming: no content key at all, reasoning_content used."""
        mock_post.return_value = _mock_json_response(json_body={
            "choices": [{"message": {"reasoning_content": "only reasoning"}}]
        })
        cfg = _cfg(stream=False)
        result = OpenAIChatClient(cfg, "endpoint").generate("prompt").text
        assert result == "only reasoning"
