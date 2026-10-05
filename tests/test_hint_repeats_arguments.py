"""A hint repeats each argument that changes the answer, the project included.

The hint of a page is the call that reads the next page.  It named the
query and the offset only, thus:

* ``search_bodies(..., project_only=True)`` led to a next page of the
  vendor bodies too, and ``lookup_symbol(..., exact=True)`` to a next page
  of the prefix match.
* After a question about another project (``project="<name>"``), the
  next page came from the project of the current directory.

Each test here follows the hints as a reader does: it parses the hint as a
call and runs it, from a directory outside the project.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import test_search_paging
from tests.test_search_paging import _answers, _notice


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The project of the search paging tests: bodies, files and names that page."""
    return test_search_paging.project.__wrapped__(tmp_path)


def _follow(tool: Callable[..., list[dict]], hint: str) -> list[dict]:
    """Run the call that *hint* names, as the MCP server runs it."""
    from fw_context_mcp.mcp.server import _merge_project_selector

    call = ast.parse(hint.removesuffix(" reads the next page."), mode="eval").body
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), hint
    assert call.func.id == tool.__name__, hint
    args = [ast.literal_eval(a) for a in call.args]
    kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
    return tool(*args, **_merge_project_selector(call.func.id, kwargs))


def _walk(tool: Callable[..., list[dict]], first: list[dict], key: str) -> list[str]:
    seen = [r[key] for r in _answers(first)]
    notice = _notice(first)
    while notice and notice["more"]:
        rows = _follow(tool, notice["hint"])
        seen += [r[key] for r in _answers(rows)]
        notice = _notice(rows)
    return seen


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A current directory that is not the project, as for a cross-project question."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def test_the_hints_of_a_filtered_search_stay_in_the_filter(project, elsewhere):
    from fw_context_mcp.mcp.handlers.search import search_bodies

    first = search_bodies("probe", project_root=str(project), project_only=True, limit=2)
    seen = _walk(search_bodies, first, "qualified_name")
    assert seen == [f"app::app_probe_{i}" for i in range(4)], "the four project bodies, once"


def test_the_hints_of_an_exact_lookup_stay_exact(project, elsewhere):
    from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

    first = lookup_symbol("read", project_root=str(project), exact=True, limit=2)
    seen = _walk(lookup_symbol, first, "qualified_name")
    exact = _answers(lookup_symbol("read", project_root=str(project), exact=True, limit=50))
    assert seen == [r["qualified_name"] for r in exact]
    assert all(name.endswith("::read") for name in seen), seen


def test_a_hint_names_the_project_that_the_caller_named():
    from fw_context_mcp.mcp.shared.paging import page_hint, selector_args

    assert page_hint("find_callers", "uart_init", **selector_args("FM"), next_offset=50) == (
        "find_callers('uart_init', project='FM', offset=50) reads the next page."
    )
    assert page_hint(
        "find_callers", "uart_init", **selector_args("FM", "debug", "app"), next_offset=50,
    ) == (
        "find_callers('uart_init', project='FM', variant='debug', image='app', offset=50) "
        "reads the next page."
    )


def test_a_plain_call_keeps_a_short_hint():
    from fw_context_mcp.mcp.shared.paging import page_hint, selector_args

    assert selector_args(None, None, None) == {}
    assert page_hint("find_callers", "uart_init", **selector_args(), next_offset=50) == (
        "find_callers('uart_init', offset=50) reads the next page."
    )


def test_the_scope_of_dead_code_and_hotspots_goes_into_the_hint():
    from fw_context_mcp.mcp.handlers.callgraph import _scope_hint_args

    assert _scope_hint_args(True, None) == {}, "the defaults stay out"
    assert _scope_hint_args(False, ["lib/%"]) == {"project_only": False, "exclude_paths": ["lib/%"]}


def test_search_code_and_search_content_repeat_their_filters(project):
    from fw_context_mcp.mcp.handlers.search import search_code, search_content
    from tests._paging import with_project

    rows = search_code("read", project_root=str(project), kind="method", limit=2)
    assert _notice(rows)["hint"] == with_project(
        "search_code('read', kind='method', offset=2) reads the next page.", project,
    )
    rows = search_content("probe", project_root=str(project), project_only=True, limit=1)
    assert _notice(rows)["hint"] == with_project(
        "search_content('probe', project_only=True, offset=1) reads the next page.", project,
    )
