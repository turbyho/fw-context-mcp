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


def test_the_ollama_backend_names_its_document_prompt():
    assert OllamaEmbedder(LLMConfig(embed_doc_prompt="")).document_settings == ""
    assert OllamaEmbedder(LLMConfig(embed_doc_prompt="passage: ")).document_settings == "doc_prompt=passage: "


def test_the_sentence_transformers_backend_names_its_prompt_and_its_dimension():
    pytest.importorskip("numpy")
    from fw_context_mcp.llm.st_embedder import SentenceTransformerEmbedder

    assert SentenceTransformerEmbedder(LLMConfig()).document_settings == ""
    settings = SentenceTransformerEmbedder(LLMConfig(embed_doc_prompt="passage: ", embed_dim=256)).document_settings
    assert settings == "doc_prompt=passage: ;dim=256"
