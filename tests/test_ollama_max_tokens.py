"""The number of tokens of one input that Ollama embeds: OllamaEmbedder.max_tokens.

Two values set it, and the smaller one applies: the context length of the
server for the loaded model (``/api/ps``), and the context length that the
model was trained for (``/api/show``, a POST).  Before, the code sent a GET
to /api/show (405) and read "context_length", thus it found no value, and
each model got the default for its name: 32768 for qwen3-embedding:8b,
while the server cut each input at 4096 tokens.  Now a document request
asks the server not to cut, and a server without a value gives an error,
not a guessed value.
"""
from __future__ import annotations

from unittest import mock

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import ollama
from fw_context_mcp.llm.ollama import OllamaEmbedder

_SHOW = {"model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}}


class _FakeServer:
    """Answers /api/show, /api/ps and /api/embed; /api/embed loads the model."""

    def __init__(self, *, show=_SHOW, served=4096, loaded=True, name="qwen3-embedding:8b", down=False):
        self.show, self.served, self.loaded, self.name, self.down = show, served, loaded, name, down
        self.embeds = 0
        self.keep_alive: list = []

    def _answer(self, method: str, url: str, body: dict) -> httpx.Response:
        request = httpx.Request(method, url)
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if url.endswith("/api/show"):
            if method != "POST":
                return httpx.Response(405, json={"error": "method not allowed"}, request=request)
            return httpx.Response(200 if self.show else 404, json=self.show or {"error": "no"}, request=request)
        if url.endswith("/api/ps"):
            models = [{"name": self.name, "model": self.name, "context_length": self.served}] if self.loaded else []
            return httpx.Response(200, json={"models": models}, request=request)
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": self.name}]}, request=request)
        if url.endswith("/api/embed"):
            self.embeds += 1
            self.keep_alive.append(body.get("keep_alive"))
            self.loaded = body.get("keep_alive") not in ("0", 0)
            return httpx.Response(200, json={"embeddings": [[0.0] * 4 for _ in body["input"]]}, request=request)
        raise AssertionError(url)

    def post(self, url, json=None, **_):
        return self._answer("POST", url, json or {})

    def get(self, url, **_):
        return self._answer("GET", url, {})


def _max_tokens(server: _FakeServer, model: str = "qwen3-embedding:8b") -> int:
    embedder = OllamaEmbedder(LLMConfig(embed_model=model))
    with mock.patch.object(ollama.httpx, "post", side_effect=server.post), \
            mock.patch.object(ollama.httpx, "get", side_effect=server.get):
        return embedder.max_tokens


def test_the_context_of_the_server_applies_when_it_is_smaller():
    assert _max_tokens(_FakeServer(served=4096)) == 4096


def test_the_trained_context_applies_when_it_is_smaller():
    assert _max_tokens(_FakeServer(served=131072)) == 40960


def test_a_model_that_is_not_loaded_is_loaded_first():
    server = _FakeServer(loaded=False)
    assert _max_tokens(server) == 4096
    assert server.embeds == 1


def test_a_loaded_model_gets_the_request_of_the_run_first():
    """Another client can have loaded the model with another num_ctx."""
    server = _FakeServer(loaded=True)
    assert _max_tokens(server) == 4096
    assert server.embeds == 1


def test_the_tag_of_a_registry_name_follows_the_last_slash():
    server = _FakeServer(name="registry.local:5000/team/embed:latest", show=None, served=2048)
    assert _max_tokens(server, "registry.local:5000/team/embed") == 2048


def _embed_payload(query: bool) -> dict:
    sent = {}

    def post(url, json=None, **_):
        sent.update(json)
        return httpx.Response(200, json={"embeddings": [[0.0]]}, request=httpx.Request("POST", url))

    with mock.patch.object(ollama.httpx, "post", side_effect=post):
        ollama._call_ollama_embed_impl(["text"], LLMConfig(embed_model="m"), query=query)
    return sent


def test_a_document_is_not_cut_by_the_server_and_a_query_is():
    assert _embed_payload(query=False)["truncate"] is False
    assert "truncate" not in _embed_payload(query=True)


def test_an_input_longer_than_the_context_raises_input_too_long():
    from fw_context_mcp.llm.embedder import EmbedderInputTooLong

    def post(url, json=None, **_):
        return httpx.Response(
            400, json={"error": "the input length exceeds the context length"}, request=httpx.Request("POST", url),
        )

    with mock.patch.object(ollama.httpx, "post", side_effect=post), pytest.raises(EmbedderInputTooLong):
        ollama._call_ollama_embed_impl(["text"], LLMConfig(embed_model="m"), query=False)


def test_a_name_without_a_tag_means_latest():
    server = _FakeServer(name="mxbai-embed-large:latest", served=512, show=None)
    assert _max_tokens(server, "mxbai-embed-large") == 512


def test_each_read_asks_the_server():
    """A kept value differs from the value of the server after a change of its setting."""
    server = _FakeServer(loaded=False)
    embedder = OllamaEmbedder(LLMConfig(embed_model="qwen3-embedding:8b"))
    with mock.patch.object(ollama.httpx, "post", side_effect=server.post), \
            mock.patch.object(ollama.httpx, "get", side_effect=server.get):
        assert embedder.max_tokens == 4096
        server.served = 8192
        assert embedder.max_tokens == 8192


def test_a_keep_alive_of_zero_still_gives_the_context():
    """The second warm-up keeps the model loaded long enough for /api/ps."""
    server = _FakeServer(loaded=False)
    embedder = OllamaEmbedder(LLMConfig(embed_model="qwen3-embedding:8b", keep_alive="0"))
    with mock.patch.object(ollama.httpx, "post", side_effect=server.post), \
            mock.patch.object(ollama.httpx, "get", side_effect=server.get):
        assert embedder.max_tokens == 4096
    assert server.keep_alive == ["0", "30s"]


def test_another_error_400_is_not_input_too_long():
    from fw_context_mcp.llm.embedder import EmbedderInputTooLong

    def post(url, json=None, **_):
        return httpx.Response(400, json={"error": "invalid input type"}, request=httpx.Request("POST", url))

    with mock.patch.object(ollama.httpx, "post", side_effect=post), pytest.raises(ollama.OllamaError) as raised:
        ollama._call_ollama_embed_impl(["text"], LLMConfig(embed_model="m"), query=False)
    assert not isinstance(raised.value, EmbedderInputTooLong)


def test_without_a_value_max_tokens_raises_and_keeps_nothing():
    """A guessed value changes the chunks and the content hash of each long body."""
    from fw_context_mcp.llm.embedder import EmbedderContextUnknown

    server = _FakeServer(down=True)
    embedder = OllamaEmbedder(LLMConfig(embed_model="qwen3-embedding:8b"))
    with mock.patch.object(ollama.httpx, "post", side_effect=server.post), \
            mock.patch.object(ollama.httpx, "get", side_effect=server.get):
        with pytest.raises(EmbedderContextUnknown):
            _ = embedder.max_tokens
        server.down = False
        assert embedder.max_tokens == 4096, "the next read asks the server again"


class _UnloadingServer(_FakeServer):
    """Another client makes the server unload the model after the first embedding request."""

    def _answer(self, method: str, url: str, body: dict) -> httpx.Response:
        response = super()._answer(method, url, body)
        if url.endswith("/api/embed") and self.embeds == 1:
            self.loaded = False
        return response


def test_a_model_unloaded_before_the_read_is_loaded_again():
    server = _UnloadingServer(loaded=False)
    assert _max_tokens(server) == 4096
    assert server.embeds == 2


def test_a_server_that_never_names_the_context_raises():
    from fw_context_mcp.llm.embedder import EmbedderContextUnknown

    server = _FakeServer(served=None)
    with pytest.raises(EmbedderContextUnknown, match="update Ollama"):
        _max_tokens(server)
    assert server.embeds == 2


def test_an_unknown_context_stops_the_embedding_step(populated_db, tmpdir):
    from fw_context_mcp.indexer._embedding import EmbeddingsIncomplete, _build_embeddings

    server = _FakeServer(served=None)
    cfg = LLMConfig(embed_model="qwen3-embedding:8b")
    embedder = OllamaEmbedder(cfg)
    with mock.patch.object(ollama.httpx, "post", side_effect=server.post), \
            mock.patch.object(ollama.httpx, "get", side_effect=server.get), \
            mock.patch("fw_context_mcp.indexer._embedding.httpx.get", side_effect=server.get), \
            mock.patch("fw_context_mcp.indexer._embedding.get_embedder", return_value=embedder), \
            pytest.raises(EmbeddingsIncomplete, match="no context length"):
        _build_embeddings(populated_db, "hash-deadbeef", cfg, tmpdir)
