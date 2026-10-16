"""MCP ``check_ollama`` and the chat calls use the Ollama server of the chat calls.

That server is ``ollama_url``, or the Ollama server that ``chat_api_base``
names (`chat_router.chat_ollama_server`).  Before:

- ``check_ollama`` (`llm.ollama.check_setup`) did not look for the chat
  model at all when ``chat_api_base`` was set, also when it named an
  Ollama server.
- Each message gave a plain ``ollama pull <model>``.  The ``ollama`` CLI
  reads the server only from ``OLLAMA_HOST``, thus a person or the LLM
  pulled the model onto a different server.
- ``auto_pull`` of a chat call sent the pull to the raw ``chat_api_base``:
  for ``.../api`` the pull went to ``.../api/api/pull``.
"""

from __future__ import annotations

import sys

import httpx
import pytest

LOCAL = "http://localhost:11434"
REMOTE = "http://gpu-box:11434"
EMBED = "qwen3-embedding:0.6b"
CHAT = "qwen2.5-coder:14b"


class _Tags:
    def __init__(self, names):
        self._names = names

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": name, "details": {"parameter_size": "14B"}} for name in self._names]}


@pytest.fixture
def servers(monkeypatch):
    """Map of the server URL to its model names; a URL that is not in it does not answer."""

    class _Servers(dict):
        asked: list[str]

    known = _Servers()
    known.asked = []

    def _get(url, timeout):
        server = url.removesuffix("/api/tags")
        known.asked.append(server)
        if server not in known:
            raise httpx.ConnectError("connection refused")
        return _Tags(known[server])

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(sys, "platform", "linux")
    return known


def _cfg(**overrides):
    from fw_context_mcp.config.settings import LLMConfig

    cfg = LLMConfig()
    cfg.enabled = True
    cfg.ollama_url = LOCAL
    cfg.model = CHAT
    cfg.embed_model = EMBED
    cfg.chat_api_base = None
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _setup(cfg):
    from fw_context_mcp.llm.ollama import check_setup

    return check_setup(cfg)


@pytest.mark.parametrize("chat_api_base", [REMOTE, LOCAL])
def test_check_ollama_looks_for_the_chat_model_on_the_ollama_server_of_chat_api_base(servers, chat_api_base):
    servers[LOCAL] = [EMBED]
    servers[REMOTE] = []

    result = _setup(_cfg(chat_api_base=chat_api_base))

    assert result["status"] == "model_missing", result
    assert f"Run: OLLAMA_HOST={chat_api_base} ollama pull {CHAT}" in result["message"]


def test_check_ollama_finds_the_chat_model_on_the_ollama_server_of_chat_api_base(servers):
    servers[LOCAL] = [EMBED]
    servers[REMOTE] = [CHAT]

    assert _setup(_cfg(chat_api_base=REMOTE))["status"] == "ok"
    assert servers.asked == [LOCAL, REMOTE], "the check asks the chat server"


def test_check_ollama_reports_a_chat_server_without_a_model_list(servers):
    servers[LOCAL] = [EMBED]

    result = _setup(_cfg(chat_api_base=REMOTE))

    assert result["status"] == "error", result
    assert REMOTE in result["message"]


def test_the_error_of_the_chat_server_keeps_the_text_of_the_embedding_model(servers):
    """For "error", the operator reads only `message`: the missing embedding model was not in it."""
    servers[LOCAL] = []

    result = _setup(_cfg(chat_api_base=REMOTE))

    assert result["status"] == "error", result
    assert REMOTE in result["message"]
    assert f"Run: OLLAMA_HOST={LOCAL} ollama pull {EMBED}" in result["message"]


def test_check_ollama_does_not_ask_for_the_chat_model_of_an_openai_compatible_api(servers):
    servers[LOCAL] = [EMBED]

    result = _setup(_cfg(model="deepseek-chat", chat_api_base="https://api.deepseek.com/v1"))

    assert result["status"] == "ok", result
    assert servers.asked == [LOCAL]


def test_the_messages_name_the_server_of_each_model(servers):
    servers[LOCAL] = []

    message = _setup(_cfg())["message"]

    assert f"Run: OLLAMA_HOST={LOCAL} ollama pull {CHAT}" in message
    assert f"Run: OLLAMA_HOST={LOCAL} ollama pull {EMBED}" in message


def test_the_doctor_diagnosis_names_the_server(servers):
    from fw_context_mcp.llm import _diag

    servers[LOCAL] = []

    message = _diag.check_setup(_cfg())["message"]

    assert f"Run: OLLAMA_HOST={LOCAL} ollama pull {CHAT}" in message
    assert f"Run: OLLAMA_HOST={LOCAL} ollama pull {EMBED}" in message


def test_the_error_of_a_missing_model_names_the_server(monkeypatch):
    from fw_context_mcp.llm.ollama import OllamaModelNotFoundError

    monkeypatch.setattr(sys, "platform", "linux")

    assert f"Run: OLLAMA_HOST={REMOTE} ollama pull {CHAT}" in str(OllamaModelNotFoundError(CHAT, REMOTE))


class _Answer:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return {"response": "done"}


@pytest.mark.parametrize("chat_api_base", ["http://gpu-box:11434/api", "http://gpu-box:11434/api/generate"])
def test_auto_pull_of_a_chat_call_pulls_onto_the_ollama_server(monkeypatch, chat_api_base):
    from fw_context_mcp.llm import ollama

    answers = [_Answer(404), _Answer(200)]
    posted = []
    pulled = []
    monkeypatch.setattr(ollama.httpx, "post", lambda url, json, timeout: posted.append(url) or answers.pop(0))
    monkeypatch.setattr(ollama, "_pull_model", lambda model, url: pulled.append((model, url)))

    cfg = _cfg(chat_api_base=chat_api_base, auto_pull=True, stream=False)

    assert ollama.call_ollama("hello", cfg) == "done"
    assert pulled == [(CHAT, REMOTE)]
    assert posted == [f"{REMOTE}/api/generate"] * 2
