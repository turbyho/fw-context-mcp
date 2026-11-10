"""The key of a file in the index: one spelling for each file, on each system.

The index holds the path of a file in many tables (``files.path``,
``symbols.file_path``, ``refs.from_file``, ``header_owners``, the manifest
and others), and each lookup compares these strings.  Thus each file must
have exactly one spelling.  This module makes it.

The key of a file:

- A file inside the project root: its path relative to the root.
- A file outside the root (an SDK, the compiler headers): its absolute path.
- The separator is always ``/``, also on Windows (``src/a.c``,
  ``C:/ncs/zephyr/kernel/init.c``).
- The case of each name is the case on the disk.

WHY ``/`` on each system: the build-directory and vendor patterns
(``build/``, ``mbed-os/%``), the output of ``git ls-files`` and the paths of
the MCP queries use ``/``.  On Linux and macOS ``str(Path)`` already gives
``/``, thus the keys of an existing index do not change there.  The other
choice, the separator of the system, made Windows keys such as
``deps\\ncs/%`` that match nothing.

WHY the case of the disk: Windows and macOS have file systems that ignore
case.  ``SRC/Main.c`` and ``src/main.c`` are one file there, but two
strings for SQLite and Python.  The disk has one spelling.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "ABSOLUTE_KEY_SQL",
    "canonical_key",
    "canonical_path",
    "index_key",
    "is_absolute_key",
    "key_path",
    "pattern_key",
]

#: A SQL condition that is true for an absolute key in the column ``path``.
#: The two forms: ``/opt/...`` (POSIX) and ``C:/...`` (Windows, a drive
#: letter).  ``GLOB`` is case-sensitive, thus the class holds the two cases.
ABSOLUTE_KEY_SQL = "(path LIKE '/%' OR path GLOB '[A-Za-z]:/*')"


def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Return the absolute path of *path*, with links resolved and the case of the disk.

    The file does not have to exist.  For a path that does not exist, the
    part that exists gets the case of the disk, and the rest stays as it is.

    - Linux: ``Path.resolve()``.  The file systems of Linux keep the case
      that a name has, thus no change of case is possible.
    - Windows: ``Path.resolve()``.  It gives the case of the disk for each
      part that exists (measured on Windows 11, Python 3.11, 2026-11-08:
      ``mixeddir\\SUB\\file.c`` gives ``MixedDir\\Sub\\File.C``).
    - macOS: ``Path.resolve()`` keeps the case that the caller wrote.  The
      function asks the kernel for the path of the part that exists
      (``fcntl.F_GETPATH``), and that path has the case of the disk.
    """
    resolved = Path(path).resolve()
    if sys.platform == "darwin":
        return _disk_case_darwin(resolved)
    return resolved


if sys.platform == "darwin":
    import fcntl

    #: ``MAXPATHLEN`` of macOS: the size of the buffer of ``F_GETPATH``.
    _MAXPATHLEN = 1024

    def _disk_case_darwin(resolved: Path) -> Path:
        """Return *resolved* with the case of the disk for the part that exists.

        ``O_EVTONLY`` opens the file for events only, and with
        ``O_NONBLOCK`` a FIFO does not block the open.  A path that the
        process cannot examine keeps the spelling of *resolved*: a file with
        mode 000 (``os.open`` gives EACCES), or a path below a directory
        without search permission (``Path.exists`` raises EACCES on Python
        before 3.12).  The indexer cannot read such a file, and no other
        source gives its case.  Measured on macOS 26 (APFS), 2026-11-09:
        ``F_GETPATH`` gives the case of the disk for a file and for a
        directory, the path of the link that the open used for a file with
        two hard links, and the name of a FIFO.

        WHY the compare after ``F_GETPATH``: the kernel gives one path of
        the vnode.  For a file with two hard links it can be the path of the
        other link, for example a link outside the project.  That path is
        not a different spelling of *resolved*, thus the function keeps
        *resolved*.
        """
        existing = resolved
        tail: list[str] = []
        try:
            while not existing.exists():
                if existing.parent == existing:
                    return resolved
                tail.append(existing.name)
                existing = existing.parent
            fd = os.open(existing, os.O_EVTONLY | os.O_NONBLOCK)
            try:
                buffer = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_MAXPATHLEN))
            finally:
                os.close(fd)
        except OSError:
            return resolved
        on_disk = Path(os.fsdecode(buffer.split(b"\0", 1)[0]))
        if not _same_spelling(on_disk, existing):
            return resolved
        return on_disk.joinpath(*reversed(tail))

    def _same_spelling(a: Path, b: Path) -> bool:
        """Return True when *a* and *b* differ only in case and in the Unicode form of the names.

        These are the two differences that the default file system of macOS
        (APFS, not case-sensitive) ignores.
        """
        import unicodedata

        def folded(path: Path) -> str:
            return unicodedata.normalize("NFD", str(path)).casefold()

        return folded(a) == folded(b)


def index_key(path: str | os.PathLike[str], project_root: Path) -> str:
    """Return the key of the file *path* in the index of the project at *project_root*.

    *project_root* goes through :func:`canonical_path` too.  WHY: a root
    that a caller wrote with a different case, or through a link, is not a
    prefix of the canonical path of its own file, and the file would get an
    absolute key.
    """
    return canonical_key(canonical_path(path), project_root)


def canonical_key(canonical: Path, project_root: Path) -> str:
    """Return the index key of *canonical*, a path that :func:`canonical_path` gave.

    The second half of :func:`index_key`, for a caller that needs the
    canonical path too: it makes the path canonical one time, and not a
    second time inside ``index_key`` (on macOS an open and an ``fcntl``
    for each call).
    """
    try:
        relative = canonical.relative_to(_canonical_root(os.fspath(project_root)))
    except ValueError:
        return canonical.as_posix()
    return relative.as_posix()


@functools.lru_cache(maxsize=32)
def _canonical_root(project_root: str) -> Path:
    """Return :func:`canonical_path` of a project root, one time for each spelling.

    WHY a cache: the indexer makes a key for each symbol and for each header
    of each unit (86k header records on the largest test project), and each
    call made the root canonical again.  On macOS that is an open and an
    ``fcntl`` for each call.  A root does not move while a process works on
    its project.
    """
    return canonical_path(project_root)


def key_path(key: str, project_root: Path) -> Path:
    """Return the canonical absolute path of the file with the index key *key*: the reverse of :func:`index_key`.

    No call to the file system for each key: a relative key is joined to
    the canonical root, and the key already has the case of the disk.
    """
    if is_absolute_key(key):
        return Path(key)
    return _canonical_root(os.fspath(project_root)) / key


def is_absolute_key(key: str) -> bool:
    """Return True when *key* is the key of a file outside the project root.

    WHY not ``os.path.isabs``: the result depends on the system that runs
    the check.  On Windows ``os.path.isabs("/opt/sdk")`` is False since
    Python 3.13, and on Linux ``os.path.isabs("C:/ncs")`` is False.  A key
    has one of the two forms on each system.
    """
    return PurePosixPath(key).is_absolute() or PureWindowsPath(key).is_absolute()


def pattern_key(pattern: str, project_root: Path) -> str:
    """Return the SQL LIKE *pattern* of paths in the form of the index keys.

    A vendor or project pattern (``mbed-os/%``, ``deps/ncs/zephyr/%``,
    ``C:/esp/components/fork/%``) must match the keys of
    :func:`index_key`.  Thus:

    - On Windows a ``\\`` becomes ``/``.
    - The directory part before the first ``%`` (``deps/ncs/zephyr``) goes
      through :func:`index_key`: the case of the disk, links resolved,
      relative inside the root.  The keys of the files below it are made
      the same way, thus the pattern and the keys agree also for a vendor
      tree that is a link, or a pattern of the user in a different case.
    - A pattern that starts with ``%`` (``%mbed-os/%``), or has no ``/``
      before its first ``%``, has no directory part to make canonical, and
      keeps its text.  The text after the last ``/`` of the directory part
      also keeps its text: it is not a complete name.

    Examples on Windows: ``deps\\ncs/%`` gives ``deps/ncs/%``, and
    ``C:\\ESP\\fork\\%`` gives ``C:/esp/fork/%`` when the disk has
    ``C:\\esp\\fork``.
    """
    if os.name == "nt":
        pattern = pattern.replace("\\", "/")
    literal, wildcard, rest = pattern.partition("%")
    directory, slash, partial = literal.rpartition("/")
    if not wildcard or not slash or not directory:
        return pattern
    return index_key(project_root / directory, project_root) + "/" + partial + wildcard + rest
