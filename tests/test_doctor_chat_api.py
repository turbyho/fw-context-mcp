"""Doctor looks for the chat model on the Ollama server of the chat calls.

That server is ``ollama_url``, or the Ollama server that ``chat_api_base``
names (`chat_router.chat_ollama_server`).  The doctor check looked only on
``ollama_url`` and did not read ``chat_api_base``:

- With an OpenAI-compatible API, it reported a name such as
  ``deepseek-chat`` missing on Ollama, and ``doctor --fix`` tried to pull
  it from Ollama, which fails.
- With ``chat_api_base`` on a different Ollama server, it looked on
  ``ollama_url`` and pulled the model there, and the chat calls still did
  not find it.

The check asks the chat server itself: ``_diag.check_setup``, which
``semantic_search`` calls on each query, does not ask it.
"""

from __future__ import annotations

import httpx
import pytest

LOCAL = "http://localhost:11434"
REMOTE = "http://gpu-box:11434"
EMBED = "qwen3-embedding:0.6b"
CHAT = "qwen2.5-coder:14b"


class _Tags:
    """The answer of ``/api/tags`` of an Ollama server that holds the models *names*."""

    def __init__(self, names):
        self._names = names

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": name} for name in self._names]}


class _Html:
    """The answer of a proxy that gives an HTML page with HTTP 200."""

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.fixture
def servers(monkeypatch):
    """Map of the server URL to its model names, or to an answer object.

    A URL that is not in the map does not answer.  ``servers.asked`` lists
    the servers that a call asked.
    """

    class _Servers(dict):
        asked: list[str]

    known = _Servers()
    known.asked = []

    def _get(url, timeout):
        server = url.removesuffix("/api/tags")
        known.asked.append(server)
        if server not in known:
            raise httpx.ConnectError("connection refused")
        answer = known[server]
        return answer if isinstance(answer, _Html) else _Tags(answer)

    monkeypatch.setattr(httpx, "get", _get)
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


def _cloud():
    return _cfg(model="deepseek-chat", chat_api_base="https://api.deepseek.com/v1")


def _check(cfg):
    from fw_context_mcp.deps._checks import check_ollama_chat_model

    return check_ollama_chat_model(cfg)


@pytest.mark.parametrize(("chat_api_base", "chat_api_format", "server"), [
    (None, "auto", LOCAL),
    ("https://api.deepseek.com/v1", "auto", None),
    ("http://gpu-box:11434", "auto", REMOTE),
    ("http://gpu-box:11434/", "auto", REMOTE),
    ("http://proxy/ollama/api", "auto", "http://proxy/ollama"),
    ("http://proxy/ollama/api/generate", "auto", "http://proxy/ollama"),
    ("http://proxy/ollama", "ollama", "http://proxy/ollama"),
    ("http://gpu-box:11434", "openai", None),
])
def test_the_chat_server_follows_the_format_of_the_chat_calls(chat_api_base, chat_api_format, server):
    from fw_context_mcp.llm.chat_router import chat_ollama_server

    assert chat_ollama_server(_cfg(chat_api_base=chat_api_base, chat_api_format=chat_api_format)) == server


def test_the_check_skips_a_chat_model_of_an_openai_compatible_api(servers):
    servers[LOCAL] = [EMBED]

    result = _check(_cloud())

    assert result.status == "skipped", result
    assert "OpenAI-compatible API" in result.message
    assert result.fix_cmd is None
    assert servers.asked == [], "no Ollama server holds the model"


def test_the_fix_does_not_pull_the_chat_model_of_an_openai_compatible_api(tmp_path, monkeypatch):
    """`run_fixes` skips a skipped check; the fix itself must also refuse the pull."""
    from fw_context_mcp.deps import DepCheckResult, _fixes

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "local.toml").write_text(
        '[llm]\nchat_api_base = "https://api.deepseek.com/v1"\nmodel = "deepseek-chat"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(_fixes, "_ollama_pull", lambda model, url: calls.append(model) or (True, "pulled"))

    ok, message = _fixes.fix_chat_model(
        DepCheckResult("chat-model", "missing", "", fix_cmd="ollama pull deepseek-chat"), str(root))

    assert (ok, calls) == (False, []), message
    assert "OpenAI-compatible API" in message


@pytest.mark.parametrize("chat_api_base", [None, LOCAL])
def test_a_missing_chat_model_on_the_server_of_ollama_url_is_reported(servers, chat_api_base):
    """A chat_api_base on the server of ollama_url is an Ollama server: the model must be there."""
    servers[LOCAL] = [EMBED]

    result = _check(_cfg(chat_api_base=chat_api_base))

    assert result.status == "missing", result
    assert result.fix_cmd == f"ollama pull {CHAT}"


def test_a_chat_model_on_the_server_of_ollama_url_is_ok(servers):
    servers[LOCAL] = [EMBED, CHAT]

    assert _check(_cfg()).status == "ok"


def test_the_chat_model_is_looked_for_on_the_ollama_server_of_chat_api_base(servers):
    servers[LOCAL] = [EMBED]
    servers[REMOTE] = [CHAT]

    assert _check(_cfg(chat_api_base=REMOTE)).status == "ok"
    assert servers.asked == [REMOTE]


def test_a_chat_model_that_only_ollama_url_holds_is_missing_on_chat_api_base(servers):
    servers[LOCAL] = [EMBED, CHAT]
    servers[REMOTE] = []

    assert _check(_cfg(chat_api_base=REMOTE)).status == "missing"


def test_the_chat_server_is_checked_when_ollama_url_does_not_answer(servers):
    """Chat on a remote Ollama and local embeddings: the server of ollama_url is not necessary."""
    servers[REMOTE] = []

    assert _check(_cfg(chat_api_base=REMOTE)).status == "missing"


def test_the_fix_pulls_the_chat_model_onto_the_ollama_server_of_chat_api_base(tmp_path, monkeypatch):
    from fw_context_mcp.deps import DepCheckResult, _fixes

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "local.toml").write_text(f'[llm]\nchat_api_base = "{REMOTE}"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(_fixes, "_ollama_pull", lambda model, url: calls.append((model, url)) or (True, "pulled"))

    _fixes.fix_chat_model(DepCheckResult("chat-model", "missing", "", fix_cmd=f"ollama pull {CHAT}"), str(root))

    assert calls == [(CHAT, REMOTE)]


@pytest.mark.parametrize("answer", ["none", "html"])
def test_a_chat_server_without_a_model_list_is_degraded(servers, answer):
    servers[LOCAL] = [EMBED]
    if answer == "html":
        servers[REMOTE] = _Html()

    result = _check(_cfg(chat_api_base=REMOTE))

    assert result.status == "degraded", result
    assert REMOTE in result.message


def test_a_url_that_is_not_valid_gives_no_model_list(monkeypatch):
    from fw_context_mcp.llm._diag import installed_models

    def _invalid(url, timeout):
        raise httpx.InvalidURL("Invalid port: 'abc'")

    monkeypatch.setattr(httpx, "get", _invalid)

    assert installed_models("http://gpu:abc") is None


def test_a_chat_api_format_that_is_not_valid_is_an_error_and_not_a_crash(servers):
    servers[LOCAL] = [EMBED]

    result = _check(_cfg(chat_api_base=REMOTE, chat_api_format="grpc"))

    assert result.status == "error", result
    assert "Invalid chat_api_format" in result.message


def test_the_diagnosis_of_semantic_search_does_not_ask_the_chat_server(servers):
    """`semantic_search` calls `_diag.check_setup` on each query: a chat server that does not answer made it wait."""
    from fw_context_mcp.llm import _diag

    servers[LOCAL] = [EMBED]

    assert _diag.check_setup(_cfg(chat_api_base=REMOTE))["ollama_running"] is True
    assert servers.asked == [LOCAL]


def test_the_fix_does_not_pull_for_a_chat_server_without_a_model_list(servers, monkeypatch):
    """`run_fixes` calls the fix for "degraded": the pull took the name "unknown"."""
    from fw_context_mcp.deps import _fixes, run_fixes

    servers[LOCAL] = [EMBED]
    calls = []
    monkeypatch.setattr(_fixes, "_ollama_pull", lambda model, url: calls.append(model) or (True, "pulled"))

    [result] = run_fixes([_check(_cfg(chat_api_base=REMOTE))])

    assert calls == []
    assert REMOTE in result.message, "the message of the check stays"
