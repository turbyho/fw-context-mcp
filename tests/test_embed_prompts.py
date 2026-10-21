"""The instruction of a query and of a document for the embedding models (``config.settings._apply_embed_prompt_defaults``).

Measured 2026-10-21 (plans/eval/embed-models-2026-10-21): qwen3-embedding
with the task and the query glued together found the right symbol much less
often (MRR@10 0.432) than with the format of the model card (0.682), and
nomic-embed-text gained 0.07 with its task prefixes.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from fw_context_mcp.config.settings import LLMConfig, _apply_embed_prompt_defaults
from fw_context_mcp.llm.ollama import call_ollama_embed

TASK = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
QWEN3 = f"Instruct: {TASK}\nQuery:"


def _defaults(model: str, query_prompt: str | None = None, doc_prompt: str | None = None) -> LLMConfig:
    cfg = LLMConfig(embed_model=model, embed_query_prompt=query_prompt, embed_doc_prompt=doc_prompt)
    _apply_embed_prompt_defaults(cfg)
    return cfg


@pytest.fixture
def auto_model_cache():
    """The auto-detected model is cached for the process: each test starts and ends without it."""
    from fw_context_mcp.llm import auto_model

    auto_model._reset_auto_model_cache()
    yield auto_model
    auto_model._reset_auto_model_cache()


@pytest.mark.parametrize("model", ["qwen3-embedding:8b", "qwen3-embedding:4b", "qwen3-embedding:0.6b"])
def test_a_qwen3_query_has_the_format_of_the_model_card(model):
    """Model card: "Instruct: {task}\\nQuery:{query}"; a document gets no instruction."""
    cfg = _defaults(model)

    assert (cfg.embed_query_prompt, cfg.embed_doc_prompt) == (QWEN3, "")


def test_the_old_default_in_a_config_is_replaced():
    """The template of older releases showed it, thus a config can hold it (one project of the operator does)."""
    assert _defaults("qwen3-embedding:8b", query_prompt=TASK).embed_query_prompt == QWEN3


@pytest.mark.parametrize("prompt", ["Find firmware code: ", f"{QWEN3} ", f"{TASK} More words.", f"{TASK} "],
                         ids=["other", "model card with a space", "old text and more", "old text and a space"])
def test_another_instruction_of_the_user_stays(prompt):
    """Only the exact old default is replaced: a text that only starts with it is a choice of the user."""
    assert _defaults("qwen3-embedding:8b", query_prompt=prompt).embed_query_prompt == prompt


def test_the_old_task_stays_for_another_model():
    """The replacement is for qwen3-embedding only: for another model the text is a choice of the user."""
    assert _defaults("mxbai-embed-large", query_prompt=TASK).embed_query_prompt == TASK


@pytest.mark.parametrize("model", ["qwen3-embedding:8b", "nomic-embed-text:latest", "mxbai-embed-large"])
def test_an_empty_prompt_turns_the_default_off(model):
    """The docs promise it; before, "" was "not set" and got the default, also the document prefix of nomic."""
    cfg = _defaults(model, query_prompt="", doc_prompt="")

    assert (cfg.embed_query_prompt, cfg.embed_doc_prompt) == ("", "")


def test_without_a_model_the_prompts_wait_for_the_detected_model():
    """auto_model selects the model later and applies the defaults then."""
    cfg = _defaults("")

    assert (cfg.embed_query_prompt, cfg.embed_doc_prompt) == (None, None)


def test_the_query_that_goes_to_ollama_ends_the_instruction_before_the_query():
    cfg = _defaults("qwen3-embedding:8b")
    cfg.ollama_url = "http://localhost:11434"
    sent: list[dict] = []

    def _post(url, json, timeout):
        sent.append(json)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}, request=httpx.Request("POST", url))

    with patch.object(httpx, "post", _post):
        call_ollama_embed(["write words to the flash"], cfg, query=True)

    assert sent[0]["input"] == [f"{QWEN3}write words to the flash"]


def test_a_project_with_the_old_instruction_in_local_toml_gets_the_format_after_load(tmp_path):
    from fw_context_mcp.config import load, settings

    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "local.toml").write_text(
        f'[llm]\nembed_model = "qwen3-embedding:8b"\nembed_query_prompt = "{TASK}"\n', encoding="utf-8")
    settings._config_cache.clear()

    assert load(tmp_path).llm.embed_query_prompt == QWEN3


def test_an_empty_prompt_in_local_toml_stays_empty_after_load(tmp_path):
    from fw_context_mcp.config import load, settings

    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "local.toml").write_text(
        '[llm]\nembed_model = "nomic-embed-text:latest"\nembed_doc_prompt = ""\n', encoding="utf-8")
    settings._config_cache.clear()

    llm = load(tmp_path).llm
    assert (llm.embed_query_prompt, llm.embed_doc_prompt) == ("search_query: ", "")


def test_nomic_text_gets_its_task_prefixes():
    """Its model card asks for them; measured 2026-10-21: MRR@10 0.522 without, 0.591 with them."""
    cfg = _defaults("nomic-embed-text:latest")

    assert (cfg.embed_query_prompt, cfg.embed_doc_prompt) == ("search_query: ", "search_document: ")


def test_nomic_code_gets_no_prefix_of_nomic_text():
    """nomic-embed-code has other prefixes on its model card."""
    cfg = _defaults("nomic-embed-code")

    assert (cfg.embed_query_prompt, cfg.embed_doc_prompt) == ("", "")


def test_the_document_prefix_of_nomic_embeds_an_index_again():
    """The document prefix changes each vector: it is in the content hash of an embedding."""
    from fw_context_mcp.llm.embedder_factory import get_embedder

    without = _defaults("nomic-embed-text:latest", doc_prompt="")
    with_prefix = _defaults("nomic-embed-text:latest")

    assert get_embedder(without).document_settings != get_embedder(with_prefix).document_settings


def test_the_automatic_default_with_a_gpu_stays_qwen3_embedding_8b(monkeypatch, auto_model_cache):
    """A change of the automatic model would leave an index of another model: each search would find nothing."""
    monkeypatch.setattr(auto_model_cache, "_gpu_available", lambda: True)
    monkeypatch.setattr(auto_model_cache, "_model_installed", lambda url, model: True)
    cfg = LLMConfig()

    auto_model_cache.resolve_embed_model(cfg, skip_pull=True)

    assert cfg.embed_model == "qwen3-embedding:8b" and cfg.embed_query_prompt == QWEN3
