"""An ``/api/tags`` answer that is not the answer of Ollama gives a status, not an exception.

The two ``check_setup`` functions called ``resp.json().get("models")``
outside of each ``try``.  A proxy that gives an HTML page with HTTP 200, or
JSON of a different shape, then raised out of ``semantic_search`` (it
catches only RuntimeError and OSError), ``check_ollama``, ``fw-context
init`` and the Ollama checks of doctor ("check crashed").
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig

URL = "http://127.0.0.1:11434"

ANSWERS = {
    "html": httpx.Response(200, text="<html><body>proxy login</body></html>"),
    "json list": httpx.Response(200, json=[1, 2]),
    "no models": httpx.Response(200, json={"error": "x"}),
    "models not a list": httpx.Response(200, json={"models": "x"}),
}


def _answer(monkeypatch, response: httpx.Response) -> None:
    def fake_get(url, **kwargs):
        response.request = httpx.Request("GET", url)
        return response

    monkeypatch.setattr(httpx, "get", fake_get)


@pytest.mark.parametrize("answer", sorted(ANSWERS))
@pytest.mark.parametrize(("module", "status"), [("fw_context_mcp.llm.ollama", "not_configured"),
                                                 ("fw_context_mcp.llm._diag", "error")])
def test_an_answer_that_is_not_ollama_gives_a_status(monkeypatch, module, status, answer):
    """`ollama.check_setup` gives the status of a server that does not run, `_diag` its "error"."""
    import importlib

    _answer(monkeypatch, ANSWERS[answer])

    result = importlib.import_module(module).check_setup(LLMConfig(ollama_url=URL))

    assert (result["status"], result["ollama_running"]) == (status, False), result
    assert result["message"].startswith(f"{URL}/api/tags did not give the model list of Ollama"), result


def test_a_setup_without_ollama_is_ok(monkeypatch):
    """Chat on an API and local embeddings: a proxy page on ollama_url gave "error", no server gave "ok"."""
    from fw_context_mcp.llm.ollama import check_setup

    _answer(monkeypatch, ANSWERS["html"])
    cfg = LLMConfig(ollama_url=URL, chat_api_base="http://127.0.0.1:4000/v1", chat_api_format="openai",
                    embed_model="sentence-transformers/all-MiniLM-L6-v2")

    result = check_setup(cfg)

    assert (result["status"], result["ollama_running"]) == ("ok", False), result


def test_the_embedding_model_check_of_load_does_not_raise(monkeypatch):
    """`_model_installed` raised KeyError for an entry without a name, out of `load`: each tool failed."""
    from fw_context_mcp.llm.auto_model import _model_installed

    _answer(monkeypatch, httpx.Response(200, json={"models": [{"size": 1}, {"name": "qwen3-embedding:0.6b"}]}))

    assert _model_installed(URL, "qwen3-embedding:0.6b") is True
    assert _model_installed(URL, "nomic-embed-text") is False


@pytest.mark.parametrize("module", ["fw_context_mcp.llm._diag", "fw_context_mcp.llm.auto_model"])
def test_a_url_with_a_port_that_is_not_valid(module):
    """httpx.InvalidURL is not an HTTPError, and the URL check of the config passes "http://localhost:abc"."""
    import importlib

    target = importlib.import_module(module)
    url = "http://localhost:abc"

    if module.endswith("_diag"):
        assert target.check_setup(LLMConfig(ollama_url=url))["status"] == "error"
    else:
        assert target._model_installed(url, "m") is False


def test_installed_models_gives_none(monkeypatch):
    from fw_context_mcp.llm._diag import installed_models

    _answer(monkeypatch, ANSWERS["html"])

    assert installed_models(URL) is None


def test_an_entry_without_a_name_does_not_count(monkeypatch):
    """`m["name"]` raised KeyError for an entry of another shape."""
    from fw_context_mcp.llm.ollama import check_setup

    _answer(monkeypatch, httpx.Response(200, json={"models": [
        {"name": "qwen3-embedding:0.6b", "details": "x"}, {"size": 1}, "text",
    ]}))

    result = check_setup(LLMConfig(ollama_url=URL, embed_model="qwen3-embedding:0.6b"))

    assert result["installed_models"] == ["qwen3-embedding:0.6b"]
    assert result["model_details"][0]["family"] == "unknown"


def test_the_doctor_check_does_not_crash(monkeypatch):
    from fw_context_mcp.deps._checks import check_ollama_running

    _answer(monkeypatch, ANSWERS["html"])

    result = check_ollama_running(LLMConfig(ollama_url=URL))

    assert result.status == "degraded" and "did not give the model list" in result.message, result


@pytest.mark.parametrize(("chat_api_base", "status"), [("", "not_configured"), ("http://127.0.0.1:4000/v1", "embedding_unavailable")])
def test_a_server_that_runs_gets_the_advice_to_correct_the_url(monkeypatch, chat_api_base, status):
    """A proxy page got "Start Ollama": the server runs, and ollama_url is wrong."""
    from fw_context_mcp.llm.ollama import check_setup

    _answer(monkeypatch, ANSWERS["html"])

    result = check_setup(LLMConfig(ollama_url=URL, chat_api_base=chat_api_base, chat_api_format="openai"))

    assert result["status"] == status, result
    assert "correct ollama_url so that it names an ollama server" in result["message"].lower(), result
    assert "start Ollama" not in result["message"], result


def test_init_says_ok_only_for_a_server_that_runs(monkeypatch, capsys):
    """`init` read only the status, and "ok" also comes for a setup that needs no Ollama."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.llm import auto_model, ollama

    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "ollama_running": False})
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: default)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr("fw_context_mcp.llm.configure.configure_llm_core", lambda *a, **k: {"status": "error", "message": "x"})

    _init_interactive._prompt_ollama_setup(Path("."), Config())

    out = capsys.readouterr().out
    assert "[ok] Ollama running" not in out and "[warn] Ollama cannot be used" in out, out


@pytest.mark.parametrize(("embed_model", "chat_api_base", "status"), [
    ("qwen3-embedding:0.6b", "", "not_configured"),
    ("sentence-transformers/all-MiniLM-L6-v2", "http://127.0.0.1:4000/v1", "ok"),
])
def test_an_http_error_of_a_server_is_the_case_of_a_proxy_page(monkeypatch, embed_model, chat_api_base, status):
    """A proxy with HTTP 502 gave "error": a setup that needs no Ollama was not "ok", and no advice named the URL."""
    from fw_context_mcp.llm.ollama import check_setup

    _answer(monkeypatch, httpx.Response(502, text="Bad Gateway"))

    result = check_setup(LLMConfig(ollama_url=URL, embed_model=embed_model, chat_api_base=chat_api_base,
                                   chat_api_format="openai"))

    assert (result["status"], result["ollama_running"]) == (status, False), result
    if status != "ok":
        assert result["message"].startswith(f"{URL}/api/tags gave HTTP 502"), result
        assert "correct ollama_url" in result["message"], result


def test_init_prints_the_message_of_an_error_with_a_running_server(monkeypatch, capsys):
    """The Ollama server of chat_api_base gives no model list: `init` said only "[ok] Ollama running"."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.llm import auto_model, ollama

    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {
        "status": "error", "ollama_running": True, "installed_models": [], "message": "chat server gives no model list",
    })
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, default=None, **kwargs: default)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)
    monkeypatch.setattr("fw_context_mcp.llm.configure.configure_llm_core", lambda *a, **k: {"status": "error", "message": "x"})

    _init_interactive._prompt_ollama_setup(Path("."), Config())

    assert "[info] chat server gives no model list" in capsys.readouterr().out
