"""The USR of a cursor, with a key of the file in place of the base name of a file-local USR.

WHY this module exists: libclang writes the location of a declaration into
the USR of each symbol with no external name: a `static` function or
variable (`c:drv.c@F@isr`), a local variable or parameter
(`c:drv.c@312@F@isr@arg`), a macro (`c:drv.h@744@macro@DRV_H`), and the
members of an anonymous namespace.  The location is the BASE NAME of the
file of the canonical declaration.  Two files of one base name in two
directories thus give one USR, and the index holds one symbol for two:
one body, one row of references, and the facts of both.  Measured: the
two `fatal.c` of Zephyr (`LOG_MODULE_REGISTER` defines `__log_level` in
each), and a copy of an SDK header in a project.

``cursor_usr`` replaces the base name with ``file_key``: the base name and
8 hex digits of the SHA-1 of the resolved path.  The hash has no ``@``,
thus a reader can split a USR at ``@`` as before; a path can hold one
(``AsyncTCP@3.4.10``).  The base name stays for the reader.  The path is
absolute: the extraction does not know the root of the project, and a
project that moves gets a new ``config_hash`` all the same.

The file of the prefix is the file of the canonical declaration of the
cursor: clang writes the location of the canonical declaration
(``USRGenerator::GenLoc``).  Measured on 107 units of a Zephyr, an Mbed and
an ESP32 build (3.65 million USRs with a prefix): the canonical
declaration gave each prefix, and no two USRs of libclang got one new USR.
A USR whose prefix is not the base name of that file stays as libclang
wrote it: no guess.
"""

from __future__ import annotations

import functools
import hashlib
import os

import clang.cindex as cx


@functools.lru_cache(maxsize=16384)
def file_key(path: str) -> str:
    """The key of the file *path* in a USR: ``<base name>~<8 hex digits of the SHA-1 of the resolved path>``."""
    resolved = os.path.realpath(path)
    digest = hashlib.sha1(resolved.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()
    return f"{os.path.basename(resolved)}~{digest[:8]}"


def cursor_usr(cursor: cx.Cursor) -> str:
    """The USR of *cursor*, with ``file_key`` in place of the base name of a file-local USR ("" for none)."""
    usr = cursor.get_usr()
    if not usr.startswith("c:") or usr.startswith("c:@"):
        return usr
    base, separator, rest = usr[2:].partition("@")
    if not separator:
        return usr
    canonical = cursor.canonical
    file = canonical.location.file if canonical is not None else None
    # WHY os.path.basename and not Path: this runs for each reference of a
    # unit, and Path builds an object each time (measured: 1.1 against
    # 0.1 microseconds a call).
    if file is None or os.path.basename(file.name) != base:
        return usr
    return f"c:{file_key(file.name)}@{rest}"
