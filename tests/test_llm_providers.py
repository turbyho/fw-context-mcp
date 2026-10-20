"""The providers openrouter and deepseek of an LLM profile (``llm.providers``).

A profile of such a provider needs only a model: the URL and the key
variable have defaults.  Each request turns the reasoning of the model
off, and OpenRouter gets the routing (sort by price, no quantization
below 8 bits by default: the fp4 endpoint of DeepSeek gave answers of 3.1
of 5 against 4.2 for fp8, measured 2026-10-20).
"""

from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest

from fw_context_mcp.config import settings
from fw_context_mcp.config.settings import ConfigError, LLMConfig, load
from fw_context_mcp.llm.errors import ChatApiError


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path))
    settings._config_cache.clear()
    return path


def _profile(home: Path, body: str) -> LLMConfig:
    home.write_text(f'[llm]\nprofile = "p"\nembed_model = "e:1b"\n\n[llm.profiles.p]\n{body}', encoding="utf-8")
    settings._config_cache.clear()
    return load(None).llm


def test_an_openrouter_profile_needs_only_a_model(home):
    llm = _profile(home, 'provider = "openrouter"\nmodel = "openai/gpt-6-luna"\n')

    assert (llm.chat_api_base, llm.chat_api_format) == ("https://openrouter.ai/api/v1", "openai")
    assert (llm.api_key_env, llm.routing) == ("OPENROUTER_API_KEY", {})


def test_a_deepseek_profile_needs_only_a_model(home):
    llm = _profile(home, 'provider = "deepseek"\nmodel = "deepseek-flash"\n')

    assert (llm.chat_api_base, llm.api_key_env) == ("https://api.deepseek.com", "DEEPSEEK_API_KEY")


@pytest.mark.parametrize(("body", "message"), [
    ('routing = { sort = "cheapest" }', "sort must be one of price, throughput, latency"),
    ('routing = { quantizations = "fp8" }', "quantizations must be an array of strings"),
    ('routing = { quantizations = ["FP8"] }', "quantizations must be an array of int4, int8"),
    ('routing = { data_collection = "no" }', "data_collection must be one of allow, deny"),
    ('routing = { only = ["x"] }', "only is not a key of routing"),
    ('routing = "price"', "routing must be a table"),
])
def test_a_wrong_routing_value_is_an_error(home, body, message):
    with pytest.raises(ConfigError, match=message):
        _profile(home, f'provider = "openrouter"\nmodel = "m"\n{body}\n')


def test_routing_is_not_a_key_of_a_deepseek_profile(home):
    with pytest.raises(ConfigError, match="routing is not a key of a profile of provider deepseek"):
        _profile(home, 'provider = "deepseek"\nmodel = "m"\nrouting = { sort = "price" }\n')


# ── The requests ──


def _replies(monkeypatch, *responses: tuple[int, dict | str]) -> list[dict]:
    """Answer each request with the next of *responses*; return the payloads that were sent."""
    sent: list[dict] = []
    queue = list(responses)

    def _post(url, json, headers, timeout):
        sent.append(copy.deepcopy(json))
        status, body = queue.pop(0)
        request = httpx.Request("POST", url)
        if isinstance(body, dict):
            return httpx.Response(status, json=body, request=request)
        return httpx.Response(status, text=body, request=request)

    monkeypatch.setattr(httpx, "post", _post)
    return sent


OK = (200, {"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 10, "cost": 0.0001}})


def _client(llm: LLMConfig):
    from fw_context_mcp.llm.client import get_chat_client

    return get_chat_client(llm)


def test_an_openrouter_request_turns_the_reasoning_off_and_routes_by_price(home, monkeypatch):
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\nrouting = { zdr = true }\n')
    sent = _replies(monkeypatch, OK)

    result = _client(llm).generate("hi")

    assert result.usage.cost == 0.0001
    payload = sent[0]
    assert payload["reasoning"] == {"enabled": False}
    assert payload["usage"] == {"include": True}
    assert payload["provider"] == {
        "sort": "price", "quantizations": ["int8", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"], "zdr": True,
    }


def test_a_routing_value_of_the_profile_wins_over_the_default(home, monkeypatch):
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\nrouting = { quantizations = [] }\n')
    sent = _replies(monkeypatch, OK)

    _client(llm).generate("hi")

    assert sent[0]["provider"]["quantizations"] == []


def test_a_deepseek_request_turns_the_thinking_off(home, monkeypatch):
    llm = _profile(home, 'provider = "deepseek"\nmodel = "deepseek-flash"\n')
    sent = _replies(monkeypatch, OK)

    _client(llm).generate("hi")

    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "provider" not in sent[0] and "reasoning" not in sent[0]


def test_an_openai_compatible_request_gets_no_field_of_a_provider(home, monkeypatch):
    llm = _profile(home, 'provider = "openai_compatible"\nurl = "http://127.0.0.1:9/v1"\nmodel = "m"\n')
    sent = _replies(monkeypatch, OK)

    _client(llm).generate("hi")

    assert set(sent[0]) == {"model", "messages", "temperature", "stream"}


def test_a_model_that_cannot_turn_the_reasoning_off_gets_the_lowest_reasoning(home, monkeypatch):
    """gpt-oss refuses reasoning.enabled false: the request goes again, and the later ones ask low at once."""
    llm = _profile(home, 'provider = "openrouter"\nmodel = "openai/gpt-oss-20b"\n')
    sent = _replies(monkeypatch, (400, "Reasoning is mandatory for this endpoint and cannot be disabled."), OK, OK)
    client = _client(llm)

    assert client.generate("one").text == "OK"
    assert client.generate("two").text == "OK"

    assert [payload["reasoning"] for payload in sent] == [
        {"enabled": False}, {"effort": "low", "exclude": True}, {"effort": "low", "exclude": True},
    ]


def test_another_400_is_not_sent_again(home, monkeypatch):
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\n')
    sent = _replies(monkeypatch, (400, "max_tokens is too large"))

    with pytest.raises(ChatApiError) as error:
        _client(llm).generate("hi")

    assert error.value.status_code == 400 and len(sent) == 1


def test_no_endpoint_for_the_routing_names_the_routing(home, monkeypatch):
    llm = _profile(home, 'provider = "openrouter"\nmodel = "openai/gpt-oss-20b"\n')
    _replies(monkeypatch, (404, "No endpoints found matching your data policy"))

    with pytest.raises(ChatApiError) as error:
        _client(llm).generate("hi")

    message = str(error.value)
    assert "quantizations" in message and "[llm.profiles.p.routing]" in message, message


# ── The answers of an error, the key of a provider ──


def test_a_reasoning_word_after_the_shown_part_of_the_answer_is_found(home, monkeypatch):
    """OpenRouter wraps the error of the host in metadata.raw: the word was after the 200 characters of the message."""
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\n')
    body = "x" * 300 + " reasoning is mandatory for this endpoint"
    sent = _replies(monkeypatch, (400, body), OK)

    assert _client(llm).generate("hi").text == "OK"
    assert len(sent) == 2


def test_a_400_about_finish_reason_does_not_turn_the_reasoning_on(home, monkeypatch):
    """Before, "reason" matched, and each later request of the run got billed reasoning."""
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\n')
    sent = _replies(monkeypatch, (400, "invalid finish_reason filter"))
    client = _client(llm)

    with pytest.raises(ChatApiError):
        client.generate("hi")

    assert len(sent) == 1 and client._low_reasoning is False


def _stream_replies(monkeypatch, status: int, body: bytes) -> None:
    """Answer a stream with an error whose body is not read yet, as a real server does."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=httpx.ByteStream(body))

    real_stream = httpx.stream

    def _stream(method, url, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        kwargs.pop("timeout", None)
        return client.stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "stream", _stream)
    assert real_stream is not _stream


def test_a_stream_reads_the_body_of_an_error_before_the_message(home, monkeypatch):
    """Before, httpx raised ResponseNotRead in the place of the error: no retry, and no sentence for the operator."""
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\nstream = true\n')
    _stream_replies(monkeypatch, 404, b"No endpoints found")

    with pytest.raises(ChatApiError) as error:
        _client(llm).generate("hi")

    assert "No endpoints found" in str(error.value) and error.value.status_code == 404


def test_an_ollama_stream_reads_the_body_of_an_error_before_the_message(monkeypatch):
    from fw_context_mcp.llm.errors import LLMError
    from fw_context_mcp.llm.ollama import OllamaClient

    _stream_replies(monkeypatch, 500, b"out of memory")
    cfg = LLMConfig(model="m", stream=True)

    with pytest.raises(LLMError) as error:
        OllamaClient(cfg, "http://127.0.0.1:9/api/generate", "http://127.0.0.1:9").generate("hi")

    assert "out of memory" in str(error.value)


def test_a_profile_with_its_own_url_gets_no_key_of_the_provider(home):
    """A proxy must not get the key of OpenRouter: no default variable, and no key of the provider."""
    llm = _profile(home, 'provider = "openrouter"\nmodel = "m"\nurl = "https://proxy.example.test/v1"\n')

    assert llm.api_key_env == ""
    assert _client(llm)._provider_of_key() == ""


def test_an_empty_key_variable_turns_the_default_off(home):
    llm = _profile(home, 'provider = "deepseek"\nmodel = "m"\napi_key_env = ""\n')

    assert llm.api_key_env == ""


def test_an_empty_url_is_an_error(home):
    with pytest.raises(ConfigError, match="url must not be empty"):
        _profile(home, 'provider = "deepseek"\nmodel = "m"\nurl = ""\n')


def test_a_profile_named_like_a_provider_does_not_give_its_key_to_that_provider(home, monkeypatch):
    """A profile "deepseek" of OpenRouter: its key went to api.deepseek.com for a profile of DeepSeek."""
    from fw_context_mcp.llm import secrets

    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    secrets.set_api_key("deepseek", "sk-openrouter-" + "key")

    assert secrets.get_api_key("ds-direct", "", "deepseek") is None
