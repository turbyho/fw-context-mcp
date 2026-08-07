"""Tests for the Ollama client: routing, auto_pull, check_setup, and backward compat.

Covers:
- call_ollama() routing to OpenAI when chat_api_base is set
- call_ollama() backward compat when chat_api_base is None
- auto_pull switch (both true and false, for chat and embed)
- check_setup() all status combinations
- Regression: existing behavior must not change when chat_api_base is None
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm.ollama import (
    OllamaError,
    OllamaModelNotFoundError,
    _parse_parameter_size,
    call_ollama,
    call_ollama_embed,
    check_setup,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(
    chat_api_base: str | None = None,
    chat_api_key: str | None = None,
    chat_api_format: str = "auto",
    auto_pull: bool = False,
    model: str = "test-model",
    embed_model: str = "test-embed",
    ollama_url: str = "http://localhost:11434",
    timeout: float = 30.0,
    debug_log: Path | None = None,
    num_ctx: int = 16384,
    keep_alive: str = "10m",
) -> LLMConfig:
    return LLMConfig(
        chat_api_base=chat_api_base,
        chat_api_key=chat_api_key,
        chat_api_format=chat_api_format,
        auto_pull=auto_pull,
        model=model,
        embed_model=embed_model,
        ollama_url=ollama_url,
        timeout=timeout,
        debug_log=debug_log,
        num_ctx=num_ctx,
        keep_alive=keep_alive,
    )


def _mock_post_response(
    status_code: int = 200,
    json_body: dict | None = None,
    text: str | None = None,
) -> httpx.Response:
    if json_body is None:
        json_body = {"response": "ollama response"}
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    kwargs: dict = {"status_code": status_code, "json": json_body, "request": request}
    if text is not None:
        kwargs.pop("json")
        kwargs["text"] = text
    return httpx.Response(**kwargs)


def _mock_get_response(
    status_code: int = 200,
    json_body: dict | None = None,
) -> httpx.Response:
    if json_body is None:
        json_body = {"models": []}
    request = httpx.Request("GET", "http://localhost:11434/api/tags")
    return httpx.Response(status_code=status_code, json=json_body, request=request)


def _mock_embed_response(embeddings: list[list[float]] | None = None) -> httpx.Response:
    if embeddings is None:
        embeddings = [[0.1, 0.2, 0.3]]
    return httpx.Response(
        status_code=200,
        json={"embeddings": embeddings},
        request=httpx.Request("POST", "http://localhost:11434/api/embed"),
    )


# ── call_ollama: backward compatibility (no chat_api_base) ───────────────────


class TestCallOllamaBackwardCompat:
    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_none_chat_api_base_uses_ollama_url(self, mock_post):
        mock_post.return_value = _mock_post_response()
        cfg = _cfg(chat_api_base=None)
        call_ollama("prompt", cfg)
        args, _ = mock_post.call_args
        assert args[0] == "http://localhost:11434/api/generate"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_sends_keep_alive_in_payload(self, mock_post):
        mock_post.return_value = _mock_post_response()
        cfg = _cfg(chat_api_base=None, keep_alive="5m")
        call_ollama("prompt", cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["keep_alive"] == "5m"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_sends_num_ctx_in_options(self, mock_post):
        mock_post.return_value = _mock_post_response()
        cfg = _cfg(chat_api_base=None, num_ctx=8192)
        call_ollama("prompt", cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["options"]["num_ctx"] == 8192

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_returns_response_field(self, mock_post):
        mock_post.return_value = _mock_post_response(json_body={"response": "generated text"})
        cfg = _cfg(chat_api_base=None)
        result = call_ollama("prompt", cfg)
        assert result == "generated text"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_connect_error_message_mentions_ollama(self, mock_post):
        mock_post.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(chat_api_base=None)
        with pytest.raises(OllamaError, match="Ollama"):
            call_ollama("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_timeout_raises_ollama_error(self, mock_post):
        mock_post.side_effect = httpx.TimeoutException("slow")
        cfg = _cfg(chat_api_base=None)
        with pytest.raises(OllamaError, match="timed out"):
            call_ollama("prompt", cfg)


# ── call_ollama: routing to OpenAI ───────────────────────────────────────────


class TestCallOllamaRouting:
    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_chat_api_base_openai_delegates_to_openai(self, mock_openai_post):
        mock_openai_post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "openai result"}}]},
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )
        cfg = _cfg(chat_api_base="https://api.example.com/v1", chat_api_key="sk-test")
        result = call_ollama("prompt", cfg)
        assert result == "openai result"
        mock_openai_post.assert_called_once()

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_chat_api_base_ollama_uses_detected_endpoint(self, mock_post):
        """Remote Ollama via chat_api_base uses the detected endpoint, not cfg.ollama_url."""
        mock_post.return_value = _mock_post_response()
        cfg = _cfg(
            chat_api_base="http://remote:11434",
            ollama_url="http://localhost:11434",
        )
        call_ollama("prompt", cfg)
        args, _ = mock_post.call_args
        assert args[0] == "http://remote:11434/api/generate"

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_chat_api_base_ollama_preserves_keep_alive(self, mock_post):
        mock_post.return_value = _mock_post_response()
        cfg = _cfg(chat_api_base="http://remote:11434", keep_alive="5m")
        call_ollama("prompt", cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["keep_alive"] == "5m"

    @patch("fw_context_mcp.llm.chat_router.httpx.post")
    def test_explicit_format_openai_delegates(self, mock_openai_post):
        """Explicit openai format even on loopback -> openai path."""
        mock_openai_post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", "test"),
        )
        cfg = _cfg(
            chat_api_base="http://localhost:11434",
            chat_api_format="openai",
        )
        result = call_ollama("prompt", cfg)
        assert result == "ok"
        mock_openai_post.assert_called_once()


# ── call_ollama: auto_pull switch ────────────────────────────────────────────


class TestCallOllamaAutoPull:
    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_false_404_raises_model_not_found(self, mock_pull, mock_post):
        mock_post.return_value = _mock_post_response(status_code=404, text="not found")
        cfg = _cfg(chat_api_base=None, auto_pull=False)
        with pytest.raises(OllamaModelNotFoundError):
            call_ollama("prompt", cfg)
        mock_pull.assert_not_called()

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_true_404_pulls_and_retries(self, mock_pull, mock_post):
        mock_post.side_effect = [
            _mock_post_response(status_code=404, text="not found"),
            _mock_post_response(status_code=200, json_body={"response": "ok after pull"}),
        ]
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        result = call_ollama("prompt", cfg)
        assert result == "ok after pull"
        mock_pull.assert_called_once()

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_true_pull_fails_raises_ollama_error(self, mock_pull, mock_post):
        mock_post.return_value = _mock_post_response(status_code=404, text="not found")
        mock_pull.side_effect = OllamaError("pull failed")
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        with pytest.raises(OllamaError, match="pull failed"):
            call_ollama("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_true_retry_still_404_raises_model_not_found(self, mock_pull, mock_post):
        mock_post.side_effect = [
            _mock_post_response(status_code=404, text="not found"),
            _mock_post_response(status_code=404, text="still not found"),
        ]
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        with pytest.raises(OllamaModelNotFoundError):
            call_ollama("prompt", cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_non_404_error_not_pulled(self, mock_pull, mock_post):
        mock_post.return_value = _mock_post_response(status_code=500, text="server error")
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        with pytest.raises(OllamaError):
            call_ollama("prompt", cfg)
        mock_pull.assert_not_called()

    def test_default_llm_config_has_auto_pull_false(self):
        """Guard against accidental revert to True."""
        cfg = LLMConfig()
        assert cfg.auto_pull is False

    def test_default_llm_config_chat_api_base_none(self):
        """Backward-compat guard."""
        cfg = LLMConfig()
        assert cfg.chat_api_base is None


# ── call_ollama_embed: auto_pull switch ──────────────────────────────────────


class TestCallOllamaEmbedAutoPull:
    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_false_404_raises_model_not_found(self, mock_pull, mock_post):
        mock_post.return_value = httpx.Response(
            status_code=404,
            json={"error": "not found"},
            text="not found",
            request=httpx.Request("POST", "http://localhost:11434/api/embed"),
        )
        cfg = _cfg(chat_api_base=None, auto_pull=False)
        with pytest.raises(OllamaModelNotFoundError):
            call_ollama_embed(["text"], cfg)
        mock_pull.assert_not_called()

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_true_404_pulls_and_retries(self, mock_pull, mock_post):
        mock_post.side_effect = [
            httpx.Response(
                status_code=404,
                json={"error": "not found"},
                text="not found",
                request=httpx.Request("POST", "test"),
            ),
            _mock_embed_response(),
        ]
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        result = call_ollama_embed(["text"], cfg)
        assert len(result) == 1
        mock_pull.assert_called_once()

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    @patch("fw_context_mcp.llm.ollama._pull_model")
    def test_auto_pull_true_pull_fails_raises(self, mock_pull, mock_post):
        mock_post.return_value = httpx.Response(
            status_code=404,
            json={"error": "not found"},
            text="not found",
            request=httpx.Request("POST", "test"),
        )
        mock_pull.side_effect = OllamaError("pull failed")
        cfg = _cfg(chat_api_base=None, auto_pull=True)
        with pytest.raises(OllamaError, match="pull failed"):
            call_ollama_embed(["text"], cfg)

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_successful_embed_returns_vectors(self, mock_post):
        mock_post.return_value = _mock_embed_response([[0.1, 0.2], [0.3, 0.4]])
        cfg = _cfg(chat_api_base=None)
        result = call_ollama_embed(["a", "b"], cfg)
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_embed_uses_timeout_doubled(self, mock_post):
        mock_post.return_value = _mock_embed_response()
        cfg = _cfg(chat_api_base=None, timeout=42.0)
        call_ollama_embed(["text"], cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["timeout"] == 84.0

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_embed_query_prompt_prepended(self, mock_post):
        mock_post.return_value = _mock_embed_response()
        cfg = _cfg(chat_api_base=None)
        cfg.embed_query_prompt = "QUERY: "
        call_ollama_embed(["hello"], cfg, query=True)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["QUERY: hello"]

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_embed_doc_prompt_prepended(self, mock_post):
        mock_post.return_value = _mock_embed_response()
        cfg = _cfg(chat_api_base=None)
        cfg.embed_doc_prompt = "DOC: "
        call_ollama_embed(["hello"], cfg, query=False)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["DOC: hello"]

    @patch("fw_context_mcp.llm.ollama.httpx.post")
    def test_embed_no_prompt_when_empty(self, mock_post):
        mock_post.return_value = _mock_embed_response()
        cfg = _cfg(chat_api_base=None)
        cfg.embed_query_prompt = ""
        call_ollama_embed(["hello"], cfg, query=True)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["hello"]


# ── check_setup: status combinations ─────────────────────────────────────────


class TestCheckSetupStatuses:
    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_ollama_running_model_installed_status_ok(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "test-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "ok"
        assert result["ollama_running"] is True

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_ollama_running_model_missing_status_model_missing(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "model_missing"

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_ollama_running_embed_missing(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-model"}]})
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "model_missing"
        assert "test-embed" in result["message"]

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_ollama_not_running_no_chat_api_status_not_configured(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(chat_api_base=None)
        result = check_setup(cfg)
        assert result["status"] == "not_configured"
        assert result["ollama_running"] is False

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_ollama_not_running_with_chat_api_status_embedding_unavailable(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(chat_api_base="https://api.example.com/v1")
        result = check_setup(cfg)
        assert result["status"] == "embedding_unavailable"
        assert result["ollama_running"] is False

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_other_exception_status_error(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected")
        cfg = _cfg(chat_api_base=None)
        result = check_setup(cfg)
        assert result["status"] == "error"

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_model_prefix_match(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "qwen2.5-coder:latest"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="qwen2.5-coder:14b", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "ok"

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_embed_prefix_match(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "test-model"},
                    {"name": "mxbai-embed-large:latest"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="mxbai-embed-large")
        result = check_setup(cfg)
        assert result["status"] == "ok"


# ── check_setup: chat_api info ───────────────────────────────────────────────


class TestCheckSetupChatApi:
    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_chat_api_configured_false_when_no_base(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "test-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None)
        result = check_setup(cfg)
        assert result["chat_api"]["configured"] is False

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_chat_api_configured_true_when_base_set(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base="https://api.example.com/v1")
        result = check_setup(cfg)
        assert result["chat_api"]["configured"] is True
        assert "endpoint" in result["chat_api"]
        assert result["chat_api"]["format"] == "openai"

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_compliance_warning_for_external_url(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base="https://api.openai.com/v1")
        result = check_setup(cfg)
        assert "compliance_warning" in result["chat_api"]
        assert "external" in result["chat_api"]["compliance_warning"].lower()

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_no_compliance_warning_for_loopback(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base="http://127.0.0.1:8080/v1")
        result = check_setup(cfg)
        assert "compliance_warning" not in result["chat_api"]

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_no_compliance_warning_for_localhost(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base="http://localhost:8080/v1")
        result = check_setup(cfg)
        assert "compliance_warning" not in result["chat_api"]

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_compliance_warning_when_ollama_down_external_chat(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("refused")
        cfg = _cfg(chat_api_base="https://api.deepseek.com/v1")
        result = check_setup(cfg)
        assert result["status"] == "embedding_unavailable"
        assert "compliance_warning" in result["chat_api"]


# ── check_setup: suggest_cloud ───────────────────────────────────────────────


class TestCheckSetupSuggestCloud:
    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_suggest_cloud_true_when_no_large_model(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {
                        "name": "llama3.2:latest",
                        "details": {"parameter_size": "3.2B", "family": "llama", "quantization_level": "Q4_K_M"},
                    },
                    {
                        "name": "test-embed",
                        "details": {"parameter_size": "0.7B", "family": "bert", "quantization_level": "F16"},
                    },
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result.get("suggest_cloud") is True

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_suggest_cloud_false_when_large_model_present(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {
                        "name": "llama3.1:70b",
                        "details": {"parameter_size": "70B", "family": "llama", "quantization_level": "Q4_K_M"},
                    },
                    {
                        "name": "test-embed",
                        "details": {"parameter_size": "0.7B", "family": "bert", "quantization_level": "F16"},
                    },
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "model_missing"
        assert result.get("suggest_cloud") is False

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_suggest_cloud_false_when_model_found(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {
                        "name": "test-model",
                        "details": {"parameter_size": "3.2B", "family": "llama", "quantization_level": "Q4_K_M"},
                    },
                    {
                        "name": "test-embed",
                        "details": {"parameter_size": "0.7B", "family": "bert", "quantization_level": "F16"},
                    },
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "ok"
        assert result.get("suggest_cloud") is False

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_model_details_included(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {
                        "name": "qwen2.5-coder:14b",
                        "details": {"parameter_size": "14B", "family": "qwen2", "quantization_level": "Q4_K_M"},
                    },
                    {
                        "name": "test-embed",
                        "details": {"parameter_size": "0.7B", "family": "bert", "quantization_level": "F16"},
                    },
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="qwen2.5-coder:14b", embed_model="test-embed")
        result = check_setup(cfg)
        assert "model_details" in result
        assert len(result["model_details"]) == 2
        assert result["model_details"][0]["name"] == "qwen2.5-coder:14b"
        assert result["model_details"][0]["parameter_size"] == "14B"
        assert result["model_details"][0]["family"] == "qwen2"
        assert result["model_details"][0]["quantization"] == "Q4_K_M"

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_does_not_estimate_download_sizes(self, mock_get):
        mock_get.return_value = _mock_get_response(json_body={"models": [{"name": "test-embed"}]})
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert "download_size" not in result
        assert "estimated_size" not in result
        assert "model_download_info" not in result

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_suggest_cloud_false_when_chat_api_external(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {
                        "name": "test-embed",
                        "details": {"parameter_size": "0.7B", "family": "bert", "quantization_level": "F16"},
                    },
                ]
            }
        )
        cfg = _cfg(chat_api_base="https://api.deepseek.com/v1", embed_model="test-embed")
        result = check_setup(cfg)
        assert result.get("suggest_cloud") is False


# ── check_setup: debug_log ───────────────────────────────────────────────────


class TestCheckSetupDebugLog:
    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_debug_log_included_when_set(self, mock_get, tmp_path):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "test-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, debug_log=tmp_path / "debug.jsonl")
        result = check_setup(cfg)
        assert "debug_log" in result

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_debug_log_absent_when_none(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "test-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, debug_log=None)
        result = check_setup(cfg)
        assert "debug_log" not in result


# ── _parse_parameter_size: unit tests ─────────────────────────────────────────


class TestParseParameterSize:
    def test_decimal_billions(self):
        assert _parse_parameter_size("7.6B") == 7.6

    def test_integer_billions(self):
        assert _parse_parameter_size("13B") == 13.0

    def test_large_model(self):
        assert _parse_parameter_size("70B") == 70.0

    def test_small_model(self):
        assert _parse_parameter_size("0.7B") == 0.7

    def test_strips_whitespace(self):
        assert _parse_parameter_size("  7.6B  ") == 7.6

    def test_empty_string_returns_zero(self):
        assert _parse_parameter_size("") == 0.0

    def test_unknown_string_returns_zero(self):
        assert _parse_parameter_size("unknown") == 0.0

    def test_missing_b_suffix_returns_zero(self):
        assert _parse_parameter_size("7.6") == 0.0

    def test_only_b_returns_zero(self):
        assert _parse_parameter_size("B") == 0.0


# ── check_setup: missing details field (defensive) ────────────────────────────


class TestCheckSetupMissingDetails:
    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_no_crash_when_details_missing(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "mystery-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result["status"] == "model_missing"
        assert "model_details" in result

    @patch("fw_context_mcp.llm.ollama.httpx.get")
    def test_suggest_cloud_true_when_all_details_missing(self, mock_get):
        mock_get.return_value = _mock_get_response(
            json_body={
                "models": [
                    {"name": "mystery-model"},
                    {"name": "test-embed"},
                ]
            }
        )
        cfg = _cfg(chat_api_base=None, model="test-model", embed_model="test-embed")
        result = check_setup(cfg)
        assert result.get("suggest_cloud") is True
