"""Add the compilers of a project to the allowlist in ``.fw-context/toolchains.toml``.

WHY: the GCC driver query runs only a compiler that matches the allowlist
(see ``_driver_query``).  A toolchain in a directory of its own, for
example ``~/tools/gcc-arm-none-eabi-9/bin``, is outside the default list,
thus its units kept the guessed headers until the developer edited
``local.toml`` by hand, and nothing told the developer that the list
exists.  ``fw-context init``, ``fw-context doctor`` and each index run
(``fw-context index``, the background reindex, ``reindex_file``) now do
it, with no flag: they read the compilers that the project's
``compile_commands.json`` names, and write the path of each GCC driver
that is outside the list.

WHY the path of the file and not ``<bin>/*``: the checks below look at the
one compiler that the build names.  A glob for the directory would also
allow each other file in it, and a file that is added later, with no check.

WHY a file of its own and not ``local.toml``: ``local.toml`` belongs to the
developer.  A TOML write loses its comments, and a write over a file with a
syntax error would lose all of it.  The paths go under
``[index] query_driver_extra``, which ``config.load`` ADDS to
``query_driver``: a written copy of the effective list would freeze the
default, and a later release with more default entries would not reach the
project.  ``[index] query_driver_auto = false`` stops the writes and the
use of the file.

Not added (a refused compiler gets a warning with the reason; the developer
can add it to ``local.toml`` by hand):

- A compiler inside the project, or inside a work tree of a git
  repository that holds the project (a superproject too, and each
  worktree of each of them).  The files of a repository are not the developer's
  own: a committed ``compile_commands.json`` can name a script that the
  same repository commits.  A home directory that is a git work tree
  itself thus refuses each compiler under it; add those by hand.
- A binary that is not a GCC driver by name (``ccache``, ``clang``).
- A compiler that does not exist on this machine.
- On POSIX, a compiler that another user can replace.  Each entry that the
  kernel visits to open the file is checked: each link and each directory
  must belong to the current user or root, and no group and no other user
  may write to a directory or to the file.  WHY no group: the primary group
  of a user is not always a group of one (``staff`` on macOS, ``domain
  users`` with LDAP).  A sticky directory above the directory of the
  compiler (``/tmp``) is accepted: there, only the owner can replace an
  entry.
- On Windows, a compiler outside the home directory and the Program Files
  directories.  fw-context does not read ACLs; those directories are the
  ones that other users cannot write by default.
- A path with ``*`` or ``?`` in it.  The allowlist would read them as
  wildcards and allow more than the one file.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

from fw_context_mcp.indexer._driver_query import (
    compiler_token,
    driver_allowed,
    inside_project,
    is_gcc_driver_name,
    known_compilers,
    resolve_compiler,
)

log = logging.getLogger(__name__)

#: The first lines of ``toolchains.toml``.  WHY: a developer who finds the
#: file must see who writes it and where the keys of their own belong.
TOOLCHAINS_HEADER = (
    "# Written by fw-context (init, doctor and each index run).\n"
    "# The GCC compilers that this project's build names. fw-context runs them\n"
    "# to read their headers and macros.\n"
    "# Set your own globs in local.toml: [index] query_driver or query_driver_extra.\n"
    "# [index] query_driver_auto = false in local.toml stops the use of this file.\n"
)

#: The characters that ``_driver_query._glob_regex`` reads as wildcards.
_GLOB_CHARACTERS = frozenset("*?")

#: The limit of links in one path, as the ``ELOOP`` limit of Linux.
_MAX_LINKS = 40


def compile_commands_files(project_root: Path, configured: Path) -> list[Path]:
    """Give the compilation databases of the project: the configured one and those of variants."""
    candidates = [configured if configured.is_absolute() else project_root / configured]
    build_dir = project_root / ".fw-context" / "build"
    if build_dir.is_dir():
        candidates += sorted(build_dir.rglob("compile_commands.json"))
    unique: dict[str, Path] = {}
    for path in candidates:
        if path.is_file():
            unique.setdefault(os.path.normcase(str(path.resolve())), path)
    return list(unique.values())


def _entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        log.debug("cannot read %s: %s", path, error)
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def _home_glob(compiler: Path) -> str:
    """Give the path of *compiler*, with the home directory as ``~`` (the file is per developer)."""
    home = Path.home()
    try:
        return f"~/{compiler.relative_to(home).as_posix()}"
    except ValueError:
        return compiler.as_posix()


def repository_roots(project_root: Path) -> list[Path]:
    """Give each git work tree of each repository that holds *project_root*.

    Every directory at or above the project counts, by its path and by its
    real path, not only the nearest: a submodule is inside the work tree of
    its superproject, and a project root can be a link into a repository.
    For each repository found, all of its work trees count (see
    ``_work_trees``): another worktree of the same repository holds files
    of the same origin.
    """
    starts = dict.fromkeys([Path(os.path.abspath(project_root)), Path(os.path.realpath(project_root))])
    roots: dict[Path, None] = {}
    for start in starts:
        for directory in (start, *start.parents):
            marker = directory / ".git"
            if marker.exists():
                roots.setdefault(directory, None)
                for tree in _work_trees(marker):
                    roots.setdefault(tree, None)
    return list(roots)


def _git_dir(marker: Path) -> Path | None:
    """Give the git directory of the ``.git`` *marker* (a directory, or a file with ``gitdir:``)."""
    if marker.is_dir():
        return marker
    text = marker.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    return _relative_to(marker.parent, text.removeprefix("gitdir:").strip())


def _core_worktree(common: Path) -> Path | None:
    """Give ``core.worktree`` of the git config in *common*, or None.

    WHY: a submodule (``<super>/.git/modules/<name>``) and a repository
    made with ``--separate-git-dir`` keep the git directory away from the
    work tree, and only this key names the work tree.  A relative value is
    relative to the git directory.
    """
    import configparser

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(common / "config", encoding="utf-8")
    except configparser.Error as error:
        log.debug("cannot read %s: %s", common / "config", error)
        return None
    value = parser.get("core", "worktree", fallback=None)
    if not value:
        return None
    return _relative_to(common, value)


def _work_trees(marker: Path) -> list[Path]:
    """Give the work trees of the repository whose ``.git`` is *marker*.

    The common git directory is the git directory, or the directory that
    its ``commondir`` file names (a linked worktree).  Its work trees are:

    - the main one: the parent of a common directory named ``.git``, else
      ``core.worktree``; a bare repository has none;
    - each linked one: ``<common>/worktrees/<name>/gitdir`` names the
      ``.git`` file of that work tree.  Since git 2.48 the path can be
      relative (``--relative-paths``), and then it is relative to
      ``<common>/worktrees/<name>``.

    A file that cannot be read gives fewer trees, never an error, and only
    that one tree is lost: each read has its own ``try``.
    """
    try:
        gitdir = _git_dir(marker)
        if gitdir is None:
            return []
        common = gitdir
        common_file = gitdir / "commondir"
        if common_file.is_file():
            common = _relative_to(gitdir, common_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        log.debug("cannot read the git directory of %s: %s", marker, error)
        return []
    trees: list[Path] = []
    try:
        main = common.parent if common.name == ".git" else _core_worktree(common)
    except (OSError, ValueError) as error:
        log.debug("cannot read the main work tree of %s: %s", common, error)
        main = None
    if main is not None:
        trees.append(main)
    try:
        entries = sorted((common / "worktrees").iterdir())
    except OSError:
        entries = []  # no linked worktree
    for entry in entries:
        try:
            pointer = entry / "gitdir"
            if pointer.is_file():
                trees.append(_relative_to(entry, pointer.read_text(encoding="utf-8").strip()).parent)
        except (OSError, ValueError) as error:
            log.debug("cannot read the worktree %s: %s", entry, error)
    return trees


def _relative_to(base: Path, value: str) -> Path:
    """Give the path *value* of a git file; a relative one is relative to *base*."""
    path = Path(value)
    return Path(os.path.normpath(path if path.is_absolute() else base / path))


def _resolution_chain(path: Path) -> list[Path]:
    """Give each entry that the kernel visits to open *path*, links included, in order.

    WHY not the path and its real path only: in a chain of links
    ``a -> b -> c`` the location of ``b`` is in neither, and a user who can
    write to the directory of ``b`` can point it to a file of their choice.
    The last entry is the file itself.  Raises OSError on a loop.
    """
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    pending = list(absolute.parts[1:])
    visited: list[Path] = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        visited.append(candidate)
        if not candidate.is_symlink():
            current = candidate
            continue
        links += 1
        if links > _MAX_LINKS:
            raise OSError(f"too many links in {path}")
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            current = Path(target.anchor)
            pending = list(target.parts[1:]) + pending
        else:
            pending = list(target.parts) + pending
    return visited


def _windows_refusal(compiler: Path) -> str | None:
    """Give why *compiler* is outside the directories that other users cannot write by default.

    The path as the build names it and its real path must both be inside:
    a junction in ``C:\\tools`` that points into Program Files runs from
    ``C:\\tools``, and other users can replace it there.
    """
    names = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    roots = [str(Path.home()), *(os.environ[name] for name in names if os.environ.get(name))]
    keys = [os.path.normcase(os.path.realpath(root)).rstrip("\\/") + os.sep for root in roots]
    for candidate in (os.path.normpath(os.path.abspath(compiler)), os.path.realpath(compiler)):
        if not any(os.path.normcase(candidate).startswith(key) for key in keys):
            return "it is outside the home directory and Program Files, and fw-context does not read ACLs"
    return None


def replaceable_by_others(compiler: Path) -> str | None:
    """Give why another user could replace *compiler*, or None when no one but the owner can.

    See the module docstring for the rules.  The file and the directory
    that holds it must not be writable by others even when that directory
    is sticky: a world-writable directory is not a place for a compiler.
    """
    if os.name == "nt":
        return _windows_refusal(compiler)
    uid = os.getuid()
    chain = _resolution_chain(compiler)
    strict = {chain[-1], chain[-1].parent}
    for path in [Path(chain[-1].anchor), *chain]:
        info = path.lstat()
        if info.st_uid not in (uid, 0):
            return f"{path} belongs to another user (uid {info.st_uid})"
        if stat.S_ISLNK(info.st_mode):
            continue
        sticky = bool(info.st_mode & stat.S_ISVTX) and path not in strict
        if info.st_mode & stat.S_IWOTH and not sticky:
            return f"all users can write to {path}"
        if info.st_mode & stat.S_IWGRP and not sticky:
            return f"the group {info.st_gid} can write to {path}"
    return None


def _refusal(compiler: Path, repositories: Sequence[Path]) -> str | None:
    """Give why *compiler* must not go into the allowlist, or None."""
    if _GLOB_CHARACTERS & set(str(compiler)):
        return "its path holds a glob character (* or ?)"
    for repository in repositories:
        if inside_project(compiler, repository):
            return f"it is inside the git repository {repository}"
    try:
        return replaceable_by_others(compiler)
    except OSError as error:
        # A compiler that cannot be checked is not trusted; the other
        # compilers of the build are still checked.
        return f"cannot check its owner and mode: {error}"


def _named_compilers(entries: list[dict], cc_path: Path) -> tuple[list[dict], list[tuple[str, str]]]:
    """Give the entries whose compiler can be read, and each different GCC driver among them.

    A driver is a (compiler word, directory) pair.  An entry with an
    unclosed quote in its ``command`` is skipped: it must not stop the
    other entries, and ``known_compilers`` would raise on it.
    """
    readable: list[dict] = []
    named: dict[tuple[str, str], None] = {}
    for entry in entries:
        try:
            token = compiler_token(entry)
        except ValueError as error:
            log.debug("cannot read the compiler of an entry of %s: %s", cc_path, error)
            continue
        readable.append(entry)
        if token and not token.startswith("-") and is_gcc_driver_name(Path(token)):
            named.setdefault((token, str(entry.get("directory", cc_path.parent))), None)
    return readable, list(named)


def missing_toolchain_globs(
    cc_files: Iterable[Path], patterns: Sequence[str], project_root: Path,
) -> list[str]:
    """Give the path of each GCC driver of *cc_files* that *patterns* do not allow, sorted.

    Each compiler is resolved once: a build names one compiler in hundreds
    of entries, and the resolution can walk ``PATH``.  A compiler that is
    refused (see ``_refusal``) gets a warning and no entry.
    """
    repositories = repository_roots(project_root)
    globs: set[str] = set()
    for cc_path in cc_files:
        entries, drivers = _named_compilers(_entries(cc_path), cc_path)
        known = known_compilers(entries)
        for token, directory in drivers:
            compiler = resolve_compiler(token, Path(directory), known)
            if compiler is None or inside_project(compiler, project_root):
                continue
            if driver_allowed(compiler, patterns, project_root):
                continue
            reason = _refusal(compiler, repositories)
            if reason is not None:
                log.warning("fw-context does not add %s to the allowlist: %s", compiler, reason)
                continue
            globs.add(_home_glob(compiler))
    return sorted(globs)


def _writable_toolchains_file(project_root: Path) -> Path:
    """Give the path of ``toolchains.toml``; raise OSError when a write there is not safe.

    WHY: a repository can commit ``.fw-context`` or the file as a link.  A
    write through such a link would change a file outside the project, and
    a link that ``config.load`` refuses would make the write useless.
    """
    from fw_context_mcp.config.settings import toolchains_path

    path = toolchains_path(project_root)
    config_dir = path.parent
    if config_dir.is_symlink() or not config_dir.is_dir():
        raise OSError(f"{config_dir} is not a directory of the project")
    if os.path.realpath(config_dir.parent) != os.path.realpath(project_root):
        raise OSError(f"{config_dir} is outside {project_root}")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f"{path} is not a regular file")
    return path


def allow_project_toolchains(project_root: Path, extra_cc: Path | None = None) -> list[str]:
    """Write the missing compilers of *project_root* into ``toolchains.toml``; give them.

    *extra_cc* is a compilation database that a caller uses beside the
    configured one (an index run with ``--compile-commands``).  Gives the
    paths that were added (empty when nothing was missing, or when
    ``[index] query_driver_auto`` is false).  The file is written only when
    a path is missing, thus a second run changes nothing.  The entries that
    are already in the file stay.
    """
    import tomli_w

    from fw_context_mcp.config import load as load_config
    from fw_context_mcp.config._toml_editor import _atomic_write
    from fw_context_mcp.config.settings import read_toolchain_globs

    cfg = load_config(project_root=project_root)
    if not cfg.index.query_driver_auto:
        return []
    cc_files = compile_commands_files(project_root, cfg.index.compile_commands)
    if extra_cc is not None and extra_cc.is_file():
        known = {os.path.normcase(str(path.resolve())) for path in cc_files}
        if os.path.normcase(str(extra_cc.resolve())) not in known:
            cc_files.append(extra_cc)
    added = missing_toolchain_globs(cc_files, cfg.index.query_driver, project_root)
    if not added:
        return []
    path = _writable_toolchains_file(project_root)
    globs = list(dict.fromkeys([*read_toolchain_globs(project_root), *added]))
    body = tomli_w.dumps({"index": {"query_driver_extra": globs}})
    _atomic_write(path, TOOLCHAINS_HEADER + "\n" + body)
    return added


def ensure_project_toolchains(project_root: Path, extra_cc: Path | None = None) -> None:
    """Add the missing compilers before an index run parses; log what changed.

    WHY in each index run: a new toolchain appears with a build, and the
    build runs at index time (``fw-context index --build``, the background
    reindex of the daemon).  Without this step the run would parse that
    build with the guessed headers.  An error does not stop the run: the
    run then parses as before, and the warning of ``_driver_query`` names
    the compiler.  An uninitialized directory is left alone.
    """
    if not (project_root / ".fw-context").is_dir():
        return
    try:
        added = allow_project_toolchains(project_root, extra_cc)
    except (OSError, ValueError) as error:
        log.warning("Cannot add the toolchains of %s to the allowlist: %s", project_root, error)
        return
    for glob in added:
        log.warning("Added %s to [index] query_driver_extra in %s", glob, project_root / ".fw-context" / "toolchains.toml")
