"""The settings of the embedder are inputs of the content hash of an embedding.

The model key holds the model name and the description version only.  The
chunk size (``max_tokens * 3``), the document prompt and the vector size
(MRL ``embed_dim``) also change the vector of a symbol.  Before, a change of
one of them kept the vectors of the old value: the run compared the fields
of the symbol only.
"""
from __future__ import annotations

from unittest import mock

import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.indexer._embedding import _build_embeddings, _embed_content_hash
from fw_context_mcp.llm.ollama import OllamaEmbedder
from tests import test_functional_fixes as fixes

CONFIG = "hash-deadbeef"
_seed = fixes.TestBuildEmbeddingsIncremental._seed

_LONG_BODY = "void foo() {\n" + "".join(f"    step_{i}();\n" for i in range(40)) + "}\n"


def _row(source: str, kind: str = "function") -> dict:
    return {
        "name": "foo", "qualified_name": "foo", "kind": kind, "file_path": "src/foo.c",
        "signature": "void foo()", "docstring": "", "summary": "", "source": source,
    }


def test_the_defaults_keep_the_hash_of_an_older_version():
    """Without settings and with a short body, an index keeps its hashes."""
    row = _row("void foo() {}")
    assert _embed_content_hash(row, max_body_chars=10_000, settings="") == _embed_content_hash(row)


def test_the_chunk_size_is_in_the_hash_of_a_long_body_only():
    long_row, short_row = _row(_LONG_BODY), _row("void foo() {}")
    assert _embed_content_hash(long_row, max_body_chars=60) != _embed_content_hash(long_row, max_body_chars=120)
    assert _embed_content_hash(short_row, max_body_chars=60) == _embed_content_hash(short_row, max_body_chars=120)
    # A kind without a body in the description: no chunks at all.
    variable = _row(_LONG_BODY, kind="varglobal")
    assert _embed_content_hash(variable, max_body_chars=60) == _embed_content_hash(variable, max_body_chars=120)


def test_the_settings_are_in_the_hash():
    row = _row("void foo() {}")
    assert _embed_content_hash(row, settings="doc_prompt=passage: ") != _embed_content_hash(row)


class _SettingsEmbedder(fixes._FakeEmbedder):
    document_settings = ""


def _run(conn, fake, tmpdir) -> None:
    with mock.patch("fw_context_mcp.indexer._embedding.get_embedder", return_value=fake):
        _build_embeddings(conn, CONFIG, None, tmpdir)


def test_a_new_document_prompt_embeds_again(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    fake = _SettingsEmbedder()
    _run(conn, fake, tmpdir)
    first = fake.calls
    _run(conn, fake, tmpdir)
    assert fake.calls == first, "the precondition: nothing changed, nothing to embed"

    fake.document_settings = "doc_prompt=passage: "
    _run(conn, fake, tmpdir)

    assert fake.calls > first


def test_a_new_chunk_size_embeds_a_long_body_again(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _SettingsEmbedder()
    fake.max_tokens = 20
    _run(conn, fake, tmpdir)
    first = fake.calls

    fake.max_tokens = 40
    _run(conn, fake, tmpdir)

    assert fake.calls > first


class _ContextEmbedder(_SettingsEmbedder):
    """A model whose context holds *limit* characters; a longer text fails, as Ollama with truncate=false."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.lengths: list[int] = []

    def embed_documents(self, descs):
        from fw_context_mcp.llm.embedder import EmbedderInputTooLong

        if any(len(text) > self.limit for text in descs):
            raise EmbedderInputTooLong("an input is longer than the context")
        self.lengths.extend(len(text) for text in descs)
        return super().embed_documents(descs)


def _chunks_of_foo(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM embeddings e JOIN symbols s ON s.id = e.symbol_id WHERE s.usr = 'u_foo'"
    ).fetchone()[0]


def test_a_chunk_longer_than_the_context_goes_in_smaller_chunks(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _ContextEmbedder(limit=600)
    fake.max_tokens = 1000  # chunks of 3000 characters: the body is one chunk, too long

    _run(conn, fake, tmpdir)

    # The step can embed twice: the fake gives 8 dimensions, and the step
    # restarts for a vec0 table of another dimension.
    assert fake.lengths and max(fake.lengths) <= 600
    assert _chunks_of_foo(conn) > 1


def test_a_symbol_that_never_fits_is_not_embedded_and_the_step_reports_it(populated_db, tmpdir):
    from fw_context_mcp.indexer._embedding import EmbeddingsIncomplete

    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _ContextEmbedder(limit=10)

    with pytest.raises(EmbeddingsIncomplete):
        _run(conn, fake, tmpdir)

    assert _chunks_of_foo(conn) == 0


def test_the_ollama_backend_names_its_document_prompt():
    assert OllamaEmbedder(LLMConfig(embed_doc_prompt="")).document_settings == ""
    assert OllamaEmbedder(LLMConfig(embed_doc_prompt="passage: ")).document_settings == "doc_prompt=passage: "


def test_the_sentence_transformers_backend_names_its_prompt_and_its_dimension():
    pytest.importorskip("numpy")
    from fw_context_mcp.llm.st_embedder import SentenceTransformerEmbedder

    assert SentenceTransformerEmbedder(LLMConfig()).document_settings == ""
    settings = SentenceTransformerEmbedder(LLMConfig(embed_doc_prompt="passage: ", embed_dim=256)).document_settings
    assert settings == "doc_prompt=passage: ;dim=256"


def test_a_split_symbol_is_not_embedded_again_on_the_next_run(populated_db, tmpdir):
    """The split depends on the symbol only, thus its content hash stays valid."""
    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _ContextEmbedder(limit=600)
    fake.max_tokens = 1000
    _run(conn, fake, tmpdir)
    embedded, chunks = len(fake.lengths), _chunks_of_foo(conn)

    _run(conn, fake, tmpdir)

    assert len(fake.lengths) == embedded
    assert _chunks_of_foo(conn) == chunks > 1


def test_a_reparsed_split_symbol_takes_its_chunks_from_the_stash(populated_db, tmpdir):
    from tests.test_embedding_stash import _embeddings_of_foo, _reparse

    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _ContextEmbedder(limit=600)
    fake.max_tokens = 1000
    _run(conn, fake, tmpdir)
    embedded, before = len(fake.lengths), _embeddings_of_foo(conn)

    _reparse(conn, _LONG_BODY)
    _run(conn, fake, tmpdir)

    assert len(fake.lengths) == embedded, "the model must not be called for an unchanged symbol"
    assert _embeddings_of_foo(conn) == before


class _FlakyEmbedder(_ContextEmbedder):
    """The request of one symbol on its own fails *failures* times (a timeout), then succeeds."""

    def __init__(self, limit: int, failures: int):
        super().__init__(limit)
        self.failures = failures
        self.too_long_seen = False

    def embed_documents(self, descs):
        from fw_context_mcp.llm.embedder import EmbedderInputTooLong
        from fw_context_mcp.llm.ollama import OllamaError

        if self.too_long_seen and self.failures > 0:
            self.failures -= 1
            raise OllamaError("timeout")
        try:
            return super().embed_documents(descs)
        except EmbedderInputTooLong:
            self.too_long_seen = True
            raise


def _run_without_pauses(conn, fake, tmpdir) -> None:
    with mock.patch("fw_context_mcp.indexer._embedding.time.sleep"):
        _run(conn, fake, tmpdir)


def test_a_timeout_of_a_smaller_chunk_request_goes_again(populated_db, tmpdir):
    """Before, the error left the step: no retry, and the rows of the batches before it were lost."""
    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _FlakyEmbedder(limit=600, failures=2)
    fake.max_tokens = 1000

    _run_without_pauses(conn, fake, tmpdir)

    assert _chunks_of_foo(conn) > 1


def test_a_smaller_chunk_request_that_always_fails_is_reported(populated_db, tmpdir):
    from fw_context_mcp.indexer._embedding import EmbeddingsIncomplete

    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    fake = _FlakyEmbedder(limit=600, failures=99)
    fake.max_tokens = 1000

    with pytest.raises(EmbeddingsIncomplete, match="timeout"):
        _run_without_pauses(conn, fake, tmpdir)

    assert fake.failures == 99 - 3, "three requests in all, as in the batch loop"


class _CountingEmbedder(_ContextEmbedder):
    def __init__(self, limit: int):
        super().__init__(limit)
        self.requests = 0

    def embed_documents(self, descs):
        self.requests += 1
        return super().embed_documents(descs)


def test_a_symbol_without_a_body_that_does_not_fit_gets_one_request_on_its_own(populated_db, tmpdir):
    """A smaller chunk size does not change its text, thus the same request fails again."""
    from fw_context_mcp.indexer._embedding import EmbeddingsIncomplete

    conn = populated_db
    _seed(conn, CONFIG, _LONG_BODY)
    conn.execute("UPDATE symbols SET kind = 'struct' WHERE usr = 'u_foo'")
    conn.commit()
    fake = _CountingEmbedder(limit=10)

    with pytest.raises(EmbeddingsIncomplete):
        _run(conn, fake, tmpdir)

    assert fake.requests == 2, "the batch, then the symbol on its own"
