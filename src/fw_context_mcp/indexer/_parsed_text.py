"""The text that libclang parsed, read from libclang and not from the disk.

An index row holds the symbols of one parse, and the hash next to them must
be the hash of the text that this parse read.  A read of the disk after the
parse can see a newer text: a header that the operator saves while the parse
runs, or while the unit waits for the write lock.  The row then holds the
symbols of the old text and the hash of the new one, and the next run finds
the hash current and never parses the unit again.

libclang keeps the bytes of each file that it loaded, and
``clang_getFileContents`` gives them.  The bytes are the raw file, thus their
hash is the hash of the file, as :func:`fw_context_mcp.utils.compute_source_hash`
computes it.  Measured with libclang 18.1.1:

* 32 283 files of 210 units of seven real projects: each buffer has the
  hash of its file on the disk.
* A byte order mark, CRLF, no final newline, latin-1 bytes, an empty file
  and a file of 320 kB: each buffer is equal to the file.
* A change after the parse, by a rename or by a write in place of the same
  length, also of the 320 kB file: the buffer keeps the text of the parse.

The python bindings do not give this function, thus the module binds it with
``ctypes``, as ``skipped_ranges.py`` does for its function.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import logging
from functools import cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@cache
def _bind() -> Any | None:
    """Bind ``clang_getFileContents`` once, or give None when libclang lacks it.

    libclang exports the function from version 13, and the project needs
    18.1.1 or later.  Without it each hash is empty: an empty hash matches
    no stored hash, thus each unit is parsed again in each run.  That is
    slow, but the index stays correct, and the warning tells the operator.
    """
    from clang import cindex

    try:
        get_contents = cindex.conf.lib.clang_getFileContents
    except AttributeError:
        log.warning(
            "libclang gives no clang_getFileContents — no parsed file gets a hash, "
            "thus each index run parses each unit again"
        )
        return None
    get_contents.argtypes = [cindex.TranslationUnit, cindex.File, ctypes.POINTER(ctypes.c_size_t)]
    get_contents.restype = ctypes.c_void_p
    return get_contents


def parsed_bytes(tu: Any, cx_file: Any) -> bytes | None:
    """Return the bytes of *cx_file* as the parse of *tu* read them, or None.

    None when the file is not part of the parse: ``TranslationUnit.get_file``
    gives a handle with a null pointer for a file that libclang did not load,
    and the C function must not get it.
    """
    get_contents = _bind()
    if get_contents is None or not getattr(cx_file, "_as_parameter_", None):
        return None
    size = ctypes.c_size_t(0)
    pointer = get_contents(tu, cx_file, ctypes.byref(size))
    if not pointer:
        return None
    return ctypes.string_at(pointer, size.value)


def parsed_hash(tu: Any, cx_file: Any) -> str:
    """Return the SHA-256 of the parsed bytes of *cx_file*, or ``""``.

    ``""`` is what ``compute_source_hash`` gives for a file that it cannot
    read, and no stored hash is ``""``.  Thus a file without a buffer reads as
    changed in the next run, which is the safe direction.
    """
    data = parsed_bytes(tu, cx_file)
    return "" if data is None else hashlib.sha256(data).hexdigest()


def decode_lines(data: bytes) -> list[str]:
    """Return the lines of *data*, decoded as the content fill decoded the disk.

    The decode is the one of ``open(path, encoding="utf-8", errors="replace")``
    followed by ``readlines()``: UTF-8 with replacement, and universal
    newlines.  The stored content therefore does not change for a file that
    did not change.
    """
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace").readlines()


def parsed_hashes(tu: Any, main_file: Path) -> dict[Path, str]:
    """Return ``{resolved path: parsed hash}`` for the main file and each include of *tu*.

    One map serves each row that one unit writes.  The key is the resolved
    path, because one file has more than one spelling in one unit.

    *main_file* is the resolved path of the unit, the key of its hash and
    the name of the lookup.  ``tu.spelling`` is the name that the parse got,
    and it can be relative to the directory of the unit, which is not the
    current directory: the lookup by that name found no file.
    """
    hashes: dict[Path, str] = {}
    hashes[main_file] = parsed_hash(tu, tu.get_file(str(main_file)))
    seen: set[str] = set()
    for inc in tu.get_includes():
        name = str(inc.include.name)
        if name in seen:
            continue
        seen.add(name)
        hashes.setdefault(Path(name).resolve(), parsed_hash(tu, inc.include))
    return hashes
