"""The Ollama client asks the server not to cut a prompt, and its count_tokens gives the exact count.

Measured with Ollama 0.35.1: without "truncate": false, /api/generate cut a
prompt of 6413 tokens to 1026 tokens for a context of 2048, and the model
answered without an error.  With it, the server answers HTTP 400 with the
count of the tokenizer of the model.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest import mock

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import ollama
from fw_context_mcp.llm.client import call_llm, get_chat_client
from fw_context_mcp.llm.errors import LLMError

# The error of Ollama 0.35.1: the error of its runner as a JSON string.
_TOO_LONG = {"error": json.dumps({"error": {
    "code": 400, "message": "request (6413 tokens) exceeds the available context size (2048 tokens)",
    "type": "exceed_context_size_error", "n_prompt_tokens": 6413, "n_ctx": 2048,
}})}


# The error of Ollama 0.40.7: only the message of its runner, as a plain string.
# Taken from the log of a real analysis (2026-10-24).
_TOO_LONG_040 = {
    "error": "request (18924 tokens) exceeds the available context size (16384 tokens), try increasing it",
}


def _response(status: int, body: dict, url: str = "http://localhost:11434/api/generate") -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", url))


def test_the_request_asks_the_server_not_to_cut_and_takes_the_context_of_the_call():
    sent: dict = {}

    def post(url, json=None, **_):
        sent.update(json)
        return _response(200, {"response": "ok"})

    with mock.patch.object(ollama.net, "post", side_effect=post):
        call_llm("p", LLMConfig(num_ctx=16384), num_ctx=24576)
    assert sent["truncate"] is False
    assert sent["options"]["num_ctx"] == 24576


def test_without_a_context_the_call_has_the_context_of_the_config():
    sent: dict = {}

    def post(url, json=None, **_):
        sent.update(json)
        return _response(200, {"response": "ok"})

    with mock.patch.object(ollama.net, "post", side_effect=post):
        call_llm("p", LLMConfig(num_ctx=16384))
    assert sent["options"]["num_ctx"] == 16384


def test_a_prompt_longer_than_the_context_raises_with_the_count():
    with mock.patch.object(ollama.net, "post", return_value=_response(400, _TOO_LONG)), \
            pytest.raises(ollama.OllamaPromptTooLong) as raised:
        call_llm("p", LLMConfig())
    assert (raised.value.prompt_tokens, raised.value.context) == (6413, 2048)


def test_a_streamed_prompt_after_a_pull_raises_with_the_count(monkeypatch):
    answers = [404, 400]

    @contextmanager
    def stream(method, url, **_):
        status = answers.pop(0)
        body = json.dumps(_TOO_LONG if status == 400 else {"error": "model not found"}).encode()
        yield httpx.Response(status, content=body, request=httpx.Request(method, url))

    monkeypatch.setattr(ollama, "_pull_model", lambda model, base: None)
    with mock.patch.object(ollama.net, "stream", side_effect=stream), \
            pytest.raises(ollama.OllamaPromptTooLong):
        call_llm("p", LLMConfig(stream=True, auto_pull=True))


def test_a_streamed_prompt_longer_than_the_context_raises_with_the_count():
    @contextmanager
    def stream(method, url, **_):
        yield httpx.Response(400, content=json.dumps(_TOO_LONG).encode(), request=httpx.Request(method, url))

    with mock.patch.object(ollama.net, "stream", side_effect=stream), \
            pytest.raises(ollama.OllamaPromptTooLong) as raised:
        call_llm("p", LLMConfig(stream=True))
    assert raised.value.prompt_tokens == 6413


def test_the_plain_message_of_ollama_040_raises_with_the_count():
    """Ollama 0.40 gives the message of its runner, not the JSON of 0.35.

    The parse of the JSON form alone took it as another HTTP 400: the count
    request failed three times, and the symbol got no analysis and no
    second pass with a larger context.
    """
    with mock.patch.object(ollama.net, "post", return_value=_response(400, _TOO_LONG_040)), \
            pytest.raises(ollama.OllamaPromptTooLong) as raised:
        call_llm("p", LLMConfig())
    assert (raised.value.prompt_tokens, raised.value.context) == (18924, 16384)


def test_another_error_400_stays_an_ollama_error():
    with mock.patch.object(ollama.net, "post", return_value=_response(400, {"error": "invalid options"})), \
            pytest.raises(ollama.OllamaError) as raised:
        call_llm("p", LLMConfig())
    assert not isinstance(raised.value, ollama.OllamaPromptTooLong)


@pytest.mark.parametrize(("answer", "count"), [
    (_response(200, {"response": "x", "prompt_eval_count": 812}), 812),
    (_response(400, _TOO_LONG), 6413),
    (_response(400, _TOO_LONG_040), 18924),
])
def test_the_count_comes_from_the_server(answer, count):
    sent: dict = {}

    def post(url, json=None, **_):
        sent.update(json)
        return answer

    with mock.patch.object(ollama.net, "post", side_effect=post):
        assert get_chat_client(LLMConfig()).count_tokens("p", 16384) == count
    assert sent["options"] == {"num_ctx": 16384, "num_predict": 1}
    assert sent["truncate"] is False
    # Measured 2026-10-23: gemma4:26b counted 3 tokens more without "think": false, the prompt of
    # the analysis is the one with it.
    assert sent["think"] is False


def test_an_openai_endpoint_gives_no_count():
    client = get_chat_client(LLMConfig(chat_api_base="https://api.example.test/v1"))
    assert client.counts_tokens is False
    assert client.trained_context() is None
    with pytest.raises(LLMError):
        client.count_tokens("p", 16384)


# ── The oldest Ollama that fw-context can use ──


def _version_server(version):
    def get(url, **_):
        assert url.endswith("/api/version")
        return httpx.Response(200, json={"version": version}, request=httpx.Request("GET", url))

    return mock.patch.object(ollama.net, "get", side_effect=get)


@pytest.mark.parametrize(("version", "old"), [
    # 0.30.0 counted a prompt without its cached tokens.
    ("0.29.9", True), ("0.12.6", True), ("0.30.0", True), ("0.30.1", False), ("0.35.1", False),
    ("0.40.2-rc0", False),
    ("0.0.0", False),  # a build from source: not known to be old
])
def test_an_ollama_older_than_the_minimum_is_named(version, old):
    with _version_server(version):
        problem = ollama.ollama_version_problem("http://localhost:11434")
    assert (problem is not None) is old
    if old:
        assert version in problem and "Update Ollama" in problem


def test_a_server_that_does_not_answer_gives_no_version_problem():
    def get(url, **_):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    with mock.patch.object(ollama.net, "get", side_effect=get):
        assert ollama.ollama_version("http://localhost:11434") is None
        assert ollama.ollama_version_problem("http://localhost:11434") is None


def test_doctor_and_init_report_an_old_ollama():
    """check_ollama_running is a check of `fw-context doctor` and of `fw-context init`."""
    from fw_context_mcp.deps import _checks

    with mock.patch("fw_context_mcp.llm._diag.check_setup", return_value={"status": "ok"}), \
            _version_server("0.20.0"):
        result = _checks.check_ollama_running(LLMConfig())
    assert result.status == "degraded"
    assert "0.20.0" in result.message


def test_doctor_reports_an_old_ollama_of_the_chat_server():
    """The chat model can live on another Ollama server (chat_api_base): its version counts too."""
    from fw_context_mcp.deps import _checks

    cfg = LLMConfig(chat_api_base="http://chat.example.test:11434")
    with mock.patch("fw_context_mcp.llm._diag.installed_models", return_value=[cfg.model]), \
            _version_server("0.29.0"):
        result = _checks.check_ollama_chat_model(cfg)
    assert result.status == "degraded"
    assert "chat.example.test" in result.message


@pytest.mark.parametrize("stream", [False, True])
def test_a_stop_of_the_run_is_not_wrapped(stream):
    """The SIGTERM handler of the index lock raises IndexTerminated, then ignores SIGTERM.

    Before, the streaming request wrapped it into OllamaError, and the LLM
    analysis took it as the failure of one symbol: no signal but SIGKILL
    stopped the process.
    """
    from fw_context_mcp.indexer.runner import IndexTerminated
    from fw_context_mcp.utils import is_fatal

    def stopped(*a, **k):
        raise IndexTerminated("a SIGTERM stopped this run")

    with mock.patch.object(ollama.net, "stream", side_effect=stopped), \
            mock.patch.object(ollama.net, "post", side_effect=stopped), \
            pytest.raises(IndexTerminated):
        call_llm("p", LLMConfig(stream=stream))
    assert is_fatal(IndexTerminated("x"))


def test_the_request_turns_off_the_thinking_of_the_model():
    """Measured 2026-10-23 with gemma4:26b, which thinks by default: without "think": false it wrote its
    reasoning first, used each of the 1024 answer tokens, and gave an empty response for each symbol.
    """
    sent: dict = {}

    def post(url, json=None, **_):
        sent.update(json)
        return _response(200, {"response": "ok"})

    with mock.patch.object(ollama.net, "post", side_effect=post):
        call_llm("p", LLMConfig(num_ctx=16384))
    assert sent["think"] is False


def test_a_streamed_request_turns_off_the_thinking_too():
    sent: dict = {}

    @contextmanager
    def stream(method, url, json=None, **_):
        sent.update(json)
        yield httpx.Response(200, content=b'{"response": "ok", "done": true}\n', request=httpx.Request(method, url))

    with mock.patch.object(ollama.net, "stream", side_effect=stream):
        call_llm("p", LLMConfig(stream=True))
    assert sent["think"] is False
