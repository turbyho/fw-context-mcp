"""Tests for the shared stale annotation that the handlers use.

Before this code, only ``search.py``, ``_lookup.py``, and ``variables.py``
told the caller that a result covered a file that changed after the index
run.  ``callgraph.py``, ``inheritance.py``, ``get_file_map``, and
``read_file`` gave index data as current data, with nothing to show the
difference.  ``with_stale_annotation`` closes that gap for both result
shapes: a list of records and a single dict.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.shared.stale import (
    STALE_RESULT_MESSAGE,
    annotate_stale,
    collect_result_paths,
    with_stale_annotation,
)

_CONFIG_HASH = "hash-deadbeef"


class _FakeExecutor:
    """Run the query on one connection, as ``SyncQueryExecutor`` does."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute_sync(self, query_fn, config_hash):
        return query_fn(self._conn, config_hash)


@pytest.fixture
def indexed_file(populated_db, tmp_path: Path):
    """Index one C file and give (executor, root, absolute path, db key)."""
    from fw_context_mcp.indexer.db import transaction, upsert_file
    from fw_context_mcp.indexer.ops import _normalize_file_path

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    file_path = src_dir / "modem.c"
    file_path.write_text("void modem_init(void) {\n}\n")

    db_key = _normalize_file_path(str(file_path), tmp_path)
    with transaction(populated_db):
        upsert_file(
            populated_db,
            _CONFIG_HASH,
            db_key,
            "c",
            mtime=os.path.getmtime(file_path),
        )

    return _FakeExecutor(populated_db), tmp_path, file_path, db_key


def _touch_newer(path: Path) -> None:
    """Make *path* look changed, clear of MTIME_TOLERANCE_S (1.0 s)."""
    now = os.path.getmtime(path)
    os.utime(path, (now + 100, now + 100))


class TestCollectResultPaths:
    def test_reads_a_list_of_records(self, tmp_path: Path):
        result = [{"file": "src/a.c"}, {"file": "src/b.c"}]
        paths = collect_result_paths(result, tmp_path)
        assert paths == [str(tmp_path / "src/a.c"), str(tmp_path / "src/b.c")]

    def test_reads_a_single_dict(self, tmp_path: Path):
        paths = collect_result_paths({"file": "src/a.c"}, tmp_path)
        assert paths == [str(tmp_path / "src/a.c")]

    def test_keeps_an_absolute_path(self, tmp_path: Path):
        absolute = str(tmp_path / "src/a.c")
        assert collect_result_paths({"file": absolute}, tmp_path) == [absolute]

    def test_reads_the_file_path_key(self, tmp_path: Path):
        """find_dead_code, find_hotspots, find_all_callers_recursive, and
        find_callees_recursive give the raw ``file_path`` column.

        A check for ``file`` alone left those four tools with no staleness
        detection.  Verified against an indexed the Mbed project on 2026-08-27.
        """
        result = [{"file_path": "src/a.c", "name": "dead_fn"}]
        assert collect_result_paths(result, tmp_path) == [str(tmp_path / "src/a.c")]

    def test_file_wins_when_a_record_holds_both_keys(self, tmp_path: Path):
        """One path per record — the two keys name the same file."""
        record = {"file": "src/a.c", "file_path": "src/a.c"}
        assert collect_result_paths(record, tmp_path) == [str(tmp_path / "src/a.c")]

    def test_an_empty_file_falls_through_to_file_path(self, tmp_path: Path):
        record = {"file": "", "file_path": "src/b.c"}
        assert collect_result_paths(record, tmp_path) == [str(tmp_path / "src/b.c")]

    def test_reads_the_source_file_key(self, tmp_path: Path):
        """trace_data_flow names the path ``source_file``."""
        record = {"source_name": "modem_init", "source_file": "src/a.c"}
        assert collect_result_paths(record, tmp_path) == [str(tmp_path / "src/a.c")]

    def test_reads_both_paths_of_an_indirect_target(self, tmp_path: Path):
        """find_indirect_targets names two different files in one record.

        The assignment site and the call site are separate places, thus a
        first-match-wins rule would check only one of them.
        """
        record = {
            "rhs_name": "on_rx",
            "assign_file": "src/setup.c",
            "call_file": "src/isr.c",
        }
        assert collect_result_paths(record, tmp_path) == [
            str(tmp_path / "src/setup.c"),
            str(tmp_path / "src/isr.c"),
        ]

    def test_a_null_call_file_is_skipped(self, tmp_path: Path):
        """find_indirect_targets gives call_file=None with no call site."""
        record = {"assign_file": "src/setup.c", "call_file": None}
        assert collect_result_paths(record, tmp_path) == [str(tmp_path / "src/setup.c")]

    def test_reads_paths_from_a_nested_record_list(self, tmp_path: Path):
        """find_wrapper_callers keeps its paths one level down, in methods.

        One wrapper class often spans several files (18% of the classes in
        the Mbed project, up to 49 files), thus a single path on the class record
        would leave most of them unchecked.
        """
        result = [{
            "wrapper_class": "the Mbed project::ZMODEM",
            "method_count": 2,
            "methods": [
                {"method": "start", "file": "src/zmodem.cpp", "calls": []},
                {"method": "stop", "file": "src/zmodem_hal.cpp", "calls": []},
            ],
        }]

        paths = collect_result_paths(result, tmp_path)

        assert paths == [
            str(tmp_path / "src/zmodem.cpp"),
            str(tmp_path / "src/zmodem_hal.cpp"),
        ]

    def test_the_descent_stops_at_one_level(self, tmp_path: Path):
        """No handler nests deeper, and an open walk would cost for nothing."""
        result = [{
            "methods": [
                {"file": "src/a.c", "calls": [{"file": "src/too_deep.c"}]},
            ],
        }]

        paths = collect_result_paths(result, tmp_path)

        assert paths == [str(tmp_path / "src/a.c")]

    def test_a_list_of_plain_values_is_not_a_record_list(self, tmp_path: Path):
        record = {"file": "src/a.c", "tags": ["one", "two"]}
        assert collect_result_paths(record, tmp_path) == [str(tmp_path / "src/a.c")]

    def test_a_record_without_a_file_key_adds_nothing(self, tmp_path: Path):
        """An error dict must not cost a stat() call."""
        assert collect_result_paths({"error": "not found"}, tmp_path) == []

    def test_an_empty_file_value_adds_nothing(self, tmp_path: Path):
        assert collect_result_paths([{"file": ""}], tmp_path) == []


class TestAnnotateStale:
    def test_no_stale_file_keeps_the_result(self):
        result = [{"file": "src/a.c"}]
        assert annotate_stale(result, []) is result

    def test_a_list_gets_a_leading_warning(self):
        annotated = annotate_stale([{"file": "src/a.c"}], ["/abs/src/a.c"])
        assert annotated[0]["warning"] == STALE_RESULT_MESSAGE.format(count=1)
        assert annotated[1] == {"file": "src/a.c"}

    def test_a_dict_gets_keys_instead_of_a_record(self):
        """A leading record would break the dict contract of the handlers."""
        annotated = annotate_stale({"file": "src/a.c"}, ["/abs/src/a.c"])
        assert annotated["stale"] is True
        assert annotated["stale_warning"] == STALE_RESULT_MESSAGE.format(count=1)
        assert annotated["file"] == "src/a.c"

    def test_the_input_dict_stays_unchanged(self):
        original = {"file": "src/a.c"}
        annotate_stale(original, ["/abs/src/a.c"])
        assert "stale" not in original

    def test_the_count_names_every_stale_file(self):
        annotated = annotate_stale([], ["/a.c", "/b.c", "/c.h"])
        assert "3 file(s) changed" in annotated[0]["warning"]


class TestWithStaleAnnotation:
    def test_an_unchanged_file_gives_no_warning(self, indexed_file):
        executor, root, file_path, _db_key = indexed_file

        result = with_stale_annotation(
            root, executor, lambda c, h: [{"file": str(file_path)}], _CONFIG_HASH
        )

        assert result == [{"file": str(file_path)}]

    def test_a_changed_file_gives_a_warning(self, indexed_file):
        """The regression that callgraph and inheritance had."""
        executor, root, file_path, _db_key = indexed_file
        _touch_newer(file_path)

        result = with_stale_annotation(
            root, executor, lambda c, h: [{"file": str(file_path)}], _CONFIG_HASH
        )

        assert "warning" in result[0]
        assert "changed" in result[0]["warning"]
        assert result[1] == {"file": str(file_path)}

    def test_a_changed_file_annotates_a_dict_result(self, indexed_file):
        """get_file_map and read_file give a dict, not a list."""
        executor, root, file_path, _db_key = indexed_file
        _touch_newer(file_path)

        result = with_stale_annotation(
            root, executor, lambda c, h: {"file": str(file_path), "lines": 2}, _CONFIG_HASH
        )

        assert result["stale"] is True
        assert result["lines"] == 2, "the payload survives the annotation"

    def test_an_error_result_passes_through(self, indexed_file):
        executor, root, _file_path, _db_key = indexed_file

        result = with_stale_annotation(
            root, executor, lambda c, h: {"error": "Symbol not found"}, _CONFIG_HASH
        )

        assert result == {"error": "Symbol not found"}

    def test_a_file_that_the_index_does_not_hold_is_not_stale(self, indexed_file):
        """_stale_files compares against stored rows, thus an unknown path is quiet."""
        executor, root, _file_path, _db_key = indexed_file

        result = with_stale_annotation(
            root, executor, lambda c, h: [{"file": "src/absent.c"}], _CONFIG_HASH
        )

        assert result == [{"file": "src/absent.c"}]


class TestExecuteScopedMultiScope:
    """A multi-variant project must not repeat one warning per scope."""

    def _context(self, indexed_file, scope_count: int):
        from fw_context_mcp.mcp.handlers._base import DbContext

        executor, root, file_path, _db_key = indexed_file
        scopes = [
            {"config_hash": _CONFIG_HASH, "variant": f"v{i}", "image": "app"}
            for i in range(scope_count)
        ]
        ctx = DbContext(
            db_path=root / "index.db",
            executor=executor,
            config_hash=_CONFIG_HASH,
            cfg=None,
            project_id="proj-001",
            root=root,
            scopes=scopes,
            multi=scope_count > 1,
        )
        return ctx, file_path

    def test_three_scopes_give_one_warning(self, indexed_file):
        ctx, file_path = self._context(indexed_file, 3)
        _touch_newer(file_path)

        result = ctx.execute_scoped(lambda c, h: [{"file": str(file_path)}])

        warnings = [r for r in result if set(r) == {"warning"}]
        assert len(warnings) == 1, f"expected one notice, got {len(warnings)}"
        payload = [r for r in result if "warning" not in r]
        assert len(payload) == 3, "one record per scope survives"
        assert all(r["variant"].startswith("v") for r in payload)

    def test_no_warning_when_nothing_changed(self, indexed_file):
        ctx, file_path = self._context(indexed_file, 3)

        result = ctx.execute_scoped(lambda c, h: [{"file": str(file_path)}])

        assert not [r for r in result if "warning" in r]


class TestEmptyResult:
    """An empty result names no file, thus per-record detection cannot fire.

    This is the case the caller reads as "the symbol does not exist", and the
    empty-result playbook of CLAUDE.md makes it worse: the model tries four
    or five tools and reads the repeated silence as proof.
    """

    def test_an_empty_list_over_a_changed_tree_warns(self, indexed_file):
        executor, root, file_path, _db_key = indexed_file
        _touch_newer(file_path)

        result = with_stale_annotation(root, executor, lambda c, h: [], _CONFIG_HASH)

        assert len(result) == 1
        warning = result[0]["warning"]
        assert "not proof" in warning, warning
        assert "1 indexed file(s)" in warning

    def test_an_empty_list_over_a_clean_tree_stays_empty(self, indexed_file):
        """No change, no warning — the tool must not cry wolf."""
        executor, root, _file_path, _db_key = indexed_file

        result = with_stale_annotation(root, executor, lambda c, h: [], _CONFIG_HASH)

        assert result == []

    def test_a_symbol_not_found_dict_warns(self, indexed_file):
        """`Symbol not found` is exactly the answer that needs the caveat."""
        executor, root, file_path, _db_key = indexed_file
        _touch_newer(file_path)

        result = with_stale_annotation(
            root, executor, lambda c, h: {"error": "Symbol not found: modem_reset"},
            _CONFIG_HASH,
        )

        assert result["stale"] is True
        assert "not proof" in result["stale_warning"]
        assert result["error"] == "Symbol not found: modem_reset"

    def test_a_deleted_file_counts_as_changed(self, indexed_file):
        executor, root, file_path, _db_key = indexed_file
        file_path.unlink()

        result = with_stale_annotation(root, executor, lambda c, h: [], _CONFIG_HASH)

        assert result and "not proof" in result[0]["warning"]


class TestAnnotateStaleEmptyCount:
    def test_the_per_file_message_wins_over_the_empty_one(self):
        """The two conditions never overlap, but the order must be defined."""
        annotated = annotate_stale(
            [{"file": "src/a.c"}], ["/abs/src/a.c"], empty_dirty_count=7
        )
        assert "7" not in annotated[0]["warning"]
        assert annotated[0]["warning"] == STALE_RESULT_MESSAGE.format(count=1)

    def test_a_zero_count_adds_nothing(self):
        result = []
        assert annotate_stale(result, [], empty_dirty_count=0) is result
