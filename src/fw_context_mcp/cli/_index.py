"""``fw-context index`` — build or rebuild the libclang symbol index.

This command parses ``compile_commands.json`` with libclang and stores
function/class/enum metadata, cross-references, embeddings, and optional
LLM analysis in an SQLite database.

Indexing phases run in order:
1. **Build** (if ``--build`` or missing cc.json) — generate
   compile_commands.json from the detected build system.
2. **Validate** — check cc.json completeness, detect stale entries,
   fix path separators and other common issues.
3. **Parse** — libclang extraction of symbols from every translation unit.
4. **Refs** — cross-reference indexing: callers, callees, indirect edges.
5. **Embeddings** — generate vector embeddings for semantic search.
6. **Analyze** (optional) — LLM-generated summaries for each symbol.

WHY: The index is the core data structure of fw-context.  Without a
complete and accurate index, every search, call-graph query, and
semantic search returns wrong or empty results.  This command must
handle incremental updates, stale detection, background conflict
resolution, and graceful degradation when optional phases fail.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..indexer import autobuild
from ..mcp.shared.pid_file import PidFile
from ..utils import (
    SAFE_EXCEPT,
    build_dir_patterns_with_fw_context,
    process_start_time,
    record_build_groups,
)
from . import VerboseFormatter

if TYPE_CHECKING:
    from ..indexer.build import BuildConfig

log = logging.getLogger(__name__)


# ── Automatic build for a source file the build system never saw ──
# A new .c file has no translation unit: it is absent from
# compile_commands.json, and only a build puts it there.  A plain reindex
# skips it without a word, thus fw-context runs the build itself.
#
# The markers and the state machine live in indexer/autobuild.py, because
# mcp/handlers/maintenance.py needs the same answers to tell the caller what
# will happen, and it cannot import from cli/.


def _checked_build(conn, project_root: Path, cfg):
    """Return the build that the checks of an index run read, or None without one.

    The build of a query without ``variant`` and ``image``, as
    ``get_active_build`` reports it (``active_build``), thus the CLI and the
    MCP server check one build.  The newest build only when no build
    answers such a query: the newest one is the program that the last run
    did last, a bootloader after a failed application.
    """
    from ..config import derive_project_id
    from ..indexer.db import get_active_config
    from ..mcp.shared.variants import active_build

    project_id = derive_project_id(project_root)
    row, _refusal = active_build(conn, project_id, cfg, project_root)
    return row if row is not None else get_active_config(conn, project_id)


def _indexed_compile_commands(project_root: Path, db_path: Path) -> Path | None:
    """Give the compile_commands.json of the active index, or None without one.

    ``build.checked_compile_commands`` needs it: only the file that the
    index came from is checked.  A database that cannot be read counts as no
    index; the run that opens it next gives the real error.
    """
    if not db_path.exists():
        return None
    from ..config import load as load_config
    from ..indexer.db import open_db

    try:
        conn = open_db(db_path)
    except SAFE_EXCEPT:
        log.debug("could not read the active compile_commands.json", exc_info=True)
        return None
    try:
        active = _checked_build(conn, project_root, load_config(project_root=project_root))
    finally:
        conn.close()
    if not active or not active["compile_commands_path"]:
        return None
    return Path(active["compile_commands_path"])


def _build_if_missing(
    args: argparse.Namespace, project_root: Path, cfg, detected_system: str | None,
    indexed: Path | None,
) -> None:
    """Turn ``--build`` on when a build directory of the default compile_commands.json is gone.

    *indexed* is the file of the active index (``_indexed_compile_commands``).
    A missing compile_commands.json is not handled here: the default path of
    ``_resolve_compile_commands`` builds then, in the build directory of the
    user, and it gives its own error when it cannot.  When fw-context cannot
    run the build of the project (a stub such as STM32CubeIDE, see
    ``build.can_run_build``), nothing is turned on, and
    ``_refuse_without_build`` stops the run.
    """
    from ..indexer.build import can_run_build, checked_compile_commands

    cc = checked_compile_commands(project_root, cfg, indexed)
    if cc is None or not cc.exists():
        return
    reason = autobuild.build_missing_reason(cc)
    if reason and can_run_build(cfg.build, cfg.build.system or detected_system):
        args.build = True
        print(f"{reason} — running the build (--build)", file=sys.stderr)


def _refuse_without_build(
    project_root: Path, cfg, detected_system: str | None, indexed: Path | None,
    *, at_the_end: bool = False,
) -> str:
    """Give the error that stops a run whose build is not there, or "".

    Called when the run builds nothing, and at the end of a run that ended
    well (*at_the_end*).  *indexed* is the file of the active index.  Before
    the run, a missing compile_commands.json is left to
    ``_resolve_compile_commands``, which builds or gives its own error.  At
    the end, nothing builds any more, and a build of fw-context keeps its
    database in its output directory, thus a missing database is a build
    that went away during the run.  The text names what the user must do: a
    build that fw-context can run is ``fw-context index --build``; any other
    build runs outside of fw-context (an IDE), and ``fw-context index``
    follows it.
    """
    from ..indexer.build import can_run_build, checked_compile_commands

    cc = checked_compile_commands(project_root, cfg, indexed)
    if cc is None or (not at_the_end and not cc.exists()):
        return ""
    reason = autobuild.build_missing_reason(cc)
    if not reason:
        return ""
    if can_run_build(cfg.build, cfg.build.system or detected_system):
        return (f"{reason}. fw-context does not index without the build, because each unit would get "
                "no answer of its compiler. Run 'fw-context index --build'.")
    return (f"{reason}, and fw-context cannot run the build of this project. fw-context does not "
            "index without the build. Run the build outside of fw-context (for example in the "
            "IDE), then run 'fw-context index'.")


def _run_failed_text(failure: str, bg: bool, db_dir: Path) -> str:
    """Give the text that the MCP answers carry after a failed index run.

    *failure* is ``"exit code N"``, or the type and text of the exception
    that stopped the run.  The text names where the full error is: each
    failed step prints its own error, and the run does not collect them.  A
    background run is a run of the daemon, which writes its output to
    ``reindex.log`` (``daemon._run_index_async``).
    """
    where = (f"Its output is in {db_dir / 'reindex.log'}." if bg
             else "Its error is in the output of 'fw-context index'.")
    return (f"The last index run failed ({failure}), thus the index holds the data "
            f"of an earlier run. {where} Repair the cause, then run 'fw-context index'.")


def _plan_auto_build(
    project_root: Path,
    db_path: Path,
    cfg,
    detected_system: str | None,
    *,
    background: bool,
) -> tuple[list[str], BuildConfig | None, str]:
    """Return ``(the keys of this build, its config, a line to log)``.

    An empty list means "do not build", and the config is then None.  The
    list is non-empty only when all of these hold:

    1. An index exists.  Without one there is nothing to compare against.
    2. Something a build can repair and a reindex cannot.  Three such
       things, and each one is enough:

       * The build is gone: compile_commands.json or a ``directory`` of its
         entries does not exist (``autobuild.build_missing_reason``).  Only
         for a *background* run: a run of the user builds as ``--build``
         does (``_build_if_missing`` and the default path of
         ``_resolve_compile_commands``).
       * Source files sit on disk that compile_commands.json does not cover.
         Such a file has no translation unit, so a reindex skips it.
       * The tree is on a different branch than the index.
         compile_commands.json is a build artifact of the branch it was
         generated on and carries that branch's file list AND its compiler
         flags.  Measured on the Mbed project, a switch from 4.15.3 to 4.15.1
         left two generated zcbor sources listed in it and absent from the
         tree; a reindex reads the same file again.

    3. The backend may build in the background — it writes its output into
       the build tree of fw-context, or it compiles nothing.  See
       ``builders.background_build_safe``.

    The KEYS name the trigger: ``"build-missing"``, the source paths, or
    ``"branch:<name>"``.  An earlier failure does not block the build: the
    run that fails again writes the error for the MCP answers
    (``autobuild.record_problem``), and that is what makes the user repair
    it.

    Condition 3 is the important one.  The build runs while the user works,
    possibly while an IDE builds the same project, and fw-context cannot
    lock the build of the IDE.

    The config that comes back is ``cfg.build``, the config the build uses.
    """
    if not db_path.exists():
        return [], None, ""

    from ..indexer.build import checked_compile_commands
    from ..indexer.build_layout import BuildLayout
    from ..indexer.git_context import branch_moved_since
    from ..mcp.shared.stale import find_unindexed_sources

    if not _may_build_in_background(cfg, detected_system):
        return [], None, ""
    candidate = cfg.build
    # Where the build goes, for the log line.  A project with variants
    # builds each of them, each into its own directory.
    target = BuildLayout(project_root).root if candidate.variants else BuildLayout(project_root).out_dir("")

    from ..indexer.db import open_db

    conn = open_db(db_path)
    try:
        active = _checked_build(conn, project_root, cfg)
        if not active or not active["compile_commands_path"]:
            return [], None, ""
        # The build is gone as a whole: no file list and no compiler answer
        # can come from it, thus it outranks the two other reasons.  The
        # file is the one that this run reads and that the index came from,
        # as `get_active_build` checks it (`build.checked_compile_commands`).
        checked = (
            checked_compile_commands(project_root, cfg, Path(active["compile_commands_path"]))
            if background else None
        )
        missing = autobuild.build_missing_reason(checked) if checked is not None else ""
        if missing:
            return ["build-missing"], candidate, (
                f"{missing} — running a build into {target}"
            )
        new_sources = find_unindexed_sources(
            conn,
            active["config_hash"],
            project_root,
            Path(active["compile_commands_path"]),
        )
        recorded = ""
        if "description" in active.keys():
            recorded = str(active["description"] or "")
    finally:
        conn.close()

    # The branch first: it makes compile_commands.json wrong as a whole,
    # while an uncovered source makes it incomplete.  A build repairs both,
    # so the more complete reason is the one worth logging.
    indexed_branch, live_branch = branch_moved_since(recorded, project_root)
    if indexed_branch:
        return [f"branch:{live_branch}"], candidate, (
            f"branch changed from {indexed_branch} to {live_branch} — "
            f"compile_commands.json belongs to the old branch, "
            f"running a build into {target}"
        )

    if not new_sources:
        return [], None, ""
    listed = ", ".join(new_sources[:3])
    more = f" and {len(new_sources) - 3} more" if len(new_sources) > 3 else ""
    return new_sources, candidate, (
        f"{len(new_sources)} source file(s) are missing from "
        f"compile_commands.json ({listed}{more}) — running a build into "
        f"{target}"
    )


def _may_build_in_background(cfg, detected_system: str | None) -> bool:
    """Say if a background run may start the build of this project.

    The backend decides, see ``builders.background_build_safe``: it must
    write its output into the build tree of fw-context, or compile nothing.
    A ``[build] command`` never may: it runs a build that the user wrote,
    and fw-context does not know where that build puts its output.
    """
    from ..indexer.builders import background_build_safe, registry

    if cfg.build.command:
        return False
    system = cfg.build.system or detected_system
    builder_cls = registry.get(system) if system else None
    return builder_cls is not None and background_build_safe(builder_cls(), cfg.build)


def _resolve_compile_commands(
    args: argparse.Namespace,
    project_root: Path,
    cfg,
    detected_system: str | None,
    bg: bool,
) -> tuple[Path | None, bool]:
    """Resolve the compile_commands.json path.

    Returns (path, is_explicit) or (None, False) on fatal error.
    Caller checks for None → return 1.

    Three resolution strategies, tried in order:
    1. ``--build`` — force a fresh build, ignoring any existing file.
    2. Explicit path (positional arg) — use the provided file.
    3. Default — the database of the build of fw-context, built when it is
       missing; or the database of the user that ``[index]
       compile_commands`` names (``build.user_database``), never built.

    WHY: Users should not need to know where their build system stores
    compile_commands.json.  The explicit path option exists for edge cases
    (CI pipelines, unsupported build systems) where the user manages
    cc.json themselves.
    """
    from ..indexer.build import user_database

    explicit_cc = bool(args.compile_commands)

    # `--build` writes the database of the build of fw-context, and a run
    # with a database of the user reads the other file: the index would
    # change from one database to the other with each run.
    if args.build and not explicit_cc and user_database(project_root, cfg) is not None:
        print(
            "error: [index] compile_commands names a database of your own, and --build would "
            "index the build of fw-context instead. Remove the key, or run without --build.",
            file=sys.stderr,
        )
        return None, False

    if args.build:
        # A background run builds only with a backend that may build on its
        # own (`builders.background_build_safe`): fw-context turned the build
        # on itself, after it found a missing build, a source file that
        # compile_commands.json does not cover, or another branch, and the
        # user did not ask for it.
        if bg and not _may_build_in_background(cfg, detected_system):
            print("error: --build and --background are mutually exclusive", file=sys.stderr)
            return None, False
        from ..indexer.build import generate_compile_commands

        build_cfg = cfg.build
        if args.no_clean:
            build_cfg.clean = False
        try:
            return generate_compile_commands(project_root, build_cfg), False
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, False

    if explicit_cc:
        cc = Path(args.compile_commands)
        if not cc.is_absolute():
            cc = (project_root / cc).resolve()
        if not cc.exists():
            print(f"error: {cc} not found", file=sys.stderr)
            print("  Run 'fw-context index --build' to build and index automatically.", file=sys.stderr)
            return None, False
        from ..indexer.build import check_completeness
        for warning in check_completeness(cc, project_root):
            print(f"warning: {warning}", file=sys.stderr)
        return cc, True

    # Default: reuse existing, build only if missing
    from ..indexer.build import (
        check_completeness,
        default_compile_commands,
        generate_compile_commands,
    )

    cc = default_compile_commands(project_root, cfg)
    if not cc.exists() and user_database(project_root, cfg) is not None:
        # A build of fw-context writes another file, thus it cannot give
        # the user the file that the config names.
        print(f"error: {cc} not found — [index] compile_commands names it", file=sys.stderr)
        print("  Make the file, or remove the key to index the build of fw-context.", file=sys.stderr)
        return None, False
    if not cc.exists():
        if bg and not _may_build_in_background(cfg, detected_system):
            print(f"error: compile_commands.json not found at {cc}", file=sys.stderr)
            print("  fw-context cannot run this build in the background.", file=sys.stderr)
            print("  Run 'fw-context index --build' first to set up the project.", file=sys.stderr)
            return None, False
        print("compile_commands.json not found, running build...", file=sys.stderr)
        try:
            return generate_compile_commands(project_root, cfg.build), False
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, False

    for warning in check_completeness(cc, project_root):
        print(f"warning: {warning}", file=sys.stderr)
    # A database of the user is explicit: `_validate_and_fix_artifacts` must
    # not rebuild it, as it does not rebuild a file on the command line.
    return cc, user_database(project_root, cfg) is not None


def _validate_and_fix_artifacts(
    compile_commands: Path,
    project_root: Path,
    detected_system: str | None,
    cfg,
    bg: bool,
    explicit_cc: bool,
    args: argparse.Namespace,
) -> tuple[Path | None, list[str] | None, bool]:
    """Validate build artifacts and optionally auto-fix.

    Returns (compile_commands, build_dir_patterns, ok).
    compile_commands may be updated if a rebuild was triggered.
    Caller returns 1 when ok is False.

    Checks performed:
    * Staleness — cc.json older than project sources.
    * Completeness — all source files present in cc.json entries.
    * Path correctness — Windows backslashes, mixed separators.

    WHY: A stale compile_commands.json produces an index that silently
    misses new or renamed files.  Auto-detection and auto-rebuild
    prevent the user from debugging "symbol not found" errors caused
    by an outdated cc.json rather than actual code issues.
    """
    if not detected_system:
        return compile_commands, None, True

    from ..indexer.build import generate_compile_commands
    from ..indexer.builders import registry as builder_registry
    from ..indexer.validator import is_compile_commands_stale, validate_and_fix

    builder_cls = builder_registry.get(detected_system)
    if builder_cls is None:
        return compile_commands, None, True

    builder_instance = builder_cls()
    build_dir_patterns = build_dir_patterns_with_fw_context(
        builder_instance.get_build_dir_patterns(project_root)
    )

    if not bg:
        stale, stale_reasons = is_compile_commands_stale(compile_commands, project_root)
        if stale:
            if explicit_cc:
                print(f"warning: explicit compile_commands.json is stale ({'; '.join(stale_reasons)})", file=sys.stderr)
            else:
                print(f"compile_commands.json is stale ({'; '.join(stale_reasons)}), rebuilding...")
                try:
                    compile_commands_new = generate_compile_commands(project_root, cfg.build)
                    print(f"Generated: {compile_commands_new}")
                    compile_commands = compile_commands_new
                except RuntimeError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    print("  Run 'fw-context index --build' to retry.", file=sys.stderr)
                    return compile_commands, None, False

    issues = validate_and_fix(compile_commands, project_root, builder_instance, cfg.build, fix=not bg)
    errors = [i for i in issues if i.severity == "error"]
    for w in [i for i in issues if i.severity == "warning"]:
        print(f"warning: {w.message}", file=sys.stderr)

    if errors:
        for e in errors:
            print(f"error: {e.message}", file=sys.stderr)
        if not bg and not explicit_cc and not args.build:
            print("Rebuilding to fix issues...")
            try:
                compile_commands_new = generate_compile_commands(project_root, cfg.build)
                print(f"Generated: {compile_commands_new}")
                issues = validate_and_fix(compile_commands_new, project_root, builder_instance, cfg.build, fix=True)
                errors = [i for i in issues if i.severity == "error"]
                for i in issues:
                    if i.severity == "warning":
                        print(f"warning: {i.message}", file=sys.stderr)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return compile_commands, None, False
        if errors:
            for e in errors:
                print(f"error: {e.message}", file=sys.stderr)
            if explicit_cc:
                # `--build` would replace the file of the user in the index.
                print("Repair the compile_commands.json that you gave, then run 'fw-context index'.", file=sys.stderr)
            elif bg:
                print("Run 'fw-context index' to resolve issues.", file=sys.stderr)
            else:
                print("Run 'fw-context index --build' to rebuild and fix issues.", file=sys.stderr)
            return compile_commands, None, False

    return compile_commands, build_dir_patterns, True


# How long a run that was asked to stop (SIGTERM) gets to release the index
# lock, and how long a run that got SIGKILL gets after that.  A cooperative
# run stops at its next translation unit or at the next Python bytecode after
# a C call returns — a single libclang parse or an HTTP request to Ollama can
# hold it for longer than that, which is what SIGKILL is for.
_TAKEOVER_GRACE_S = 10.0
_KILL_GRACE_S = 5.0


def _process_argv(pid: int) -> list[str] | None:
    """Return the argv of *pid*, or None when it cannot be read.

    ``/proc/<pid>/cmdline`` where it exists (Linux).  Elsewhere — macOS has no
    ``/proc`` — ``ps -o command=``.  The ``/proc``-only version answered None
    for every process on macOS, so no index run was ever recognised there and
    the takeover never happened.

    ``ps`` joins argv with spaces, so an argument that contains a space is
    split.  The caller only looks for whole words (``index``,
    ``--background``) and for a program name suffix, which a split does not
    create.
    """
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        # /proc joins argv with NULs.
        return [arg for arg in proc_cmdline.read_bytes().decode(errors="replace").split("\0") if arg]
    except FileNotFoundError:
        if Path("/proc/self").exists():
            return None  # /proc works, so the process is gone
    except OSError:
        return None

    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.split() or None


def _index_run_kind(pid: int) -> str | None:
    """Return ``"background"`` or ``"foreground"`` for an fw-context index run.

    None for anything else: a dead PID, a recycled PID that now belongs to an
    unrelated process, another fw-context subcommand.  Only an index run may
    ever be signalled — a stale PID naming an unrelated service must not take
    it out.

    WHY the argv and not the process name: this used to compare
    ``/proc/<pid>/comm`` against ``("fw-context", "python", "python3")``.  A
    virtualenv interpreter is named for its version — ``python3.14`` here —
    so the comparison never matched.  Observed exactly that: a run started at
    16:42 was still going when a second one started at 17:04.
    """
    argv = _process_argv(pid)
    if not argv:
        return None
    if not any(arg.endswith(("fw-context", "fw_context_mcp.cli", "fw_context_mcp")) for arg in argv):
        return None
    if "index" not in argv:
        return None
    return "background" if "--background" in argv else "foreground"


def _signal_index_run(pid: int, sig: signal.Signals) -> None:
    """Send *sig* to *pid* when it is still an fw-context index run.

    Checked again right before each signal: the PID was read from the lock
    file earlier, and the process may have exited and the PID been reused
    since.
    """
    if _index_run_kind(pid) is None:
        return
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


# The marker that tells a run which SIGTERM is a takeover.  A signal handler
# cannot see the sender of a signal (Python gives it no siginfo), thus the run
# that takes over writes this file BEFORE it sends SIGTERM.  Without it, each
# SIGTERM read as a takeover: a CI timeout or a `kill` printed "Superseded"
# and exited 75, and the daemon retried work that somebody wanted stopped.
_TAKEOVER_MARKER = "reindex.takeover"


def _write_takeover_marker(db_dir: Path, victim: int) -> None:
    """Write ``{"taker": <this PID>, "victim": <victim>}`` atomically.

    Atomic, because the victim reads it in its signal handler at any time
    after the signal.  The victim is named, thus a SIGTERM that a different
    run gets at the same time does not read as a takeover.

    Another marker than ``reindex.pause`` and ``reindex.pid``.  The rule
    "write nothing before the lock is won" (see _acquire_index) keeps those
    two from naming a run that lost.  No code reads this file except the
    victim, and only to classify a signal, thus a run that loses harms
    nothing with it.
    """
    marker = db_dir / _TAKEOVER_MARKER
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}")
    temporary.write_text(json.dumps({"taker": os.getpid(), "victim": victim}), encoding="utf-8")
    os.replace(temporary, marker)


def _remove_takeover_marker(db_dir: Path) -> None:
    """Remove the takeover marker when this process wrote it."""
    marker = db_dir / _TAKEOVER_MARKER
    if _read_takeover_marker(marker).get("taker") == os.getpid():
        marker.unlink(missing_ok=True)


def _read_takeover_marker(marker: Path) -> dict[str, int]:
    """Return the marker as a dict, or an empty dict when it is missing or bad."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, int)}


def _taken_over(db_dir: Path) -> bool:
    """Return True when a process that still runs takes the index from this process.

    The check is that the taker runs, not that it is an index run (see
    _index_run_kind): that check reads the argv, and a signal handler must
    stay short and must not start ``ps`` on macOS.  The rest risk is a
    marker that a dead taker left, whose PID the system gave to another
    process, and that names THIS process as the victim.  Then a foreign
    SIGTERM reads as a takeover, and the run exits 75 instead of 143.
    """
    data = _read_takeover_marker(db_dir / _TAKEOVER_MARKER)
    taker = data.get("taker")
    if data.get("victim") != os.getpid() or taker is None or taker == os.getpid():
        return False
    return PidFile._pid_exists(taker)


def _acquire_index(db_dir: Path, stack: ExitStack, args: argparse.Namespace) -> None:
    """Enter ``index_run_lock`` on *stack*, taking the index over if needed.

    A manual run always wins over a background one: the daemon starts those
    and starts another when the work is still needed (the killed run exits
    ``EXIT_SUPERSEDED``, which the daemon retries).  A manual run wins over
    another MANUAL run only with ``--takeover`` — that one belongs to a person.
    A background run takes over nothing.

    The takeover asks first: SIGTERM makes the holder raise IndexSuperseded
    (see _owned_index), which unwinds its ``finally`` blocks and drops the
    markers and the lock.  A holder that does not release the lock within
    _TAKEOVER_GRACE_S — stuck in a long C call, or in a handler that
    swallowed the exception — gets SIGKILL.  The kernel drops a flock
    however the process exits.

    Writes nothing before the lock is won.  A run that wrote the pause marker
    and then lost the lock would leave a marker naming a live process, and
    the run that won would read it as "somebody took the index over" and
    abandon itself — see _claim_index().

    Raises IndexRunLocked when the holder may not be taken over, when it
    survived SIGKILL, or when a third process won the lock in between.
    """
    from ..indexer.db._locking import IndexRunLocked, index_run_lock

    try:
        stack.enter_context(index_run_lock(db_dir))
        return
    except IndexRunLocked as exc:
        pid = exc.holder_pid
        if getattr(args, "background", False) or pid is None or pid == os.getpid():
            raise
        kind = _index_run_kind(pid)
        if kind is None:
            raise
        if kind == "foreground" and not getattr(args, "takeover", False):
            raise IndexRunLocked(
                db_dir, pid, hint="a foreground run; use --takeover to terminate it and index anyway"
            ) from None

    print(f"Taking over the index from pid {pid} ({kind} run)", file=sys.stderr)
    # The marker stays until this run releases the index, not only until it
    # wins the lock.  A holder that released the lock before the signal
    # arrived reads the marker in its handler after that, and the marker
    # must still be there, or that holder takes the signal as a foreign one
    # and dies (see _SigtermOutsideTheLock).
    _write_takeover_marker(db_dir, pid)
    won = False
    try:
        _signal_index_run(pid, signal.SIGTERM)
        won = _wait_for_the_lock(db_dir, stack, pid)
    finally:
        if won:
            stack.callback(_remove_takeover_marker, db_dir)
        else:
            _remove_takeover_marker(db_dir)
    if not won:
        raise IndexRunLocked(db_dir, pid, hint="it did not release the index even after SIGKILL")


def _wait_for_the_lock(db_dir: Path, stack: ExitStack, pid: int) -> bool:
    """Wait for *pid* to release the lock after SIGTERM, then SIGKILL it.

    Returns True when this process holds the lock on *stack*, and False
    when *pid* survived SIGKILL.  Raises IndexRunLocked when a third
    process won the lock in between.
    """
    from ..indexer.db._locking import IndexRunLocked, index_run_lock

    for grace, escalate in ((_TAKEOVER_GRACE_S, True), (_KILL_GRACE_S, False)):
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                stack.enter_context(index_run_lock(db_dir))
                return True
            except IndexRunLocked as again:
                # None: the holder is between truncating its PID and
                # unlocking, or a new holder has not written its PID yet.
                if again.holder_pid not in (pid, None):
                    raise
            time.sleep(0.1)
        if escalate:
            print(f"  pid {pid} did not stop within {grace:.0f}s — sending SIGKILL", file=sys.stderr)
            _signal_index_run(pid, signal.SIGKILL)
    return False


@contextmanager
def _owned_index(db_dir: Path, args: argparse.Namespace) -> Iterator[None]:
    """Own the index for the whole block: lock, markers, SIGTERM → superseded or terminated.

    The ONE place where ``cmd_index`` takes the index, for the single build
    and for ``[[build.variants]]`` alike.  The two used to do it separately,
    and the single build did it only AFTER building — a run that was then
    refused had already run ``pio run --target clean`` and rewritten
    compile_commands.json under the run that held the index.

    Inside the block SIGTERM unwinds the run normally.  A run that another
    one takes over (see _acquire_index, and the takeover marker) raises
    IndexSuperseded and exits ``EXIT_SUPERSEDED``.  Any other SIGTERM raises
    IndexTerminated and exits ``EXIT_TERMINATED``.  The handler ignores a
    second SIGTERM, so the unwinding itself is not interrupted.  On exit the
    handler, the markers and the lock are released in that order.

    The handler goes in BEFORE the lock is taken.  The lock writes the PID
    of this run as soon as it is won, and a taker can read it and send its
    SIGTERM before _claim_index ends.  With the handler of the caller still
    in place, that SIGTERM was ignored as "a takeover after the release",
    and the taker had to use SIGKILL.
    """
    from ..indexer.runner import IndexSuperseded, IndexTerminated

    def _stopped(signum: int, frame: object) -> None:
        # SIGHUP too: a signal during the unwinding would interrupt it.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if _taken_over(db_dir):
            raise IndexSuperseded("terminated by another index run that took the index over")
        raise IndexTerminated("a SIGTERM that no index run sent stopped this run")

    def _hung_up(signum: int, frame: object) -> None:
        # The terminal closed, or the ssh session dropped.  The build runs in
        # its own session (see utils.run_in_process_group), thus the kernel
        # no longer sends it SIGHUP with the terminal.  The default action
        # stopped this process with no cleanup, and the build ran on.  Now
        # the run unwinds, and the unwinding stops the build group.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise IndexTerminated("the terminal closed (SIGHUP)")

    previous = signal.signal(signal.SIGTERM, _stopped)
    # A SIGHUP that the caller ignores stays ignored: `nohup fw-context index`
    # asks the run to live on after the terminal closes, and the build does
    # (it has its own session).
    previous_hup = signal.getsignal(signal.SIGHUP)
    if previous_hup is not signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _hung_up)

    def _restore_handler() -> None:
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
        signal.signal(signal.SIGHUP, signal.SIG_DFL if previous_hup is None else previous_hup)

    with ExitStack() as stack:
        try:
            _acquire_index(db_dir, stack, args)
            # Before _claim_index: a failure or a signal between its two
            # writes left reindex.pause with the PID of a dead run.
            # _release_index removes only the markers of this process, thus
            # it is safe for a marker that _claim_index did not write yet.
            stack.callback(_release_index, db_dir)
            _claim_index(db_dir)
            # The index is ours now, thus a build that a dead owner left is
            # ours to stop, before this run builds in the same directory.
            _reap_orphan_build_group(db_dir)
        except BaseException:
            # Before the lock is released: a late SIGTERM of a takeover must
            # reach the handler of the caller, not _stopped.
            _restore_handler()
            raise
        stack.enter_context(record_build_groups(db_dir / _BUILD_GROUP_RECORD))
        # Registered last, thus it runs first: the handler of the caller is
        # back before the markers and the lock go.
        stack.callback(_restore_handler)
        yield


# The file where a build of the index run records its process group — see
# utils.record_build_groups.
_BUILD_GROUP_RECORD = "reindex.build"


def _reap_orphan_build_group(db_dir: Path) -> None:
    """Stop the build that a run which SIGKILL (or the OOM killer) stopped left.

    Such a run could not stop its build: the build runs in its own process
    group (see utils.run_in_process_group), and nothing stops it when the
    index run dies.  Its compilers then wrote into the build directory while
    this run cleaned and filled it.

    Call this function ONLY while this process holds index_run_lock.  Then
    no other live index run owns the record, and a record whose index run
    still runs is a leftover of no one.

    A PID can come back for an unrelated process, thus four checks come
    before the signal:

    1. The index run of the record is no index run any more (see
       _index_run_kind).  A live PID alone is not sufficient: the system can
       give the PID of the dead run to an unrelated process.
    2. The group still has members.
    3. When the leader still runs, its argv names the recorded program
       (argv[0], or argv[1] for a script that an interpreter runs).
    4. When the leader still runs, it has the recorded start time.  When
       the start time cannot be read for a live leader, nothing is signalled
       (fail-closed): the name alone does not identify a ``bash`` leader.

    A group whose leader exited cannot get a new process under its PGID
    while a member lives, thus it needs no leader checks.  Such a group can
    also be a member that a build left on purpose (a compiler server); it
    stops too, because it writes to the build directory of this run.
    """
    from ..utils import stop_process_group

    record = db_dir / _BUILD_GROUP_RECORD
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        record.unlink(missing_ok=True)
        return
    if not isinstance(data, dict):
        record.unlink(missing_ok=True)
        return
    index_pid, pgid, leader = data.get("index_pid"), data.get("pgid"), data.get("leader")
    if not isinstance(index_pid, int) or not isinstance(pgid, int) or not isinstance(leader, str):
        record.unlink(missing_ok=True)
        return
    if index_pid != os.getpid() and _index_run_kind(index_pid) is not None:
        return
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        record.unlink(missing_ok=True)
        return
    except PermissionError:
        # The group belongs to another user, thus not to an index run of ours.
        record.unlink(missing_ok=True)
        return
    argv = _process_argv(pgid)
    if argv is not None and leader not in {Path(arg).name for arg in argv[:2]}:
        record.unlink(missing_ok=True)
        return
    # The name alone is not sufficient: a build under `activate` has "bash"
    # as its leader, and so has the shell of a new terminal that got the
    # PID.  A live leader must also have the recorded start time.
    if argv is not None:
        started = data.get("started")
        current = process_start_time(pgid)
        if not isinstance(started, str) or current is None or current != started:
            record.unlink(missing_ok=True)
            return
    print(
        f"Stopping process group {pgid}: an earlier index run started it, "
        "and it still runs in the build directory",
        file=sys.stderr,
    )
    stop_process_group(pgid)
    record.unlink(missing_ok=True)


def _claim_index(db_dir: Path) -> None:
    """Write the pause marker and the pid marker.

    Call this function ONLY while the caller holds index_run_lock.  The pause
    marker stops the file-watcher daemon from starting a background run, and
    the pid marker tells every other process which run owns the index.  Both
    are claims, and a process that lost the lock owns nothing.

    Creates no directory: index_run_lock already does that before it takes
    the lock.
    """
    PidFile(db_dir / "reindex.pause").write()
    PidFile(db_dir / "reindex.pid").write()


def _release_index(db_dir: Path) -> None:
    """Drop the markers this process wrote, and only those.

    unlink_if_ours checks the PID, so a marker another run owns survives.
    """
    PidFile(db_dir / "reindex.pid").unlink_if_ours()
    PidFile(db_dir / "reindex.pause").unlink_if_ours()


def _post_index_optimize(
    db_path: Path,
    project_root: Path,
    project_id: str,
    detected_system: str | None,
    args: argparse.Namespace,
) -> None:
    """Run PRAGMA optimize and update the global project registry."""
    try:
        import sqlite3 as _sqlite3

        _opt_conn = _sqlite3.connect(str(db_path))
        _opt_conn.execute("PRAGMA optimize")
        _opt_conn.close()
    except _sqlite3.Error:
        log.debug("PRAGMA optimize failed for %s", db_path, exc_info=True)

    from ..config.global_db import (
        open_global_db,
        prune_registry_report,
        upsert_project_registry,
    )

    _ptype = detected_system or "unknown"
    _gconn = open_global_db()
    try:
        upsert_project_registry(
            _gconn, project_id, getattr(args, "name", None) or project_root.name, _ptype, str(project_root)
        )
        # The registry only ever grew.  An index run is a good moment to
        # drop what nothing on disk answers for any more — it already
        # writes the row of THIS project, and a stale row of another one
        # costs the `project="<name>"` selector an ambiguity error.
        #
        # ``--no-prune`` names the rows and leaves them: this is a DELETE
        # over the data of the operator, thus it has to be refusable.
        #
        # A ``--background`` run prunes NOTHING.  The file-watcher daemon
        # spawns it with a fixed argv (``mcp/daemon.py``), thus no flag can
        # reach it, and its output goes to reindex.log where nobody reads
        # it.  A delete that the operator can neither refuse nor see is not
        # a tidy-up.  The next run that the operator starts does the work.
        if getattr(args, "background", False):
            log.debug("registry: background run — the prune is left to the operator")
        elif (_msg := prune_registry_report(
            _gconn, keep=getattr(args, "no_prune", False)
        )):
            print(f"  {_msg}")
    finally:
        _gconn.close()


def _select_variants(variants: list, args: argparse.Namespace) -> list:
    """Return the variants selected by --variant/--variants CLI filters."""
    if not variants:
        return []
    requested: set[str] | None = None
    if getattr(args, "variant", None):
        requested = {args.variant}
    elif getattr(args, "variants", None):
        requested = {v.strip() for v in args.variants.split(",") if v.strip()}
    if requested is None:
        return list(variants)
    selected = [v for v in variants if v.name in requested]
    missing = requested - {v.name for v in selected}
    for m in sorted(missing):
        print(f"error: variant '{m}' not found in [[build.variants]]", file=sys.stderr)
    return selected


def _find_variant(variants: list, name: str):
    for v in variants:
        if v.name == name:
            return v
    return None


def _effective_board(build_cfg, variant, image: str) -> str:
    """Resolve the concrete board per-(variant, image).

    Per-image override wins (FLPR → cpuflpr), else the variant default board,
    else the shared ``[build] board``.
    """
    for img in variant.images:
        if img.name == image and img.board:
            return img.board
    return variant.board or build_cfg.board or ""


def _is_narrowed(args: argparse.Namespace) -> bool:
    """Say if ``--variant``, ``--variants``, ``--image`` or ``--exclude-image`` limits the run."""
    return bool(
        getattr(args, "variant", None) or getattr(args, "variants", None)
        or getattr(args, "image", None) or getattr(args, "exclude_image", None)
    )


def _pair_names(pairs: list[tuple[str, str]]) -> str:
    """Give ``variant/image`` for each pair, and "(no variant)" for the build without variants."""
    return ", ".join("/".join(part for part in pair if part) or "(no variant)" for pair in pairs)


def _filter_images(cc_list: list, args: argparse.Namespace) -> list:
    """Apply --image (inclusive) and --exclude-image (exclusive) index filters."""
    include = getattr(args, "image", None)
    exclude = set(getattr(args, "exclude_image", None) or [])
    out = []
    for item in cc_list:
        _, image, _, _ = item
        if image and image in exclude:
            continue
        if include and image != include:
            continue
        out.append(item)
    return out


def _unknown_images(cc_list: list, args: argparse.Namespace) -> str:
    """Return an error line for an --image or --exclude-image name that no build makes, or "".

    A name that the build does not make is a typo or a stale command line.
    --exclude-image ignored it without a word, and --image indexed nothing.
    """
    known = sorted({image for _, image, _, _ in cc_list if image})
    asked = [getattr(args, "image", None) or "", *(getattr(args, "exclude_image", None) or [])]
    unknown = sorted({name for name in asked if name and name not in known})
    if not unknown:
        return ""
    return (
        f"error: no build makes the image(s) {', '.join(unknown)}. "
        f"The images are: {', '.join(known) or '(none)'}."
    )


def _discover_existing_cc(project_root, variants: list, build_cfg, builder) -> list:
    """Return ``(variant, image, database, board)`` of each build that is on disk.

    Each variant has its output directory, ``.fw-context/build/<variant>/out``
    (see ``build_layout``), and the backend says which databases its build
    left there (``builders.output_compile_commands``).  A variant without a
    build gets no entry; the caller builds it or reports it.
    """
    from ..indexer.build import build_variant_config
    from ..indexer.build_layout import BuildLayout
    from ..indexer.builders import application_database, output_compile_commands

    layout = BuildLayout(project_root)
    found: list = []
    for variant in variants:
        vcfg = build_variant_config(build_cfg, variant)
        out_dir = layout.out_dir(variant.name)
        databases = output_compile_commands(builder, out_dir, vcfg)
        application = application_database(builder, out_dir)
        # The application last, as the run that builds orders them
        # (`_images_of_build`): get_active_config gives the newest build.
        for image, cc in sorted(databases.items(), key=lambda item: (item[1] == application, item[0])):
            found.append((variant.name, image, cc, _effective_board(build_cfg, variant, image)))
    return found


def _build_variants(project_root: Path, build_cfg, builder, variants: list) -> list:
    """Build *variants* and return ``(variant, image, database, board)`` of each build.

    sysbuild is a [build] key that a variant can override, thus the choice
    of the build command is made for each variant, with the config that the
    build of that variant uses.  The discovery of its images
    (``builders.output_compile_commands``) reads the same config.  A
    variant whose build fails gets an error line and no entry: one failed
    build does not stop the others.
    """
    from ..indexer.build import build_variant_config, generate_compile_commands
    from ..indexer.build_layout import BuildLayout

    found: list = []
    build_multi = getattr(builder, "build_multi", None)
    multi = [
        v for v in variants
        if build_multi is not None and build_variant_config(build_cfg, v).sysbuild
    ]
    if build_multi is not None and multi:
        for variant, image, path in build_multi(project_root, build_cfg, multi):
            v = _find_variant(variants, variant)
            board = _effective_board(build_cfg, v, image) if v else ""
            found.append((variant, image, path, board))
    multi_names = {v.name for v in multi}
    for variant in variants:
        if variant.name in multi_names:
            continue
        # Each variant builds into its own output directory, thus the
        # variants cannot overwrite each other.
        vcfg = build_variant_config(build_cfg, variant)
        try:
            path = generate_compile_commands(project_root, vcfg)
        except RuntimeError as exc:
            print(f"error: variant '{variant.name}': {exc}", file=sys.stderr)
            continue
        # One build can make several programs (ESP-IDF: the application and
        # its bootloader).  The run without a build finds them through the
        # same backend method, thus a variant has the same images either way.
        images = _images_of_build(builder, BuildLayout(project_root).out_dir(variant.name), vcfg, path)
        if images is None:
            continue
        for image, cc in images:
            found.append((variant.name, image, cc, _effective_board(build_cfg, variant, image)))
    return found


def _run_multi(
    args,
    cfg,
    project_root,
    project_id,
    db_path,
    detected_system,
    run_kwargs,
) -> int:
    """Orchestrate indexing of all (variant, image) builds.

    Builds each variant (sysbuild via ``build_multi``, else per-variant
    ``build()``), indexes each (variant, image) with deferred FTS/cleanup,
    then runs the FTS rebuild and per-(variant, image) retention once at the
    end (§5.8).  A failure in one build does not abort the others.
    """
    from ..indexer._postprocess import cleanup_old_builds_multi, cleanup_retired_builds
    from ..indexer.build_layout import InvalidVariantName, check_variant_names
    from ..indexer.builders import registry as builder_registry
    from ..indexer.db import open_db, rebuild_files_fts, rebuild_fts, rebuild_macros_fts
    from ..indexer.runner import IndexStopped, run

    build_cfg = cfg.build
    variants = _select_variants(build_cfg.variants, args)
    if not variants:
        print("error: no variants selected", file=sys.stderr)
        return 1

    system = build_cfg.system or detected_system
    builder_cls = builder_registry.get(system) if system else None
    builder = builder_cls() if builder_cls else None

    cc_list: list[tuple[str, str, Path, str]] = []

    # Each variant names one directory, .fw-context/build/<variant> (see
    # build_layout), thus a name that cannot be a directory stops the run
    # before any build, and so do two names of one directory.
    try:
        check_variant_names(v.name for v in build_cfg.variants)
    except InvalidVariantName as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.build:
        cc_list = _build_variants(project_root, build_cfg, builder, variants)
    else:
        # A variant without a build is built here, as the build without
        # variants is (`_resolve_compile_commands`): the index needs the
        # build, and a run without `--build` must not stop where one with it
        # goes on.  A background run builds only with a backend that may
        # build on its own.
        cc_list = _discover_existing_cc(project_root, variants, build_cfg, builder)
        found_names = {name for name, _, _, _ in cc_list}
        missing = [v for v in variants if v.name not in found_names]
        if missing and getattr(args, "background", False) and not _may_build_in_background(cfg, detected_system):
            for variant in missing:
                print(
                    f"error: no build for variant '{variant.name}', and fw-context cannot run it "
                    "in the background — run 'fw-context index --build'",
                    file=sys.stderr,
                )
        elif missing:
            print(f"No build for variant(s) {', '.join(v.name for v in missing)} — running the build")
            cc_list += _build_variants(project_root, build_cfg, builder, missing)

    unknown = _unknown_images(cc_list, args)
    if unknown:
        print(unknown, file=sys.stderr)
        return 1
    cc_list = _filter_images(cc_list, args)

    if args.no_index:
        print(f"Built {len(cc_list)} build(s) — skipping indexing (--no-index)")
        return 0

    if not cc_list:
        print("error: no builds to index", file=sys.stderr)
        return 1

    touched_pairs: list[tuple[str, str]] = []
    failed_variants: set[str] = set()
    for variant_name, image, cc_path, board in cc_list:
        v = _find_variant(variants, variant_name)
        env = dict(build_cfg.env)
        if v is not None:
            env.update(v.env)
        # The build of each variant is in the build tree of fw-context, thus
        # the fw-context directory is the whole build output.
        build_dir_patterns = build_dir_patterns_with_fw_context([]) if v is not None else None
        # The path layers are summed per build, not once for the command:
        # two variants may vendor different in-tree trees.  run_kwargs holds
        # the [index] layer (or the CLI flag, which replaces it).
        per_build = dict(run_kwargs)
        idx = v.index_overrides if v is not None else {}
        for key in ("vendor_paths", "project_paths"):
            extra = list(idx.get(key) or [])
            per_build[key] = _layered_paths(list(run_kwargs[key]), extra)
            if extra:
                print(
                    f"  {variant_name}: {key} = "
                    f"{len(run_kwargs[key])} from [index] + {len(extra)} from variant"
                )
        try:
            ch = run(
                compile_commands=cc_path,
                db_path=db_path,
                variant=variant_name,
                image=image,
                board=board,
                build_env=env,
                build_dir_patterns=build_dir_patterns,
                defer_fts=True,
                defer_cleanup=True,
                **per_build,
            )
        except IndexStopped as exc:
            # Another process owns the index now, or a SIGTERM asked this run
            # to stop.  The remaining builds would stop on the same cause, so
            # stop the sweep here — a partial sweep would leave some builds
            # indexed against a database the other process is changing.
            # IndexStopped comes before the broad handler below, which would
            # otherwise swallow it and continue with the next build.
            print(f"{exc.label}: {exc}", file=sys.stderr)
            return exc.exit_code
        except Exception as exc:  # noqa: BLE001 — best-effort per-build
            print(f"error: indexing variant={variant_name} image={image}: {exc}", file=sys.stderr)
            failed_variants.add(variant_name)
            continue
        touched_pairs.append((variant_name, image))
        print(f"Indexed variant={variant_name} image={image} config_hash={ch[:16]}…")

    # Final: rebuild FTS once and run per-(variant, image) retention once.
    conn = open_db(db_path)
    fts_problems: list[str] = []
    try:
        rebuild_fts(conn)
        rebuild_files_fts(conn)
        rebuild_macros_fts(conn)
        # Each build skipped its own FTS check because the rebuild was
        # deferred to here — see _step_verify_integrity.  This is the one
        # place that can run it, and it has to: the FTS index backs
        # search_code, search_content and the macro search, and an index that
        # disagrees with its content table serves rows that are not there.
        from ..indexer._postprocess import fts_inconsistencies

        fts_problems = fts_inconsistencies(conn)
        deleted = cleanup_old_builds_multi(conn, project_id, db_path.parent, touched_pairs)
        if deleted:
            print(f"Cleaned up {deleted} stale build(s)")
        # Only a run over the whole set knows which builds the project no
        # longer makes; a narrowed run does not see the other builds.  A
        # variant with an image that failed to index keeps all its builds,
        # as a variant whose build failed does: its build without an image
        # name, from before the images, can be its only complete application.
        if not _is_narrowed(args):
            retired = cleanup_retired_builds(
                conn, project_id, db_path.parent,
                {v.name for v in variants},
                {
                    (variant, image) for variant, image, _, _ in cc_list
                    if variant not in failed_variants
                },
            )
            if retired:
                print(f"Removed the builds that the project no longer makes: {_pair_names(retired)}")
    finally:
        conn.close()

    if fts_problems:
        for problem in fts_problems:
            print(f"error: {problem}", file=sys.stderr)
        print(
            "error: the full-text index disagrees with its content after a "
            "rebuild — reindex with --force",
            file=sys.stderr,
        )
        return 1

    _post_index_optimize(db_path, project_root, project_id, system, args)
    return 0


def _layered_paths(base: list[str], variant_extra: list[str]) -> list[str]:
    """Return the ``[index]`` layer PLUS this variant's, in a stable order.

    Adds, never replaces.  A user with a value in ``[index]`` and in the
    variant would otherwise lose the shared entries without a word, which is
    the same class of silent wrongness this series of changes is about.  The
    precedent is already in the repo: ``_DICT_FIELDS`` in build.py merges
    ``env`` per key, and says so.

    The cost, and it is real: a variant cannot NARROW the set.  The channels
    for that are ``project_paths``, which wins over every vendor pattern, or
    a fix to the detection.  No escape hatch is added.

    ``dict.fromkeys`` and not ``set``: the order shows up in the LIKE filters,
    in the log and in the manifest, and a stable order diffs cleanly.
    """
    return list(dict.fromkeys([*base, *variant_extra]))


def _build_run_kwargs(
    args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
) -> dict:
    """Build the shared ``runner.run()`` kwargs from config + CLI flags."""
    from ..indexer.build import detect_build_system

    return dict(
        vendor_paths=vendor_paths,
        project_paths=project_paths,
        project_name=args.name or cfg.project.name,
        index_refs=False if args.no_refs else cfg.index.index_refs,
        index_embeddings=(
            False
            if getattr(args, "no_embeddings", False)
            else getattr(args, "embeddings", None) or cfg.index.index_embeddings
        ),
        analyze_symbols=(
            False
            if getattr(args, "no_analyze", False)
            else getattr(args, "analyze", False) or cfg.llm.analyze_symbols
        ),
        analyze_overrides=True,
        project_root=project_root,
        project_id=project_id,
        llm_config=cfg.llm,
        cache_server_config=cs_config,
        config_header=cfg.index.config_header,
        force=args.force,
        analyze_vendor=(
            False
            if getattr(args, "no_analyze_vendor", False)
            else getattr(args, "analyze_vendor", False) or cfg.llm.analyze_vendor
        ),
        purge_max_missing_percent=cfg.index.purge_max_missing_percent,
        build_system=cfg.build.system or detect_build_system(project_root),
    )


def _ensure_watcher_after_index(project_root: Path) -> None:
    """Start the watcher daemon after an index run that was successful.

    WHY: an MCP server spawns the daemon only at startup, and only when
    ``index.db`` already exists.  A server that starts before the first
    index takes an early return and spawns no daemon.  The daemon is the
    only component that starts a background reindex, thus that project
    gets no automatic reindex.

    This function closes that window.  After the index run, ``index.db``
    exists, which is the only precondition of ``_ensure_daemon_running``.
    The call does nothing when a daemon already runs.
    """
    try:
        from ..mcp.background import _ensure_daemon_running

        _ensure_daemon_running(project_root)
    except (RuntimeError, OSError):
        # Not fatal — the index is complete and usable.  Only the
        # automatic reindex of changed files is not available.  The
        # command `fw-context watch restart` corrects this.
        log.debug("Could not start the watcher daemon for %s", project_root, exc_info=True)


class _SigtermOutsideTheLock:
    """The SIGTERM handler of ``cmd_index`` while this run does not own the index.

    Inside the lock, _owned_index has its own handler, and it gives this one
    back when the run releases the index.

    WHY a handler here and not the default action: a run that takes over
    sends SIGTERM once, and the holder can release the lock between the
    check of the taker and the arrival of the signal.  With the default
    action that holder died with -15 in the middle of its work after the
    lock (the daemon start, the registry update), and the daemon logged it
    as a failure.  The taker only wants the index, and it has it, thus this
    handler ignores a SIGTERM of a takeover (see _taken_over).

    Any other SIGTERM stops the run with EXIT_TERMINATED.  SystemExit, not
    a signal to itself: ``finally`` blocks then run, and the status is the
    same as the one that _owned_index gives.

    ``db_dir`` is None until ``cmd_index`` knows the index directory.
    ``index_done`` is True once the run wrote its index: ``cmd_index`` then
    writes no "run failed" warning for an error of a later step, because the
    index is complete.
    """

    def __init__(self) -> None:
        self.db_dir: Path | None = None
        self.index_done = False

    def __call__(self, signum: int, frame: object) -> None:
        from ..exit_codes import EXIT_TERMINATED

        if self.db_dir is not None and _taken_over(self.db_dir):
            log.info("Ignored the SIGTERM of a takeover: this run no longer owns the index")
            return
        # os.write, not print: the signal can arrive while the main code
        # writes to sys.stderr, and a second write to that buffered stream
        # then raises "reentrant call" instead of this SystemExit.
        os.write(2, b"Terminated: a SIGTERM that no index run sent stopped this run\n")
        raise SystemExit(EXIT_TERMINATED)


def cmd_index(args: argparse.Namespace) -> int:
    """Build or rebuild the symbol index from compile_commands.json.

    By default, reuses an existing ``compile_commands.json`` for fast
    incremental indexing.  When the file is missing, a clean build is
    triggered automatically (auto-detecting Mbed OS / Zephyr / PlatformIO).

    Pass ``--build`` to force a fresh build and full re-index.

    WHY: Most invocations are incremental (user edits a file and runs
    ``fw-context index``).  Forcing a build on every run would be
    wasteful — Mbed/zephyr builds can take minutes.  The auto-detect
    logic only triggers a build when necessary.

    The SIGTERM handler of the whole command is _SigtermOutsideTheLock, and
    the handler of the caller comes back when the command ends.

    An exception that stops the run writes the MCP warning as a non-zero
    exit does, then goes on to the caller unchanged.  WHY the broad catch:
    this is the boundary of the command, and any error that stops the run
    leaves the index of an earlier run, which the caller must know of.  It
    applies only after the run knows its index (``outside.db_dir``); an
    error before that point stops the run before it touches the index.  It
    does not apply after the run wrote its index (``outside.index_done``).
    """
    outside = _SigtermOutsideTheLock()
    previous = signal.signal(signal.SIGTERM, outside)
    try:
        return _cmd_index(args, outside)
    except Exception as exc:
        if outside.db_dir is not None and not outside.index_done:
            autobuild.record_problem(outside.db_dir, _run_failed_text(
                f"{type(exc).__name__}: {exc}", bool(getattr(args, "background", False)),
                outside.db_dir,
            ))
        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)


def _cmd_index(args: argparse.Namespace, outside: _SigtermOutsideTheLock) -> int:
    """The body of :func:`cmd_index`, under its SIGTERM handler."""
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED, EXIT_TERMINATED
    from ..indexer.build import detect_build_system
    from ..indexer.db._locking import IndexRunLocked
    from ..indexer.runner import IndexStopped
    from ..utils import resolve_project_root

    if args.verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(VerboseFormatter())
        logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )

    # Suppress httpx INFO logs (one per HTTP request — extremely noisy during
    # embedding generation, LLM analysis, and cache server communication).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    bg = getattr(args, "background", False)
    # The config wins over the markers, the same form _run_multi already
    # uses.  The two can disagree: a freestanding NCS application has
    # CMakeLists.txt and no west.yml, so a marker scan calls it a CMake
    # project.  This value also decides which builder generates and
    # validates compile_commands.json, not only the vendor patterns.
    detected_system = cfg.build.system or detect_build_system(project_root)

    if detected_system:
        print(f"Project: {project_root.name}  path={project_root}  build={detected_system}")
    elif bg:
        from ..indexer.build import default_compile_commands

        if default_compile_commands(project_root, cfg).exists():
            print(f"Project: {project_root.name}  path={project_root}  build=unknown (bg, reusing cc)")
        else:
            print(f"error: No build system detected and no compile_commands.json for {project_root}.", file=sys.stderr)
            print("  Run 'fw-context index --build' first.", file=sys.stderr)
            return 1
    else:
        print(f"Project: {project_root.name}  path={project_root}  build=unknown")

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    # A retired key stops the run before any build.  Each build goes to
    # .fw-context/build/<variant>/out, and a config that still names another
    # directory says something that the build does not do.  The marker gives
    # the same text to each MCP answer, because a background run has no
    # terminal.
    from ..indexer.build import retired_build_dir_keys

    retired = retired_build_dir_keys(cfg.build)
    if retired:
        text = (
            f"{', '.join(retired)} in .fw-context/config.toml has no effect: each build "
            "goes to .fw-context/build/<variant>/out. Remove the key, then run "
            "'fw-context index --build'."
        )
        print(f"error: {text}", file=sys.stderr)
        autobuild.record_problem(db_path.parent, text)
        return 1

    # Each variant builds its own database, thus [index] compile_commands
    # with a database of the user would have no effect for them: an error,
    # and not a file that the run ignores without a word.
    from ..indexer.build import explicit_compile_commands, user_database

    if cfg.build.variants and user_database(project_root, cfg) is not None:
        text = (
            "[index] compile_commands names a database of your own, and [[build.variants]] build "
            "their own databases. Remove one of the two from .fw-context/config.toml."
        )
        print(f"error: {text}", file=sys.stderr)
        autobuild.record_problem(db_path.parent, text)
        return 1

    # A project file that holds more than one build (each [env:<name>] of
    # platformio.ini) gives one variant for each, when the config declares
    # none.  The build system answers, thus the set is the set that its
    # build makes.  An explicit compile_commands.json (on the command line,
    # or in [index] compile_commands) and a [build] command name their build
    # themselves.
    if (
        not cfg.build.variants and not cfg.build.command
        and not getattr(args, "compile_commands", None)
        and explicit_compile_commands(project_root, cfg) is None
    ):
        from ..indexer.builders import implicit_variants
        from ..indexer.builders import registry as builder_registry

        builder_cls = builder_registry.get(detected_system) if detected_system else None
        try:
            found = implicit_variants(builder_cls() if builder_cls else None, project_root, cfg.build)
        except (RuntimeError, OSError) as exc:
            # OSError: the program of [build] python or of pio is not there.
            text = f"cannot list the builds of the project: {exc}"
            print(f"error: {text}", file=sys.stderr)
            autobuild.record_problem(db_path.parent, text)
            return 1
        if found:
            cfg.build.variants = found
            print(f"The project builds {len(found)} variants: {', '.join(v.name for v in found)}")
            if not cfg.build.default_variant:
                print(
                    "  A query without `variant` needs [build] default_variant in "
                    ".fw-context/config.toml, for example: default_variant = "
                    f'"{found[0].name}"'
                )

    # A compile_commands.json that the user names is the input of this run.
    # No automatic build may replace it: `--build` comes before the explicit
    # file in `_resolve_compile_commands`, thus a build would drop the file.
    # The user names it on the command line, or in [index] compile_commands
    # for a build that fw-context can run (`build.user_database`).
    explicit_cc = bool(getattr(args, "compile_commands", None)) or user_database(project_root, cfg) is not None
    # The file of the active index: only the build of that file is checked
    # (`build.checked_compile_commands`).
    indexed_cc = _indexed_compile_commands(project_root, db_path)

    # ── A run of the user without a build: build first ──
    # The run asks the compiler of each unit in the build directory, thus a
    # missing build gives units without the answer of the compiler.  The
    # user started this run, thus it builds as `--build` does.  A background
    # run takes the path below, which builds only with a backend that may
    # build on its own.
    if not bg and not getattr(args, "build", False) and not explicit_cc and not cfg.build.variants:
        _build_if_missing(args, project_root, cfg, detected_system, indexed_cc)

    # ── Automatic build: a missing build, a source that the build never saw,
    # or another branch ──
    # A plain reindex cannot repair any of them.  `_plan_auto_build` returns
    # a non-empty list only when the backend may build on its own.
    auto_build_sources: list[str] = []
    if not getattr(args, "build", False) and not explicit_cc:
        auto_build_sources, auto_build_cfg, auto_build_reason = _plan_auto_build(
            project_root, db_path, cfg, detected_system, background=bg
        )
        # The two always arrive together; the second test is what lets the
        # type checker see that, and it costs nothing.
        if auto_build_sources and auto_build_cfg is not None:
            args.build = True
            cfg.build = auto_build_cfg
            log.info("%s", auto_build_reason)

    # ── A build that is not there, and that this run does not make ──
    # The run would index units with no answer of their compiler: wrong
    # system headers and macros, presented as a good index.  It stops with a
    # clear error instead.  A background run has no terminal: the marker
    # gives the same text to each query tool answer of the project.
    if not getattr(args, "build", False) and not explicit_cc and not cfg.build.variants:
        refusal = _refuse_without_build(project_root, cfg, detected_system, indexed_cc)
        if refusal:
            print(f"error: {refusal}", file=sys.stderr)
            autobuild.record_problem(db_path.parent, refusal, build_missing=True)
            return 1

    # The CLI flag REPLACES the [index] layer.  A variant's own [index] keys
    # are added on top of whichever of the two won — see _layered_paths().
    vendor_paths = list(getattr(args, "vendor_paths", None) or cfg.index.vendor_paths)
    project_paths = list(getattr(args, "project_paths", None) or cfg.index.project_paths)

    cs_config = cfg.cache_server
    if cs_config is not None and getattr(args, "force", False):
        from dataclasses import replace
        cs_config = replace(cs_config, force=True)

    run_kwargs = _build_run_kwargs(
        args, cfg, project_root, project_id, vendor_paths, project_paths, cs_config,
    )
    multi = bool(cfg.build.variants)

    # The index is owned BEFORE anything is built, on both paths below.  A
    # build run under another index run cleans the build directory and
    # rewrites compile_commands.json under it, and a run that is refused
    # afterwards has spent the whole build for nothing.  _owned_index takes
    # over from a background run, and from a foreground one with --takeover.
    outside.db_dir = db_path.parent
    try:
        with _owned_index(db_path.parent, args):
            # ── Multi-variant: dispatch BEFORE single-build resolution ──
            # WHY: [[build.variants]] replaces the single [build] board/source
            # flow.  build_multi() builds each variant; the single-build path
            # (generate_compile_commands → builder.build()) would fail on a
            # project whose board lives per-variant (Zephyr "requires a board
            # name").  The index stays owned across every (variant, image)
            # build: a second run slipping in between two builds would index
            # against a database this one is still changing.
            if multi:
                exit_code = _run_multi(
                    args, cfg, project_root, project_id, db_path,
                    detected_system, run_kwargs,
                )
            else:
                exit_code = _run_single(
                    args, cfg, project_root, project_id, db_path,
                    detected_system, bg, run_kwargs,
                )
    except IndexRunLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except IndexStopped as exc:
        # Not a failure — see IndexStopped.  The distinct exit codes let the
        # daemon retry a superseded run, and not a terminated one, instead
        # of treating either as a broken run.
        print(f"{exc.label}: {exc}", file=sys.stderr)
        exit_code = exc.exit_code

    # Everything below runs outside the index lock.  The daemon does a
    # staleness check when it starts, thus a daemon that starts inside the
    # lock could start a second index run against the database that this run
    # still changes.
    if exit_code != 0:
        # The failed step printed its error.  The marker gives the LLM the
        # same news in each query tool answer, because a background run has no
        # terminal and a run of the user can end before the LLM asks.  A run
        # that another run took over, or that a SIGTERM stopped, did not
        # fail: the run that follows decides.
        if exit_code not in (EXIT_SUPERSEDED, EXIT_TERMINATED):
            autobuild.record_problem(
                db_path.parent, _run_failed_text(f"exit code {exit_code}", bg, db_path.parent)
            )
        return exit_code

    outside.index_done = True

    # The run ended well, but the check is done again and not taken from
    # its start: another run can refuse, and write its marker, while this
    # one runs, and the build can go away meanwhile.  A plain clear would
    # then remove a true warning.  The file of the index is read again,
    # because this run wrote it.
    refusal = "" if explicit_cc else _refuse_without_build(
        project_root, cfg, detected_system, _indexed_compile_commands(project_root, db_path),
        at_the_end=True,
    )
    if refusal:
        autobuild.record_problem(db_path.parent, refusal, build_missing=True)
    else:
        autobuild.clear_problem(db_path.parent)

    if not multi and getattr(args, "build", False):
        _record_still_uncovered(project_root, db_path)

    _ensure_watcher_after_index(project_root)
    return 0


def _run_single(
    args: argparse.Namespace,
    cfg,
    project_root: Path,
    project_id: str,
    db_path: Path,
    detected_system: str | None,
    bg: bool,
    run_kwargs: dict,
) -> int:
    """Build (when needed), validate and index the single ``[build]``.

    Called ONLY inside _owned_index — the build writes the files the index
    run reads, so it has to be excluded the same way the indexing is.
    """
    from ..indexer.runner import IndexStopped, run

    # ── Resolve compile_commands.json ──
    cc_result = _resolve_compile_commands(args, project_root, cfg, detected_system, bg)
    if cc_result[0] is None:
        return 1
    compile_commands, explicit_cc = cc_result
    assert compile_commands is not None  # checked above via cc_result[0]

    # ── Validate build artifacts ──
    compile_commands, build_dir_patterns, ok = _validate_and_fix_artifacts(
        compile_commands, project_root, detected_system, cfg, bg, explicit_cc, args
    )
    if not ok:
        return 1
    assert compile_commands is not None  # _validate_and_fix_artifacts returns Path|None, but ok=True → non-None

    images = _single_build_images(project_root, cfg, detected_system, compile_commands, explicit_cc)
    if images is None:
        return 1
    if (getattr(args, "image", None) or getattr(args, "exclude_image", None)) and not any(
        image for image, _ in images
    ):
        print(
            "error: the build makes one program, and its image has no name, thus "
            "--image and --exclude-image name nothing. Run without them.",
            file=sys.stderr,
        )
        return 1
    unknown = _unknown_images([("", image, cc, "") for image, cc in images], args)
    if unknown:
        print(unknown, file=sys.stderr)
        return 1
    selected = [
        (image, cc) for _, image, cc, _ in _filter_images([("", image, cc, "") for image, cc in images], args)
    ]
    if not selected:
        print(
            f"error: no image to index. The build makes {', '.join(image for image, _ in images)}.",
            file=sys.stderr,
        )
        return 1

    failed: list[str] = []
    for image, cc in selected:
        try:
            config_hash = run(
                compile_commands=cc,
                db_path=db_path,
                build_dir_patterns=build_dir_patterns,
                image=image,
                **run_kwargs,
            )
        except IndexStopped:
            raise
        except Exception as exc:  # noqa: BLE001 — one image of several, as in _run_multi
            if len(selected) == 1:
                raise
            # The images are separate programs: a failure in the bootloader
            # must not keep the application out of the index.
            print(f"error: indexing image={image}: {exc}", file=sys.stderr)
            failed.append(image)
            continue
        label = f" image={image}" if image else ""
        print(f"Indexed{label}. config_hash={config_hash[:16]}…  db={db_path}")

    # The project has one build now, thus the builds of variants that an
    # earlier config (or an earlier platformio.ini) made are retired, and so
    # are the images that this build no longer makes.  A run that --image
    # narrowed does not see the other images.  A run with a failed image
    # retires nothing: the build of the image without a name, from before
    # the images, can be the only complete one.
    if not _is_narrowed(args) and not failed:
        from ..indexer._postprocess import cleanup_retired_builds
        from ..indexer.db import open_db

        conn = open_db(db_path)
        try:
            retired = cleanup_retired_builds(
                conn, project_id, db_path.parent, {""}, {("", image) for image, _ in selected},
            )
        finally:
            conn.close()
        if retired:
            print(f"Removed the builds that the project no longer makes: {_pair_names(retired)}")

    _post_index_optimize(db_path, project_root, project_id, detected_system, args)
    return 1 if failed else 0


def _single_build_images(
    project_root: Path,
    cfg,
    detected_system: str | None,
    compile_commands: Path,
    explicit_cc: bool,
) -> list[tuple[str, Path]] | None:
    """Return ``(image, database)`` of each program of the build without variants.

    Most builds make one program, and the result is ``[("", compile_commands)]``.
    ESP-IDF makes two, the application and its bootloader, and the backend
    names each with its database (``builders.output_compile_commands``).
    *compile_commands* is the database of the application, the one that this
    run built or validated, and it comes LAST: ``get_active_config`` gives the
    build that the index run did last, and the daemon and the staleness
    checks read that build.  A file that the user names is one program.

    None, with an error line, when the build names images and
    *compile_commands* is none of them: the output directory holds another
    build than the one that this run validated.
    """
    if explicit_cc:
        return [("", compile_commands)]

    from ..indexer.build_layout import BuildLayout
    from ..indexer.builders import registry as builder_registry

    builder_cls = builder_registry.get(detected_system) if detected_system else None
    return _images_of_build(
        builder_cls() if builder_cls else None, BuildLayout(project_root).out_dir(""), cfg.build, compile_commands,
    )


def _images_of_build(builder, out_dir: Path, build_cfg, compile_commands: Path) -> list[tuple[str, Path]] | None:
    """Return ``(image, database)`` of each program of the build in *out_dir*, the application last.

    *compile_commands* is the database that the build returned, the one of
    its application.  A build of one program gives ``[("", compile_commands)]``.
    The backend names the images of a build of several programs
    (``builders.output_compile_commands``).  The build without variants and
    each variant without sysbuild use this function after the build, and the
    run without a build reads the same backend method, thus the image names
    do not change with the kind of run.

    None, with an error line, when the backend names images and
    *compile_commands* is none of them: the output directory holds another
    build than the one that this run made or validated.
    """
    from ..indexer.builders import output_compile_commands

    named = {image: cc for image, cc in output_compile_commands(builder, out_dir, build_cfg).items() if image}
    if not named:
        return [("", compile_commands)]
    main = [image for image, cc in named.items() if cc.resolve() == compile_commands.resolve()]
    if not main:
        print(
            f"error: the build in {out_dir} makes the images {', '.join(sorted(named))}, and "
            f"{compile_commands} is the database of none of them. Run 'fw-context index --build'.",
            file=sys.stderr,
        )
        return None
    return [(image, named[image]) for image in sorted(named) if image != main[0]] + [(main[0], named[main[0]])]


def _record_still_uncovered(project_root: Path, db_path: Path) -> None:
    """Remember the sources a build ran for and still did not cover.

    Called only after a run that built.  What is left uncovered then is not
    a file waiting for a build — the build system does not want it: a test
    outside the build, an old experiment, a variant that is not compiled.
    Reporting it again would tell the caller to run `fw-context index
    --build`, which changes nothing, and would hold get_active_build on
    "reindex_needed" for as long as the file stays edited.

    Recomputing the scan here is simpler than remembering the list from
    before the build, and it covers an explicit `--build` from the user just
    as well as an automatic one.  ``apply_exclusions=False`` is required, or
    the scan would filter out the files this is about to record, and
    ``limit=None`` because the marker has to describe the WHOLE set.

    Best-effort: a marker that cannot be written costs one repeated report,
    never a wrong answer, thus no failure here may break the index run.
    """
    from ..config import derive_project_id
    from ..indexer.db import get_active_config, open_db
    from ..mcp.shared.stale import find_unindexed_sources
    from ..utils import compute_source_hash

    try:
        conn = open_db(db_path)
        try:
            active = get_active_config(conn, derive_project_id(project_root))
            if not active or not active["compile_commands_path"]:
                return
            # limit=None, and that is not a detail.  record_excluded
            # replaces the marker wholesale, thus a list the report cap
            # truncated would silently drop every entry past it — the
            # marker could never hold more than the cap, and always the
            # same first twenty in sort order.  A file past that point was
            # reported again on every edit, armed a build that changed
            # nothing, and never reached the suppression this marker is for.
            uncovered = find_unindexed_sources(
                conn,
                active["config_hash"],
                project_root,
                Path(active["compile_commands_path"]),
                limit=None,
                apply_exclusions=False,
            )
        finally:
            conn.close()
    except SAFE_EXCEPT:
        log.debug("could not recompute the uncovered sources", exc_info=True)
        return

    if uncovered:
        log.info(
            "%d source file(s) stay outside compile_commands.json after the "
            "build — recorded so they are not reported again", len(uncovered),
        )
    autobuild.record_excluded(
        db_path.parent,
        {path: compute_source_hash(project_root / path) for path in uncovered},
    )
