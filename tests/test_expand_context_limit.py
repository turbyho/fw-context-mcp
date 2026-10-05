"""ExpandContextPhase must never give more results than ``ctx.limit``.

The phase puts call-graph neighbors after the first ``SEEDS`` results.
Before the fix, it cut only the tail after the neighbors, thus a limit
below ``SEEDS + MAX_NEIGHBORS`` (15) got all seeds and all neighbors:
``limit=12`` gave 15 results.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest


def _result(name: str) -> dict:
    return {"name": name, "file_path": f"src/{name}.c", "usr": f"c:@F@{name}"}


def _run(limit: int, result_count: int, neighbor_count: int) -> list[str]:
    return _run_with_executor(limit, result_count, neighbor_count)[0]


def _run_with_executor(limit: int, result_count: int, neighbor_count: int) -> tuple[list[str], mock.MagicMock]:
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.phases.expand_context import ExpandContextPhase

    executor = mock.MagicMock()
    executor.execute_sync.return_value = [_result(f"n{i}") for i in range(neighbor_count)]
    ctx = PipelineContext(
        config_hash="hash-deadbeef",
        project_root=Path("/tmp/project"),
        db_path=Path("/tmp/project/index.db"),
        query="q",
        original_query="q",
        limit=limit,
        config=Config(),
        executor=executor,
        final_results=[_result(f"r{i}") for i in range(result_count)],
    )
    names = [r["name"] for r in asyncio.run(ExpandContextPhase().run(ctx)).final_results]
    return names, executor


@pytest.mark.parametrize("limit", [5, 10])
def test_seeds_that_fill_the_limit_skip_the_call_graph_query(limit):
    """The input is longer than the limit, as when adaptive fusion fails."""
    names, executor = _run_with_executor(limit=limit, result_count=50, neighbor_count=5)

    executor.execute_sync.assert_not_called()
    assert names == [f"r{i}" for i in range(limit)]


@pytest.mark.parametrize("limit", [5, 10, 12, 14])
def test_a_small_limit_is_never_exceeded(limit):
    names = _run(limit=limit, result_count=limit, neighbor_count=5)

    assert len(names) == limit
    # The seeds keep their place; neighbors only fill what is left of the limit.
    seeds = min(limit, 10)
    assert names[:seeds] == [f"r{i}" for i in range(seeds)]
    assert names[seeds:] == [f"n{i}" for i in range(limit - seeds)]


def test_the_default_limit_keeps_the_mixed_order():
    """Seeds 1-10, neighbors 11-15, then the tail of the fusion results."""
    names = _run(limit=20, result_count=20, neighbor_count=5)

    assert names == (
        [f"r{i}" for i in range(10)] + [f"n{i}" for i in range(5)] + [f"r{i}" for i in range(10, 15)]
    )


def test_an_input_longer_than_the_limit_is_cut_when_no_neighbor_comes():
    """50 rows, limit 20: the seeds do not fill the limit, and the call graph gives nothing.

    The phase returned its input unchanged on this path, thus smart_search
    gave 50 results when adaptive fusion failed.
    """
    names = _run(limit=20, result_count=50, neighbor_count=0)

    assert names == [f"r{i}" for i in range(20)]


def test_an_input_longer_than_the_limit_is_cut_when_no_seed_has_a_usr():
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.phases.expand_context import ExpandContextPhase

    executor = mock.MagicMock()
    ctx = PipelineContext(
        config_hash="hash-deadbeef",
        project_root=Path("/tmp/project"),
        db_path=Path("/tmp/project/index.db"),
        query="q",
        original_query="q",
        limit=20,
        config=Config(),
        executor=executor,
        final_results=[{"name": f"r{i}", "file_path": f"src/r{i}.c"} for i in range(50)],
    )

    names = [r["name"] for r in asyncio.run(ExpandContextPhase().run(ctx)).final_results]

    executor.execute_sync.assert_not_called()
    assert names == [f"r{i}" for i in range(20)]


def test_neighbors_fill_a_short_result_list():
    """8 results below a limit of 20: all 8 stay, and the 5 neighbors come after them."""
    names = _run(limit=20, result_count=8, neighbor_count=5)

    assert names == [f"r{i}" for i in range(8)] + [f"n{i}" for i in range(5)]
