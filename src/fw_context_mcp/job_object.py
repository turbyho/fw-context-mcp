"""Windows: a build in a job object, thus each stop reaches each process of the build.

A build tool starts processes of its own (``pio`` runs scons and the
compilers, ``make -j`` its jobs).  On POSIX the build therefore runs in its
own process group (``utils.run_in_process_group``).  Windows has no process
group that a stop can reach, thus the build runs in a job object:

- ``start``: the process starts suspended (``CREATE_SUSPENDED``), goes into
  the job, and only then runs.  Each process that it starts is in the job
  from its start, thus no early child can escape.  Measured on Windows 11
  (2026-11-09): a grandchild that the child starts on its first line is in
  the job, also through the launcher ``python.exe`` of a virtual
  environment.  The start needs 0.06 s.
- ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when the handle of the job
  closes, Windows ends each process of the job.  The handle is not
  inherited, thus only this process holds it, and a run that dies (also
  through ``TerminateProcess``) ends its build.  On POSIX the next index
  run must find such a build through a record (``record_build_groups``);
  on Windows no record is necessary.
- A stop closes the job, thus Windows ends each process of the build at
  once.  WHY no cooperative stop first, as SIGTERM on POSIX: ``CTRL_BREAK_EVENT`` to the process group of
  the build ended the child, but its grandchild ran on (measured
  2026-11-09, from a terminal and from a hidden console as the daemon has).
  A stop that reaches only a part of the build is the defect that this
  module repairs.  The cost: a compiler that the stop ends can leave an
  incomplete object file.  ``make`` and ``ninja`` then fail at the link with
  an error, and scons (``pio``) compiles again, because it compares content
  signatures.

Only for Windows.  When the system refuses a call of the job API,
``JobObjectError`` (a ``RuntimeError``, as each other build failure) goes
to the caller, and the build does not run: a build that a stop cannot
reach is not safe to run.

WHY a thread snapshot to resume the process, and not ``NtResumeProcess``
with the handle of ``Popen``: the snapshot is documented API, and the
handle is a private attribute of ``Popen``.  The snapshot lists each thread
of the system, but a run starts a few builds, and the whole start needs
0.06 s (measured).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

if sys.platform == "win32":
    import _thread
    import collections
    import ctypes
    import signal
    import threading
    from types import FrameType

    from . import _win32
    from .processes import HANGUP_SIGNAL

    #: A ``Popen`` in text mode or in bytes mode.
    _Process = TypeVar("_Process", subprocess.Popen[str], subprocess.Popen[bytes])

    class JobObjectError(RuntimeError):
        """Windows refused a call of the job API: the build does not run (module docstring)."""

    def _check(ok: object, call: str) -> None:
        """Raise ``JobObjectError`` with the last error of Windows when *ok* is false."""
        if not ok:
            raise JobObjectError(f"{call} failed: {ctypes.WinError(ctypes.get_last_error())}")

    def _resume_process(pid: int) -> None:
        """Resume each thread of the suspended process *pid*.

        WHY the threads of a snapshot: ``subprocess.Popen`` closes the handle
        of the primary thread, and it keeps no thread ID.  A process that
        ``CREATE_SUSPENDED`` started has one thread, its primary thread.
        """
        snapshot = _win32.kernel32.CreateToolhelp32Snapshot(_win32.TH32CS_SNAPTHREAD, 0)
        if snapshot == _win32.INVALID_HANDLE_VALUE:
            _check(False, "CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = _win32.ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            more = _win32.kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _win32.kernel32.OpenThread(_win32.THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    _check(thread, "OpenThread")
                    try:
                        _check(_win32.kernel32.ResumeThread(thread) != 0xFFFFFFFF, "ResumeThread")
                    finally:
                        _win32.kernel32.CloseHandle(thread)
                    resumed += 1
                more = _win32.kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _win32.kernel32.CloseHandle(snapshot)
        if resumed == 0:
            raise JobObjectError(f"process {pid} has no thread to resume")

    #: The signals whose Python handler stops a run: Ctrl-C, the stop
    #: request (``processes.request_stop`` runs the SIGTERM handler), and the
    #: close of the console.
    _STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, HANGUP_SIGNAL)

    @contextmanager
    def _stops_deferred() -> Iterator[None]:
        """Keep each stop signal until the block ends, then give it to the handler of the caller.

        Python runs a signal handler in the main thread only, thus a
        different thread gets no exception of a stop, and the block runs
        with no change there.  In the main thread the block installs a
        handler that only records the signal.  At the end, in this order:

        1. The handler stops to record: from now on it gives each signal to
           the handler of the caller.
        2. The handlers of the caller come back.
        3. ``_thread.interrupt_main`` marks each recorded signal as received,
           in ONE call from C.  Python runs the handlers of the caller at its
           next check, as for signals that came after the block.

        WHY this order and the one call: a handler of the caller that runs
        and raises stops the steps after it.

        - A mark before step 2: ``signal.signal`` runs the pending handlers
          first, and a handler that raised left the recording handler
          installed for the other signals.
        - A Python loop of marks: the first handler ran (and raised) before
          the second signal was marked, and that signal was lost (a Ctrl-C
          and a takeover: the run exited as interrupted, not as superseded).
          ``map`` over the recorded signals runs no Python code between two
          marks.  When a handler raises, CPython keeps the other marked
          signals pending, as it does for two signals that come together.
        - Step 1 first: a handler that still recorded took a new signal
          that came in steps 2 and 3, and nothing marked it.
        - Step 3 in a ``finally`` of step 2: a NEW signal that comes during
          step 2 runs its handler in ``signal.signal``, and a handler that
          raises stops the loop.  The recorded signals are marked all the
          same.  A handler that the loop did not put back is the recording
          one, and it gives each later signal to the handler of the caller.

        Between step 1 and the ``try`` of step 2 there is no call, thus
        Python runs no handler there.

        WHY ``interrupt_main`` and not ``signal.raise_signal``:
        ``raise_signal`` runs the handler at once.

        Only a signal with a Python handler is kept.  WHY: the stop request
        (``_thread.interrupt_main``) does nothing for a signal with no
        Python handler.  The default action of a recorded signal would
        thus be a stop that the caller did not have.
        """
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        # signal.Signals and not int: the conversion runs Python code, and
        # the marks in the finally must run none (see above).
        received: list[signal.Signals] = []
        previous: dict[int, Callable[[int, FrameType | None], Any]] = {}
        recording = True

        def record(signum: int, frame: FrameType | None) -> None:
            if recording:
                received.append(signal.Signals(signum))
            else:
                previous[signum](signum, frame)

        try:
            for stop_signal in _STOP_SIGNALS:
                handler = signal.getsignal(stop_signal)
                if callable(handler):
                    previous[stop_signal] = handler
                    signal.signal(stop_signal, record)
            yield
        finally:
            recording = False
            try:
                for kept, handler in previous.items():
                    signal.signal(kept, handler)
            finally:
                collections.deque(map(_thread.interrupt_main, dict.fromkeys(received)), maxlen=0)

    class BuildJob:
        """A job object that ends each of its processes when it closes (module docstring)."""

        def __init__(self) -> None:
            handle = _win32.kernel32.CreateJobObjectW(None, None)
            _check(handle, "CreateJobObjectW")
            self._handle: int | None = handle
            info = _win32.ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            try:
                _check(_win32.kernel32.SetInformationJobObject(
                    handle, _win32.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
                ), "SetInformationJobObject")
            except JobObjectError:
                self.close()
                raise

        def start(
            self, cmd: list[str] | str, *, cwd: Path, env: dict[str, str], stdout: int | None, stderr: int | None,
        ) -> subprocess.Popen[bytes]:
            """Start *cmd* suspended, put it into this job, then let it run.

            The output is bytes, because the caller knows its encoding
            (``utils.run_in_process_group``).

            *cmd* is a list of arguments, or a Windows command line for a
            program whose command line has rules of its own (``cmd.exe /c``
            for a batch file, ``utils._windows_command``).

            ``CREATE_NEW_PROCESS_GROUP`` keeps the Ctrl-C of the terminal
            from the build, as the new session does on POSIX: this process
            gets it, and it stops the build.  No stdin: in an MCP server it
            is the JSON-RPC pipe of the client.
            """
            return self._started(lambda: subprocess.Popen(  # noqa: S603 — argv list or a command line of fw-context
                cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=_win32.CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP,
            ))

        def _started(self, popen: Callable[[], _Process]) -> _Process:
            """Run *popen* (a suspended start), put the process into this job, then resume it.

            When the process cannot go into the job, it is ended and the
            error is raised: it has run no instruction.

            A stop (Ctrl-C, ``IndexStopped``, the close of the console)
            waits until the process is in the job (:func:`_stops_deferred`).
            WHY: the exception of a stop that came inside ``Popen``, after
            ``CreateProcess``, left *popen* before it returned the process.
            Nothing then knew the process, and it stayed suspended outside
            of the job forever.  After the start the stop runs: the caller
            closes the job, and Windows ends the process.
            """
            with _stops_deferred():
                proc = popen()
                try:
                    self._adopt(proc.pid)
                except BaseException:
                    proc.kill()
                    proc.wait()
                    raise
            return proc

        def _adopt(self, pid: int) -> None:
            """Put the suspended process *pid* into this job, then resume it."""
            process = _win32.kernel32.OpenProcess(_win32.PROCESS_SET_QUOTA | _win32.PROCESS_TERMINATE, False, pid)
            _check(process, "OpenProcess")
            try:
                _check(_win32.kernel32.AssignProcessToJobObject(self._require(), process), "AssignProcessToJobObject")
            finally:
                _win32.kernel32.CloseHandle(process)
            _resume_process(pid)

        def close(self) -> None:
            """Close the handle: Windows ends each process that is still in the job."""
            if self._handle is not None:
                _win32.kernel32.CloseHandle(self._handle)
                self._handle = None

        def _require(self) -> int:
            if self._handle is None:
                raise JobObjectError("the job object is closed")
            return self._handle

        def __enter__(self) -> BuildJob:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()
