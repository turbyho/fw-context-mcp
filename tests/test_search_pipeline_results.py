"""What semantic_search and smart_search do with the results of the pipeline.

- ``semantic_search`` reads the similarity and the emptiness of a result
  from the pipeline symbols: ``FormatPhase`` drops ``_similarity`` and always
  adds a dict, thus a check on the formatted list never fired.
- ``EmbeddingPhase`` orders the rows of the KNN and of the legacy BLOB path
  the same way, and keeps the raw cosine similarity.
- ``smart_search`` gives the symbols that the phases before a timeout found.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.phases.embedding import EmbeddingPhase
from fw_context_mcp.search.pipeline import PipelineConfig, PipelineRunner


def _ctx(tmp_path: Path, **overrides) -> PipelineContext:
    fields = {
        "config_hash": "h",
        "project_root": tmp_path,
        "db_path": tmp_path / "index.db",
        "query": "modem connect",
        "original_query": "modem connect",
        "limit": 20,
        "config": Config(),
        "executor": MagicMock(),
    }
    fields.update(overrides)
    return PipelineContext(**fields)


def _symbol(name: str, similarity: float | None = None, **extra) -> dict:
    row = {"name": name, "qualified_name": name, "kind": "function",
           "file_path": "src/a.c", "line": 1, "is_definition": 1, **extra}
    if similarity is not None:
        row["_similarity"] = similarity
    return row


# ── semantic_search ─────────────────────────────────────────────────────────


def _semantic_search(tmp_path: Path, final_results: list[dict]) -> tuple[list[dict], MagicMock]:
    """Run semantic_search with a pipeline that ends with *final_results*."""
    from fw_context_mcp.mcp.handlers import search
    from fw_context_mcp.search.phases.format import FormatPhase

    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"")
    cfg = Config()
    cfg.llm.enabled = True

    async def _run(self, ctx):
        return await FormatPhase().run(ctx.evolve(final_results=final_results))

    fallback = MagicMock(return_value=[{"warning": "fallback"}])
    with patch.object(search, "resolve_project_root", return_value=tmp_path), \
            patch.object(search, "_db_path", return_value=db_path), \
            patch.object(search, "load_config", return_value=cfg), \
            patch.object(search, "check_setup", return_value={"ollama_running": True}), \
            patch.object(search, "_fallback_to_search_code", fallback), \
            patch.object(search, "_append_staleness_warning", side_effect=lambda r, *_: r), \
            patch.object(PipelineContext, "create", return_value=_ctx(tmp_path)), \
            patch.object(PipelineRunner, "run", _run):
        return asyncio.run(search.semantic_search(query="modem connect")), fallback


def test_no_symbol_falls_back_to_the_lexical_search(tmp_path):
    results, fallback = _semantic_search(tmp_path, [])

    assert fallback.called
    assert results == [{"warning": "fallback"}]


def test_a_low_best_similarity_gives_the_relevance_warning(tmp_path):
    results, _ = _semantic_search(tmp_path, [_symbol("a", 0.61), _symbol("b", 0.62)])

    assert len(results) == 1
    assert results[0]["_best_similarity"] == 0.62
    assert results[0]["_fallback_suggestion"] == "search_code"


def test_a_relevant_result_is_returned(tmp_path):
    results, fallback = _semantic_search(tmp_path, [_symbol("a", 0.80)])

    assert not fallback.called
    assert [r["name"] for r in results] == ["a"]


# ── EmbeddingPhase._rank ────────────────────────────────────────────────────


def test_rank_orders_by_the_boosted_score_and_keeps_the_raw_similarity():
    phase = EmbeddingPhase(independent=True, source_boost=True)
    rows = [
        {"id": 1, "name": "vendor", "is_project": 0},
        {"id": 2, "name": "project", "is_project": 1},
        {"id": 3, "name": "dropped", "is_project": 1},
    ]

    ranked, best = phase._rank(rows, {1: 0.80, 2: 0.70, 3: 0.50}, limit=2)

    # 0.70 * 1.2 = 0.84 beats 0.80 * 0.85 = 0.68.
    assert [r["name"] for r in ranked] == ["project", "vendor"]
    assert [r["_similarity"] for r in ranked] == [0.70, 0.80]
    assert best == 0.80


def test_rank_gives_the_best_raw_similarity_of_a_row_that_the_limit_drops():
    """The boost puts the vendor row with the best raw score after the limit."""
    phase = EmbeddingPhase(independent=True, source_boost=True)
    rows = [
        {"id": 1, "name": "vendor", "is_project": 0},
        {"id": 2, "name": "project", "is_project": 1},
    ]

    ranked, best = phase._rank(rows, {1: 0.70, 2: 0.65}, limit=1)

    # 0.65 * 1.2 = 0.78 beats 0.70 * 0.85 = 0.595, thus "vendor" is cut.
    assert [r["name"] for r in ranked] == ["project"]
    assert best == 0.70


def test_rank_without_boost_orders_by_similarity_and_keeps_every_candidate():
    phase = EmbeddingPhase(independent=True, source_boost=False)
    rows = [{"id": i, "name": str(i), "is_project": 1} for i in range(1, 4)]

    ranked, best = phase._rank(rows, {1: 0.5, 2: 0.9, 3: 0.7}, limit=1)

    assert [r["name"] for r in ranked] == ["2", "3", "1"]
    assert best == 0.9


def test_rank_gives_no_best_similarity_for_no_row():
    phase = EmbeddingPhase(independent=True, source_boost=True)

    assert phase._rank([{"id": 9, "name": "x"}], {1: 0.9}, limit=5) == ([], None)


# ── smart_search timeout ────────────────────────────────────────────────────


class _FindsRows(Phase):
    name = "_finds_rows"

    async def run(self, ctx):
        return ctx.evolve(fts5_results=[_symbol("modem_connect")])


class _Stalls(Phase):
    name = "_stalls"

    async def run(self, ctx):
        await asyncio.sleep(30)
        return ctx


class _GivesFiftyRows(Phase):
    """Stand-in embedding phase: 50 uncut rows, as with no source boost."""

    name = "_gives_fifty_rows"

    async def run(self, ctx):
        rows = [_symbol(f"s{i}") for i in range(50)]
        return ctx.evolve(embedding_results=rows, final_results=list(rows))


class _Fails(Phase):
    name = "_fails"

    async def run(self, ctx):
        # Imported here: sqlite3 must not load before fw_context_mcp, which
        # redirects it to pysqlite3.
        import sqlite3

        raise sqlite3.OperationalError("database is locked")


def test_the_limit_holds_when_the_cutting_phases_fail(tmp_path):
    """Fusion and expansion cut to the limit.  When both fail, the runner goes on.

    The uncut rows then reach FormatPhase, the last phase, which must cut.
    """
    from fw_context_mcp.search.phases.format import FormatPhase

    runner = PipelineRunner(PipelineConfig(phases=[_GivesFiftyRows(), _Fails(), _Fails(), FormatPhase()]))

    ctx = asyncio.run(runner.run(_ctx(tmp_path, limit=20)))

    assert len([r for r in ctx.formatted_results if "name" in r]) == 20
    assert len(ctx.warnings) == 2


def test_a_cancelled_run_stops_and_keeps_what_it_found(tmp_path):
    runner = PipelineRunner(PipelineConfig(phases=[_FindsRows(), _Stalls()]))

    async def _main():
        await asyncio.wait_for(runner.run(_ctx(tmp_path)), timeout=0.2)

    with pytest.raises(TimeoutError):
        asyncio.run(_main())
    assert [r["name"] for r in runner.last_ctx.fts5_results] == ["modem_connect"]


def test_smart_search_timeout_gives_the_partial_symbols(tmp_path):
    from fw_context_mcp.mcp.handlers import search

    cfg = Config()
    cfg.llm.timeout = 0.2
    runner = PipelineRunner(PipelineConfig(phases=[_FindsRows(), _Stalls()]))
    with patch.object(PipelineContext, "create", return_value=_ctx(tmp_path, config=cfg)), \
            patch("fw_context_mcp.search.pipeline._build_smart_search"), \
            patch("fw_context_mcp.search.pipeline.PipelineRunner", return_value=runner):
        results = asyncio.run(search.smart_search(query="modem connect"))

    assert results[0]["_partial"] is True
    assert results[0]["hint"].startswith("Partial results")
    assert [r["name"] for r in results[1:]] == ["modem_connect"]


# ── A local embedding model needs no Ollama ─────────────────────────────────


@pytest.mark.parametrize(("model", "expected"), [
    ("", True),
    ("mxbai-embed-large", True),
    ("qwen3-embedding:0.6b", True),
    ("BAAI/bge-small-en-v1.5", False),
    ("sentence-transformers/all-MiniLM-L6-v2", False),
    ("ft://latest", False),
])
def test_uses_ollama_follows_the_backend_rules(model, expected):
    from fw_context_mcp.llm.embedder_factory import uses_ollama

    cfg = Config()
    cfg.llm.embed_model = model

    assert uses_ollama(cfg.llm) is expected


def test_semantic_search_with_a_local_model_does_not_probe_ollama(tmp_path):
    from fw_context_mcp.mcp.handlers import search
    from fw_context_mcp.search.phases.format import FormatPhase

    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"")
    cfg = Config()
    cfg.llm.enabled = True
    cfg.llm.embed_model = "BAAI/bge-small-en-v1.5"

    async def _run(self, ctx):
        return await FormatPhase().run(ctx.evolve(final_results=[_symbol("a", 0.9)]))

    probe = MagicMock(return_value={"ollama_running": False})
    with patch.object(search, "resolve_project_root", return_value=tmp_path), \
            patch.object(search, "_db_path", return_value=db_path), \
            patch.object(search, "load_config", return_value=cfg), \
            patch.object(search, "check_setup", probe), \
            patch.object(search, "_append_staleness_warning", side_effect=lambda r, *_: r), \
            patch.object(PipelineContext, "create", return_value=_ctx(tmp_path)), \
            patch.object(PipelineRunner, "run", _run):
        results = asyncio.run(search.semantic_search(query="modem connect"))

    assert not probe.called
    assert [r["name"] for r in results] == ["a"]


def test_check_setup_with_a_local_model_and_a_cloud_chat_is_ok_without_ollama():
    import httpx

    from fw_context_mcp.llm.ollama import check_setup

    cfg = Config()
    cfg.llm.embed_model = "BAAI/bge-small-en-v1.5"
    cfg.llm.chat_api_base = "http://localhost:4000/v1"
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = check_setup(cfg.llm)

    assert result["status"] == "ok"
    assert result["embedding_backend"] == "local"
    assert result["ollama_running"] is False
