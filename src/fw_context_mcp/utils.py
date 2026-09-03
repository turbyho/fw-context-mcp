"""Shared utilities used across fw-context-mcp.

Avoids duplication of project-root resolution, path normalisation, and
other small helpers that were copied into multiple modules.

Why a single utils module instead of topic-split utility files:

- These helpers have zero interdependencies and are each called from 3+
  unrelated modules.  Splitting them across files would scatter imports
  without improving cohesion.
- The ``SAFE_EXCEPT`` tuple and its ``is_fatal`` / ``is_db_exception``
  guard pair must be in one import-able place — every try/except in the
  codebase needs the same fatal-vs-recoverable classification.
- ``read_file_lines`` with its LRU cache and encoding fallback chain
  replaces four copy-pasted file-reading loops that had diverged subtly
  (different encoding orders, no mtime invalidation, different error
  handling for missing files).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fw_context_mcp.indexer.build import BuildConfig

from fw_context_mcp import _stdlib_sqlite3

log = logging.getLogger(__name__)

__all__ = [
    "AUTOBUILD_REL",
    "CC_OUTPUT_REL",
    "CPP_EXTENSIONS",
    "CPP_HEADER_EXTENSIONS",
    "CPP_SOURCE_EXTENSIONS",
    "C_SOURCE_EXTENSIONS",
    "DEPS_REL",
    "FW_CONTEXT_REL",
    "HEADER_EXTENSIONS",
    "MTIME_TOLERANCE_S",
    "SAFE_EXCEPT",
    "TU_EXTENSIONS",
    "abs_path",
    "autobuild_dir",
    "build_dir_patterns_with_fw_context",
    "build_env",
    "cc_output_path",
    "compute_content_hash",
    "compute_source_hash",
    "fmt_count",
    "format_number_ranges",
    "ignore_autobuild_dir",
    "is_compile_commands_stale",
    "is_db_exception",
    "is_fatal",
    "read_file_lines",
    "resolve_build_dir",
    "resolve_project_root",
    "resolve_real_binary",
    "truncate_path_middle",
]

# Seconds of tolerance when comparing file mtimes to account for
# clock skew between the indexer and the filesystem.
MTIME_TOLERANCE_S: float = 1.0

# ── Compiler rules, from the file-suffix table in `man gcc` ──
#
# These are rules of the COMPILER, not a setting of the project: a project
# cannot decide that `.C` means C.  What a project CAN decide — which
# extensions its own build compiles — is derived from compile_commands.json
# instead; see indexer/manifest.py.
#
# CASE IS PART OF THE RULE and nothing here goes through `.lower()`.  gcc
# reads `.c` as C and `.C` as C++, and lowercasing merged the two: a `.C`
# file was classified as C, which is the opposite of what the compiler did
# with it.  Verified against gcc and clang directly, not from memory.
C_SOURCE_EXTENSIONS: frozenset[str] = frozenset({".c"})
CPP_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {".cc", ".cp", ".cxx", ".cpp", ".CPP", ".c++", ".C"}
)

# What the compiler will build as a translation unit from source.  Assembly
# is deliberately absent: it reaches the index through the derived set of a
# project that compiles it, not through a rule that says every project does.
TU_EXTENSIONS: frozenset[str] = C_SOURCE_EXTENSIONS | CPP_SOURCE_EXTENSIONS

# `.h` is the ambiguous one — gcc calls it "C, C++, ObjC or ObjC++ header",
# so it is not in the C++ set and a `.h` counts as C for classification.
CPP_HEADER_EXTENSIONS: frozenset[str] = frozenset(
    {".hh", ".H", ".hp", ".hxx", ".hpp", ".HPP", ".h++", ".tcc"}
)
HEADER_EXTENSIONS: frozenset[str] = CPP_HEADER_EXTENSIONS | {".h"}

# Everything the compiler reads as C++, source or header.  One name for the
# question "is this C++?", which five places used to answer with five
# inline literals.
CPP_EXTENSIONS: frozenset[str] = CPP_SOURCE_EXTENSIONS | CPP_HEADER_EXTENSIONS

# Everything fw-context writes into a project sits under this one directory,
# which ``fw-context init`` adds to .gitignore.  One name, because more than
# one place has to recognise it: the paths below are built from it, and the
# new-source scan skips it wholesale.
FW_CONTEXT_REL: Path = Path(".fw-context")

# Relative location (from the project root) of the generated
# compile_commands.json.  Kept out of the project root so the build artifact
# does not pollute the repository.
CC_OUTPUT_REL: Path = FW_CONTEXT_REL / "build" / "compile_commands.json"

# Output directory of a build that fw-context starts on its own, one
# subdirectory per variant.  It sits apart from the directory of the build
# that the user runs: fw-context cannot lock that build, thus separation is
# the only defence against two builds in one directory.
AUTOBUILD_REL: Path = FW_CONTEXT_REL / "autobuild"

# Where a backend puts dependency (.d) files when no isolated build
# directory is set.  They must never land beside the source: there they sit
# where the build of the user reads them, and a compiler does not write them
# atomically, thus a concurrent make can read a truncated file.
DEPS_REL: Path = FW_CONTEXT_REL / "build" / "deps"


def build_dir_patterns_with_fw_context(patterns: list[str]) -> list[str]:
    """Add the fw-context directory to the build-output patterns of a backend.

    Everything fw-context writes into a project sits under FW_CONTEXT_REL,
    and an isolated automatic build puts the generated configuration headers
    there — mbed_config.h, sdkconfig.h, autoconf.h.  The patterns of the
    backend do not reach them.  Measured against the real manifests:
    mbed-os gives ``BUILD/``, platformio ``.pio/build/``, zephyr
    ``build/nrf52840_sysbuild/``, and none of the three matches.  The
    backends whose pattern is ``build/`` match only because
    ".fw-context/autobuild/" happens to hold that substring, which is not a
    rule anything should rest on.

    A header the pipeline does not know is generated is re-hashed on every
    index run, and a generated header changes with every build without a
    change of meaning — so every unit that includes it is marked
    header-stale and re-parsed, which is the opposite of what the three
    staleness tiers are for.
    """
    return [*patterns, f"{FW_CONTEXT_REL}/"]


def autobuild_dir(variant: str = "") -> str:
    """Return the isolated build directory for *variant*, relative to the root.

    An empty *variant* names the single-build case.  The name goes into the
    path because every variant has its own output directory, thus one shared
    directory would make the variants overwrite each other.
    """
    return str(AUTOBUILD_REL / (variant or "default"))


def format_number_ranges(numbers: Sequence[int]) -> str:
    """Fold sorted numbers into ``"0-88, 90, 219-221"``.

    A table of 290 interrupt slots can have 284 of them to report, and a
    bare list of 284 numbers is not something a reader takes in.  Ranges
    stay complete while staying short — the worst case measured folds to
    51 characters.

    *numbers* must be sorted and hold no duplicate; the callers derive them
    from a set difference, which gives both.
    """
    if not numbers:
        return ""
    parts: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def ignore_autobuild_dir(project_root: Path) -> None:
    """Keep the autobuild output out of the project's git status.

    fw-context builds into ``.fw-context/autobuild/`` when it starts a build
    of its own, and that output is a build artifact of a tool, never
    something to commit.  The project's ``.gitignore`` cannot be relied on
    for it: ``fw-context init`` is the only writer of that file, so a
    project initialised before this directory existed lists
    ``.fw-context/build/`` and nothing more, and the autobuild output then
    shows up as untracked — measured on the Mbed project.

    Writing the rule INSIDE the directory fixes that without touching a
    file the user owns, and it covers a project that will never run ``init``
    again.  ``*`` also hides this ``.gitignore`` itself, which is what we
    want: nothing here belongs in a commit.

    Silent on failure.  A build artifact that stays visible in git status
    is untidy; refusing to build over it would be worse.
    """
    marker = project_root / AUTOBUILD_REL / ".gitignore"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("# Build output of fw-context. Not for committing.\n*\n",
                          encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).debug(
            "could not write %s", marker, exc_info=True,
        )


# Standard exception tuple for non-fatal recoverable errors.
# Use in all broad-except blocks where the operation can safely
# continue or log+skip.  Always pair with is_fatal() check first.
#
# Includes stdlib sqlite3.Error (not just pysqlite3) because
# external libraries that import sqlite3 before the pysqlite3
# redirect may raise the stdlib type.
_STDLIB_DB_ERROR = (_stdlib_sqlite3.Error,) if _stdlib_sqlite3 is not None else ()

SAFE_EXCEPT = (
    ValueError, TypeError, RuntimeError, AttributeError,
    sqlite3.Error, OSError,
) + _STDLIB_DB_ERROR


def is_fatal(exc: BaseException) -> bool:
    """Return True for exceptions that must never be swallowed."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError, SystemError))


def is_db_exception(exc: BaseException) -> bool:
    """Return True for sqlite3 database exceptions from either stdlib or pysqlite3.

    After :mod:`fw_context_mcp` redirects ``sqlite3`` → ``pysqlite3``, internal
    ``sqlite3.Error`` resolves to ``pysqlite3.dbapi2.Error``, but
    externally‑created connections raise stdlib ``sqlite3.Error`` — a
    different C‑extension type.  This helper matches both.

    Always call **before** :func:`is_fatal` — fatal exceptions
    (:class:`KeyboardInterrupt`, :class:`SystemExit`, :class:`MemoryError`)
    are not DB errors and must propagate.

    Usage::

        try:
            rows = conn.execute(...).fetchall()
        except Exception as exc:
            if not is_db_exception(exc):
                raise
            return None
    """
    if is_fatal(exc):
        return False
    if isinstance(exc, sqlite3.Error):
        return True
    if _stdlib_sqlite3 is not None and isinstance(exc, _stdlib_sqlite3.Error):
        return True
    return False


# Shell startup variables that must not reach a build subprocess.
#
# ``bash -c`` reads the file named by BASH_ENV BEFORE it runs the command,
# and neither ``--norc`` nor ``--noprofile`` suppresses that — measured, not
# assumed.  An AI coding harness sets BASH_ENV to a hook that re-execs the
# command through its own guarded shell, and that guard refused ours::
#
#   Build command failed (exit 126): west build --sysbuild (nrf52840-dev)
#   stderr: [BLOCKED — DO NOT RETRY] Command uses eval or $()/ backticks
#           at command position
#
# The whole environment is inherited down the chain: the MCP server takes it
# from the harness, ``background._spawn_daemon`` from the server, and
# ``daemon._run_index_async`` from the daemon.  So a background reindex
# could not build at all, while the same build from a plain shell passed.
#
# fw-context reaches for bash only to source an activate script — it wants
# no shell startup file, from any source.  A build that depends on ambient
# shell state is not reproducible; ``activate``, ``extra_path``, and
# ``extra_env`` are the declared ways to give a build its environment.
#
# Measured scope: ``sh -c`` reads neither BASH_ENV nor ENV when it is not
# interactive, so make recipes and cmake sub-shells were never touched.
# The whole vector is our own ``bash -c``.
_SHELL_STARTUP_VARS: frozenset[str] = frozenset({"BASH_ENV"})


def build_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment for a build subprocess.

    The inherited environment, minus the shell startup hooks named in
    ``_SHELL_STARTUP_VARS``, with *overrides* applied on top.

    Stripping happens BEFORE *overrides*, so a project that sets one of
    those variables deliberately — in ``[build] env`` or ``extra_env`` —
    still gets it.  Configuration is a declared intent; inheritance from
    whoever started fw-context is an accident.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SHELL_STARTUP_VARS}
    if overrides:
        env.update(overrides)
    return env


def run_build_command(
    cmd: list[str],
    cwd: Path,
    timeout: float | None = None,
    description: str = "",
    env: dict[str, str] | None = None,
    build_cfg: BuildConfig | None = None,
) -> subprocess.CompletedProcess:
    """Run a build command with consistent timeout and output capture.

    All build-system invocations should use this helper instead of raw
    ``subprocess.run()`` to ensure consistent timeout and output capture
    behaviour across the ten builders.

    Why wrapper vs raw subprocess:

    - The ten build-system backends each have different argument
      conventions.  Without a single wrapper, every caller duplicates
      the ``cwd``, ``capture_output=True``, ``text=True``,
      ``timeout``, and ``env`` merge logic — a maintenance liability.
    - The ``activate`` block maps a declarative ``build_cfg.activate``
      field (a shell script path) into a ``bash -c "source <script>
      && <cmd>"`` invocation.  Centralising this logic means only one
      call site tests quoting (``shlex.quote``) and shell injection
      resistance — and only one call site has to keep the shell startup
      hooks out of that bash, which :func:`build_env` does.
    - The ``extra_path`` / ``extra_env`` blocks apply per-build-system
      configuration that callers must not need to know about.

    Args:
        cmd: Command and arguments as a list (shell=False is enforced).
        cwd: Working directory.
        timeout: Maximum time in seconds.  ``None`` (default) resolves to
            ``build_cfg.timeout`` when *build_cfg* is given, else 7200 s —
            long first builds (Zephyr, ESP-IDF) must not be killed at
            10 min.
        description: Human-readable description for error messages.
        env: Optional environment variables dict, applied on top of the
            environment that :func:`build_env` gives.
        build_cfg: Optional BuildConfig — when set, ``activate`` wraps the
            command in ``bash -c "source <activate> && <cmd>"``, and
            ``extra_path`` / ``extra_env`` are merged into the environment.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        RuntimeError: On non-zero exit or timeout.
    """
    if timeout is None:
        timeout = build_cfg.timeout if build_cfg is not None else 7200

    merged_env = build_env(env)

    if build_cfg:
        if build_cfg.env:
            merged_env.update(build_cfg.env)
        if build_cfg.extra_path:
            merged_env["PATH"] = os.pathsep.join(build_cfg.extra_path) + os.pathsep + merged_env.get("PATH", "")
        if build_cfg.extra_env:
            merged_env.update(build_cfg.extra_env)
        if build_cfg.activate:
            expanded = str(Path(build_cfg.activate).expanduser())
            cmd_str = " ".join(shlex.quote(c) for c in cmd)
            cmd = ["bash", "-c", f"source {shlex.quote(expanded)} && {cmd_str}"]

    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=merged_env
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Build command timed out after {timeout}s: "
            f"{description or ' '.join(cmd)}"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(
            f"Build command failed (exit {result.returncode}): "
            f"{description or ' '.join(cmd)}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result

def resolve_real_binary(name: str) -> str | None:
    """Resolve *name* to a real binary, skipping version-manager shims.

    ``shutil.which`` returns the first match on PATH, which for tools
    installed through pyenv/asdf is a shim script that re-execs *name*
    by looking it up on PATH again.  When a build prepends a wrapper
    directory to PATH (the ninja ``-d keepdepfile`` wrapper), that
    re-resolution picks up the wrapper and recurses infinitely — each
    cycle appends another flag until the argument list overflows.

    WHY a PATH scan instead of trusting ``shutil.which``: a shim
    re-resolves the program by name, so the only reliable way to get a
    real executable is to walk PATH and skip known shim directories
    (``/shims/`` — pyenv and asdf both use this layout).

    Returns the first non-shim match, or the shim itself as a last
    resort when no real binary exists (preserving the old
    ``shutil.which`` behaviour for callers that only need a path).
    """
    which = shutil.which(name)
    if which is None:
        return None

    _SHIM_MARKERS = ("/shims/",)

    def _is_shim(p: str) -> bool:
        return any(m in p for m in _SHIM_MARKERS)

    if not _is_shim(which):
        return which

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if _is_shim(str(candidate)):
            continue
        return str(candidate)
    return which


class AmbiguousProjectError(ValueError):
    """One project name matches more than one project in the registry.

    The ``name`` column of the global registry has no UNIQUE constraint.
    fw-context does not guess which of the matches the user wants,
    because a wrong guess answers with symbols from other source code —
    and such an answer looks correct.  The error message lists each
    candidate with its ``project_id`` and root path, thus the caller can
    repeat the request with a value that has one match only.
    """


def _lookup_registry_root(selector: str) -> Path | None:
    """Resolve a project name or ``project_id`` to its root directory.

    Reads the global registry at ``~/.fw-context/projects.db``.  The
    ``project_id`` is tried first because it is the primary key and thus
    always has one match; a name can have more than one.

    Args:
        selector: Project name or ``project_id``, as ``list_projects``
            shows them.

    Returns:
        The registered root directory, or ``None`` when the registry
        holds no match for *selector*.  ``None`` is also the result when
        the registry cannot be read — a broken or missing registry must
        not stop path resolution, thus the caller continues with the
        path branch and gets the usual error for an unknown project.

    Raises:
        AmbiguousProjectError: More than one project has this name.
    """
    # Local import: fw_context_mcp.config.global_db imports this module,
    # thus a module-level import here would be circular.
    from fw_context_mcp.config.global_db import get_project_by_id, get_projects_by_name

    try:
        by_id = get_project_by_id(selector)
        matches = [by_id] if by_id is not None else get_projects_by_name(selector)
    except sqlite3.Error as e:
        log.debug("Global registry lookup failed for %r: %s", selector, e)
        return None

    if not matches:
        return None
    if len(matches) > 1:
        candidates = "\n".join(
            f"  {m['name']} ({m['project_id']}) → {m['root_path']}" for m in matches
        )
        raise AmbiguousProjectError(
            f"The project name {selector!r} matches {len(matches)} projects:\n"
            f"{candidates}\n"
            "Use the project_id, or give the root path as project_root."
        )

    root_path = matches[0]["root_path"]
    if not root_path:
        # The registry row predates root_path tracking, or the column was
        # written empty.  There is no path to return, thus the caller
        # continues with the path branch.
        log.debug("Registry entry %r has no root_path", selector)
        return None
    return Path(root_path).resolve()


def resolve_project_root(explicit: str | Path | None = None) -> Path:
    """Return the project root directory.

    Resolution order:

    1. *explicit* when it names an existing directory (resolved absolutely)
    2. *explicit* as a project name or ``project_id`` in the global registry
    3. *explicit* as a path that does not exist yet (resolved absolutely)
    4. Nearest git-root from ``$PWD``
    5. ``$PWD`` when no git repository is found

    WHY an existing directory wins over a registry name: a path is what
    the caller can see on disk, and step 3 keeps the behaviour that
    ``fw-context init`` needs — it must accept a directory that holds no
    project yet.  Steps 1 and 3 together give the same result as before
    for every path, thus the registry branch is added and changes
    nothing that worked before.

    Args:
        explicit: Root directory, project name, or ``project_id``.
            ``None`` starts the search from ``$PWD``.

    Returns:
        The resolved project root.  The directory is not guaranteed to
        exist — the caller reports a missing or uninitialized project.

    Raises:
        AmbiguousProjectError: *explicit* is a project name with more
            than one match in the registry.
    """
    if explicit:
        as_path = Path(explicit).resolve()
        if as_path.is_dir():
            return as_path
        from_registry = _lookup_registry_root(str(explicit))
        return from_registry if from_registry is not None else as_path
    cwd = Path(os.getcwd())
    p = cwd
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return cwd


def abs_path(root: Path, path: str) -> str:
    """Resolve a stored (potentially relative) file path to an absolute path.

    Absolute paths are returned unchanged; relative paths are joined with
    *root*.  An empty string is returned as-is (callers should guard
    against empty paths before calling).
    """
    if not path:
        return ""  # Empty path — caller should guard before calling
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(root / p)


def cc_output_path(project_root: Path) -> Path:
    """Return the default compile_commands.json output path for a project.

    Ensures the parent directory exists.  Every builder writes the generated
    compilation database here so fw-context owns one stable, gitignored
    location regardless of where the native build system produces its file.
    """
    target = project_root / CC_OUTPUT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def resolve_build_dir(project_root: Path, cfg, default: str) -> Path:
    """Return the directory that the build must write to.

    ``cfg.isolated_build_dir`` wins when it is set.  fw-context sets it for a
    build that it starts on its own, so that the artifacts stay apart from
    the ones of a build that the user runs in an IDE at the same time —
    fw-context cannot lock that build, thus separation is the only defence.

    A relative value resolves against *project_root*.  Without the setting
    the backend keeps *default*, its usual directory.
    """
    isolated = getattr(cfg, "isolated_build_dir", None)
    if isolated:
        candidate = Path(isolated)
        return candidate if candidate.is_absolute() else project_root / candidate
    return project_root / default


# Content LRU cache — mtime-keyed to auto-invalidate on file changes.
#
# Why an LRU cache instead of functools.lru_cache:
# - functools.lru_cache keys by argument equality; we want equality on
#   (path, mtime) and eviction by access order (LRU), not by recency
#   of adding new items.
# - A daemon process reads the same hot files (project config, linker
#   scripts, common headers) hundreds of times per session; disk I/O
#   for those reads is pure waste.
# - The mtime key means a file edited between reads is never stale —
#   the old cache entry has a different key than the new mtime.
#
# 500 entries ≈ 500 source files in memory, each ~20 KB average →
# ~10 MB memory footprint — acceptable for a long-running server.
_read_cache: OrderedDict[tuple[str, float], list[str]] = OrderedDict()
_read_cache_max = 500
_read_cache_lock = threading.Lock()


def read_file_lines(abs_path: str) -> list[str] | None:
    """Read all lines from a source file, detecting the encoding.

    Tries a sequence of common encodings for embedded C/C++ projects
    (UTF-8 first, then regional code pages, then repair mode as last
    resort).  Returns ``None`` when the file cannot be read at all.

    Results are cached by (path, mtime) — file changes invalidate the cache.
    """
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return None
    cache_key = (abs_path, mtime)
    with _read_cache_lock:
        if cache_key in _read_cache:
            _read_cache.move_to_end(cache_key)
            return _read_cache[cache_key]

    # Encoding fallback chain — ordered by coverage in embedded C/C++ projects:
    #
    # 1. UTF-8 — universal, handles 99%+ of source files.
    # 2. cp1252 (Windows-1252) — common in legacy Windows-authored firmware;
    #    characters like 0x80-0x9F decode cleanly (unlike latin1 where they
    #    map to control characters that produce valid-but-garbled output).
    # 3. latin1 — ISO-8859-1 never raises UnicodeDecodeError (every byte
    #    maps to a valid character), so any file that survives this far
    #    is readable; the content may be garbled but the caller gets
    #    line-oriented access without crashing.
    #
    # NOTE: UTF-16 files will produce garbled content; extremely low risk
    # in embedded C/C++ projects.
    encodings = ["utf-8", "cp1252", "latin1"]
    for encoding in encodings:
        try:
            with open(abs_path, encoding=encoding) as f:
                result = f.readlines()
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except (FileNotFoundError, OSError):
            return None
    else:
        # Last resort — replace invalid bytes
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                result = f.readlines()
        except (FileNotFoundError, OSError):
            return None

    # Evict oldest entry if at capacity (LRU via OrderedDict — move_to_end on hit)
    with _read_cache_lock:
        if len(_read_cache) >= _read_cache_max:
            _read_cache.popitem(last=False)
        _read_cache[cache_key] = result
    return result


def compute_content_hash(body: str, qualified_name: str, signature: str, docstring: str) -> str:
    """Stable SHA256 hex fingerprint of a symbol's content.

    Used for content-addressable LLM analysis caching — survives
    re-indexes, config changes, and branch switches.

    Why include all four fields and why the Unit Separator delimiter:

    - A hash over just ``body`` would fingerprint ``void foo() { }``
      and ``void bar() { }`` to the same value when both have empty
      bodies — cache collisions across symbols.
    - Including ``qualified_name`` and ``signature`` disambiguates
      identically-bodied functions (stubs, thin wrappers).
    - Including ``docstring`` makes the cache invalidate when
      documentation changes — LLM analysis output changes when its
      input docstring changes, even when the code is identical.
    - ``\\x1f`` (ASCII Unit Separator, 0x1F) is a non-printable
      control character that cannot appear in C/C++ identifiers,
      signatures, or docstrings — no escaping needed, no collision
      risk from literal delimiter characters in content.
    """
    raw = f"{body.strip()}\x1f{qualified_name}\x1f{signature or ''}\x1f{(docstring or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def compute_source_hash(file_path: Path) -> str:
    """Return SHA-256 hex digest of file content, or empty string on error."""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards ``%``, ``_``, and the escape char ``\\``.

    Callers must append ``ESCAPE '\\'`` to the LIKE clause when using the
    escaped string.  The double-backslash in the replacement string
    produces a single backslash followed by the wildcard character in the
    SQL value.

    Why manual escaping instead of query parameters: ``LIKE`` patterns
    with ``?`` placeholders require the caller to decide at call time
    whether a value should be escaped — AND gate automation that
    text-searches user-supplied file paths are always LIKE patterns
    where unescaped ``%`` would match arbitrary substrings and
    unescaped ``_`` would match any single character.
    """
    # Order matters: escape backslash FIRST so the escaping character
    # itself is escaped before it appears in later replacements.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_compile_commands_stale(
    created_at: str,
    compile_commands_path: str | Path,
    tolerance_s: float = MTIME_TOLERANCE_S,
) -> tuple[bool, str | None]:
    """Check if compile_commands.json is newer than *created_at* timestamp.

    Returns ``(is_stale, reason)`` where *reason* is ``None`` when the
    compile_commands file is present and up to date, or a string like
    ``"compile_commands_missing"`` or ``"compile_commands_newer"`` when
    the index should be considered stale.

    A missing ``compile_commands.json`` returns ``(True,
    "compile_commands_missing")`` — the index IS stale when its source
    of truth has disappeared.
    """
    try:
        cc_path = Path(compile_commands_path)
        if not cc_path.exists():
            return True, "compile_commands_missing"
        cc_mtime = os.path.getmtime(cc_path)
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        indexed_at = dt
        if cc_mtime > indexed_at.timestamp() + tolerance_s:
            return True, "compile_commands_newer"
        return False, None
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        log.warning("is_compile_commands_stale failed for %s: %s", compile_commands_path, e)
        return True, f"stale_check_error: {e}"


def truncate_path_middle(path: str, max_len: int) -> str:
    """Truncate a path by removing the middle, keeping start and end visible.

    Example: ``/home/user/dev/…/projects/boiler-fw``
    """
    if len(path) <= max_len:
        return path
    keep_start = max(max_len // 3, 12)
    keep_end = max_len - keep_start - 1
    return path[:keep_start] + "…" + path[-keep_end:]


def fmt_count(n: int) -> str:
    """Format a count with thousand separators (non-breaking thin spaces)."""
    if n < 0:
        return "-" + f"{abs(n):_d}".replace("_", " ")
    return f"{n:_d}".replace("_", " ")
