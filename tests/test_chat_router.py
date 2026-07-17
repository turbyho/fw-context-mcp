"""Tests for the chat router: endpoint detection and OpenAI-compatible client.

Covers:
- detect_chat_endpoint() URL auto-detection (all priority rules)
- call_openai_chat() request format, response parsing, error handling
- Explicit format override
- Edge cases (trailing slashes, uppercase, ports, IPv6)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm.chat_router import call_openai_chat, detect_chat_endpoint
from fw_context_mcp.llm.ollama import OllamaError

# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(
    chat_api_base: str = "https://api.example.com",
    chat_api_format: str = "auto",
    chat_api_key: str | None = None,
    model: str = "test-model",
    timeout: float = 30.0,
    debug_log: Path | None = None,
) -> LLMConfig:
    return LLMConfig(
        chat_api_base=chat_api_base,
        chat_api_format=chat_api_format,
        chat_api_key=chat_api_key,
        model=model,
        timeout=timeout,
        debug_log=debug_log,
    )


def _mock_response(
    status_code: int = 200,
    json_body: dict | None = None,
    text: str | None = None,
) -> httpx.Response:
    if json_body is None:
        json_body = {"choices": [{"message": {"content": "test response"}}]}
    request = httpx.Request("POST", "https://example.com")
    kwargs: dict = {"status_code": status_code, "json": json_body, "request": request}
    if text is not None:
        kwargs.pop("json")
        kwargs["text"] = text
    return httpx.Response(**kwargs)


# ── detect_chat_endpoint: explicit format override ──────────────────────────


class TestDetectChatEndpointExplicitFormat:
    def test_explicit_ollama_bare_url_appends_api_generate(self):
        cfg = _cfg("http://host:11434", chat_api_format="ollama")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"

    def test_explicit_ollama_already_has_api_generate_no_double(self):
        cfg = _cfg("http://host:11434/api/generate", chat_api_format="ollama")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"

    def test_explicit_ollama_trailing_slash_normalized(self):
        cfg = _cfg("http://host:11434/", chat_api_format="ollama")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"

    def test_explicit_ollama_with_custom_path(self):
        cfg = _cfg("http://host:8080/llm", chat_api_format="ollama")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:8080/llm/api/generate"

    def test_explicit_openai_bare_url_appends_v1_chat_completions(self):
        cfg = _cfg("https://api.openai.com", chat_api_format="openai")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_explicit_openai_already_has_v1_appends_chat_completions(self):
        cfg = _cfg("https://host/v1", chat_api_format="openai")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://host/v1/chat/completions"

    def test_explicit_openai_already_has_chat_completions_no_double(self):
        cfg = _cfg("https://host/v1/chat/completions", chat_api_format="openai")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://host/v1/chat/completions"

    def test_explicit_format_case_insensitive(self):
        for val in ("OpenAI", "OLLAMA", "Auto", "OPENAI"):
            cfg = _cfg("http://host:4000", chat_api_format=val)
            fmt, _ = detect_chat_endpoint(cfg)
            assert fmt in ("ollama", "openai"), f"Failed for format={val}"

    def test_invalid_format_value_raises(self):
        cfg = _cfg("http://host:4000", chat_api_format="claude")
        with pytest.raises(ValueError, match="Invalid chat_api_format"):
            detect_chat_endpoint(cfg)


# ── detect_chat_endpoint: auto-detection ─────────────────────────────────────


class TestDetectChatEndpointAutoOllama:
    def test_auto_port_11434_appends_api_generate(self):
        cfg = _cfg("http://host:11434")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"

    def test_auto_port_11434_with_path(self):
        cfg = _cfg("http://host:11434/extra")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/extra/api/generate"

    def test_auto_ends_with_api_appends_generate(self):
        cfg = _cfg("http://host:9000/api")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:9000/api/generate"

    def test_auto_ends_with_api_generate_as_is(self):
        cfg = _cfg("http://host:9000/api/generate")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:9000/api/generate"

    def test_auto_ipv4_loopback_11434(self):
        cfg = _cfg("http://127.0.0.1:11434")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://127.0.0.1:11434/api/generate"

    def test_auto_ipv6_loopback_11434(self):
        cfg = _cfg("http://[::1]:11434")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://[::1]:11434/api/generate"

    def test_auto_port_11434_api_suffix(self):
        """http://host:11434/api should end with /api -> append /generate, not /api/generate."""
        cfg = _cfg("http://host:11434/api")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"


class TestDetectChatEndpointAutoOpenAI:
    def test_auto_contains_v1_appends_chat_completions(self):
        cfg = _cfg("https://host/v1")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://host/v1/chat/completions"

    def test_auto_bare_host_appends_v1_chat_completions(self):
        cfg = _cfg("https://api.example.com")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://api.example.com/v1/chat/completions"

    def test_auto_bare_host_with_port(self):
        cfg = _cfg("http://host:8000")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://host:8000/v1/chat/completions"

    def test_auto_litellm_bare_host(self):
        cfg = _cfg("http://localhost:4000")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://localhost:4000/v1/chat/completions"

    def test_auto_ends_with_chat_completions_as_is(self):
        cfg = _cfg("https://host/v1/chat/completions")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://host/v1/chat/completions"

    def test_auto_deepseek_with_v1(self):
        cfg = _cfg("https://api.deepseek.com/v1")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://api.deepseek.com/v1/chat/completions"

    def test_auto_deepseek_without_v1(self):
        cfg = _cfg("https://api.deepseek.com")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://api.deepseek.com/v1/chat/completions"


# ── detect_chat_endpoint: priority rules ─────────────────────────────────────


class TestDetectChatEndpointPriority:
    def test_chat_completions_beats_port_11434(self):
        """URL ending /chat/completions takes priority over :11434."""
        cfg = _cfg("http://host:11434/chat/completions")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://host:11434/chat/completions"

    def test_api_generate_beats_v1(self):
        """URL ending /api/generate takes priority over containing /v1."""
        cfg = _cfg("http://host/v1/api/generate")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host/v1/api/generate"

    def test_v1_beats_ends_with_api(self):
        """URL containing /v1 takes priority over ending /api."""
        cfg = _cfg("http://host/v1/api")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://host/v1/api/chat/completions"

    def test_ends_with_api_beats_port_11434(self):
        """URL ending /api takes priority over containing :11434."""
        cfg = _cfg("http://host:11434/api")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"
        assert url == "http://host:11434/api/generate"


# ── detect_chat_endpoint: edge cases ─────────────────────────────────────────


class TestDetectChatEndpointEdgeCases:
    def test_empty_string_raises(self):
        cfg = _cfg("")
        with pytest.raises(ValueError, match="chat_api_base is empty"):
            detect_chat_endpoint(cfg)

    def test_trailing_slash_on_bare_host(self):
        cfg = _cfg("https://host/")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "https://host/v1/chat/completions"

    def test_uppercase_scheme(self):
        cfg = _cfg("HTTP://host:11434")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "ollama"

    def test_https_no_port(self):
        cfg = _cfg("https://api.openai.com")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"

    def test_lm_studio_default_port(self):
        cfg = _cfg("http://localhost:1234/v1")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://localhost:1234/v1/chat/completions"

    def test_llama_cpp_default_port(self):
        cfg = _cfg("http://localhost:8080/v1")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://localhost:8080/v1/chat/completions"

    def test_ollama_openai_compat_with_v1(self):
        """Ollama's own OpenAI layer at :11434/v1 -> openai format."""
        cfg = _cfg("http://localhost:11434/v1")
        fmt, url = detect_chat_endpoint(cfg)
        assert fmt == "openai"
        assert url == "http://localhost:11434/v1/chat/completions"


# ── call_openai_chat: success cases ──────────────────────────────────────────


class TestCallOpenaiChatSuccess:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_returns_response_content(self, mock_post):
        mock_post.return_value = _mock_response(json_body={"choices": [{"message": {"content": "hello world"}}]})
        cfg = _cfg("https://api.example.com/v1", chat_api_key="sk-test")
        result = call_openai_chat("test prompt", cfg, "https://api.example.com/v1/chat/completions")
        assert result == "hello world"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sends_content_type_header(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "https://api.example.com/v1/chat/completions")
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Content-Type"] == "application/json"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_sends_authorization_when_key_set(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1", chat_api_key="sk-secret")
        call_openai_chat("p", cfg, "https://api.example.com/v1/chat/completions")
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-secret"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_authorization_when_key_none(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1", chat_api_key=None)
        call_openai_chat("p", cfg, "https://api.example.com/v1/chat/completions")
        _, kwargs = mock_post.call_args
        assert "Authorization" not in kwargs["headers"]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_has_model_and_messages(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1", model="gpt-4o")
        call_openai_chat("hello", cfg, "https://api.example.com/v1/chat/completions")
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["model"] == "gpt-4o"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_has_stream_false(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "https://api.example.com/v1/chat/completions")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["stream"] is False

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_has_temperature(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint", temperature=0.7)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["temperature"] == 0.7

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_has_max_tokens_when_num_predict_set(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint", num_predict=500)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["max_tokens"] == 500

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_no_max_tokens_when_num_predict_none(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint", num_predict=None)
        _, kwargs = mock_post.call_args
        assert "max_tokens" not in kwargs["json"]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_excludes_keep_alive(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint")
        _, kwargs = mock_post.call_args
        assert "keep_alive" not in kwargs["json"]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_payload_excludes_num_ctx_and_options(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint")
        _, kwargs = mock_post.call_args
        assert "options" not in kwargs["json"]
        assert "num_ctx" not in kwargs["json"]

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_uses_cfg_timeout(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1", timeout=42.0)
        call_openai_chat("p", cfg, "endpoint")
        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 42.0

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_posts_to_detected_endpoint(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        endpoint = "https://api.example.com/v1/chat/completions"
        call_openai_chat("p", cfg, endpoint)
        args, _ = mock_post.call_args
        assert args[0] == endpoint

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_temperature_default_zero(self, mock_post):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1")
        call_openai_chat("p", cfg, "endpoint")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["temperature"] == 0.0


# ── call_openai_chat: error handling ─────────────────────────────────────────


class TestCallOpenaiChatErrors:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_connect_error_wrapped_as_ollama_error(self, mock_post):
        mock_post.side_effect = httpx.ConnectError("refused")
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="Cannot connect"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_timeout_wrapped_as_ollama_error(self, mock_post):
        mock_post.side_effect = httpx.TimeoutException("slow")
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="timed out"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_404_raises_ollama_error_no_pull(self, mock_post):
        mock_post.return_value = _mock_response(status_code=404, json_body={"error": "not found"}, text="not found")
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="not found"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_500_raises_ollama_error(self, mock_post):
        mock_post.return_value = _mock_response(
            status_code=500, json_body={"error": "server error"}, text="server error"
        )
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="500"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_missing_choices_key_raises(self, mock_post):
        mock_post.return_value = _mock_response(json_body={})
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="Unexpected chat API response format"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_empty_choices_list_raises(self, mock_post):
        mock_post.return_value = _mock_response(json_body={"choices": []})
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="Unexpected chat API response format"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_missing_message_key_raises(self, mock_post):
        mock_post.return_value = _mock_response(json_body={"choices": [{}]})
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="Unexpected chat API response format"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_missing_content_key_raises(self, mock_post):
        mock_post.return_value = _mock_response(json_body={"choices": [{"message": {}}]})
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="Unexpected chat API response format"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_ollama_error_passes_through(self, mock_post):
        err = OllamaError("pre-existing")
        mock_post.side_effect = err
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="pre-existing"):
            call_openai_chat("p", cfg, "endpoint")

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_generic_exception_wrapped(self, mock_post):
        mock_post.side_effect = RuntimeError("boom")
        cfg = _cfg("https://api.example.com/v1")
        with pytest.raises(OllamaError, match="boom"):
            call_openai_chat("p", cfg, "endpoint")


# ── call_openai_chat: debug log ──────────────────────────────────────────────


class TestCallOpenaiChatDebugLog:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_debug_log_written_when_set(self, mock_post, tmp_path):
        mock_post.return_value = _mock_response()
        log_path = tmp_path / "debug.jsonl"
        cfg = _cfg("https://api.example.com/v1", debug_log=log_path)
        call_openai_chat("test prompt", cfg, "endpoint")
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["prompt"] == "test prompt"
        assert entry["response"] == "test response"
        assert "ts" in entry
        assert "model" in entry
        assert "latency_s" in entry
        assert "endpoint" in entry

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_no_debug_log_when_none(self, mock_post, tmp_path):
        mock_post.return_value = _mock_response()
        cfg = _cfg("https://api.example.com/v1", debug_log=None)
        call_openai_chat("p", cfg, "endpoint")
        # No assertion needed — just ensure no exception. File should not exist.
        assert not (tmp_path / "debug.jsonl").exists()
