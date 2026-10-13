"""smart_search and semantic_search name their build, as every other query tool does.

The two tools took no ``variant`` and no ``image`` until 2026-10.  On a
project of several builds they answered for the newest build, thus for the
program that the last index run did, and the answer did not say so.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.search.context import PipelineContext

PROJECT_ID = "proj"


@pytest.fixture
def two_builds(tmp_path: Path) -> tuple[Path, Config]:
    """An index of two builds, ``a/app`` and ``a/boot``, at the path that the config gives."""
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project

    cfg = Config()
    cfg.index.db_dir = tmp_path / "db"
    db_path = cfg.index.db_dir / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "t", str(tmp_path))
            upsert_build_config(conn, "h-app", PROJECT_ID, "/tmp/app.json", variant="a", image="app")
            upsert_build_config(conn, "h-boot", PROJECT_ID, "/tmp/boot.json", variant="a", image="boot")
    finally:
        conn.close()
    return db_path, cfg


def _create(tmp_path: Path, cfg: Config, **selector) -> PipelineContext:
    with (
        mock.patch("fw_context_mcp.search.context.resolve_project_root", return_value=tmp_path),
        mock.patch("fw_context_mcp.search.context.derive_project_id", return_value=PROJECT_ID),
        mock.patch("fw_context_mcp.search.context.load_config", return_value=cfg),
    ):
        return PipelineContext.create(query="q", project_root=str(tmp_path), **selector)


class TestPipelineContext:
    def test_the_named_build_answers(self, tmp_path, two_builds):
        _db_path, cfg = two_builds

        assert _create(tmp_path, cfg, variant="a", image="app").config_hash == "h-app"
        assert _create(tmp_path, cfg, variant="a", image="boot").config_hash == "h-boot"

    def test_no_selector_is_refused_and_not_the_newest_build(self, tmp_path, two_builds):
        """``h-boot`` is the newest build, and it answered such a call until 2026-10."""
        _db_path, cfg = two_builds

        with pytest.raises(ValueError, match="names no build") as refusal:
            _create(tmp_path, cfg)

        assert 'variant="a", image="app"' in str(refusal.value)
        assert 'variant="a", image="boot"' in str(refusal.value)
class TestSemanticSearch:
    @staticmethod
    def _run(tmp_path: Path, db_path: Path, cfg: Config, path: str, **selector) -> tuple[list[dict], mock.MagicMock]:
        """Run semantic_search into the lexical fallback of *path*.

        * ``disabled`` — ``[llm] enabled = false``
        * ``not_running`` — the Ollama server does not answer
        * ``embedding_failed`` — the pipeline gives an ``ollama_warning``
        * ``no_match`` — the pipeline finds no symbol above the threshold
        """
        from fw_context_mcp.mcp.handlers import search as handler
        from fw_context_mcp.search.pipeline import PipelineRunner

        cfg.llm.enabled = path != "disabled"

        async def fake_run(_runner, ctx):
            if path == "embedding_failed":
                return ctx.evolve(ollama_warning={"detail": "embedding failed"})
            return ctx.evolve(final_results=[])

        fallback = mock.MagicMock(return_value=[{"warning": "fallback"}])
        with (
            mock.patch.object(handler, "resolve_project_root", return_value=tmp_path),
            mock.patch.object(handler, "_db_path", return_value=db_path),
            mock.patch.object(handler, "load_config", return_value=cfg),
            mock.patch.object(handler, "_fallback_to_search_code", fallback),
            mock.patch.object(handler, "uses_ollama", return_value=True),
            mock.patch.object(handler, "check_setup", return_value={"ollama_running": path != "not_running"}),
            mock.patch.object(PipelineRunner, "run", fake_run),
            mock.patch("fw_context_mcp.search.context.resolve_project_root", return_value=tmp_path),
            mock.patch("fw_context_mcp.search.context.derive_project_id", return_value=PROJECT_ID),
            mock.patch("fw_context_mcp.search.context.load_config", return_value=cfg),
        ):
            rows = asyncio.run(handler.semantic_search("q", **selector))
        return rows, fallback

    @pytest.mark.parametrize("path", ["disabled", "not_running", "embedding_failed", "no_match"])
    def test_each_fallback_answers_for_the_named_build(self, tmp_path, two_builds, path: str):
        """The fallback resolved a build of its own, without the selector, until 2026-10."""
        db_path, cfg = two_builds

        _rows, fallback = self._run(tmp_path, db_path, cfg, path, variant="a", image="boot")

        fallback.assert_called_once()
        assert fallback.call_args.kwargs["config_hash"] == "h-boot"
        assert fallback.call_args.kwargs["hint_args"] == {"variant": "a", "image": "boot"}

    def test_no_selector_is_refused_before_the_llm_check(self, tmp_path, two_builds):
        db_path, cfg = two_builds

        rows, fallback = self._run(tmp_path, db_path, cfg, "disabled")

        fallback.assert_not_called()
        assert len(rows) == 1 and "names no build" in rows[0]["error"], rows


class TestSmartSearch:
    @staticmethod
    def _run(tmp_path: Path, cfg: Config, **selector) -> tuple[list[dict], list[str]]:
        """Run smart_search with a pipeline of two symbols, one on a page; give the rows and the build it ran for."""
        from fw_context_mcp.mcp.handlers import search as handler
        from fw_context_mcp.mcp.handlers._smart_cache import CachedAnswer

        searched: list[str] = []

        async def fake_run(ctx, key, _page_size, _skip):
            searched.append(ctx.config_hash)
            return CachedAnswer(key=key, meta=(), symbols=({"name": "f", "file": "a.c", "line": 1},
                                                          {"name": "g", "file": "a.c", "line": 2}))

        handler.SMART_SEARCH_CACHE.clear()
        with (
            mock.patch.object(handler, "_smart_search_run", fake_run),
            mock.patch("fw_context_mcp.search.context.resolve_project_root", return_value=tmp_path),
            mock.patch("fw_context_mcp.search.context.derive_project_id", return_value=PROJECT_ID),
            mock.patch("fw_context_mcp.search.context.load_config", return_value=cfg),
        ):
            rows = asyncio.run(handler.smart_search("q", limit=1, **selector))
        handler.SMART_SEARCH_CACHE.clear()
        return rows, searched

    def test_the_named_build_is_searched(self, tmp_path, two_builds):
        _db_path, cfg = two_builds

        _rows, searched = self._run(tmp_path, cfg, variant="a", image="boot")

        assert searched == ["h-boot"]

    def test_the_page_hint_repeats_the_selector(self, tmp_path, two_builds):
        """A next page without the selector gets the refusal, and not the next page."""
        _db_path, cfg = two_builds

        rows, _searched = self._run(tmp_path, cfg, variant="a", image="boot")

        hint = next(r["hint"] for r in rows if "total" in r)
        assert "variant='a'" in hint and "image='boot'" in hint, hint

    def test_no_selector_is_refused(self, tmp_path, two_builds):
        _db_path, cfg = two_builds

        rows, searched = self._run(tmp_path, cfg)

        assert searched == [], "the pipeline ran without a build"
        assert len(rows) == 1 and "names no build" in rows[0]["error"], rows


class TestFallback:
    """``_fallback_to_search_code`` reads the build that the caller gives."""

    @staticmethod
    def _run(tmp_path: Path, db_path: Path, config_hash: str) -> list[dict]:
        from fw_context_mcp.mcp.shared.fallback import _fallback_to_search_code

        with mock.patch("fw_context_mcp.mcp.shared.fallback.derive_project_id", return_value=PROJECT_ID):
            return _fallback_to_search_code(tmp_path, db_path, "q", 10, "fallback", config_hash=config_hash)

    def test_the_given_build_answers(self, tmp_path, two_builds):
        db_path, _cfg = two_builds

        rows = self._run(tmp_path, db_path, "h-boot")

        assert not any("error" in r for r in rows), rows
        assert any("no lexical results" in r.get("warning", "") for r in rows), rows

    def test_a_build_that_the_index_does_not_hold_is_an_error(self, tmp_path, two_builds):
        db_path, _cfg = two_builds

        assert self._run(tmp_path, db_path, "h-gone") == [{"error": "No build config indexed."}]
