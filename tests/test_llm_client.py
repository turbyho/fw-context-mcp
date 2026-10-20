"""The chat client interface: the selection of the backend, the usage of an answer, the errors.

Before, ``call_ollama`` sent each request and returned only the text: the
token counts and the cost that the backend reports were lost, and the LLM
analysis asked a second function whether the backend is Ollama.  Each
backend is now a class with the same methods, and an answer carries its
usage.

No server: ``httpx`` of each client module is replaced.
"""

from __future__ import annotations

from unittest import mock

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import chat_router, ollama
from fw_context_mcp.llm.chat_router import OpenAIChatClient
from fw_context_mcp.llm.client import ChatResult, Usage, call_llm, get_chat_client
from fw_context_mcp.llm.errors import ChatApiError, LLMError
from fw_context_mcp.llm.ollama import OllamaClient, OllamaError


def _response(status: int, body: dict, url: str = "http://test/x") -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", url))


# ── The selection of the backend ──


def test_without_an_api_base_the_client_is_the_ollama_of_the_config():
    client = get_chat_client(LLMConfig(ollama_url="http://gpu:11434/"))

    assert isinstance(client, OllamaClient)
    assert client.generate_url == "http://gpu:11434/api/generate"
    assert client.server_url == "http://gpu:11434"
    assert client.counts_tokens is True


def test_a_trailing_slash_of_the_url_still_gives_the_trained_context():
    """Before the fix, "http://gpu:11434//api/show" got HTTP 307, which httpx does not follow.

    The trained context was then unknown, and the run made no analysis at all.
    """
    posted = []
    show = {"model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768}}

    def post(url, **_):
        posted.append(url)
        return _response(200, show, url)

    with mock.patch.object(ollama.httpx, "post", side_effect=post):
        assert get_chat_client(LLMConfig(ollama_url="http://gpu:11434/")).trained_context() == 32768
    assert posted == ["http://gpu:11434/api/show"]


def test_an_openai_api_base_gives_the_openai_client():
    client = get_chat_client(LLMConfig(chat_api_base="https://api.example.test/v1"))

    assert isinstance(client, OpenAIChatClient)
    assert client.endpoint == "https://api.example.test/v1/chat/completions"
    assert client.counts_tokens is False


def test_an_ollama_api_base_gives_an_ollama_client_of_that_server():
    client = get_chat_client(LLMConfig(chat_api_base="http://remote:11434/api"))

    assert isinstance(client, OllamaClient)
    assert client.generate_url == "http://remote:11434/api/generate"
    assert client.server_url == "http://remote:11434"


def test_an_unknown_api_format_is_an_error():
    with pytest.raises(ValueError, match="chat_api_format"):
        get_chat_client(LLMConfig(chat_api_base="https://api.example.test/v1", chat_api_format="soap"))


# ── The usage of an Ollama answer ──


def test_an_ollama_answer_gives_its_token_counts():
    answer = {"response": "ok", "prompt_eval_count": 2500, "prompt_eval_cached_count": 1952, "eval_count": 180}
    with mock.patch.object(ollama.httpx, "post", return_value=_response(200, answer)):
        result = get_chat_client(LLMConfig()).generate("p")

    assert result == ChatResult("ok", Usage(prompt_tokens=2500, cached_tokens=1952, completion_tokens=180))


def test_an_ollama_answer_without_counts_gives_none():
    with mock.patch.object(ollama.httpx, "post", return_value=_response(200, {"response": "ok"})):
        result = get_chat_client(LLMConfig()).generate("p")

    assert result.usage == Usage()


def test_a_streamed_ollama_answer_takes_the_counts_of_its_last_line():
    lines = ['{"response": "o", "done": false}', '{"response": "k", "done": false}',
             '{"response": "", "done": true, "prompt_eval_count": 40, "eval_count": 2}']

    class _Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(lines)

    with mock.patch.object(ollama.httpx, "stream", return_value=_Stream()):
        result = get_chat_client(LLMConfig(stream=True)).generate("p")

    assert result == ChatResult("ok", Usage(prompt_tokens=40, completion_tokens=2))


# ── The usage of an OpenAI-compatible answer ──


def _openai(usage: dict) -> dict:
    return {"choices": [{"message": {"content": "ok"}}], "usage": usage}


@pytest.mark.parametrize(("usage", "expected"), [
    # OpenAI and OpenRouter: details objects, and the cost of OpenRouter.
    ({"prompt_tokens": 2600, "completion_tokens": 700, "cost": 0.00036,
      "prompt_tokens_details": {"cached_tokens": 2048},
      "completion_tokens_details": {"reasoning_tokens": 418}},
     Usage(prompt_tokens=2600, cached_tokens=2048, completion_tokens=700, reasoning_tokens=418, cost=0.00036)),
    # DeepSeek: the cache hits have their own field.
    ({"prompt_tokens": 2650, "completion_tokens": 300, "prompt_cache_hit_tokens": 1800},
     Usage(prompt_tokens=2650, cached_tokens=1800, completion_tokens=300)),
    # A value of the wrong type is not a count.
    ({"prompt_tokens": "2600", "completion_tokens": True, "cost": "free"}, Usage()),
])
def test_an_openai_answer_gives_its_usage(usage, expected):
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1")
    with mock.patch.object(chat_router.httpx, "post", return_value=_response(200, _openai(usage))):
        result = get_chat_client(cfg).generate("p")

    assert result == ChatResult("ok", expected)


def test_an_openai_answer_without_usage_gives_none():
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1")
    body = {"choices": [{"message": {"content": "ok"}}]}
    with mock.patch.object(chat_router.httpx, "post", return_value=_response(200, body)):
        assert get_chat_client(cfg).generate("p").usage == Usage()


def test_call_llm_returns_only_the_text():
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1")
    with mock.patch.object(chat_router.httpx, "post", return_value=_response(200, _openai({"prompt_tokens": 1}))):
        assert call_llm("p", cfg) == "ok"


# ── The errors ──


def test_each_backend_error_is_an_llm_error_and_only_ollama_is_an_ollama_error():
    """A handler of "the chat failed" catches LLMError; before, an API error had to derive from OllamaError."""
    assert issubclass(OllamaError, LLMError)
    assert issubclass(ChatApiError, LLMError)
    assert not issubclass(ChatApiError, OllamaError)


def test_an_api_error_that_passes_through_keeps_its_own_cause():
    """Before the fix, ``raise e from e`` made an error the cause of itself (``e.__cause__ is e``)."""
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1")
    sse = httpx.Response(200, text="data: [DONE]\n", headers={"content-type": "text/event-stream"},
                         request=httpx.Request("POST", "https://api.example.test/v1/chat/completions"))
    with mock.patch.object(chat_router.httpx, "post", return_value=sse), pytest.raises(ChatApiError) as caught:
        get_chat_client(cfg).generate("p")

    assert caught.value.__cause__ is not caught.value


def test_a_timeout_of_the_api_hides_the_httpx_error_as_the_ollama_client_does():
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1")
    with mock.patch.object(chat_router.httpx, "post", side_effect=httpx.ReadTimeout("slow")), \
            pytest.raises(ChatApiError, match="timed out") as caught:
        get_chat_client(cfg).generate("p")

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("stream", [False, True])
def test_a_stop_of_the_run_passes_the_api_client_unchanged(stream):
    from fw_context_mcp.utils import StopRequested

    stop = StopRequested("stop")
    cfg = LLMConfig(chat_api_base="https://api.example.test/v1", stream=stream)
    with mock.patch.object(chat_router.httpx, "stream" if stream else "post", side_effect=stop), \
            pytest.raises(StopRequested) as caught:
        get_chat_client(cfg).generate("p")

    assert caught.value is stop
    assert caught.value.__cause__ is None


def test_an_openai_client_cannot_count_or_pull():
    client = OpenAIChatClient(LLMConfig(chat_api_base="https://api.example.test/v1"), "https://e/v1/chat/completions")

    assert client.trained_context() is None
    with pytest.raises(ChatApiError):
        client.count_tokens("p", 4096)
    with pytest.raises(ChatApiError):
        client.pull_model()
