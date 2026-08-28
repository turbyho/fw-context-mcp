"""Tests for staleness decided by content instead of by timestamp.

The mtime of a file answers wrongly in both directions:

* Too often — git rewrites it without changing the bytes.  ``checkout``,
  ``pull`` and ``stash pop`` all do it, and a checkout back to the original
  branch restores the same content with a fresh stamp.
* Not often enough — a write inside ``MTIME_TOLERANCE_S`` of the index run
  keeps the old stamp, and so do ``touch -r`` and rsync ``--times``.

Git solves this with a stat cache that filters and an object hash that
decides, plus a "racily clean" rule for entries whose stamp is too close to
trust.  These tests hold the same behaviour in place here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.shared.stale import _content_differs, _file_differs
from fw_context_mcp.utils import compute_source_hash


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "modem.c"
    path.write_text("int modem_init(void) { return 0; }\n")
    return path


def _touch_newer(path: Path, seconds: float = 100.0) -> None:
    """Rewrite the mtime without touching the content, as git does."""
    now = os.path.getmtime(path)
    os.utime(path, (now + seconds, now + seconds))


class TestContentDiffers:
    def test_a_matching_hash_reports_no_difference(self, source: Path):
        assert _content_differs(str(source), compute_source_hash(source)) is False

    def test_a_different_hash_reports_a_difference(self, source: Path):
        assert _content_differs(str(source), "0" * 64) is True

    def test_no_stored_hash_cannot_answer(self, source: Path):
        """None means "ask the timestamp", not "unchanged"."""
        assert _content_differs(str(source), "") is None

    def test_an_unreadable_file_cannot_answer(self, tmp_path: Path):
        assert _content_differs(str(tmp_path / "absent.c"), "0" * 64) is None


class TestFileDiffers:
    """One function decides for one file: the content, then the timestamp.

    It used to be three in a row — _file_differs over _check_file_stale over
    _in_racy_window.  Once the project-wide scan stopped gating on the
    timestamp, _check_file_stale had one caller left and that caller never
    passed it a hash, so its content branch was unreachable and the racy
    window it consulted changed no answer at all.  The cases below are the
    ones those three carried between them; none was dropped.
    """

    def test_a_git_touch_is_not_a_change(self, source: Path):
        """The case that started this: same bytes, fresh stamp."""
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        _touch_newer(source)

        assert _file_differs(str(source), stored_mtime, stored_hash) is False

    def test_a_real_edit_is_a_change(self, source: Path):
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        source.write_text("int modem_init(void) { return 1; }\n")
        _touch_newer(source)

        assert _file_differs(str(source), stored_mtime, stored_hash) is True

    def test_a_write_that_kept_the_old_stamp_is_caught(self, source: Path):
        """The failure mode the timestamp cannot see.

        `touch -r`, rsync --times and a fast write inside the tolerance band
        all leave the stamp alone.  The content check finds the change
        anyway, and it needs no gate to get there.
        """
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        source.write_text("int modem_init(void) { return 99; }\n")
        os.utime(source, (stored_mtime, stored_mtime))  # restore the old stamp

        assert _file_differs(str(source), stored_mtime, stored_hash) is True

    def test_a_backdated_file_is_caught_too(self, source: Path):
        """The direction the old gate dropped: a stamp that moved backwards."""
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        source.write_text("int modem_init(void) { return 7; }\n")
        os.utime(source, (stored_mtime - 3600, stored_mtime - 3600))

        assert _file_differs(str(source), stored_mtime, stored_hash) is True

    def test_without_a_hash_the_timestamp_decides(self, source: Path):
        """An index written before the hash existed keeps the old behaviour."""
        stored_mtime = os.path.getmtime(source)
        _touch_newer(source)

        assert _file_differs(str(source), stored_mtime) is True

    def test_without_a_hash_an_unchanged_stamp_is_not_a_change(self, source: Path):
        assert _file_differs(str(source), os.path.getmtime(source) + 100) is False

    def test_a_missing_file_is_a_change(self, tmp_path: Path):
        assert _file_differs(str(tmp_path / "absent.c"), 1.0, "x") is True

    def test_an_unreadable_file_without_a_hash_is_not_a_change(self, tmp_path: Path):
        """A directory stats fine and cannot be read; do not call that a change."""
        d = tmp_path / "adir"
        d.mkdir()
        assert _file_differs(str(d), os.path.getmtime(d) + 100) is False


class TestCountModifiedGate:
    """The gate in _count_modified_files must exclude what it was meant to.

    It read "newer than stored, or inside the racy window", and that window
    is symmetric around the STORED stamp.  An unchanged file carries exactly
    that stamp, so it fell inside and was hashed — measured on zbox-ecb-fw,
    1910 of 1911 rows reached the hash.  The gate filtered nothing, while
    the one set it did exclude was a stamp that had moved backwards, which
    is a file replaced by an older copy.
    """

    @staticmethod
    def _indexed(tmp_path: Path, count: int = 3):
        """Index *count* files with their real mtime and hash."""
        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )
        from fw_context_mcp.utils import compute_source_hash

        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        paths = []
        for i in range(count):
            f = root / "src" / f"f{i}.c"
            f.write_text(f"int f{i}(void) {{ return {i}; }}\n", encoding="utf-8")
            paths.append(f)

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(root / "compile_commands.json"))
            for f in paths:
                upsert_file(
                    conn, "ch", f"src/{f.name}", "c",
                    mtime=f.stat().st_mtime,
                    source_hash=compute_source_hash(f),
                )
        return conn, root, paths

    @staticmethod
    def _count_with_hash_calls(conn, root: Path, monkeypatch) -> tuple[int, int]:
        """Return (modified, number of files whose content was read)."""
        import fw_context_mcp.mcp.shared.stale as stale_mod

        calls = {"n": 0}
        original = stale_mod._content_differs

        def counting(path, stored_hash):
            calls["n"] += 1
            return original(path, stored_hash)

        monkeypatch.setattr(stale_mod, "_content_differs", counting)
        modified = stale_mod._count_modified_files(conn, "ch", root, use_cache=False)
        return modified, calls["n"]

    def test_a_project_at_rest_hashes_nothing(self, tmp_path: Path, monkeypatch):
        """The cost regression: every unchanged file used to reach the hash."""
        conn, root, _ = self._indexed(tmp_path)
        try:
            modified, hashed = self._count_with_hash_calls(conn, root, monkeypatch)
        finally:
            conn.close()

        assert modified == 0
        assert hashed == 0, (
            "an exact stamp match is the file the index hashed; reading it "
            "again is what made get_active_build hash the whole project"
        )

    def test_a_backdated_file_with_other_bytes_is_counted(self, tmp_path: Path):
        """The correctness regression: the old gate dropped exactly this case."""
        import os

        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        conn, root, paths = self._indexed(tmp_path)
        try:
            target = paths[0]
            stored = target.stat().st_mtime
            target.write_text("int completely_different(void);\n", encoding="utf-8")
            os.utime(target, (stored - 3600, stored - 3600))
            modified = _count_modified_files(conn, "ch", root, use_cache=False)
        finally:
            conn.close()

        assert modified == 1, (
            "a restore from a backup keeps the older stamp; the content is "
            "what says the file changed"
        )

    def test_the_two_helpers_agree_on_a_backdated_file(self, tmp_path: Path):
        """The invariant, not the implementation.

        get_active_build reads _count_modified_files and the search tools
        read _stale_files.  A file the one reports and the other does not
        gives the caller two readings and no way to choose — which is the
        contradiction 242472e set out to remove.
        """
        import os

        from fw_context_mcp.mcp.shared.stale import _count_modified_files, _stale_files

        conn, root, paths = self._indexed(tmp_path)
        try:
            target = paths[0]
            stored = target.stat().st_mtime
            target.write_text("int completely_different(void);\n", encoding="utf-8")
            os.utime(target, (stored - 3600, stored - 3600))

            counted = _count_modified_files(conn, "ch", root, use_cache=False)
            listed = _stale_files(conn, "ch", [str(target)], root)
        finally:
            conn.close()

        assert (counted > 0) == bool(listed), (
            f"the two disagree: counter={counted}, per-file={listed}"
        )

    def test_a_float_round_trip_does_not_count(self, tmp_path: Path):
        """The epsilon is for the SQLite REAL round trip, nothing wider."""
        from fw_context_mcp.indexer.db import transaction
        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        conn, root, paths = self._indexed(tmp_path)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE files SET mtime = mtime + 1e-9 WHERE config_hash='ch'"
                )
            modified = _count_modified_files(conn, "ch", root, use_cache=False)
        finally:
            conn.close()

        assert modified == 0

    def test_a_newer_file_is_still_counted(self, tmp_path: Path):
        """The ordinary edit must not get lost in the new rule."""
        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        conn, root, paths = self._indexed(tmp_path)
        try:
            paths[0].write_text("int changed(void);\n", encoding="utf-8")
            import os

            stored = paths[0].stat().st_mtime
            os.utime(paths[0], (stored + 3600, stored + 3600))
            modified = _count_modified_files(conn, "ch", root, use_cache=False)
        finally:
            conn.close()

        assert modified == 1

    def test_a_missing_file_is_counted(self, tmp_path: Path):
        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        conn, root, paths = self._indexed(tmp_path)
        try:
            paths[0].unlink()
            modified = _count_modified_files(conn, "ch", root, use_cache=False)
        finally:
            conn.close()

        assert modified == 1
