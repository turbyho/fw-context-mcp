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

import errno
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, AnyStr

if TYPE_CHECKING:
    from fw_context_mcp.indexer.build import BuildConfig

from fw_context_mcp import _stdlib_sqlite3
from fw_context_mcp.path_key import is_absolute_key
from fw_context_mcp.processes import MAX_PID, pid_exists

log = logging.getLogger(__name__)

__all__ = [
    "CC_STAGING_GLOB",
    "CPP_EXTENSIONS",
    "CPP_HEADER_EXTENSIONS",
    "CPP_SOURCE_EXTENSIONS",
    "C_SOURCE_EXTENSIONS",
    "FW_CONTEXT_REL",
    "HEADER_EXTENSIONS",
    "MTIME_TOLERANCE_S",
    "SAFE_EXCEPT",
    "TU_EXTENSIONS",
    "BuildTimeoutError",
    "abs_path",
    "activate_script",
    "build_dir_patterns_with_fw_context",
    "build_env",
    "build_tool_available",
    "cc_output_path",
    "cc_staging_path",
    "clear_dead_temporaries",
    "command_program",
    "command_words",
    "compute_content_hash",
    "compute_source_hash",
    "decode_source_lines",
    "env_value",
    "fmt_count",
    "format_number_ranges",
    "is_compile_commands_stale",
    "is_db_exception",
    "is_fatal",
    "is_program",
    "macro_signature",
    "missing_tool_message",
    "owner_is_dead",
    "owner_token",
    "path_directories",
    "process_group_is_gone",
    "process_start_time",
    "program_file_names",
    "read_file_lines",
    "record_build_groups",
    "resolve_project_root",
    "run_in_process_group",
    "sqlite_file_uri",
    "staging_owner",
    "stop_process_group",
    "temporary_owner",
    "truncate_path_middle",
    "windows_words",
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

# The file name of a compilation database, and the parts that the names of
# its staging files are made of.
_CC_STEM = "compile_commands"
_CC_SUFFIX = ".json"

# Glob that finds every staging file of :func:`cc_staging_path`.  A build that
# a signal stopped leaves its staging file behind, thus the next build in the
# same directory removes what it finds.
CC_STAGING_GLOB: str = f".{_CC_STEM}.*{_CC_SUFFIX}"


def build_dir_patterns_with_fw_context(patterns: list[str]) -> list[str]:
    """Add the fw-context directory to the build-output patterns of a backend.

    Everything fw-context writes into a project sits under FW_CONTEXT_REL,
    and each build that fw-context runs puts its output there, the generated
    configuration headers included — mbed_config.h, sdkconfig.h, autoconf.h.
    The patterns of the backend do not reach them.  Measured against the
    real manifests: mbed-os gives ``BUILD/``, platformio ``.pio/build/``,
    zephyr ``build/nrf52840_sysbuild/``, and none of the three matches
    ``.fw-context/build/<variant>/out/``.

    A header the pipeline does not know is generated is re-hashed on every
    index run, and a generated header changes with every build without a
    change of meaning — so every unit that includes it is marked
    header-stale and re-parsed, which is the opposite of what the three
    staleness tiers are for.
    """
    return [*patterns, f"{FW_CONTEXT_REL}/"]


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


class StopRequested(Exception):
    """A signal asked the process to stop: the handler of the index lock raises it (``IndexStopped``).

    The handler of SIGTERM and SIGHUP raises it inside the code that runs,
    and then ignores the two signals.  A broad handler that wraps it (into
    ``OllamaError``, for example) thus leaves a process that no signal but
    SIGKILL stops: measured on the streaming request of the LLM analysis.
    :func:`is_fatal` thus names it, as it names ``KeyboardInterrupt``.
    """


def is_fatal(exc: BaseException) -> bool:
    """Return True for exceptions that must never be swallowed."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError, SystemError, StopRequested))


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


def sqlite_file_uri(path: Path, mode: str) -> str:
    """Return the SQLite URI that opens the database file *path* with ``?mode=<mode>`` (``ro``, ``rw``, ``rwc``).

    *path* becomes absolute; links stay as the caller gives them.  WHY
    a URI: ``mode=ro`` exists only there.  WHY this function and not
    ``Path.as_uri()`` alone: ``as_uri`` gives a Windows UNC path
    (``\\\\server\\share\\x.db``) the authority of the server
    (``file://server/share/x.db``), and SQLite takes only an empty
    authority or ``localhost`` ("invalid uri authority", measured on
    Windows 11 with SQLite 3.54).  ``resolve()`` turns a mapped network
    drive into such a path (``H:\\`` gave ``\\\\VBoxSvr\\Documents\\``),
    thus an index on a network drive could not be opened read-only.
    """
    absolute = path.absolute()
    uri = _unc_file_uri(absolute) if absolute.drive.startswith("\\\\") else absolute.as_uri()
    return f"{uri}?mode={mode}"


def _unc_file_uri(path: PurePath) -> str:
    """Return the ``file:`` URI of the UNC path *path* in the form that SQLite takes.

    An empty authority and the server in the path:
    ``file:////server/share/x.db`` (measured on Windows 11).  The same form
    opens a long path (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\server\\...``) and a
    device path (``\\\\.\\C:\\...``), whose drive also starts with two
    backslashes, and for which ``as_uri`` of Python 3.11 gave the authority
    ``%3F`` or ``.`` (measured: each one opens).  ``quote``
    percent-encodes a space, ``#``, ``?``, ``%`` and each character above
    127, as ``as_uri`` does for each other path.
    """
    return f"file://{urllib.parse.quote(path.as_posix())}"


def build_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment for a build subprocess.

    The inherited environment, minus the shell startup hooks named in
    ``_SHELL_STARTUP_VARS``, with *overrides* applied on top.

    Stripping happens BEFORE *overrides*, so a project that sets one of
    those variables deliberately — in ``[build] env`` or ``extra_env`` —
    still gets it.  Configuration is a declared intent; inheritance from
    whoever started fw-context is an accident.

    On Windows a Python tool of the build (pio, west, idf.py, a script that
    CMake runs) writes its stdout and stderr in UTF-8: ``PYTHONIOENCODING``
    is ``utf-8`` unless the environment already sets it (decision of the
    user, 2026-11-10).  WHY: to a pipe Python writes the ANSI code page
    there, and a character that the code page does not hold stopped the
    tool with ``UnicodeEncodeError`` (measured on Windows 11: no output at
    all).  The output of a build is read as UTF-8 (``run_in_process_group``).
    Linux and macOS write UTF-8 already, thus nothing changes there.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SHELL_STARTUP_VARS}
    if sys.platform == "win32":
        env.setdefault("PYTHONIOENCODING", "utf-8")
    if overrides:
        env.update(overrides)
    return env


#: How much of each captured stream a failed build quotes.
#:
#: A build log runs to megabytes and the reason for the failure sits at
#: its END, thus the tail is the part that answers "why".  The value is
#: large enough to hold the error block of a compiler — measured on an
#: Mbed OS build, whose three ``error:`` lines with their source excerpts
#: and ``note:`` lines came to about 1200 characters.
_BUILD_OUTPUT_TAIL = 2000


def _as_text(raw: str | bytes | None) -> str | None:
    """Return a captured stream as text.

    ``subprocess.run(text=True)`` gives ``str``, but the ``stdout`` and
    ``stderr`` that ``TimeoutExpired`` carries can still be ``bytes`` —
    the exception holds what the pipes had read when the clock ran out,
    and that path does not always decode.  Undecodable bytes are
    replaced, because an error message that raises its own error tells
    the reader nothing.

    Args:
        raw: The captured stream, or ``None``.

    Returns:
        The stream as text, or ``None`` when there was none.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _quote_stream(name: str, text: str | None, limit: int = _BUILD_OUTPUT_TAIL) -> str:
    """Return the tail of one captured stream, for an error message.

    Args:
        name: Name of the stream, ``"stdout"`` or ``"stderr"``.
        text: The whole captured stream, or ``None`` when the run gave none.
        limit: How many characters of the end to keep.

    Returns:
        A block that starts with *name* on its own line, or an empty
        string when the stream holds nothing.  A stream that was cut says
        how much went missing, so that nobody reads the first line of the
        block as the first line of the output.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return f"\n{name}:\n{text}"
    return f"\n{name} (last {limit} of {len(text)} characters):\n{text[-limit:]}"


# ── Builds in their own process group ──
# A build tool starts compilers of its own: `make -j` a compiler for each
# job, `bear` the build it records, `pio` scons.  subprocess.run stops only
# the direct child, thus a run that another run took over left its
# compilers to write object files into the directory that the new build
# cleaned and filled.  Each build therefore starts a new session, and a stop
# goes to the whole process group.

# How long a build group gets to stop after SIGTERM before SIGKILL.
BUILD_GROUP_GRACE_S = 5.0

# The file that the build of this run records its process group in, or None.
# A ContextVar and not a parameter: run_build_command has ten backends as
# callers, and none of them knows the index directory.  The index run sets
# it (see record_build_groups) for the time that it owns the index.
_BUILD_GROUP_RECORD: ContextVar[Path | None] = ContextVar("_BUILD_GROUP_RECORD", default=None)


@contextmanager
def record_build_groups(record: Path) -> Iterator[None]:
    """Record the process group of each build in this block in *record*.

    WHY: a run that SIGKILL stops cannot stop its build.  The record lets
    the next run that owns the index find the group and stop it (see
    ``cli/_index._reap_orphan_build_group``).
    """
    token = _BUILD_GROUP_RECORD.set(record)
    try:
        yield
    finally:
        _BUILD_GROUP_RECORD.reset(token)


def process_start_time(pid: int) -> str | None:
    """Return a value that names the start of process *pid*, or None when unknown.

    WHY: a PID can come back for an unrelated process, and the program name
    alone does not tell the two apart — a build under ``activate`` has
    ``bash`` as its leader, and so has the shell in a new terminal.  The
    start time of a process does not change while it lives, and a new
    process with the same PID has a different one.

    Linux: field 22 of ``/proc/<pid>/stat``, the start in clock ticks after
    the boot.  The name in field 2 can hold spaces and ")", thus the fields
    are counted after the LAST ")".  Elsewhere (macOS): ``ps -o lstart=``.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if Path("/proc/self/stat").exists():
            return None  # /proc works, thus the process is gone
    except OSError:
        return None
    else:
        fields = stat.rpartition(")")[2].split()
        # fields[0] is field 3 (the state), thus field 22 is fields[19].
        return fields[19] if len(fields) > 19 else None
    # A fixed locale and time zone: `lstart` is local time in the format of
    # the locale, and the writer (a daemon run) and the reader (a run in a
    # terminal) can have different ones.  The two strings must be equal.
    env = {"LC_ALL": "C", "TZ": "UTC0", "PATH": os.environ.get("PATH", "/bin:/usr/bin")}
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _write_group_record(record: Path, pgid: int, program: str) -> None:
    """Write ``{"index_pid", "pgid", "leader", "started"}`` to *record* atomically.

    ``leader`` is the base name of the program that leads the group, and
    ``started`` its start time (see :func:`process_start_time`).  The reader
    compares the two with the live leader, because a PID can come back for
    an unrelated process.
    """
    temporary = record.with_name(f".{record.name}.{os.getpid()}")
    payload = {
        "index_pid": os.getpid(),
        "pgid": pgid,
        "leader": Path(program).name,
        "started": process_start_time(pgid),
    }
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, record)
    except OSError as exc:
        # The record helps only after a SIGKILL.  A build must not fail for it.
        log.warning("Cannot record the build process group in %s: %s", record, exc)
        temporary.unlink(missing_ok=True)


def _remove_group_record(record: Path, pgid: int) -> None:
    """Remove *record* when it still names the group *pgid*."""
    try:
        if json.loads(record.read_text(encoding="utf-8")).get("pgid") == pgid:
            record.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        return


def stop_process_group(
    pgid: int,
    grace: float = BUILD_GROUP_GRACE_S,
    leader: subprocess.Popen | None = None,
) -> None:
    """Send SIGTERM to the group *pgid*, and SIGKILL to what remains after *grace*.

    The group can live on after its leader: the leader of a ``make -j``
    exits and its compilers still run.  Thus the wait is for the whole
    group, with ``killpg(pgid, 0)``, and not for the leader only.

    *leader* is the ``Popen`` of the leader when this process started it.
    Until this process reaps it, a leader that exited is a zombie, and a
    zombie still counts as a member of the group.  The wait thus reaps it,
    or it would always last the whole *grace*.

    The SIGKILL is in ``finally``: a second Ctrl-C during the wait raises
    ``KeyboardInterrupt`` out of ``time.sleep``, and a build that ignores
    SIGTERM then ran on.

    POSIX only.  On Windows a build runs in a job object, and the job stops
    it (``job_object.py``).
    """
    if sys.platform == "win32":
        raise OSError("a process group is POSIX; on Windows the job object of the build stops it")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if process_group_is_gone(pgid, leader):
                return
            time.sleep(0.05)
    finally:
        # PermissionError: macOS refuses a signal to a group that holds only
        # zombies (EPERM, where Linux gives success).  Nothing runs there, and
        # the parent of each zombie reaps it.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def process_group_is_gone(pgid: int, leader: subprocess.Popen | None = None) -> bool:
    """Return True when no process of the group *pgid* runs.

    *leader* as in :func:`stop_process_group`: it is reaped first, because
    a zombie leader still counts as a member.  POSIX only, as
    :func:`stop_process_group`.
    """
    if sys.platform == "win32":
        raise OSError("a process group is POSIX; on Windows the job object of the build holds the build")
    if leader is not None:
        leader.poll()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


#: The extensions of ``cmd.exe`` when the environment has no ``PATHEXT``.
_CMD_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def env_value(env: Mapping[str, str], name: str) -> str | None:
    """The value of the variable *name* in *env*.

    Windows: the names are case-insensitive, and a copy of ``os.environ``
    can hold ``Path``.  POSIX: the name as it is.
    """
    if sys.platform != "win32":
        return env.get(name)
    upper = name.upper()
    return next((value for key, value in env.items() if key.upper() == upper), None)


def path_directories(env: Mapping[str, str] | None = None) -> list[Path]:
    """The directories of the ``PATH`` of *env* (of this process when None), in their order.

    An empty entry is left out.  Windows allows an entry in double quotes
    (``"C:\\Program Files\\x"``) and with spaces around it, thus those are
    removed there.  On POSIX a space is a part of the name of a directory,
    as ``execvp`` reads it.
    """
    path = env_value(os.environ if env is None else env, "PATH") or ""
    directories = []
    for entry in path.split(os.pathsep):
        if sys.platform == "win32":
            entry = entry.strip().strip('"')
        if entry:
            directories.append(Path(entry))
    return directories


def is_program(path: Path) -> bool:
    """Say if *path* is a file that the system can run as a program.

    POSIX: a file with the execute bit for this user, as ``shutil.which``
    and ``execvp`` require.  Windows: a file; the extension (``PATHEXT``)
    makes it a program, and ``program_file_names`` gives the names.
    """
    if not path.is_file():
        return False
    return sys.platform == "win32" or os.access(path, os.X_OK)


def program_file_names(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    """The file names that the program *name* can have on this system, in the order of the lookup.

    POSIX: the name itself.  Windows: no program has a name without an
    extension there.  A name with an extension of ``PATHEXT`` stays as it
    is; else each extension of ``PATHEXT`` of *env* (of this process when
    None) is added, as ``cmd.exe`` finds a program.  Without ``PATHEXT``,
    the extensions of ``cmd.exe`` apply (``_CMD_DEFAULT_PATHEXT``).
    """
    if sys.platform != "win32":
        return [name]
    pathext = env_value(os.environ if env is None else env, "PATHEXT") or _CMD_DEFAULT_PATHEXT
    extensions = [ext.lower() for ext in pathext.split(os.pathsep) if ext]
    if Path(name).suffix.lower() in extensions:
        return [name]
    return [name + ext for ext in extensions]


def run_in_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float | None,
    capture_output: bool,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> subprocess.CompletedProcess:
    """Run *cmd* in a new session, and stop its whole process group on any exit but its own.

    Same result as ``subprocess.run(..., encoding=encoding, errors=errors)``
    for stdout: a timeout raises ``TimeoutExpired`` with the output so far.  Any exception while the
    command runs (the timeout, ``IndexStopped`` from a SIGTERM handler,
    ``KeyboardInterrupt``) first stops the group, then goes on.

    A new session also takes the build out of the process group of the
    terminal.  Ctrl-C thus reaches this process only, as a
    ``KeyboardInterrupt``, and this function stops the build.  The build has
    no controlling terminal: output to the terminal still works, but an open
    of ``/dev/tty`` fails, thus a password prompt (``sudo``, ``ssh``) or a
    curses screen (``menuconfig``) in a ``pre_build`` hook does not work.
    No build that fw-context starts needs one.

    No stdin either (``DEVNULL``): in an MCP server the stdin of this
    process is the JSON-RPC pipe of the client, and a build that read it
    took the messages of the client.

    The record goes only when the group is gone.  An exception during the
    stop (a second Ctrl-C) can leave members alive, and the next index run
    then finds them through the record.

    Windows has no process group that a stop can reach: there the build
    runs in a job object (:func:`_run_in_job`).

    The output is read as bytes and decoded after the end of the command,
    with *encoding* and *errors*.  The default is the output of a build
    for a person: UTF-8, and a byte that is no UTF-8 becomes U+FFFD.  stderr
    is always decoded with replacement: it holds messages for a person,
    also from a command whose stdout a caller parses.  WHY
    UTF-8: the tools of a build write it on Linux and macOS, and on
    Windows clang, GCC 13 and later, and each Python tool
    (``PYTHONIOENCODING`` of :func:`build_env`) write it too.  An older GCC
    writes the ANSI code page; its characters above 127 become U+FFFD in a
    message, and the build is not affected.  A caller that parses the
    output gives ``errors="strict"``, and a byte of stdout that is no UTF-8
    then raises ``UnicodeDecodeError``.  WHY not the decode of ``text=True``: it
    used the locale (the ANSI code page of Windows), and it decoded in the
    reader threads of ``communicate`` on Windows, where an error left an
    ``IndexError`` and no output.  The output of a timeout is always
    decoded with replacement: it can end in the middle of a character.
    """
    if sys.platform == "win32":
        raw = _run_in_job(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=capture_output)
        return _decoded_result(raw, encoding, errors)
    pipe = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=pipe, stderr=pipe,
        start_new_session=True,
    )
    record = _BUILD_GROUP_RECORD.get()
    try:
        if record is not None:
            _write_group_record(record, proc.pid, cmd[0])
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process_group(proc.pid, leader=proc)
        stdout, stderr = proc.communicate()
        raise _decoded_timeout(subprocess.TimeoutExpired(cmd, timeout or 0, output=stdout, stderr=stderr)) from None
    except BaseException:
        stop_process_group(proc.pid, leader=proc)
        proc.wait()
        raise
    finally:
        if record is not None and process_group_is_gone(proc.pid, proc):
            _remove_group_record(record, proc.pid)
    return _decoded_result(subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr), encoding, errors)


def _decoded_output(data: bytes | None, encoding: str, errors: str) -> str | None:
    """Return the captured *data* as ``subprocess`` text mode gives it: decoded, with universal newlines.

    WHY universal newlines: ``text=True`` turned ``\\r\\n`` into ``\\n``,
    and the callers read lines with that end.  A program of Windows ends
    each line with ``\\r\\n``.
    """
    if data is None:
        return None
    return io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors=errors).read()


def _decoded_result(raw: subprocess.CompletedProcess, encoding: str, errors: str) -> subprocess.CompletedProcess:
    """Return *raw* with its captured bytes decoded: stdout with *errors*, stderr with replacement.

    See :func:`run_in_process_group`.
    """
    return subprocess.CompletedProcess(
        raw.args, raw.returncode,
        _decoded_output(raw.stdout, encoding, errors), _decoded_output(raw.stderr, encoding, "replace"),
    )


def _decoded_timeout(raw: subprocess.TimeoutExpired) -> subprocess.TimeoutExpired:
    """Return *raw* with the output so far decoded as UTF-8 with replacement (see :func:`run_in_process_group`)."""
    return subprocess.TimeoutExpired(
        raw.cmd, raw.timeout,
        output=_decoded_output(raw.output, "utf-8", "replace"),
        stderr=_decoded_output(raw.stderr, "utf-8", "replace"),
    )


if sys.platform == "win32":

    #: The longest wait of :func:`_communicate_in_slices` before Python code runs again.
    _WAIT_SLICE_S = 0.1

    def _communicate_in_slices(
        proc: subprocess.Popen[AnyStr], timeout: float | None,
    ) -> tuple[AnyStr | None, AnyStr | None]:
        """``proc.communicate(timeout=timeout)``, in waits of ``_WAIT_SLICE_S``.

        WHY: on Windows ``communicate`` waits in ``WaitForSingleObject``, and
        a Ctrl-C or a stop request (``processes.request_stop`` →
        ``interrupt_main``) runs its handler only when Python code runs
        again.  Measured on Windows 11 (2026-11-09): a stop request during a
        build of 60 s came after the 60 s.  A Ctrl-C during a build thus did
        nothing until the build ended.  A wait in slices lets the handler
        run within ``_WAIT_SLICE_S``.  ``communicate`` loses no output when
        it is called again after ``TimeoutExpired`` (Python documentation).
        The cost is 10 calls each second, which is nothing beside a build.
        WHY not a wait for the stop event of ``processes``: a Ctrl-C of the
        console sets no such event.  POSIX needs no slices: a signal
        interrupts the wait there.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout or 0)
            wait = _WAIT_SLICE_S if remaining is None else min(_WAIT_SLICE_S, remaining)
            try:
                return proc.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                continue

    def _find_program(name: str, env: dict[str, str]) -> str | None:
        """The full path of the program *name* in the ``PATH`` of *env*, with the extensions of its ``PATHEXT``.

        WHY not ``shutil.which``: on Python 3.11 it looks in the current
        directory of THIS process first (the directory of the MCP server),
        and it takes ``PATHEXT`` from this process.  The build gets the
        ``PATH`` and the ``PATHEXT`` of its environment, as POSIX
        ``execvpe`` takes the ``PATH`` of the environment and no current
        directory.
        """
        names = program_file_names(name, env)
        for directory in path_directories(env):
            for candidate in names:
                path = directory / candidate
                if is_program(path):
                    return str(path)
        return None

    class ActivateScriptError(RuntimeError):
        """The activate script cannot run on Windows: its kind or its path (a fault of the config)."""

    #: The characters that ``cmd.exe`` changes in a quoted path: ``%NAME%``
    #: expands, and ``call`` doubles each ``^``.
    _CMD_PATH_UNSAFE = ('"', "%", "^")

    def _cmd_safe_path(path: str, what: str) -> str:
        """*path* for a quoted place in a command line of ``cmd.exe``; ``ActivateScriptError`` when cmd.exe would change it."""
        unsafe = [char for char in _CMD_PATH_UNSAFE if char in path]
        if unsafe:
            raise ActivateScriptError(f"{what} {path}: cmd.exe cannot run a path that holds {' '.join(unsafe)}")
        return path

    def _batch_argument(argument: str) -> str:
        """*argument* quoted for a batch file that ``cmd.exe /c`` runs.

        The rules of the repair of CVE-2024-24576 (BatBadBut) in the Rust
        standard library: ``cmd.exe`` parses the command line of a batch
        file again, thus ``&``, ``|``, ``<`` or ``>`` in an argument that
        ``subprocess.list2cmdline`` makes start a second command.  Each
        argument goes in double quotes, where those characters are literal;
        a ``"`` is ``""``; a ``%`` is ``%%cd:~,%``, which ``cmd.exe`` gives
        back as ``%`` and does not expand; backslashes before the closing
        quote are doubled.  A line break cannot go through ``cmd.exe``.
        """
        if any(char in argument for char in "\r\n\0"):
            raise ValueError(f"an argument of a batch file cannot hold a line break: {argument!r}")
        quoted = argument.replace('"', '""').replace("%", "%%cd:~,%")
        trailing = len(quoted) - len(quoted.rstrip("\\"))
        return f'"{quoted}{chr(92) * trailing}"'

    def _windows_command(cmd: list[str], env: dict[str, str]) -> list[str] | str:
        """The program of *cmd* as a full path, and the command line of ``cmd.exe`` for a batch file.

        - A bare name: ``_find_program`` in the environment of the build.
          WHY: ``CreateProcess`` looks in the ``PATH`` of this process, not
          of the build, and it adds only ``.exe``: a tool of ``extra_path``
          or of an activate script did not start.  A name that the ``PATH``
          does not hold raises ``FileNotFoundError``, as ``Popen`` on POSIX.
        - A ``.bat`` or ``.cmd`` program (a shim of pyenv-win, a script of a
          SDK): ``cmd.exe`` runs it, with the arguments of
          ``_batch_argument``.  ``Popen`` with a list quotes them with
          ``list2cmdline``, and ``cmd.exe`` then ran the text after a ``&``
          of an argument, for example of a project path ``C:\\R&D\\fw``.
        """
        program = cmd[0]
        if not os.path.dirname(program):
            found = _find_program(program, env)
            if found is None:
                raise FileNotFoundError(errno.ENOENT, f"{program} is not on the PATH of the build", program)
            program = found
        if Path(program).suffix.lower() not in (".bat", ".cmd"):
            return [program, *cmd[1:]]
        batch = _cmd_safe_path(program, "build program")
        arguments = " ".join(_batch_argument(argument) for argument in cmd[1:])
        return f'"{_system_program("cmd.exe")}" /d /e:on /v:off /s /c ""{batch}" {arguments}"'

    def _system_program(*parts: str) -> str:
        """The full path of a program of Windows in ``%SystemRoot%\\System32``.

        WHY not the bare name: ``CreateProcess`` looks for a bare name in
        the PATH of this process, and the PATH of an operator can name a
        different ``cmd.exe``.
        """
        return str(Path(os.environ["SystemRoot"], "System32", *parts))

    #: The variable that holds the ID of one activation (``_activated_environment``).
    _ACTIVATION_ID = "FWCTX_ACTIVATION_ID"

    #: Python code that writes its environment as JSON on stdout.  ASCII only
    #: (``ensure_ascii``), thus no code page of a console or of PowerShell
    #: changes a value on its way.  No ``"``, ``'``, ``%`` or ``^``: it goes
    #: in quotes into the command lines of ``_activation_command_line``.
    _DUMP_ENVIRONMENT = "import json,os,sys;sys.stdout.write(json.dumps(dict(os.environ)))"

    def _activation_command_line(script: Path) -> str:
        """The command line that runs the activate *script*, then writes the environment that it leaves.

        The script writes its own output to stderr, and Python
        (``_DUMP_ENVIRONMENT``) writes the environment to stdout:

        - ``.bat``, ``.cmd``: ``cmd.exe`` runs the script with ``call`` and
          ``1>&2``.  ``/s`` removes only the outer quotes of the command,
          ``/v:off`` keeps ``!`` literal.
        - ``.ps1``: PowerShell dot-sources the script, with each of its
          streams to stderr.  ``-EncodedCommand`` carries the command, thus
          no quote of the path reaches a parser of the command line.

        Raises ``ActivateScriptError`` for another kind of script, and for a
        ``.bat`` path that ``cmd.exe`` changes (``_cmd_safe_path``).
        """
        import base64

        python = sys.executable
        suffix = script.suffix.lower()
        if suffix in (".bat", ".cmd"):
            batch = _cmd_safe_path(str(script), "activate script")
            interpreter = _cmd_safe_path(python, "the Python of fw-context")
            line = f'call "{batch}" 1>&2 && "{interpreter}" -c "{_DUMP_ENVIRONMENT}"'
            return f'"{_system_program("cmd.exe")}" /d /e:on /v:off /s /c "{line}"'
        if suffix == ".ps1":
            def literal(text: str) -> str:
                return "'" + text.replace("'", "''") + "'"

            command = (
                "$ErrorActionPreference = 'Stop'; "
                f". {literal(str(script))} *>&1 | ForEach-Object {{ [Console]::Error.WriteLine([string]$_) }}; "
                f"& {literal(python)} -c {literal(_DUMP_ENVIRONMENT)}; "
                "exit $LASTEXITCODE"
            )
            encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            powershell = _system_program("WindowsPowerShell", "v1.0", "powershell.exe")
            return f'"{powershell}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}'
        raise ActivateScriptError(
            f"activate script {script}: on Windows fw-context runs a .bat, .cmd or .ps1 script "
            "([build] activate)",
        )

    def _parse_environment(script: Path, stdout: bytes) -> dict[str, str]:
        """Read the environment that ``_DUMP_ENVIRONMENT`` wrote after *script*."""
        try:
            values = json.loads(stdout.decode("ascii"))
        except ValueError as exc:
            raise RuntimeError(f"activate script {script}: cannot read its environment: {exc}") from exc
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise RuntimeError(f"activate script {script}: its environment is not a map of strings")
        return values

    def _activated_environment(
        script: Path, *, cwd: Path, env: dict[str, str], timeout: float | None,
    ) -> dict[str, str]:
        """Run the activate *script* with *env*, and return the environment that it leaves.

        WHY not the command after the script in one shell, as ``source``
        on POSIX: each argument of the command would need the quoting of
        ``cmd.exe``, and a wrong quote lets ``cmd.exe`` run a part of an
        argument.  The build command then runs with the environment, and
        without a shell (``_windows_command``).

        The script runs in a job object, as a build: a timeout or a stop
        ends each process that it started.  ``ActivateScriptError`` for a
        script that cannot run on Windows, ``RuntimeError`` for a script
        that fails (with the end of its output, in the OEM code page of
        ``cmd.exe``), ``BuildTimeoutError`` for a timeout.
        """
        from .job_object import BuildJob

        command_line = _activation_command_line(script)
        # A new value for each activation: a script can name a temporary
        # file with it, and two runs at the same time must not share one
        # (``ncs-env.cmd`` of the Zephyr builder).  It is no part of the
        # environment of the build.
        activation_env = {**env, _ACTIVATION_ID: secrets.token_hex(8)}
        with BuildJob() as job:
            proc = job.start_command_line(command_line, cwd=cwd, env=activation_env)
            try:
                stdout, stderr = _communicate_in_slices(proc, timeout)
            except subprocess.TimeoutExpired:
                job.close()
                proc.communicate()
                raise BuildTimeoutError(f"activate script {script} timed out after {timeout}s") from None
            except BaseException:
                job.close()
                proc.wait()
                raise
        if proc.returncode != 0:
            output = (stderr or b"").decode("oem", errors="replace")
            raise RuntimeError(
                f"activate script {script} failed (exit {proc.returncode}){_quote_stream('output', output)}",
            )
        environment = _parse_environment(script, stdout or b"")
        environment.pop(_ACTIVATION_ID, None)
        return environment

    def _run_in_job(
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
        capture_output: bool,
    ) -> subprocess.CompletedProcess:
        """Windows form of :func:`run_in_process_group`: the build runs in a job object.

        The same result as bytes (the caller decodes them), the same
        exceptions, and ``JobObjectError`` (a
        ``RuntimeError``) when Windows refuses the job.  A stop (the timeout,
        an exception while the build runs) closes the job, and Windows ends
        each process of the build at once; ``job_object.py`` gives the
        reason and the measurements.  WHY the close and not
        ``TerminateJobObject``: the close cannot fail, thus an error of the
        stop cannot replace the exception that the stop handles.

        At the end, also after a build that ended by itself, the close of
        the job ends each process that the build left (a compiler server).
        On POSIX the next index run stops such a process through the record
        (``cli/_index._reap_orphan_build_group``).

        No record (``record_build_groups``): a run that dies closes the
        handle of the job, and Windows ends the build then.
        """
        from .job_object import BuildJob

        pipe = subprocess.PIPE if capture_output else None
        with BuildJob() as job:
            proc = job.start(_windows_command(cmd, env), cwd=cwd, env=env, stdout=pipe, stderr=pipe)
            try:
                stdout, stderr = _communicate_in_slices(proc, timeout)
            except subprocess.TimeoutExpired:
                job.close()
                stdout, stderr = proc.communicate()
                raise _decoded_timeout(
                    subprocess.TimeoutExpired(cmd, timeout or 0, output=stdout, stderr=stderr),
                ) from None
            except BaseException:
                job.close()
                proc.wait()
                raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def windows_words(text: str) -> list[str]:
    """Return the arguments that a Windows program gets for the command line *text*.

    The rules of `CommandLineToArgvW`, which the C runtime and thus ld and
    gcc use.  A double quote starts or ends a quoted part anywhere in a
    word, `""` in a quoted part is one quote, and a backslash is literal
    unless it comes before a quote: 2n backslashes give n and keep the quote
    as a delimiter, 2n+1 give n and a literal quote.  `shlex` with
    `posix=False` knows a quote only at the start of a word, thus
    `-L"C:/My Projects/x"` became two words.

    The `""` rule is the rule of the C runtime since 2008: the quoted part
    stays open after the literal quote.  The first argument of a command
    line has other rules, and this function never gets one.  The layer of
    `cmd.exe /C` (`^`, `%`), which CMake adds for PRE_LINK and POST_BUILD,
    is not modelled: those commands hold no argument of the link.
    """
    words: list[str] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t":
            index += 1
        if index == len(text):
            break
        word: list[str] = []
        quoted = False
        while index < len(text) and (quoted or text[index] not in " \t"):
            char = text[index]
            if char == "\\":
                end = index
                while end < len(text) and text[end] == "\\":
                    end += 1
                count = end - index
                if end < len(text) and text[end] == '"':
                    word.append("\\" * (count // 2))
                    if count % 2:
                        word.append('"')
                        end += 1
                else:
                    word.append("\\" * count)
                index = end
            elif char == '"':
                if quoted and index + 1 < len(text) and text[index + 1] == '"':
                    word.append('"')
                    index += 2
                    continue
                quoted = not quoted
                index += 1
            else:
                word.append(char)
                index += 1
        words.append("".join(word))
    return words


def _windows_program_word(text: str) -> tuple[str, str]:
    """Split the program name from a Windows command line; return the name and the rest.

    The first argument has rules of its own in ``CommandLineToArgvW``: a
    name that starts with a double quote ends at the next double quote, and
    each other name ends at the first space or tab.  A backslash is always
    literal there, thus ``"C:\\tools\\"`` keeps its last backslash.
    """
    text = text.lstrip(" \t")
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            return text[1:], ""
        return text[1:end], text[end + 1:]
    end = len(text)
    for index, char in enumerate(text):
        if char in " \t":
            end = index
            break
    return text[:end], text[end:]


def command_words(text: str) -> list[str]:
    """Split a command line into its arguments, by the rules of the host.

    The command lines of the config (``[build] pre_build``, ``command``),
    and the ``command`` field of ``compile_commands.json``: the arguments
    as the program got them.  POSIX shell words (``shlex``) on POSIX.  On
    Windows the rules of ``CreateProcess`` for the program name and of the
    C runtime (``CommandLineToArgvW``) for the arguments.  clang reads the
    ``command`` field with the rules of Windows on a Windows host too
    (``JSONCompilationDatabase``); for the program name its rules differ
    only for forms that no build tool writes (a quoted name that ends with
    a backslash, a quote in the middle of the name).  WHY not ``shlex`` on
    Windows: it removes each backslash, thus ``C:\\tools\\pre.bat``
    became ``C:toolspre.bat``.

    Raises ``ValueError`` for a POSIX command line with a quote that does
    not end, as ``shlex.split`` does.
    """
    if sys.platform != "win32":
        return shlex.split(text)
    program, rest = _windows_program_word(text)
    if not program and not rest.strip():
        return []
    return [program, *windows_words(rest)]


def command_program(text: str) -> str:
    """Return the first word (the program) of the command line *text* by the rules of the host, or "".

    The first word of :func:`command_words`, without the split of the
    rest: a ``command`` field of a large build holds hundreds of flags.
    Raises ``ValueError`` as :func:`command_words` does.
    """
    if sys.platform == "win32":
        return _windows_program_word(text)[0]
    words = shlex.split(text)
    return words[0] if words else ""


def build_cfg_env(build_cfg: BuildConfig | None, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a build command: :func:`build_env` plus the build config.

    *env* goes on top of :func:`build_env`, then ``build_cfg.env``,
    ``build_cfg.extra_path`` (prepended to ``PATH``) and
    ``build_cfg.extra_env``.  Every command that the build config runs gets
    it: the builders through :func:`run_build_command`, and the
    ``pre_build`` hook and the ``command`` override in ``indexer.build``.
    """
    merged_env = build_env(env)
    if build_cfg:
        if build_cfg.env:
            merged_env.update(build_cfg.env)
        if build_cfg.extra_path:
            merged_env["PATH"] = os.pathsep.join(build_cfg.extra_path) + os.pathsep + merged_env.get("PATH", "")
        if build_cfg.extra_env:
            merged_env.update(build_cfg.extra_env)
    return merged_env


class BuildTimeoutError(RuntimeError):
    """A build command ran longer than its timeout.

    A subclass of the ``RuntimeError`` that ``run_build_command`` raises for
    every failure, thus each caller that catches that error stays correct.
    A caller that tries again after a failure needs the difference: a
    command that hung once can hang again, each time for the whole timeout.
    """


def activate_script(activate: str, cwd: Path) -> Path:
    """The path of the activate script *activate* of a build in *cwd*.

    A relative path is relative to the directory of the build.  WHY:
    ``source env.sh``, a name without a slash, looks in PATH before the
    current directory, thus a different ``env.sh`` on PATH won.
    """
    script = Path(activate).expanduser()
    return script if script.is_absolute() else Path(cwd) / script


def run_build_command(
    cmd: list[str],
    cwd: Path,
    timeout: float | None = None,
    description: str = "",
    env: dict[str, str] | None = None,
    build_cfg: BuildConfig | None = None,
    errors: str = "replace",
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
      && <cmd>"`` invocation.  On Windows the command runs with the
      environment that the script leaves (``_activated_environment``).  Centralising this logic means only one
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
        errors: The decode of the UTF-8 stdout (see
            :func:`run_in_process_group`).  ``"replace"`` for output that a
            person reads, ``"strict"`` for output that the caller parses:
            a parse must not read a name with U+FFFD in it.  stderr is
            always decoded with replacement.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        RuntimeError: On non-zero exit, and with *errors* ``"strict"`` on
            a stdout that is not UTF-8.  BuildTimeoutError (a RuntimeError)
            on timeout.
    """
    if timeout is None:
        timeout = build_cfg.timeout if build_cfg is not None else 7200

    merged_env = build_cfg_env(build_cfg, env)

    if build_cfg is not None and build_cfg.activate:
        script = activate_script(build_cfg.activate, cwd)
        if sys.platform == "win32":
            # One timeout for the script and the command, as on POSIX, where
            # bash runs the two under one timeout.
            started = time.monotonic()
            merged_env = _activated_environment(script, cwd=cwd, env=merged_env, timeout=timeout)
            timeout -= time.monotonic() - started
            if timeout <= 0:
                raise BuildTimeoutError(
                    f"Build command timed out: the activate script {script} used the whole timeout: "
                    f"{description or ' '.join(cmd)}",
                )
        else:
            cmd_str = " ".join(shlex.quote(c) for c in cmd)
            cmd = ["bash", "-c", f"source {shlex.quote(str(script))} && {cmd_str}"]

    try:
        # In its own process group — see run_in_process_group.
        result = run_in_process_group(
            cmd, cwd=cwd, env=merged_env, timeout=timeout, capture_output=True, errors=errors,
        )
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"Build command wrote output that is not UTF-8: {description or ' '.join(cmd)}: {e}"
        ) from e
    except subprocess.TimeoutExpired as e:
        # Quote what the build managed to say.  A timeout with no output
        # gives the reader nothing to act on, and the last lines usually
        # name the step that hung.
        raise BuildTimeoutError(
            f"Build command timed out after {timeout}s: "
            f"{description or ' '.join(cmd)}"
            f"{_quote_stream('stdout', _as_text(e.stdout))}"
            f"{_quote_stream('stderr', _as_text(e.stderr))}"
        ) from None
    if result.returncode != 0:
        # BOTH streams, and the END of each.  A build tool writes the
        # reason for the failure wherever it likes: mbed-cli puts its own
        # wrapper text on stderr and leaves the compiler errors on stdout,
        # thus quoting the head of stderr showed a cut-off "[mbed] ERROR:
        # ... returned error" and hid every line that said why.
        raise RuntimeError(
            f"Build command failed (exit {result.returncode}): "
            f"{description or ' '.join(cmd)}"
            f"{_quote_stream('stdout', result.stdout)}"
            f"{_quote_stream('stderr', result.stderr)}"
        )
    return result


# The longest time that the lookup of one tool can take.  The activate script
# of NCS runs `nrfutil sdk-manager toolchain env`: measured 0.5 s.
_TOOL_LOOKUP_TIMEOUT_S = 60


def build_tool_available(name: str, cwd: Path, build_cfg: BuildConfig | None) -> bool:
    """Say if the build of *build_cfg* finds the program *name*.

    With ``activate``, the answer comes from the environment of the
    project: ``command -v`` runs after ``source <activate>``, as each build
    command does (:func:`run_build_command`).  The PATH of the fw-context
    process does not count.  The activate script of NCS puts ``west`` and
    ``ninja`` on PATH, and a ``west`` from outside that environment can
    name a different workspace.  Without ``activate``, the build gets the
    PATH of :func:`build_cfg_env`, with ``extra_path``, and this looks
    there.

    A failed activate script also gives False, and the caller names the
    script in its message.

    On Windows the lookup is ``_find_program`` in the environment that the
    script leaves, as the build gets it.  A script that cannot run on
    Windows (its kind, its path) raises ``ActivateScriptError``: the
    message "not on PATH" would hide a fault of the config.
    """
    if sys.platform == "win32":
        environment = build_cfg_env(build_cfg)
        if build_cfg is not None and build_cfg.activate:
            script = activate_script(build_cfg.activate, cwd)
            try:
                environment = _activated_environment(
                    script, cwd=cwd, env=environment, timeout=_TOOL_LOOKUP_TIMEOUT_S,
                )
            except ActivateScriptError:
                raise
            except RuntimeError as exc:
                log.warning("%s not found after the activate script %s: %s", name, script, exc)
                return False
        return _find_program(name, environment) is not None
    if build_cfg is not None and build_cfg.activate:
        try:
            run_build_command(
                ["command", "-v", name],
                cwd=cwd,
                timeout=_TOOL_LOOKUP_TIMEOUT_S,
                description=f"look for {name}",
                build_cfg=build_cfg,
            )
        except RuntimeError as exc:
            # The error of the caller names only the script.  The log keeps
            # why: a timeout, a failed script, or no such tool, with the
            # end of the output of the script.
            log.warning("%s not found after source %s: %s", name, build_cfg.activate, exc)
            return False
        return True
    return shutil.which(name, path=build_cfg_env(build_cfg).get("PATH")) is not None


def missing_tool_message(name: str, build_cfg: BuildConfig | None, install_hint: str) -> str:
    """Return the error text for a build that cannot find the program *name*.

    With ``activate``, the cause is the environment of the project: the
    message names the script, because a ``west`` on the PATH of the
    operator does not help that build.
    """
    if build_cfg is not None and build_cfg.activate and sys.platform == "win32":
        return (
            f"{name} is not on PATH after the activate script {build_cfg.activate} "
            "([build] activate).  Correct the script, or set [build] activate "
            "to the script that sets up the environment of the project."
        )
    if build_cfg is not None and build_cfg.activate:
        return (
            f"{name} is not on PATH after `source {build_cfg.activate}` "
            "([build] activate).  Correct the script, or set [build] activate "
            "to the script that sets up the environment of the project."
        )
    return f"{name} is required.  {install_hint}"


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
    """Return the absolute path of the index key *path*, with the separator ``/``.

    A relative key is joined to *root*.  An absolute key (``/opt/...`` or
    ``C:/...``) stays as it is.  An empty string stays empty: the caller
    must not give one.

    WHY ``/`` on each system: the answers of the MCP tools give this path
    as ``file``, and a client gives it back to a tool, or compares it with
    the path of a different answer.  The index keys use ``/`` (see
    ``path_key``), thus one file has one spelling in each answer.  On
    Linux and macOS ``as_posix()`` is ``str()``, thus no answer changes
    there.  Windows accepts ``/`` in each path.

    WHY ``is_absolute_key`` and not ``Path.is_absolute``: the result of
    ``Path.is_absolute`` depends on the system.  On Windows ``/opt/sdk/x.h``
    is not absolute, and ``root / key`` put the drive of *root* in front
    of it.  On Linux ``C:/ncs/x.h`` is not absolute.
    """
    if not path:
        return ""
    if is_absolute_key(path):
        return Path(path).as_posix()
    return (root / path).as_posix()


def cc_output_path(project_root: Path, cfg: BuildConfig) -> Path:
    """Return the file that a backend writes its compilation database to.

    Ensures the parent directory exists.  A backend calls this when the tool
    that it runs takes the output path as an argument (bear, compiledb,
    keil2clangd), or when it copies a database that the build system cannot
    put into the output directory.

    Under ``generate_compile_commands`` *cfg* has ``cc_output``, the staging
    file of that run, and one atomic rename at the end of the build gives
    the final file its new content.  WHY: a reader of the final file must
    never get the half-written output of a build that still runs — measured
    on 2026-09-23, when a background run read a file that stopped in
    mid-line.  Without ``cc_output`` the answer is the final file,
    ``out/compile_commands.json`` of the variant that *cfg* builds.
    """
    from fw_context_mcp.indexer.build_layout import COMPILE_COMMANDS_NAME, BuildLayout

    if cfg.cc_output is not None:
        target = cfg.cc_output
    else:
        target = BuildLayout(project_root).out_dir(cfg.variant_name) / COMPILE_COMMANDS_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _owner_tag() -> str:
    """Return the tag of the PID namespace that this process runs in.

    A PID has a meaning only in its own PID namespace.  A container and its
    host can share one project directory, and each has its own index lock
    (the lock is in the home directory), thus the two can build at the same
    time.  A PID from the container names no process on the host, and a host
    that deleted each file of a "dead" PID would delete a live file.

    The tag is the host name plus the inode of ``/proc/self/ns/pid``.  The
    host name alone is not sufficient: ``--uts=host`` gives a container the
    host name of the host.  The inode alone is not sufficient: the first PID
    namespace of each Linux machine has the same inode, and two machines can
    share a project on a network file system.  Where ``/proc`` is not
    available (macOS), the tag is the host name.

    The tag goes into a file name, thus each character of the host name
    other than ``[A-Za-z0-9.-]`` becomes ``_``, and an empty host name is
    ``_``.  :data:`_TMP_OWNER` accepts these characters.
    """
    host = re.sub(r"[^A-Za-z0-9.-]", "_", socket.gethostname()) or "_"
    try:
        link = os.readlink("/proc/self/ns/pid")
    except OSError:
        return host
    inode = re.sub(r"\D", "", link)
    return f"{host}-{inode}" if inode else host


#: The environment variable that moves the global directory of fw-context.
FW_CONTEXT_HOME_ENV = "FW_CONTEXT_HOME"


def fw_context_home() -> Path:
    """Return the global directory of fw-context: ``$FW_CONTEXT_HOME``, else ``~/.fw-context``.

    The global config, the local LLM cache (``llm_cache.db``) and the
    cleanup of the global files read it.  The
    test session sets it, so that a test, and each CLI process that a test
    starts, cannot change the directory of the user: the cleanup removes
    files there.  The index, the registry and the clang headers have their
    own variables (``FW_CONTEXT_INDEX_DIR``, ``FW_CONTEXT_PROJECTS_DB``,
    ``FW_CONTEXT_CLANG_RESOURCE_DIR``).
    """
    value = os.environ.get(FW_CONTEXT_HOME_ENV)
    return Path(value).expanduser() if value else Path.home() / ".fw-context"


def owner_token() -> str:
    """Return ``<pid>@<tag>``, the owner of a file that this process writes.

    See :func:`_owner_tag` for the tag.  :func:`owner_is_dead` reads the
    token back.
    """
    return f"{os.getpid()}@{_owner_tag()}"


def owner_is_dead(token: str) -> bool:
    """Return True when *token* names a process of this PID namespace that stopped.

    Only then is the file of that owner garbage.  A token from a different
    PID namespace gives False: this process cannot see that PID, thus it
    cannot know if the owner still runs.  A token that does not parse gives
    False too, because the file then has an unknown source.

    A process that runs and belongs to a different user is alive.  A PID
    outside 1 .. ``processes.MAX_PID`` (``pid_t`` on POSIX, a DWORD on
    Windows) did not come from this code, thus the file of such a token
    stays.  So does the file when the probe gives an error that is no
    answer: the cost of a kept file is disk space only.

    The probe is :func:`fw_context_mcp.processes.pid_exists`: on Windows,
    signal 0 of ``os.kill`` is ``CTRL_C_EVENT``, which sends Ctrl+C to the
    process group of the PID and tests nothing.
    """
    pid_text, sep, tag = token.partition("@")
    # isascii first: isdigit() also accepts digits such as "²", which int()
    # refuses with a ValueError.
    if not sep or not (pid_text.isascii() and pid_text.isdigit()) or tag != _owner_tag():
        return False
    pid = int(pid_text)
    if not 0 < pid <= MAX_PID:
        return False
    try:
        return not pid_exists(pid)
    except OSError:
        return False



# The owner token at the end of a temporary name: digits, "@", then the tag
# (see _owner_tag).  No base name holds "@", thus the match is unambiguous.
_TMP_OWNER = re.compile(r"\.(\d+@[A-Za-z0-9._-]+)\.tmp$")


def write_text_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write *text* to *path* through a file in the same directory and a rename.

    A process that stops during a plain write leaves a cut file, and the
    next reader gets no JSON at all.  The index run writes its manifest and
    its run marker while it runs, and a run can stop at any moment.
    ``os.replace`` is atomic on POSIX and on Windows when the two names are
    in one directory, and it replaces an existing file on both.  On Windows
    it fails while another process holds the target open (the MCP server
    reading the manifest); the error goes to the caller.

    The temporary name holds :func:`owner_token`, thus two writers never
    share one, and :func:`remove_stale_tmp` removes the file of a writer
    that a kill stopped between the write and the rename.

    The text goes to the file as it is (``newline=""``): a config file of
    the user keeps its line ends, and on Windows a text-mode write made
    each "\\r\\n" of a CRLF file "\\r\\r\\n".

    *mode* gives the new file these permission bits.  Without it, the file
    gets the default of the process (0644 with the usual umask), and a
    rewrite of a config of mode 0600 that holds a token made the token
    readable for all users.  With *mode*, the temporary file starts as
    0600, thus the text is never readable for more users than *mode* lets.
    """
    tmp = path.with_name(f".{path.name}.{owner_token()}.tmp")
    try:
        if mode is None:
            tmp.write_text(text, encoding="utf-8", newline="")
        else:
            # A file with this name is from this process, or from a dead
            # process whose PID came back: garbage in the two cases.
            # O_EXCL then makes sure that the file is new and of mode 0600,
            # not a file or a link that someone put there after the unlink.
            # O_BINARY: on Windows a text-mode descriptor changes each "\n".
            tmp.unlink(missing_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(tmp, flags, 0o600), "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def remove_stale_tmp(directory: Path) -> int:
    """Remove the temporary files whose writer stopped.

    Two writers name their temporary files with :func:`owner_token`:
    :func:`write_text_atomic` and ``PidFile.write``.  Returns the count.  A
    manifest is tens of MB on a large project, thus a file that a kill left
    behind is worth removing.  A file of a writer that still runs, or of
    another host, stays: :func:`owner_is_dead` decides.
    """
    removed = 0
    for tmp in directory.glob(".*.tmp"):
        match = _TMP_OWNER.search(tmp.name)
        if match is not None and owner_is_dead(match.group(1)):
            tmp.unlink(missing_ok=True)
            removed += 1
    return removed


def cc_staging_path(directory: Path) -> Path:
    """Return the file in *directory* that one build writes before the final database gets it.

    The name holds the owner token of this process (see :func:`owner_token`),
    thus two runs cannot write one staging file, also when they run in two
    PID namespaces.  The leading dot keeps the file out of each scan that
    looks for ``compile_commands.*``.

    The caller puts *directory* on the file system of the final database,
    thus ``os.replace`` between the two is atomic.
    """
    staging = directory / f".{_CC_STEM}.{owner_token()}{_CC_SUFFIX}"
    staging.parent.mkdir(parents=True, exist_ok=True)
    return staging


def staging_owner(path: Path) -> str | None:
    """Return the owner token in the name of a staging file, or None.

    The inverse of the names that :func:`cc_staging_path` makes:
    ``.compile_commands.<token>.json``.  The token is found by its form,
    ``<digits>@<tag>``, after a dot or at the start, because the host name in
    the token holds dots too (see :func:`temporary_owner`).
    """
    return temporary_owner(path, _CC_STEM, _CC_SUFFIX)


def temporary_owner(path: Path, stem: str, suffix: str) -> str | None:
    """Return the owner token in the name of a temporary file, or None.

    The name of a temporary file of ``<stem><suffix>`` is
    ``.<stem>.[<name>.]<token><suffix>``, as the atomic writers make it with
    :func:`owner_token`.
    """
    prefix = f".{stem}."
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    middle = name.removeprefix(prefix).removesuffix(suffix)
    match = _OWNER_TOKEN_AT_END.search(middle)
    return match.group(1) if match else None


def clear_dead_temporaries(directory: Path, stem: str, suffix: str) -> int:
    """Remove the temporary files of ``<stem><suffix>`` whose owner stopped.

    An atomic write that a signal (SIGKILL) stops leaves its temporary file
    behind, and no later write uses that name again, because the name holds
    the owner token.  A file of a process that still runs stays: it writes
    the file.  A file whose owner this process cannot see stays too, see
    :func:`owner_is_dead`.  Best-effort: a file that cannot be deleted costs
    disk space only.  Returns the number of removed files.
    """
    removed = 0
    for candidate in directory.glob(f".{stem}.*{suffix}"):
        token = temporary_owner(candidate, stem, suffix)
        if token is None or not owner_is_dead(token):
            continue
        try:
            candidate.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            log.debug("Cannot remove the dead temporary file %s: %s", candidate, exc)
    return removed


# The owner token at the end of the middle part of a staging name.  The tag
# holds only the characters that _owner_tag keeps.
_OWNER_TOKEN_AT_END = re.compile(r"(?:^|\.)(\d+@[A-Za-z0-9._-]+)$")


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


def decode_source_lines(data: bytes) -> list[str]:
    """Decode the bytes of a source file into lines, detecting the encoding.

    The lines are the ones that ``open(path, encoding=...).readlines()``
    gives: universal newlines, and the end of each line kept.  The indexer
    decodes the bytes that libclang parsed with this function, and
    :func:`read_file_lines` decodes the disk with it, thus one text gives
    one result on the two paths.

    Encoding fallback chain — ordered by coverage in embedded C/C++ projects:

    1. UTF-8 — universal, handles 99%+ of source files.
    2. cp1252 (Windows-1252) — common in legacy Windows-authored firmware;
       characters like 0x80-0x9F decode cleanly (unlike latin1 where they
       map to control characters that produce valid-but-garbled output).
    3. latin1 — ISO-8859-1 never raises UnicodeDecodeError (every byte
       maps to a valid character), so any file that survives this far
       is readable; the content may be garbled but the caller gets
       line-oriented access without crashing.

    UTF-16 files will produce garbled content; extremely low risk in
    embedded C/C++ projects.
    """
    for encoding in ("utf-8", "cp1252"):
        try:
            return io.TextIOWrapper(io.BytesIO(data), encoding=encoding).readlines()
        except UnicodeError:
            continue
    return io.TextIOWrapper(io.BytesIO(data), encoding="latin1").readlines()


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

    try:
        result = decode_source_lines(Path(abs_path).read_bytes())
    except OSError:
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


def macro_signature(name: str, is_function_like: bool, params: str) -> str:
    """Write how a macro is invoked: ``NAME``, ``NAME()`` or ``NAME(a, b)``.

    *params* is the ``macros.params`` column — the parameter list without
    its parentheses.  It is empty BOTH for an object-like macro and for a
    function-like one that takes no argument, thus *is_function_like* is
    what separates ``NAME`` from ``NAME()``.

    WHY one function and not one f-string at each reader: four places
    answer with a macro — ``lookup_symbol``, ``find_callers`` and
    ``find_references``, the macro step of ``search_code``, and the shared
    fallback behind ``get_source``, ``get_symbol_context`` and
    ``explain_symbol``.  A reader that sees two spellings of one macro has
    to guess which one the source holds.
    """
    if not is_function_like:
        return name
    return f"{name}({params})"


def fmt_count(n: int) -> str:
    """Format a count with thousand separators (non-breaking thin spaces)."""
    if n < 0:
        return "-" + f"{abs(n):_d}".replace("_", " ")
    return f"{n:_d}".replace("_", " ")


def use_utf8_stdio(*, stdout: bool = True) -> None:
    """Write ``sys.stderr``, and ``sys.stdout`` when *stdout*, in UTF-8 on Windows.

    WHY: on Windows, Python writes a pipe or a file in the ANSI code page of
    the system (cp1252, cp1250), and ``print("✓")`` then raises
    UnicodeEncodeError.  The daemon starts ``fw-context index --background``
    with its output in a log file, and ``fw-context init`` under a pipe
    stopped in its dependency audit (measured on Windows 11, 2026-11-09).
    A console is UTF-8 already (PEP 528), and Python 3.15 makes UTF-8 the
    default for the other streams too (PEP 686).

    Call it at the start of an entry point that writes text.  The MCP server
    gives ``stdout=False``: its transport wraps the binary
    ``sys.stdout.buffer`` itself, and only its log on stderr needs the
    change.
    """
    if sys.platform != "win32":
        return
    _reconfigure_utf8([sys.stderr, sys.stdout] if stdout else [sys.stderr])


def _reconfigure_utf8(streams: list) -> None:
    """Set UTF-8 on each text stream of *streams*, and keep its error handler.

    WHY keep ``errors``: ``reconfigure(encoding=...)`` alone resets it to
    ``strict``, and stderr has ``backslashreplace``, thus a traceback with a
    lone surrogate (a path decoded with ``surrogateescape``) raised instead
    of the report.
    """
    for stream in streams:
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors=stream.errors)


#: ``os.O_NOFOLLOW`` where the system has it, else 0 (Windows).
#:
#: WHY: a log in the index directory opens with it, thus a link that a
#: different user put there cannot send the write to a different file.
#: Windows has no such flag.  There a symlink needs the privilege
#: ``SeCreateSymbolicLinkPrivilege`` (or the developer mode), and the ACL
#: of the profile keeps other users out of ``~/.fw-context``.  The open
#: raised AttributeError before, which stopped the start of the daemon and
#: of each background index run.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
