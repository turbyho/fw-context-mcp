"""The embeddings through an API profile (``llm.api_embedder``, ``[llm] embed_profile`` of an API).

No test reaches a server: ``httpx.post`` and ``httpx.get`` are replaced.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fw_context_mcp.config import settings
from fw_context_mcp.config.settings import ConfigError
from fw_context_mcp.llm import api_embedder
from fw_context_mcp.llm.embedder import EmbedderContextUnknown, EmbedderError, EmbedderInputTooLong

OLLAMA = '[llm.profiles.local]\nprovider = "ollama"\nurl = "http://localhost:11434"\nmodel = "chat:14b"\n'
OPENROUTER = '[llm.profiles.or]\nprovider = "openrouter"\nmodel = "openai/gpt-6-luna"\n'
PROXY = ('[llm.profiles.proxy]\nprovider = "openai_compatible"\nurl = "http://127.0.0.1:4000/v1"\nmodel = "m"\n'
         'api_key_env = "PROXY_KEY"\n')


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(path.parent))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(api_embedder, "_CONTEXTS", {})
    # The key cache of the process: a key of another test must not answer here.
    from fw_context_mcp.llm import secrets

    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    settings._config_cache.clear()
    return tmp_path


def _load(text: str):
    settings._GLOBAL_CONFIG_PATH.write_text(text, encoding="utf-8")
    settings._config_cache.clear()
    return settings.load(None).llm


def _openrouter(model: str = "mistralai/codestral-embed-2505", extra: str = ""):
    return _load(f'[llm]\nprofile = "local"\nembed_profile = "or"\nembed_model = "{model}"\n\n'
                 + OLLAMA + OPENROUTER + extra)


class _Api:
    """A fake embedding API: records each request; *answer* maps the inputs to (status, body)."""

    def __init__(self, monkeypatch, answer=None) -> None:
        self.sent: list[dict] = []
        self.answer = answer or (lambda inputs: (200, {"data": [
            {"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(len(inputs)))]}))

        def _post(url, json, headers, timeout):
            self.sent.append({"url": url, "json": json, "headers": headers})
            status, body = self.answer(json["input"])
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", _post)


# ── The config ──


def test_an_openrouter_profile_of_the_embeddings_routes_to_hosts_without_data_retention(home):
    llm = _openrouter()

    assert (llm.embed_provider, llm.embed_api_base) == ("openrouter", "https://openrouter.ai/api/v1")
    assert llm.embed_routing == {"zdr": True, "data_collection": "deny"}
    # The chat of the Ollama profile keeps its server.
    assert llm.ollama_url == "http://localhost:11434" and llm.provider == "ollama"


def test_embed_routing_of_the_profile_replaces_a_default(home):
    llm = _openrouter(extra='\n[llm.profiles.or.embed_routing]\nzdr = false\n')

    assert llm.embed_routing == {"zdr": False, "data_collection": "deny"}


def test_an_api_profile_of_the_embeddings_needs_embed_model(home):
    """The automatic model is an Ollama tag, which the API does not serve."""
    with pytest.raises(ConfigError, match="embed_model must name"):
        _load('[llm]\nprofile = "local"\nembed_profile = "or"\n\n' + OLLAMA + OPENROUTER)


def test_a_provider_without_an_embedding_endpoint_is_refused(home):
    with pytest.raises(ConfigError, match="embed_profile = 'ds' must name a profile with provider"):
        _load('[llm]\nprofile = "local"\nembed_profile = "ds"\nembed_model = "x"\n\n' + OLLAMA
              + '[llm.profiles.ds]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n')


def test_the_factory_gives_the_api_embedder_and_no_ollama_check(home):
    from fw_context_mcp.llm.embedder_factory import get_embedder, uses_ollama

    llm = _openrouter()

    assert isinstance(get_embedder(llm), api_embedder.ApiEmbedder) and not uses_ollama(llm)


def test_load_tells_that_the_embeddings_leave_the_machine(home, tmp_path, capsys):
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text('[project]\nid = "abc"\n', encoding="utf-8")
    settings._GLOBAL_CONFIG_PATH.write_text(
        '[llm]\nprofile = "local"\nembed_profile = "or"\nembed_model = "m"\n\n' + OLLAMA + OPENROUTER,
        encoding="utf-8")
    settings._config_cache.clear()

    settings.load(root)

    assert "The embeddings of LLM profile 'local' go to https://openrouter.ai/api/v1" in capsys.readouterr().err


def test_the_check_of_the_embedding_model_skips_an_api(home):
    from fw_context_mcp.deps._checks import check_ollama_embed_model

    result = check_ollama_embed_model(_openrouter())

    assert result.status == "skipped" and "runs on the API" in result.message


# ── The requests ──


def test_a_request_sends_the_model_the_routing_and_the_key_of_the_provider(home, monkeypatch):
    api = _Api(monkeypatch)
    embedder = api_embedder.ApiEmbedder(_openrouter())

    vectors = embedder.embed_documents(["a", "b"])

    sent = api.sent[0]
    assert sent["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert sent["json"] == {"model": "mistralai/codestral-embed-2505", "input": ["a", "b"],
                            "provider": {"zdr": True, "data_collection": "deny"}}
    assert sent["headers"]["Authorization"] == "Bearer or-test-key"
    # The answer is in the order of "index", not of the list.
    assert vectors == [[0.0, 1.0], [1.0, 1.0]] and embedder.dim == 2


def test_another_api_gets_its_own_key_variable_and_no_routing(home, monkeypatch):
    monkeypatch.setenv("PROXY_KEY", "proxy-key")
    api = _Api(monkeypatch)
    llm = _load('[llm]\nprofile = "local"\nembed_profile = "proxy"\nembed_model = "text-embedding-3-large"\n\n'
                + OLLAMA + PROXY)

    api_embedder.ApiEmbedder(llm).embed_queries(["q"])

    assert api.sent[0]["url"] == "http://127.0.0.1:4000/v1/embeddings"
    assert "provider" not in api.sent[0]["json"] and api.sent[0]["headers"]["Authorization"] == "Bearer proxy-key"


def test_the_query_instruction_goes_in_front_of_a_query(home, monkeypatch):
    api = _Api(monkeypatch)
    llm = _openrouter()
    llm.embed_query_prompt = "search_query: "

    api_embedder.ApiEmbedder(llm).embed_queries(["uart"])

    assert api.sent[0]["json"]["input"] == ["search_query: uart"]


def test_a_refused_batch_goes_one_input_at_a_time(home, monkeypatch):
    """Measured 2026-10-22: the zdr host of gemini-embedding-2 gives 404 for a batch, 200 for one input."""
    def answer(inputs):
        if len(inputs) > 1:
            return 404, {"error": {"message": "No endpoints found matching your data policy"}}
        return 200, {"data": [{"index": 0, "embedding": [len(inputs[0])]}]}

    api = _Api(monkeypatch, answer)
    embedder = api_embedder.ApiEmbedder(_openrouter("google/gemini-embedding-2"))

    assert embedder.embed_documents(["a", "bb"]) == [[1], [2]]
    embedder.embed_documents(["c", "dd"])

    # The second batch goes one input at a time at once: no second refused batch.
    assert [len(s["json"]["input"]) for s in api.sent] == [2, 1, 1, 1, 1]


def test_an_input_longer_than_the_context_is_told_to_the_caller(home, monkeypatch):
    _Api(monkeypatch, lambda inputs: (400, {"error": {"message": "input exceeds the maximum context length"}}))

    with pytest.raises(EmbedderInputTooLong):
        api_embedder.ApiEmbedder(_openrouter()).embed_documents(["long"])


def test_another_error_is_an_embedder_error(home, monkeypatch):
    _Api(monkeypatch, lambda inputs: (401, {"error": {"message": "no key"}}))

    with pytest.raises(EmbedderError, match="HTTP 401: no key"):
        api_embedder.ApiEmbedder(_openrouter()).embed_documents(["a"])


def test_an_error_of_the_api_is_a_failed_batch_and_not_the_end_of_the_index_run():
    """Before: one HTTP 502 left the retry loop of the embedding step (SAFE_EXCEPT) and stopped the run."""
    from fw_context_mcp.utils import SAFE_EXCEPT

    assert issubclass(api_embedder.ApiEmbedderError, SAFE_EXCEPT)
    assert issubclass(api_embedder._HttpError, RuntimeError)


def test_the_message_of_an_error_holds_no_body(home, monkeypatch):
    """A proxy can echo the request, the key too: only error.message goes to the log and the manifest."""
    def answer(inputs):
        return 403, {"error": {"message": "denied"}, "echo": "Bearer or-test-key"}

    _Api(monkeypatch, answer)

    with pytest.raises(EmbedderError) as caught:
        api_embedder.ApiEmbedder(_openrouter()).embed_documents(["a"])

    assert "or-test-key" not in str(caught.value) and "denied" in str(caught.value)


def test_the_search_asks_for_the_key_of_the_index_run(home):
    """Before: the index stored "openrouter:<model>", the search without sqlite-vec asked for "<model>"."""
    from fw_context_mcp.indexer._embedding import _embed_model_key

    llm = _openrouter()

    assert llm.embed_key() == _embed_model_key(api_embedder.ApiEmbedder(llm).name, True)


def test_a_qwen3_model_of_an_api_gets_the_query_format_of_its_model_card(home):
    """Measured 2026-10-21: without the format, MRR@10 of qwen3-embedding fell from 0.68 to 0.43."""
    llm = _openrouter("qwen/qwen3-embedding-8b")

    assert llm.embed_query_prompt.startswith("Instruct: ")


def test_a_wrong_value_of_embed_routing_is_an_error(home):
    with pytest.raises(ConfigError, match="embed_routing"):
        _openrouter(extra='\n[llm.profiles.or.embed_routing]\nzdr = "yes"\n')


# ── The context of the model ──


def _models(monkeypatch, models: list[dict]) -> list[str]:
    asked: list[str] = []

    def _get(url, params, timeout):
        asked.append(url)
        return httpx.Response(200, json={"data": models}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _get)
    return asked


def test_the_context_comes_from_the_model_list_of_openrouter_one_time(home, monkeypatch):
    asked = _models(monkeypatch, [{"id": "mistralai/codestral-embed-2505", "context_length": 8192}])

    assert api_embedder.ApiEmbedder(_openrouter()).max_tokens == 8192
    assert api_embedder.ApiEmbedder(_openrouter()).max_tokens == 8192
    assert asked == ["https://openrouter.ai/api/v1/models"]


def test_embed_context_of_the_profile_wins(home, monkeypatch):
    _models(monkeypatch, [])

    assert api_embedder.ApiEmbedder(_openrouter(extra="embed_context = 2048\n")).max_tokens == 2048


def test_a_model_that_the_list_does_not_name_has_no_context(home, monkeypatch):
    _models(monkeypatch, [{"id": "other", "context_length": 512}])

    with pytest.raises(EmbedderContextUnknown, match="no embedding model"):
        _ = api_embedder.ApiEmbedder(_openrouter()).max_tokens


def test_another_api_without_embed_context_has_no_context(home):
    """No value is guessed: it sets the chunks and the content hash of each long body."""
    llm = _load('[llm]\nprofile = "local"\nembed_profile = "proxy"\nembed_model = "m"\n\n' + OLLAMA + PROXY)

    with pytest.raises(EmbedderContextUnknown, match="set embed_context"):
        _ = api_embedder.ApiEmbedder(llm).max_tokens


def test_the_vectors_of_an_api_model_have_a_name_of_their_own(home):
    assert api_embedder.ApiEmbedder(_openrouter("qwen/qwen3-embedding-8b")).name == "openrouter:qwen/qwen3-embedding-8b"
