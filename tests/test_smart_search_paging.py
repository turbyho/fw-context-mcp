"""smart_search pages one answer, and the pages come from one run.

An LLM generates the queries of smart_search, thus two runs of one
question can search for different words, and a page of a second run would
not continue the page of the first.  The rules:

* offset 0 (also no offset) is a new search, and its answer replaces the
  stored one;
* offset above 0 cuts the page out of the stored answer of the same query
  and build, and searches again when the slot holds another answer;
* no other tool touches the slot; a timeout answer is not stored.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.mcp.handlers import search
from fw_context_mcp.mcp.handlers._smart_cache import SMART_SEARCH_CACHE
from fw_context_mcp.search.context import PipelineContext
from tests._paging import assert_whole_and_once, notice, walk_pages

SYMBOLS = 23


def _ctx(tmp_path: Path, config_hash: str = "hash-a") -> PipelineContext:
    return PipelineContext(
        config_hash=config_hash, project_root=tmp_path, db_path=tmp_path / "index.db",
        query="q", original_query="q", limit=100, config=Config(), executor=MagicMock(),
    )


class _Runs:
    """A stand-in pipeline that counts its runs; run n gives other symbols than run n-1."""

    def __init__(self, size: int = SYMBOLS) -> None:
        self.count = 0
        self.size = size

    async def run(self, ctx):
        self.count += 1
        rows = [{"_generated_queries": ["modem", "connect"]}]
        rows += [{"name": f"run{self.count}_s{i:02d}", "file": "/p/a.c", "line": i}
                 for i in range(self.size)]
        return ctx.evolve(formatted_results=rows)


@pytest.fixture(autouse=True)
def _empty_slot():
    SMART_SEARCH_CACHE.clear()
    yield
    SMART_SEARCH_CACHE.clear()


def _call(tmp_path: Path, runs: _Runs, query: str, offset: int = 0, *, config_hash: str = "hash-a",
          limit: int = 10) -> list[dict]:
    runner = MagicMock()
    runner.run = runs.run
    runner.last_ctx = None
    with patch.object(PipelineContext, "create", return_value=_ctx(tmp_path, config_hash)), \
            patch("fw_context_mcp.search.pipeline._build_smart_search"), \
            patch("fw_context_mcp.search.pipeline.PipelineRunner", return_value=runner), \
            patch.object(search, "_append_staleness_warning", side_effect=lambda r, *_: r):
        return asyncio.run(search.smart_search(query=query, limit=limit, offset=offset))


def _names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows if "name" in r]


def test_the_pages_of_one_answer_walk_whole_and_once(tmp_path):
    runs = _Runs()
    seen, total = walk_pages(lambda offset: _call(tmp_path, runs, "modem connect", offset),
                             lambda r: r["name"], is_answer=lambda r: "name" in r)
    assert total == SYMBOLS
    assert_whole_and_once(seen, total)
    assert runs.count == 1, "the later pages must come from the stored answer"
    assert all(name.startswith("run1_") for name in seen)


def test_each_page_keeps_the_meta_rows_and_names_the_next_call(tmp_path):
    runs = _Runs()
    rows = _call(tmp_path, runs, "modem connect", 10)
    assert rows[0] == {"_generated_queries": ["modem", "connect"]}
    assert notice(rows)["hint"] == "smart_search('modem connect', offset=20) reads the next page."


def test_offset_zero_is_a_new_search_also_for_the_same_query(tmp_path):
    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    _call(tmp_path, runs, "modem connect")
    assert runs.count == 2
    assert _names(_call(tmp_path, runs, "modem connect", 10))[0].startswith("run2_")
    assert runs.count == 2


def test_a_page_of_another_query_searches_again(tmp_path):
    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    rows = _call(tmp_path, runs, "ble pairing", 10)
    assert runs.count == 2
    assert _names(rows)[0] == "run2_s10"


def test_a_page_after_a_reindex_searches_again(tmp_path):
    """A reindex with a changed build gives another config_hash."""
    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    _call(tmp_path, runs, "modem connect", 10, config_hash="hash-b")
    assert runs.count == 2


def test_a_page_with_an_empty_slot_searches(tmp_path):
    """After a restart of the server the slot is empty."""
    runs = _Runs()
    rows = _call(tmp_path, runs, "modem connect", 10)
    assert runs.count == 1
    assert _names(rows)[0] == "run1_s10"


def test_another_tool_does_not_empty_the_slot(tmp_path):
    from tests._paging import make_project

    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    project = make_project(tmp_path / "other", lambda conn, ids: None)
    search.search_code("modem", project_root=str(project))
    _call(tmp_path, runs, "modem connect", 10)
    assert runs.count == 1


def test_a_page_after_the_end_names_the_total(tmp_path):
    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    assert _call(tmp_path, runs, "modem connect", 80) == [
        {"info": f"No symbol at offset 80; the answer holds {SYMBOLS}."}
    ]


class _Stalls:
    """A pipeline that passes the timeout after it found the FTS5 rows."""

    def __init__(self, ctx: PipelineContext) -> None:
        rows = [{"name": f"fts{i:02d}", "file_path": "src/a.c", "line": i} for i in range(SYMBOLS)]
        self.last_ctx = ctx.evolve(fts5_results=rows)

    async def run(self, ctx):
        await asyncio.sleep(30)


def _timed_out(tmp_path: Path, offset: int = 0) -> tuple[list[dict], PipelineContext]:
    cfg = Config()
    cfg.llm.timeout = 0.1
    ctx = _ctx(tmp_path).evolve(config=cfg)
    with patch.object(PipelineContext, "create", return_value=ctx), \
            patch("fw_context_mcp.search.pipeline._build_smart_search"), \
            patch("fw_context_mcp.search.pipeline.PipelineRunner", return_value=_Stalls(ctx)):
        rows = asyncio.run(search.smart_search(query="modem connect", limit=10, offset=offset))
    return rows, ctx


def test_a_timeout_answer_is_not_stored(tmp_path):
    from fw_context_mcp.mcp.handlers._smart_cache import answer_key

    rows, ctx = _timed_out(tmp_path)
    assert rows[0]["_partial"] is True
    assert notice(rows) is None
    assert SMART_SEARCH_CACHE.get(answer_key(ctx.db_path, ctx.config_hash, "modem connect")) is None


def test_a_timeout_at_offset_zero_empties_the_slot(tmp_path):
    """A new search that timed out must not leave the older answer to serve page two."""
    runs = _Runs()
    _call(tmp_path, runs, "modem connect")
    _timed_out(tmp_path)
    _call(tmp_path, runs, "modem connect", 10)
    assert runs.count == 2, "page two must search again, not read the older answer"


def test_a_timeout_on_a_later_page_gives_that_page(tmp_path):
    rows, _ = _timed_out(tmp_path, offset=10)
    assert _names(rows) == [f"fts{i:02d}" for i in range(10, 20)]


def test_an_empty_answer_has_no_page_after_its_end(tmp_path):
    class _Empty(_Runs):
        async def run(self, ctx):
            self.count += 1
            return ctx.evolve(formatted_results=[{"info": "No results found for the generated queries."}])

    runs = _Empty()
    assert _call(tmp_path, runs, "nothing") == [{"info": "No results found for the generated queries."}]
    assert _call(tmp_path, runs, "nothing", 10) == [
        {"info": "No symbol at offset 10; the answer holds 0."}
    ]


def test_a_limit_above_100_is_a_page_of_100(tmp_path):
    rows = _call(tmp_path, _Runs(size=150), "modem connect", limit=500)
    assert notice(rows)["shown"] == 100
    assert notice(rows)["total"] == 150
