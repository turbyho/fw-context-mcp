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
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    #: A ``Popen`` in text mode or in bytes mode.
    _Process = TypeVar("_Process", subprocess.Popen[str], subprocess.Popen[bytes])

    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

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
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            _check(False, "CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            more = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    _check(thread, "OpenThread")
                    try:
                        _check(_kernel32.ResumeThread(thread) != 0xFFFFFFFF, "ResumeThread")
                    finally:
                        _kernel32.CloseHandle(thread)
                    resumed += 1
                more = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        if resumed == 0:
            raise JobObjectError(f"process {pid} has no thread to resume")

    class BuildJob:
        """A job object that ends each of its processes when it closes (module docstring)."""

        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            _check(handle, "CreateJobObjectW")
            self._handle: int | None = handle
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            try:
                _check(_kernel32.SetInformationJobObject(
                    handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info),
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
                creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
            ))

        def start_command_line(self, command_line: str, *, cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
            """Start the Windows *command_line* as :meth:`start` does, with stdout and stderr as bytes in pipes.

            The output is bytes, because the caller knows its encoding.
            """
            return self._started(lambda: subprocess.Popen(  # noqa: S603 — a command line of fw-context
                command_line, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
            ))

        def _started(self, popen: Callable[[], _Process]) -> _Process:
            """Run *popen* (a suspended start), put the process into this job, then resume it.

            When the process cannot go into the job, it is ended and the
            error is raised: it has run no instruction.  The same for an
            exception of a stop (Ctrl-C, ``IndexStopped``) that comes before
            the process runs: a suspended process outside of the job would
            stay forever.
            """
            proc: _Process | None = None
            try:
                proc = popen()
                self._adopt(proc.pid)
            except BaseException:
                if proc is not None:
                    proc.kill()
                    proc.wait()
                raise
            return proc

        def _adopt(self, pid: int) -> None:
            """Put the suspended process *pid* into this job, then resume it."""
            process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            _check(process, "OpenProcess")
            try:
                _check(_kernel32.AssignProcessToJobObject(self._require(), process), "AssignProcessToJobObject")
            finally:
                _kernel32.CloseHandle(process)
            _resume_process(pid)

        def close(self) -> None:
            """Close the handle: Windows ends each process that is still in the job."""
            if self._handle is not None:
                _kernel32.CloseHandle(self._handle)
                self._handle = None

        def _require(self) -> int:
            if self._handle is None:
                raise JobObjectError("the job object is closed")
            return self._handle

        def __enter__(self) -> BuildJob:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()
