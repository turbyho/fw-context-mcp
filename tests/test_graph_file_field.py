"""Every tool must name the path field the same way, and give it whole.

Four graph tools handed the caller the row as the database gave it, thus
the field was ``file_path`` and its value was relative to the project root.
Their own documents promised ``file``, and every other tool gives an
absolute ``file``.  A caller that read ``result["file"]`` got a KeyError,
and a caller that found ``file_path`` got a path whose root it had to
guess — while the instructions ask it to cite ``file:line``.

These tests pin the field for the four tools that carried the defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.mcp.handlers.callgraph import _with_absolute_file

ROOT = Path("/projects/firmware")


class TestWithAbsoluteFile:
    def test_the_relative_path_becomes_an_absolute_file(self):
        rows = _with_absolute_file([{"name": "worker_loop", "file_path": "src/worker.cpp"}], ROOT)

        assert rows == [{"name": "worker_loop", "file": "/projects/firmware/src/worker.cpp"}]

    def test_the_old_key_does_not_survive(self):
        """Two names for one value would leave the surface as it was."""
        rows = _with_absolute_file([{"file_path": "src/a.c"}], ROOT)

        assert "file_path" not in rows[0]

    def test_every_other_field_stays(self):
        row = {
            "name": "worker_loop", "qualified_name": "app::worker_loop",
            "kind": "function", "signature": "void worker_loop()",
            "depth": 2, "file_path": "src/worker.cpp",
        }

        out = _with_absolute_file([row], ROOT)[0]

        assert out["depth"] == 2
        assert out["signature"] == "void worker_loop()"

    @pytest.mark.parametrize("row", [{"info": "no results"}, {"error": "not found"}])
    def test_a_row_without_a_path_passes_through(self, row: dict):
        """An `info` or an `error` element is a row as well."""
        assert _with_absolute_file([row], ROOT) == [row]

    def test_an_absolute_path_in_the_index_is_not_doubled(self):
        """`abs_path` returns a path that is already absolute as it is."""
        rows = _with_absolute_file([{"file_path": "/elsewhere/src/a.c"}], ROOT)

        assert rows[0]["file"] == "/elsewhere/src/a.c"


def test_the_four_tools_call_the_helper():
    """A tool that skips it puts the old shape back on the surface."""
    import inspect

    from fw_context_mcp.mcp.handlers import callgraph

    for tool in (callgraph.find_all_callers_recursive, callgraph.find_callees_recursive,
                 callgraph.find_hotspots, callgraph.find_dead_code):
        source = inspect.getsource(tool)
        assert "_with_absolute_file(rows, db.root)" in source, tool.__name__


def test_the_documents_name_the_field_that_the_tools_return():
    """The four documents promised `file` while the tools sent `file_path`."""
    import inspect

    from fw_context_mcp.mcp.handlers import callgraph

    for tool in (callgraph.find_all_callers_recursive, callgraph.find_callees_recursive,
                 callgraph.find_hotspots, callgraph.find_dead_code):
        doc = inspect.getdoc(tool) or ""
        assert "file_path" not in doc, tool.__name__
        assert "file (str — absolute)" in doc, tool.__name__
