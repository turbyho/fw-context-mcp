"""A run that another process takes over must abandon, not resume.

``reindex.pause`` used to make an indexing run block until the marker
cleared and then continue from the same translation unit.  That is unsafe:
everything the loop decides with — the file mtime and hash snapshot, the
manifest lookup, the stale-header set, the header ownership accumulated TU by
TU — was captured before the pause, and a manual operation invalidates all of
it.  After ``reset_index`` the database is empty, so a resumed run takes the
mtime fast-path for rows that no longer exist and finishes by stamping a
manifest over a half-built index.

The run now gives up and the work is retried from a fresh snapshot.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.indexer.runner import (
    EXIT_SUPERSEDED,
    IndexSuperseded,
    raise_if_superseded,
)
from fw_context_mcp.utils import SAFE_EXCEPT


class TestSupersededGuard:
    def test_no_marker_lets_the_run_continue(self, tmp_path: Path):
        raise_if_superseded(tmp_path)

    def test_a_live_foreign_marker_aborts_the_run(self, tmp_path: Path):
        """PID 1 is always alive and is never us."""
        (tmp_path / "reindex.pause").write_text("1", encoding="utf-8")

        with pytest.raises(IndexSuperseded) as exc:
            raise_if_superseded(tmp_path)
        assert "pid 1" in str(exc.value)

    def test_our_own_marker_is_ignored(self, tmp_path: Path):
        """A foreground index writes the marker to stop the daemon, not itself."""
        (tmp_path / "reindex.pause").write_text(str(os.getpid()), encoding="utf-8")

        raise_if_superseded(tmp_path)

    def test_a_stale_marker_does_not_abort(self, tmp_path: Path):
        """The process that wrote it is gone, so nothing owns the index."""
        (tmp_path / "reindex.pause").write_text("2147483646", encoding="utf-8")

        raise_if_superseded(tmp_path)

    def test_an_unreadable_marker_does_not_abort(self, tmp_path: Path):
        """Garbage in the file must not wedge every future run."""
        (tmp_path / "reindex.pause").write_text("not-a-pid", encoding="utf-8")

        raise_if_superseded(tmp_path)


class TestSupersededContract:
    def test_it_is_outside_safe_except(self):
        """A handler that swallowed it would resume the run this exists to stop.

        The post-process loop catches SAFE_EXCEPT and carries on; if
        IndexSuperseded matched, the abandoned run would finish anyway and
        write its stale snapshot over the manual operation's result.
        """
        assert not issubclass(IndexSuperseded, SAFE_EXCEPT)

    def test_the_exit_code_is_neither_success_nor_failure(self):
        """The daemon has to tell three outcomes apart.

        0 means done, 1 means broken, and this means the work is still
        outstanding and has to be retried from the start.
        """
        assert EXIT_SUPERSEDED not in (0, 1)
