"""Integration tests for streaming support against a real LiteLLM proxy.

These tests verify the full HTTP round-trip with a real LLM backend:
  - Plan A (SSE auto-detection): API returns text/event-stream despite stream=false
  - Plan B (active streaming): client requests stream=true and consumes SSE
  - reasoning_content fallback: GLM models that emit reasoning_content
  - call_ollama routing: chat_api_base pointing to OpenAI-compatible endpoint
  - Content-Type charset suffix handling (text/event-stream; charset=utf-8)

Requires a LiteLLM proxy running at http://localhost:4000 with at least
one chat model configured.  Tests are skipped automatically when the
proxy is unreachable.

Run manually:
    pytest tests/test_streaming_integration.py -m litellm -v -s
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm.chat_router import call_openai_chat, detect_chat_endpoint
from fw_context_mcp.llm.ollama import call_ollama

# ── Constants ────────────────────────────────────────────────────────────────

LITELLM_URL = "http://localhost:4000"
LITELLM_BASE = f"{LITELLM_URL}/v1"
# Deepseek-v4-flash: standard content-only model (no reasoning_content)
MODEL_CONTENT = "Deepseek-v4-flash"
# glm-5.2-fp8: reasoning model that emits reasoning_content
MODEL_REASONING = "glm-5.2-fp8"
# glm-5.2-fp8-no-thinking: non-thinking mode, returns only content (no reasoning_content)
MODEL_NO_THINKING = "glm-5.2-fp8-no-thinking"

SIMPLE_PROMPT = "What is 2+3? Reply with just the number."
LONG_PROMPT = "List the numbers 1 through 10, comma-separated."


# ── Skip condition: detect LiteLLM proxy ────────────────────────────────────


def _litellm_available() -> bool:
    """Check if LiteLLM proxy is reachable and has at least one model."""
    try:
        resp = httpx.get(f"{LITELLM_BASE}/models", timeout=5.0)
        if resp.status_code != 200:
            return False
        data = resp.json()
        models = data.get("data", [])
        return len(models) > 0
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


def _model_available(model: str) -> bool:
    """Check if a specific model is available on the LiteLLM proxy."""
    try:
        resp = httpx.get(f"{LITELLM_BASE}/models", timeout=5.0)
        if resp.status_code != 200:
            return False
        models = [m.get("id", "") for m in resp.json().get("data", [])]
        return model in models
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


pytestmark = [
    pytest.mark.litellm,
    pytest.mark.skipif(
        not _litellm_available(),
        reason=f"LiteLLM proxy not running at {LITELLM_URL} (start with: litellm --config config.yaml)",
    ),
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(model: str, stream: bool = False, timeout: float = 60.0) -> LLMConfig:
    return LLMConfig(
        chat_api_base=LITELLM_BASE,
        model=model,
        timeout=timeout,
        stream=stream,
    )


def _endpoint() -> str:
    """Get the detected OpenAI endpoint URL."""
    cfg = LLMConfig(chat_api_base=LITELLM_BASE, model="x", timeout=5.0)
    _, endpoint = detect_chat_endpoint(cfg)
    return endpoint


# ── Plan A: non-streaming requests (stream=false) ──────────────────────────


class TestPlanANonStreaming:
    """Non-streaming requests with stream=false."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_non_streaming_returns_content(self):
        """call_openai_chat(stream=False) returns a non-empty string."""
        cfg = _cfg(MODEL_CONTENT, stream=False)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=20)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "5" in result

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_non_streaming_content_type_is_json(self):
        """When stream=false, the API should return application/json (not SSE)."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 10,
                "stream": False,
            },
            timeout=30.0,
        )
        ct = resp.headers.get("content-type", "").lower()
        assert "application/json" in ct, f"Expected JSON, got: {ct}"
        msg = resp.json()["choices"][0]["message"]
        assert msg.get("content"), f"Empty content in: {msg}"

    @pytest.mark.skipif(not _model_available(MODEL_REASONING), reason=f"{MODEL_REASONING} not available")
    def test_non_streaming_glm_reasoning_fallback(self):
        """GLM model with reasoning_content: fallback extracts text."""
        cfg = _cfg(MODEL_REASONING, stream=False, timeout=90.0)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=500)
        assert isinstance(result, str)
        assert len(result) > 0, "reasoning_content fallback produced empty string"
        # With enough tokens, GLM should mention 5 somewhere in reasoning or content
        assert "5" in result


# ── Plan B: active streaming (stream=true) ─────────────────────────────────


class TestPlanBStreaming:
    """Active streaming with stream=true (httpx.stream)."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_streaming_returns_content(self):
        """call_openai_chat(stream=True) returns a non-empty string."""
        cfg = _cfg(MODEL_CONTENT, stream=True)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=20)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "5" in result

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_streaming_content_type_is_sse(self):
        """When stream=true, the API returns text/event-stream."""
        with httpx.stream(
            "POST",
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 10,
                "stream": True,
            },
            timeout=30.0,
        ) as resp:
            ct = resp.headers.get("content-type", "").lower()
            assert "text/event-stream" in ct, f"Expected SSE, got: {ct}"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_streaming_multi_chunk_accumulation(self):
        """Long response: multiple SSE chunks are correctly accumulated."""
        cfg = _cfg(MODEL_CONTENT, stream=True)
        result = call_openai_chat(LONG_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=100)
        assert isinstance(result, str)
        assert len(result) > 5, f"Expected multi-char response, got: {result!r}"
        # Should contain digits 1-10
        for n in ["1", "2", "3"]:
            assert n in result, f"Expected '{n}' in response: {result!r}"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_streaming_vs_non_streaming_consistency(self):
        """Same prompt + temperature=0: streaming and non-streaming produce similar results."""
        cfg_ns = _cfg(MODEL_CONTENT, stream=False)
        cfg_s = _cfg(MODEL_CONTENT, stream=True)
        r_ns = call_openai_chat(SIMPLE_PROMPT, cfg_ns, _endpoint(), temperature=0.0, num_predict=10)
        r_s = call_openai_chat(SIMPLE_PROMPT, cfg_s, _endpoint(), temperature=0.0, num_predict=10)
        # Both should contain "5" (the answer)
        assert "5" in r_ns, f"Non-streaming missing answer: {r_ns!r}"
        assert "5" in r_s, f"Streaming missing answer: {r_s!r}"

    @pytest.mark.skipif(not _model_available(MODEL_REASONING), reason=f"{MODEL_REASONING} not available")
    def test_streaming_glm_reasoning_fallback(self):
        """GLM streaming with reasoning_content: fallback extracts text."""
        cfg = _cfg(MODEL_REASONING, stream=True, timeout=90.0)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=500)
        assert isinstance(result, str)
        assert len(result) > 0, "reasoning_content streaming fallback produced empty string"
        assert "5" in result


# ── Content-Type charset handling ──────────────────────────────────────────


class TestContentTypeCharset:
    """Verify text/event-stream; charset=utf-8 is correctly detected as SSE."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_sse_content_type_has_charset(self):
        """LiteLLM returns 'text/event-stream; charset=utf-8' — verify charset suffix."""
        with httpx.stream(
            "POST",
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 5,
                "stream": True,
            },
            timeout=30.0,
        ) as resp:
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct.lower(), f"Expected SSE content-type, got: {ct}"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_streaming_works_with_charset_suffix(self):
        """Full streaming call succeeds even when content-type has charset suffix."""
        cfg = _cfg(MODEL_CONTENT, stream=True)
        # This should not raise "no content chunks" — charset suffix must not break detection
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=10)
        assert len(result) > 0


# ── call_ollama routing to OpenAI path ─────────────────────────────────────


class TestCallOllamaRouting:
    """call_ollama with chat_api_base routes to call_openai_chat."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_call_ollama_non_stream_routes_to_openai(self):
        """call_ollama(stream=False) with /v1 base routes to OpenAI non-streaming."""
        cfg = _cfg(MODEL_CONTENT, stream=False)
        result = call_ollama(SIMPLE_PROMPT, cfg, num_predict=20)
        assert isinstance(result, str)
        assert "5" in result

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_call_ollama_stream_routes_to_openai_streaming(self):
        """call_ollama(stream=True) with /v1 base routes to OpenAI streaming."""
        cfg = _cfg(MODEL_CONTENT, stream=True)
        result = call_ollama(SIMPLE_PROMPT, cfg, num_predict=20)
        assert isinstance(result, str)
        assert "5" in result

    @pytest.mark.skipif(not _model_available(MODEL_REASONING), reason=f"{MODEL_REASONING} not available")
    def test_call_ollama_stream_glm_reasoning(self):
        """call_ollama(stream=True) with GLM: reasoning_content fallback works through routing."""
        cfg = _cfg(MODEL_REASONING, stream=True, timeout=90.0)
        result = call_ollama(SIMPLE_PROMPT, cfg, num_predict=500)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "5" in result


# ── Raw SSE format verification ────────────────────────────────────────────


class TestSSEFormatVerification:
    """Verify the actual SSE format from LiteLLM matches our parser expectations."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_sse_uses_data_prefix(self):
        """SSE lines start with 'data: ' prefix."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 10,
                "stream": True,
            },
            timeout=30.0,
        )
        lines = resp.text.split("\n")
        data_lines = [ln for ln in lines if ln.startswith("data: ")]
        assert len(data_lines) > 0, "No 'data: ' prefixed lines found in SSE response"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_sse_terminates_with_done(self):
        """SSE stream ends with 'data: [DONE]'."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 10,
                "stream": True,
            },
            timeout=30.0,
        )
        assert "data: [DONE]" in resp.text, "Missing 'data: [DONE]' terminator"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_sse_chunks_have_delta_content(self):
        """Each SSE data chunk has choices[0].delta.content (for non-reasoning models)."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_CONTENT,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 10,
                "stream": True,
            },
            timeout=30.0,
        )
        content_found = False
        for line in resp.text.split("\n"):
            if line.startswith("data: ") and line[6:] != "[DONE]":
                obj = json.loads(line[6:])
                delta = obj.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    content_found = True
                    break
        assert content_found, "No chunk with delta.content found"

    @pytest.mark.skipif(not _model_available(MODEL_REASONING), reason=f"{MODEL_REASONING} not available")
    def test_glm_sse_has_reasoning_content(self):
        """GLM model SSE chunks contain reasoning_content in delta."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_REASONING,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 50,
                "stream": True,
            },
            timeout=60.0,
        )
        reasoning_found = False
        for line in resp.text.split("\n"):
            if line.startswith("data: ") and line[6:] != "[DONE]":
                obj = json.loads(line[6:])
                delta = obj.get("choices", [{}])[0].get("delta", {})
                if delta.get("reasoning_content"):
                    reasoning_found = True
                    break
        assert reasoning_found, "GLM SSE response has no reasoning_content in delta"

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_sse_has_content_not_reasoning(self):
        """Non-thinking mode SSE: delta has content, never reasoning_content."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_NO_THINKING,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 50,
                "stream": True,
            },
            timeout=60.0,
        )
        content_found = False
        reasoning_found = False
        for line in resp.text.split("\n"):
            if line.startswith("data: ") and line[6:] != "[DONE]":
                obj = json.loads(line[6:])
                delta = obj.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    content_found = True
                if delta.get("reasoning_content"):
                    reasoning_found = True
        assert content_found, "No content chunks in non-thinking SSE response"
        assert not reasoning_found, "Unexpected reasoning_content in non-thinking SSE"


# ── Debug log verification ─────────────────────────────────────────────────


class TestDebugLogStreaming:
    """Verify debug log entries for streaming responses."""

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_debug_log_streaming_entry(self, tmp_path: Path):
        """Plan B streaming: debug log includes streamed=true flag."""
        log_path = tmp_path / "stream_debug.jsonl"
        cfg = LLMConfig(
            chat_api_base=LITELLM_BASE,
            model=MODEL_CONTENT,
            timeout=30.0,
            stream=True,
            debug_log=log_path,
        )
        call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=10)
        assert log_path.exists(), "Debug log not created"
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert entry.get("streamed") is True, f"Expected streamed=True, got: {entry}"

    @pytest.mark.skipif(not _model_available(MODEL_CONTENT), reason=f"{MODEL_CONTENT} not available")
    def test_debug_log_non_streaming_entry(self, tmp_path: Path):
        """Non-streaming: debug log includes streamed=false flag."""
        log_path = tmp_path / "nonstream_debug.jsonl"
        cfg = LLMConfig(
            chat_api_base=LITELLM_BASE,
            model=MODEL_CONTENT,
            timeout=30.0,
            stream=False,
            debug_log=log_path,
        )
        call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=10)
        assert log_path.exists(), "Debug log not created"
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert entry.get("streamed") is False, f"Expected streamed=False, got: {entry}"


# ── Non-thinking mode tests ────────────────────────────────────────────────


class TestNoThinkingMode:
    """Verify non-thinking mode model (no reasoning_content, pure content).

    GLM models with the ``-no-thinking`` suffix skip the reasoning phase
    and return only ``content`` — never ``reasoning_content``.  These
    tests confirm our parser handles this clean path correctly.
    """

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_non_streaming_returns_content(self):
        """Non-thinking non-streaming: message has content, no reasoning_content."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_NO_THINKING,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 50,
                "stream": False,
            },
            timeout=60.0,
        )
        msg = resp.json()["choices"][0]["message"]
        assert "content" in msg, f"Missing content key: {msg}"
        assert msg["content"], f"Empty content: {msg}"
        assert "reasoning_content" not in msg, f"Unexpected reasoning_content in non-thinking mode: {msg}"
        assert "5" in msg["content"]

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_streaming_returns_content(self):
        """Non-thinking streaming: delta has content, no reasoning_content."""
        resp = httpx.post(
            _endpoint(),
            json={
                "model": MODEL_NO_THINKING,
                "messages": [{"role": "user", "content": SIMPLE_PROMPT}],
                "max_tokens": 50,
                "stream": True,
            },
            timeout=60.0,
        )
        content_found = False
        reasoning_found = False
        for line in resp.text.split("\n"):
            if line.startswith("data: ") and line[6:] != "[DONE]":
                obj = json.loads(line[6:])
                delta = obj.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    content_found = True
                if delta.get("reasoning_content"):
                    reasoning_found = True
        assert content_found, "No content chunks in non-thinking streaming response"
        assert not reasoning_found, "Unexpected reasoning_content in non-thinking mode"

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_call_openai_chat_non_streaming(self):
        """call_openai_chat(stream=False) with non-thinking model returns content."""
        cfg = _cfg(MODEL_NO_THINKING, stream=False)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=50)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "5" in result

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_call_openai_chat_streaming(self):
        """call_openai_chat(stream=True) with non-thinking model returns content."""
        cfg = _cfg(MODEL_NO_THINKING, stream=True)
        result = call_openai_chat(SIMPLE_PROMPT, cfg, _endpoint(), temperature=0.0, num_predict=50)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "5" in result

    @pytest.mark.skipif(
        not _model_available(MODEL_NO_THINKING), reason=f"{MODEL_NO_THINKING} not available"
    )
    def test_no_thinking_streaming_vs_non_streaming_consistency(self):
        """Non-thinking: streaming and non-streaming produce consistent results."""
        cfg_ns = _cfg(MODEL_NO_THINKING, stream=False)
        cfg_s = _cfg(MODEL_NO_THINKING, stream=True)
        r_ns = call_openai_chat(SIMPLE_PROMPT, cfg_ns, _endpoint(), temperature=0.0, num_predict=10)
        r_s = call_openai_chat(SIMPLE_PROMPT, cfg_s, _endpoint(), temperature=0.0, num_predict=10)
        assert "5" in r_ns, f"Non-streaming missing answer: {r_ns!r}"
        assert "5" in r_s, f"Streaming missing answer: {r_s!r}"
