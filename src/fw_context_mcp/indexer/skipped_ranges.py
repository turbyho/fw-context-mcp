"""Line ranges that the preprocessor skipped, from the record of libclang.

WHY this module exists: the index must show only the code that compiles for
the active build.  Two earlier sources of that answer both let dead code
through, because each one only guessed at it:

1. ``tu.cursor.get_tokens()`` — ``clang_tokenize`` is a raw lexer over a
   range of characters.  It does not do preprocessing, thus it gives tokens
   for an inactive ``#if`` branch also.
2. The extent of a cursor — an extent is one continuous range of lines.  A
   dead block inside the body of a function is inside the extent of that
   function, thus the extent keeps it.

``clang_getAllSkippedRanges`` is not a guess.  It is the record that the
preprocessor itself made of the ranges it skipped.  The assembly path
(``asm.py``) already takes its active lines from the output of the
preprocessor.  This module gives the C path the same class of answer.

The python bindings of libclang do not give this function, thus the module
binds it with ``ctypes``.  ``clang_getAllSkippedRanges`` is in libclang from
version 5.0, and the project needs libclang 18.1.1 or later, thus the
function is always available.  The code degrades to an empty answer all the
same, because a build of libclang that cannot answer must not stop an index
run.  Three failures give that empty answer: the symbol is absent, the call
raises, and the structure of the answer does not match the layout.  An index
run of a large project takes hours, thus an unfiltered body is a far smaller
loss than a run that stops.
"""

from __future__ import annotations

import ctypes
import logging
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

from ..path_key import canonical_path

log = logging.getLogger(__name__)


@cache
def _bind() -> tuple[Any, Any] | None:
    """Bind the two C functions once, or give None when libclang lacks them.

    Returns ``(get_all_skipped_ranges, dispose_source_range_list)``, or None.

    The ``CXSourceRangeList`` structure is local to this function because it
    needs the ``SourceRange`` of the python bindings as its element type, and
    the bindings load lazily — every other module of the indexer imports
    ``clang.cindex`` inside a function for the same reason.  A local copy of
    the layout of ``CXSourceRange`` would go stale against the bindings.

    WHY a cache: the binding writes ``argtypes`` and ``restype`` on the
    shared library object of the bindings, and that work is the same every
    time.  One call for each process is enough.  Two threads that arrive
    together can run the body two times, but the writes are idempotent, thus
    the second run does no damage.
    """
    from clang import cindex

    class _SourceRangeList(ctypes.Structure):
        _fields_ = [
            ("count", ctypes.c_uint),
            ("ranges", ctypes.POINTER(cindex.SourceRange)),
        ]

    lib = cindex.conf.lib
    try:
        get_all = lib.clang_getAllSkippedRanges
        dispose = lib.clang_disposeSourceRangeList
    except AttributeError:
        log.debug(
            "libclang gives no clang_getAllSkippedRanges — inactive #if "
            "branches stay in files.content and in symbols.source"
        )
        return None

    get_all.argtypes = [cindex.TranslationUnit]
    get_all.restype = ctypes.POINTER(_SourceRangeList)
    dispose.argtypes = [ctypes.POINTER(_SourceRangeList)]
    dispose.restype = None
    return get_all, dispose


def _count_reads(tu: Any) -> dict[Path, int]:
    """Count how many times the preprocessor read each file of *tu*.

    One file can enter one translation unit more than one time.  That count
    is what ``collect_skipped_lines`` divides by, because a line is dead
    only when every read of its file skipped it.

    ``tu.get_includes()`` gives one entry for each ``#include`` directive
    that the preprocessor acted on.  A directive that the multiple-include
    optimization removed is in no entry, and that is what makes this count
    the correct divisor: clang applies the optimization when an include
    guard controls the whole file, and it then does not read the file a
    second time.  Measured with libclang 18: a header of that shape gives
    one entry for two directives, and a header of any other shape gives
    two entries.

    The main file of the unit is in no entry.  A caller must read an absent
    file as one read.
    """
    reads: Counter[Path] = Counter()
    # One resolve for each spelling, for the reason `collect_skipped_lines`
    # gives: a header reached many times names the same file every time.
    resolved: dict[str, Path] = {}
    for inclusion in tu.get_includes():
        included = inclusion.include
        if included is None:
            continue
        name = included.name
        path = resolved.get(name)
        if path is None:
            path = canonical_path(name)
            resolved[name] = path
        reads[path] += 1
    return dict(reads)


def collect_skipped_lines(tu: Any) -> dict[Path, set[int]]:
    """Give the lines that the preprocessor skipped, for each file of *tu*.

    Args:
        tu: A ``TranslationUnit`` of the python bindings of libclang.  The
            parse **must** use ``PARSE_DETAILED_PROCESSING_RECORD``.  Without
            that option the preprocessor keeps no record, and libclang gives
            zero ranges — a silent answer of "no dead code" for every file.
            Both parse sites of the indexer pass the option.

    Returns:
        A map from the resolved path of a file to the set of its dead
        lines.  A file with no dead line is not in the map.

        A line is dead only when EVERY read of its file skipped it.  One
        file can enter one unit more than one time, and a read that comes
        after the first one skips the whole block of an include guard.  The
        first read compiled that block, thus the block is live code.

        The path is resolved because libclang spells one file more than one
        way — ``./inc/api.h`` at the point of the ``#include`` and an
        absolute path in the extent of a cursor.  A caller must resolve its
        own path also, or the two never agree.

        An empty map is the correct answer for a translation unit that has
        no inactive branch.  It is also what a libclang without these
        symbols gives, thus a caller cannot tell the two apart — and does
        not need to, because both mean "blank out no line".

    A range covers the directive lines at its two ends: it starts on the
    ``#if`` (or ``#elif`` / ``#else``) line and ends on the line of the
    directive that closes the branch.  A conditional directive is always
    alone on its line, thus a caller can blank out every line of the range
    and lose no live code.
    """
    bound = _bind()
    if bound is None:
        return {}
    get_all, dispose = bound

    try:
        pointer = get_all(tu)
    except (ctypes.ArgumentError, AttributeError, TypeError):
        # A deliberate boundary: this is a hand-made binding to a C library.
        # The call raises when *tu* is not the type that `argtypes` declares,
        # and a build of libclang whose structure layout diverges raises when
        # the code reads the answer below.  The module promises that such a
        # libclang costs an index run nothing, and a run of a large project
        # takes hours — an unfiltered body is a far smaller loss than a run
        # that stops.
        log.debug("clang_getAllSkippedRanges did not answer", exc_info=True)
        return {}
    if not pointer:
        return {}

    # How many times each line of each file was skipped.  A plain union over
    # the ranges is wrong, because the ranges cover the WHOLE unit and one
    # file can be read more than one time.  See the count test below.
    hits: dict[Path, Counter[int]] = {}
    # One resolve for each spelling, and not one for each range.  A header
    # of a vendor SDK holds many `#if` regions that all name the same file,
    # and `canonical_path` walks the file system for every call.
    resolved: dict[str, Path] = {}

    def _resolve(name: str) -> Path:
        path = resolved.get(name)
        if path is None:
            path = canonical_path(name)
            resolved[name] = path
        return path

    try:
        source_range_list = pointer.contents
        for index in range(source_range_list.count):
            source_range = source_range_list.ranges[index]
            start = source_range.start
            end = source_range.end
            if start.file is None or end.file is None:
                continue
            key = _resolve(start.file.name)
            if _resolve(end.file.name) != key:
                # The two ends lie in different files, thus `end.line`
                # counts the lines of a file that is not `key`.  An
                # unterminated `#if` at the end of a header makes clang
                # report such a range: it starts in the header and ends in
                # the file that included it.  Every line between the two
                # numbers would be blanked in the header, live declarations
                # included, and no diagnostic would show it — the indexer
                # does not stop on an unterminated `#if`.
                log.debug(
                    "skipped range spans two files (%s to %s) — dropped",
                    start.file.name, end.file.name,
                )
                continue
            hits.setdefault(key, Counter()).update(range(start.line, end.line + 1))
    except (ctypes.ArgumentError, AttributeError, TypeError, ValueError):
        # Same boundary as the call above: reading the structure is where a
        # divergent layout shows itself.  Whatever was collected is dropped,
        # because a partial map blanks lines without the count that decides
        # which of them are dead.
        log.debug("could not read the skipped ranges of libclang", exc_info=True)
        return {}
    finally:
        # The C API owns this memory until the caller gives it back.  An
        # index run parses thousands of translation units, thus a leak here
        # grows without a limit.
        dispose(pointer)

    # Keep a line only when EVERY read of its file skipped it.
    #
    # WHY the count and not a union: an include guard stops clang from
    # applying its multiple-include optimization when the guard does not
    # control the whole file — another directive follows the `#endif`, or an
    # `#include` comes before the guard, and the second shape is common in a
    # vendor SDK.  clang then really reads the file again, and that second
    # read skips the full guarded block, because the guard macro is defined
    # by then.  A union of the two reads reports the live body of the header
    # as dead, and the content pass blanks out code that compiled.
    #
    # The test is `>=` and not `==` because two ranges of ONE read can cover
    # one line: a dead block inside a dead block gives a range for each, and
    # such a line is dead under either count.
    reads = _count_reads(tu)
    skipped: dict[Path, set[int]] = {}
    for path, counter in hits.items():
        # A file that get_includes() does not name is the main file of the
        # unit, and the preprocessor read it one time.
        needed = reads.get(path, 1)
        dead = {line for line, count in counter.items() if count >= needed}
        if dead:
            skipped[path] = dead

    return skipped
