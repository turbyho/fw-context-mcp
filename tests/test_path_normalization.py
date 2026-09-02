"""``files.path`` must hold one spelling per file.

Every path-keyed lookup in the indexer assumes it.  When it did not hold, an
out-of-project header got two rows — symbols hung off one, ifdef content off
the other — and the coverage purge then removed whichever spelling the
manifest did not use.  Measured on the ESP32 project: 145 duplicate rows, and the
entire C++ standard library became invisible once 6698 symbols went with them.

This is the third time the same divergence appeared in this indexer, so the
invariant gets its own tests rather than a comment.
"""
from __future__ import annotations

from fw_context_mcp.indexer.ops import _normalize_file_path


def test_an_unresolved_path_is_stored_resolved(tmp_path):
    """libclang reports system headers with '..' left in.

    The shape below is what a GCC include path really looks like:
    ``/usr/lib64/gcc/x86_64-pc-linux-gnu/16/../../../../include/c++/16/algorithm``.
    Storing it verbatim is what created the second spelling.
    """
    raw = str(tmp_path / "lib" / "gcc" / ".." / ".." / "include" / "x.h")
    stored = _normalize_file_path(raw, tmp_path / "project")
    assert ".." not in stored, f"path kept its '..' segments: {stored}"


def test_both_spellings_of_one_file_normalise_together(tmp_path):
    """The invariant itself, stated as an equality.

    A file reached two ways must land on one string, or it gets two rows.
    """
    target = tmp_path / "sdk" / "include" / "x.h"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    detour = tmp_path / "sdk" / "include" / ".." / "include" / "x.h"

    root = tmp_path / "project"
    assert _normalize_file_path(str(detour), root) == _normalize_file_path(
        str(target), root
    )


def test_a_project_file_stays_relative(tmp_path):
    """The relative form for in-project files is unchanged."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    f = root / "src" / "main.c"
    f.write_text("", encoding="utf-8")
    assert _normalize_file_path(str(f), root) == "src/main.c"
