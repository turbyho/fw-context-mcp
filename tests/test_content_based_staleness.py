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

from fw_context_mcp.mcp.shared.stale import (
    _check_file_stale,
    _content_differs,
    _file_differs,
    _in_racy_window,
)
from fw_context_mcp.utils import MTIME_TOLERANCE_S, compute_source_hash


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


class TestRacyWindow:
    """Git calls an entry whose stamp equals the index stamp "racily clean"."""

    def test_an_equal_stamp_is_racy(self):
        assert _in_racy_window(1000.0, 1000.0) is True

    def test_a_stamp_inside_the_tolerance_is_racy(self):
        assert _in_racy_window(1000.0 + MTIME_TOLERANCE_S / 2, 1000.0) is True

    def test_a_stamp_well_past_the_tolerance_is_not_racy(self):
        """Past the band the plain comparison already reports the change."""
        assert _in_racy_window(1000.0 + MTIME_TOLERANCE_S * 10, 1000.0) is False


class TestCheckFileStale:
    """mtime filters, the hash decides."""

    def test_a_git_touch_is_not_a_change(self, source: Path):
        """The case that started this: same bytes, fresh stamp."""
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        _touch_newer(source)

        assert _check_file_stale(str(source), stored_mtime, stored_hash) is False

    def test_a_real_edit_is_a_change(self, source: Path):
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        source.write_text("int modem_init(void) { return 1; }\n")
        _touch_newer(source)

        assert _check_file_stale(str(source), stored_mtime, stored_hash) is True

    def test_without_a_hash_the_timestamp_decides(self, source: Path):
        """An index written before the hash existed keeps the old behaviour."""
        stored_mtime = os.path.getmtime(source)
        _touch_newer(source)

        assert _check_file_stale(str(source), stored_mtime) is True

    def test_an_unchanged_stamp_reads_nothing(self, source: Path, monkeypatch):
        """The filter must come first, or every query would read every file."""
        import fw_context_mcp.mcp.shared.stale as stale_mod

        calls: list[str] = []
        monkeypatch.setattr(
            stale_mod, "compute_source_hash", lambda p: calls.append(str(p)) or ""
        )

        assert _check_file_stale(str(source), os.path.getmtime(source) + 100, "x") is False
        assert calls == [], "an untouched file must not be read"

    def test_a_missing_file_is_stale(self, tmp_path: Path):
        assert _check_file_stale(str(tmp_path / "absent.c"), 1.0, "x") is True


class TestFileDiffers:
    """The per-record path asks the content first, with no timestamp gate.

    A result names at most 200 files, thus the read is affordable and both
    failure modes of the timestamp disappear at once.
    """

    def test_a_git_touch_is_not_a_change(self, source: Path):
        stored_hash = compute_source_hash(source)
        _touch_newer(source)

        assert _file_differs(str(source), os.path.getmtime(source) - 100, stored_hash) is False

    def test_a_write_that_kept_the_old_stamp_is_caught(self, source: Path):
        """The failure mode the timestamp cannot see.

        `touch -r`, rsync --times and a fast write inside the tolerance band
        all leave the stamp alone.  The content check finds the change
        anyway.
        """
        stored_mtime = os.path.getmtime(source)
        stored_hash = compute_source_hash(source)
        source.write_text("int modem_init(void) { return 99; }\n")
        os.utime(source, (stored_mtime, stored_mtime))  # restore the old stamp

        assert _check_file_stale(str(source), stored_mtime, stored_hash) is True, (
            "the racy window must send this to the content check"
        )
        assert _file_differs(str(source), stored_mtime, stored_hash) is True

    def test_without_a_hash_it_falls_back_to_the_timestamp(self, source: Path):
        stored_mtime = os.path.getmtime(source)
        _touch_newer(source)

        assert _file_differs(str(source), stored_mtime, "") is True
