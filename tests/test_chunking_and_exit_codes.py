"""One chunk helper, and exit codes that pull no indexer."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from fw_context_mcp.indexer.db._chunking import MAX_BOUND_PARAMS, chunked


class TestChunked:
    def test_the_default_size_is_the_bound_param_limit(self):
        assert len(chunked(list(range(1000)))[0]) == MAX_BOUND_PARAMS

    def test_chunked_respects_a_smaller_size(self):
        """The 400 in _delete_dangling_incoming_refs is deliberate.

        One of its five statements matches fp_assignments on lhs_usr AND on
        rhs_usr, so it binds each item twice.  1 + 2 * 500 is 1001, above the
        999 that SQLite before 3.32 accepts.  A consolidation onto the
        default would have introduced that defect.
        """
        batches = chunked(list(range(1000)), 400)

        assert [len(b) for b in batches] == [400, 400, 200]
        assert 1 + 2 * 400 < 999

    def test_an_empty_sequence_gives_no_batch(self):
        assert chunked([]) == []

    def test_nothing_is_lost_or_repeated(self):
        items = [f"usr{i}" for i in range(1050)]

        flat = [x for batch in chunked(items, 400) for x in batch]

        assert flat == items

    def test_it_works_for_ints_and_for_strs(self):
        """The two element types the callers actually pass."""
        assert chunked([1, 2, 3], 2) == [[1, 2], [3]]
        assert chunked(["a", "b", "c"], 2) == [["a", "b"], ["c"]]


class TestExitCodesModule:
    def test_the_exit_codes_module_pulls_no_indexer(self):
        """A watcher process must not load libclang to read two integers.

        mcp/daemon.py imported these from indexer.runner, and that import
        pulled 38 indexer modules including clang.cindex into a process that
        never parses anything.
        """
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            import fw_context_mcp.exit_codes  # noqa: F401
            leaked = [m for m in sys.modules if m.startswith("clang")]
            leaked += [m for m in sys.modules
                       if m.startswith("fw_context_mcp.indexer")]
            print(",".join(sorted(leaked)))
            """
        )
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"fw_context_mcp.exit_codes pulled: {result.stdout.strip()}"
        )

    def test_the_daemon_reads_them_from_the_leaf_module(self):
        import inspect

        from fw_context_mcp.mcp import daemon

        source = inspect.getsource(daemon)
        assert "from ..exit_codes import" in source
        assert "from ..indexer.runner import EXIT_" not in source

    def test_the_runner_still_re_exports_them(self):
        """An existing import must keep working."""
        from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED
        from fw_context_mcp.indexer import runner

        assert runner.EXIT_SUPERSEDED is EXIT_SUPERSEDED
        assert runner.EXIT_ALREADY_RUNNING is EXIT_ALREADY_RUNNING

    def test_the_four_outcomes_stay_distinguishable(self):
        from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED

        assert len({0, 1, EXIT_SUPERSEDED, EXIT_ALREADY_RUNNING}) == 4
