"""One way through the code must be reported once.

``find_call_path`` runs a bidirectional BFS and records a path at every
node where the two fronts meet.  Several meeting nodes on ONE path
reconstruct the same text, and nothing compared the texts.  Measured on one
project, three of four queries answered with the same chain four times:

    main → <global ctors> → Worker → _thread_func → _read_telemetry → …

The limit of five paths was spent on repeats, thus a real alternative had
no room left, and a caller that reads the answer as "the ways to reach this
function" counted one way as four.
"""

from __future__ import annotations

from fw_context_mcp.indexer.db._callgraph import _MAX_CALL_PATHS, _record_path

USR = "c:@F@target"


class TestRecordPath:
    def test_a_new_chain_is_added(self):
        found: list[dict] = []
        seen: set[str] = set()

        full = _record_path(found, seen, 2, "a → b → c", USR)

        assert full is False
        assert found == [{"depth": 2, "chain": "a → b → c", "target_usr": USR}]

    def test_the_same_chain_is_not_added_twice(self):
        found: list[dict] = []
        seen: set[str] = set()

        _record_path(found, seen, 2, "a → b → c", USR)
        _record_path(found, seen, 2, "a → b → c", USR)

        assert len(found) == 1

    def test_a_repeat_does_not_report_the_list_as_full(self):
        """A repeat that counted would end the search before its time."""
        found: list[dict] = []
        seen: set[str] = set()
        for _ in range(_MAX_CALL_PATHS + 3):
            full = _record_path(found, seen, 1, "a → b", USR)

        assert full is False
        assert len(found) == 1

    def test_a_different_chain_is_kept(self):
        found: list[dict] = []
        seen: set[str] = set()

        _record_path(found, seen, 2, "a → b → c", USR)
        _record_path(found, seen, 3, "a → x → y → c", USR)

        assert [r["chain"] for r in found] == ["a → b → c", "a → x → y → c"]

    def test_the_list_is_full_at_the_limit(self):
        found: list[dict] = []
        seen: set[str] = set()
        results = [
            _record_path(found, seen, 1, f"a → n{i} → c", USR)
            for i in range(_MAX_CALL_PATHS)
        ]

        assert results[:-1] == [False] * (_MAX_CALL_PATHS - 1)
        assert results[-1] is True
        assert len(found) == _MAX_CALL_PATHS


def test_both_meeting_points_go_through_the_helper():
    """A meeting point that appends on its own puts the repeats back."""
    import inspect

    from fw_context_mcp.indexer.db import _callgraph

    source = inspect.getsource(_callgraph.find_call_path)
    assert source.count("_record_path(found, seen_chains") == 2
    assert 'found.append({"depth"' not in source
