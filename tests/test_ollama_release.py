"""The release of an Ollama model between the LLM analysis and the embeddings (``llm.ollama.release_other_model``).

The two phases share the GPU memory of one Ollama server, and a model
that ``keep_alive`` holds leaves the next model only the rest.  No test
reaches a real Ollama: httpx is replaced.
"""

from __future__ import annotations

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import ollama

URL = "http://localhost:11434"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    """The session turns the release off (conftest): these tests turn it on, with a short wait."""
    monkeypatch.delenv(ollama.NO_RELEASE_ENV, raising=False)
    monkeypatch.setattr(ollama, "_chat_used", set())
    monkeypatch.setattr(ollama, "_RELEASE_WAIT_S", 0.2)
    monkeypatch.setattr(ollama.time, "sleep", lambda seconds: None)


def _server(monkeypatch, *loaded_after: list[str]) -> list[dict]:
    """Answer /api/ps with each list of *loaded_after* in order (the last one stays); return the unload requests."""
    states = list(loaded_after)
    posted: list[dict] = []

    def _get(url, timeout):
        models = states.pop(0) if len(states) > 1 else states[0]
        return httpx.Response(200, json={"models": [{"name": name} for name in models]},
                              request=httpx.Request("GET", url))

    def _post(url, json, timeout):
        posted.append(json)
        return httpx.Response(200, json={"done_reason": "unload"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)
    return posted


def test_a_held_model_is_released(monkeypatch):
    posted = _server(monkeypatch, ["nomic-embed-text:latest"], [])

    assert ollama.release_model(URL, "nomic-embed-text") is True
    assert posted == [{"model": "nomic-embed-text:latest", "keep_alive": 0}]


def test_a_model_that_is_not_held_needs_nothing(monkeypatch):
    posted = _server(monkeypatch, ["other:1b"])

    assert ollama.release_model(URL, "chat:1b") is None
    assert posted == []


def test_a_model_that_stays_is_reported(monkeypatch):
    """Another client can load the model again at once: a release is not certain."""
    _server(monkeypatch, ["chat:1b"])

    assert ollama.release_model(URL, "chat:1b") is False


@pytest.mark.parametrize(("phase", "released"), [("analysis", "e:1b"), ("embeddings", "chat:1b")])
def test_the_model_of_the_other_phase_goes_on_one_server(monkeypatch, phase, released):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ollama, "release_model", lambda url, model: calls.append((url, model)))
    ollama.note_chat_use(URL, "chat:1b")

    ollama.release_other_model(LLMConfig(provider="ollama", model="chat:1b", embed_model="e:1b", ollama_url=URL), phase)

    assert calls == [(URL, released)]


@pytest.mark.parametrize("cfg", [
    LLMConfig(provider="deepseek", model="m", embed_model="e:1b", ollama_url=URL,
              chat_api_base="https://api.deepseek.com", chat_api_format="openai"),
    LLMConfig(provider="ollama", model="m", embed_model="e:1b", ollama_url=URL,
              chat_api_base="http://gpu-box:11434", chat_api_format="ollama"),
    LLMConfig(provider="ollama", model="m", embed_model="BAAI/bge-small-en-v1.5", ollama_url=URL),
], ids=["chat on an API", "chat on another server", "embeddings without Ollama"])
def test_nothing_is_released_when_the_phases_share_no_server(monkeypatch, cfg):
    monkeypatch.setattr(ollama, "release_model", lambda url, model: pytest.fail("no release"))

    ollama.release_other_model(cfg, "analysis")


def test_a_chat_model_that_this_process_did_not_use_stays(monkeypatch):
    """A run without an analysis unloaded the model of another project or client."""
    monkeypatch.setattr(ollama, "release_model", lambda url, model: pytest.fail("no release"))

    ollama.release_other_model(LLMConfig(provider="ollama", model="m", embed_model="e:1b", ollama_url=URL), "embeddings")


def test_an_empty_model_list_is_no_model(monkeypatch):
    def _get(url, timeout):
        return httpx.Response(200, json={"models": None}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _get)

    assert ollama.loaded_models(URL) == []


def test_a_refused_release_does_not_wait(monkeypatch):
    _server(monkeypatch, ["chat:1b"])
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: httpx.Response(404, request=httpx.Request("POST", url)))
    monkeypatch.setattr(ollama, "_RELEASE_WAIT_S", 60.0)

    assert ollama.release_model(URL, "chat:1b") is False


def test_the_session_never_releases_a_model_of_the_operator(monkeypatch):
    monkeypatch.setenv(ollama.NO_RELEASE_ENV, "1")
    monkeypatch.setattr(ollama, "release_model", lambda url, model: pytest.fail("no release"))

    ollama.release_other_model(LLMConfig(provider="ollama", model="m", embed_model="e:1b", ollama_url=URL), "analysis")
