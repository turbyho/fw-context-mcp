"""The owner of the index of one project: the lock of a whole run, its markers and the takeover.

Moved from ``cli/_index.py``: the MCP tools that write the index
(``reindex_file``, ``reset_index``) must take it as the CLI commands do, and
``mcp/`` must not import from ``cli/``.  The handlers of SIGTERM and SIGHUP
stay in the CLI (``cli._index._owned_index``): a signal handler can only be
installed in the main thread, and the MCP server runs each tool in a thread.

*say* of :func:`acquire_index` and :func:`wait_for_the_lock` prints a line
for a person: the CLI writes it to stderr, an MCP tool to its log.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..mcp.shared.pid_file import PidFile

# How long a run that was asked to stop (SIGTERM) gets to release the index
# lock, and how long a run that got SIGKILL gets after that.  A cooperative
# run stops at its next translation unit or at the next Python bytecode after
# a C call returns — a single libclang parse or an HTTP request to Ollama can
# hold it for longer than that, which is what SIGKILL is for.
TAKEOVER_GRACE_S = 10.0
KILL_GRACE_S = 5.0


def process_argv(pid: int) -> list[str] | None:
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


def index_run_kind(pid: int) -> str | None:
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
    argv = process_argv(pid)
    if not argv:
        return None
    if not any(arg.endswith(("fw-context", "fw_context_mcp.cli", "fw_context_mcp")) for arg in argv):
        return None
    if "index" not in argv:
        return None
    return "background" if "--background" in argv else "foreground"


def is_fw_context_process(pid: int) -> bool:
    """Return True when *pid* runs fw-context (any command, or the MCP server), for the message of a refusal."""
    argv = process_argv(pid)
    return bool(argv) and any(
        arg.endswith(("fw-context", "fw-context-mcp", "fw_context_mcp.cli", "fw_context_mcp")) for arg in argv or ()
    )


def is_analyze_run(pid: int) -> bool:
    """Return True when *pid* is an ``fw-context analyze`` command, for the message of a refused run."""
    argv = process_argv(pid)
    if not argv:
        return False
    return any(arg.endswith(("fw-context", "fw_context_mcp.cli", "fw_context_mcp")) for arg in argv) and (
        "analyze" in argv
    )


def signal_index_run(pid: int, sig: signal.Signals) -> None:
    """Send *sig* to *pid* when it is still an fw-context index run.

    Checked again right before each signal: the PID was read from the lock
    file earlier, and the process may have exited and the PID been reused
    since.
    """
    if index_run_kind(pid) is None:
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
TAKEOVER_MARKER = "reindex.takeover"


def write_takeover_marker(db_dir: Path, victim: int) -> None:
    """Write ``{"taker": <this PID>, "victim": <victim>}`` atomically.

    Atomic, because the victim reads it in its signal handler at any time
    after the signal.  The victim is named, thus a SIGTERM that a different
    run gets at the same time does not read as a takeover.

    Another marker than ``reindex.pause`` and ``reindex.pid``.  The rule
    "write nothing before the lock is won" (see acquire_index) keeps those
    two from naming a run that lost.  No code reads this file except the
    victim, and only to classify a signal, thus a run that loses harms
    nothing with it.
    """
    marker = db_dir / TAKEOVER_MARKER
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}")
    payload = {"taker": os.getpid(), "thread": threading.get_ident(), "victim": victim}
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, marker)


def remove_takeover_marker(db_dir: Path) -> None:
    """Remove the takeover marker when this thread of this process wrote it.

    WHY the thread: two tools of one MCP server can take the index over at
    the same time, and the one that loses removed the marker of the one
    that won.  The victim then took the SIGTERM as a foreign one, exited
    143 and not 75, and the daemon did not run its work again.
    """
    marker = db_dir / TAKEOVER_MARKER
    data = read_takeover_marker(marker)
    if data.get("taker") == os.getpid() and data.get("thread", threading.get_ident()) == threading.get_ident():
        marker.unlink(missing_ok=True)


def read_takeover_marker(marker: Path) -> dict[str, int]:
    """Return the marker as a dict, or an empty dict when it is missing or bad."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, int)}


def taken_over(db_dir: Path) -> bool:
    """Return True when a process that still runs takes the index from this process.

    The check is that the taker runs, not that it is an index run (see
    index_run_kind): that check reads the argv, and a signal handler must
    stay short and must not start ``ps`` on macOS.  The rest risk is a
    marker that a dead taker left, whose PID the system gave to another
    process, and that names THIS process as the victim.  Then a foreign
    SIGTERM reads as a takeover, and the run exits 75 instead of 143.
    """
    data = read_takeover_marker(db_dir / TAKEOVER_MARKER)
    taker = data.get("taker")
    if data.get("victim") != os.getpid() or taker is None or taker == os.getpid():
        return False
    return PidFile._pid_exists(taker)


def acquire_index(
    db_dir: Path, stack: ExitStack, *, background: bool, takeover: bool, say: Callable[[str], None],
) -> None:
    """Enter ``index_run_lock`` on *stack*, taking the index over if needed.

    A manual run always wins over a background one: the daemon starts those
    and starts another when the work is still needed (the killed run exits
    ``EXIT_SUPERSEDED``, which the daemon retries).  A manual run wins over
    another MANUAL run only with ``--takeover`` — that one belongs to a person.
    A background run takes over nothing.

    The takeover asks first: SIGTERM makes the holder raise IndexSuperseded
    (see _owned_index), which unwinds its ``finally`` blocks and drops the
    markers and the lock.  A holder that does not release the lock within
    TAKEOVER_GRACE_S — stuck in a long C call, or in a handler that
    swallowed the exception — gets SIGKILL.  The kernel drops a flock
    however the process exits.

    Writes nothing before the lock is won.  A run that wrote the pause marker
    and then lost the lock would leave a marker naming a live process, and
    the run that won would read it as "somebody took the index over" and
    abandon itself — see claim_index().

    Raises IndexRunLocked when the holder may not be taken over, when it
    survived SIGKILL, or when a third process won the lock in between.
    """
    from .db._locking import IndexRunLocked, index_run_lock

    try:
        stack.enter_context(index_run_lock(db_dir))
        return
    except IndexRunLocked as exc:
        pid = exc.holder_pid
        if background or pid is None or pid == os.getpid():
            raise
        kind = index_run_kind(pid)
        if kind is None:
            if is_analyze_run(pid):
                raise IndexRunLocked(
                    db_dir, pid, hint="'fw-context analyze' holds the index; wait for its end"
                ) from None
            if is_fw_context_process(pid):
                raise IndexRunLocked(
                    db_dir, pid,
                    hint="another fw-context operation holds the index (an MCP tool, 'fw-context db', "
                    "cleanup, doctor --fix or init); wait for its end",
                ) from None
            raise
        if kind == "foreground" and not takeover:
            raise IndexRunLocked(
                db_dir, pid,
                hint="a manual index run; wait for its end ('fw-context index --takeover' terminates it)",
            ) from None

    say(f"Taking over the index from pid {pid} ({kind} run)")
    # The marker stays until this run releases the index, not only until it
    # wins the lock.  A holder that released the lock before the signal
    # arrived reads the marker in its handler after that, and the marker
    # must still be there, or that holder takes the signal as a foreign one
    # and dies (see _SigtermOutsideTheLock).
    write_takeover_marker(db_dir, pid)
    won = False
    try:
        signal_index_run(pid, signal.SIGTERM)
        won = wait_for_the_lock(db_dir, stack, pid, say)
    finally:
        if won:
            stack.callback(remove_takeover_marker, db_dir)
        else:
            remove_takeover_marker(db_dir)
    if not won:
        raise IndexRunLocked(db_dir, pid, hint="it did not release the index even after SIGKILL")


def wait_for_the_lock(db_dir: Path, stack: ExitStack, pid: int, say: Callable[[str], None]) -> bool:
    """Wait for *pid* to release the lock after SIGTERM, then SIGKILL it.

    Returns True when this process holds the lock on *stack*, and False
    when *pid* survived SIGKILL.  Raises IndexRunLocked when a third
    process won the lock in between.
    """
    from .db._locking import IndexRunLocked, index_run_lock

    for grace, escalate in ((TAKEOVER_GRACE_S, True), (KILL_GRACE_S, False)):
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
            say(f"  pid {pid} did not stop within {grace:.0f}s — sending SIGKILL")
            signal_index_run(pid, signal.SIGKILL)
    return False


def claim_index(db_dir: Path, *, pid_marker: bool = True) -> None:
    """Write the pause marker and the pid marker.

    Call this function ONLY while the caller holds index_run_lock.  The pause
    marker stops the file-watcher daemon from starting a background run, and
    the pid marker tells every other process which run owns the index.  Both
    are claims, and a process that lost the lock owns nothing.

    Creates no directory: index_run_lock already does that before it takes
    the lock.
    """
    PidFile(db_dir / "reindex.pause").write()
    if pid_marker:
        PidFile(db_dir / "reindex.pid").write()


def release_index(db_dir: Path) -> None:
    """Drop the markers this process wrote, and only those.

    unlink_if_ours checks the PID, so a marker another run owns survives.
    """
    PidFile(db_dir / "reindex.pid").unlink_if_ours()
    PidFile(db_dir / "reindex.pause").unlink_if_ours()


def holder_kind(pid: int | None) -> str:
    """Return who holds the index: ``"index"``, ``"background"``, ``"analyze"``, ``"operation"`` or ``"other"``.

    ``"operation"`` is this process: another tool of the same MCP server
    holds the index (:func:`owned_by_operation`).  ``"other"`` is a process
    whose argv tells nothing, or no PID.
    """
    if pid is None:
        return "other"
    if pid == os.getpid():
        return "operation"
    kind = index_run_kind(pid)
    if kind == "foreground":
        return "index"
    if kind == "background":
        return "background"
    return "analyze" if is_analyze_run(pid) else "other"


@contextmanager
def owned_by_operation(db_dir: Path, say: Callable[[str], None]) -> Iterator[None]:
    """Hold the index for a manual operation of an MCP tool (``reindex_file``, ``reset_index``).

    The rules of a manual ``fw-context index``: the operation takes the index
    over from a background run, and a manual holder (``fw-context index``,
    ``fw-context analyze``, another tool) refuses it with
    :class:`IndexRunLocked`; :func:`holder_kind` names that holder.  It
    writes ``reindex.pause`` and ``reindex.pid`` as an index run does, thus
    the status tools tell the LLM to wait for the operation.

    WHY no handler of SIGTERM, as ``cli._index._owned_index`` has: a signal
    handler can only be installed in the main thread, and the MCP server
    runs each tool in a thread.  Before, the two tools only wrote
    ``reindex.pause``: a manual index run either stopped at its next unit,
    or did not see the pause and went on with an old snapshot, and the
    embedding and analysis steps of a run have no check at all.
    """
    with ExitStack() as stack:
        acquire_index(db_dir, stack, background=False, takeover=False, say=say)
        stack.callback(release_index, db_dir)
        claim_index(db_dir)
        yield
