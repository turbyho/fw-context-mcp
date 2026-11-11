"""Stop requests and process identity across processes, on POSIX and on Windows.

fw-context stops a process of its own in two steps: a request first, then a
hard stop when the process does not end in time (``indexer._index_owner``,
the daemon, ``fw-context watch stop``).  The request must run the Python
handler of SIGTERM in the target, because that handler unwinds the run:
its ``finally`` blocks drop the markers and the lock, and the build stops.

POSIX: the request is SIGTERM, and the hard stop is SIGKILL.

Windows: ``os.kill(pid, SIGTERM)`` is ``TerminateProcess``, which runs no
handler.  Thus each process that can get a request calls
:func:`install_stop_listener`: it makes the named event
``Global\\fw-context-stop-<pid>`` and a thread that waits for it.  When
another process sets the event (:func:`request_stop`), the thread calls
``_thread.interrupt_main(SIGTERM)``, and the main thread runs the same
handler as on POSIX.  Measured on Windows 11, Python 3.11, 2026-11-09:

- A Python loop stops at once.
- A C call (a libclang parse) stops when it returns, as on POSIX.
- ``time.sleep`` stops only when it ends: on Windows only SIGINT wakes it.
  The caller of :func:`request_stop` waits a grace time and then uses
  :func:`kill_hard`, as for a long C call on POSIX.

A stop token for a child process: on Windows the ``python.exe`` of a
virtual environment is a launcher that starts the interpreter as its child
(measured: ``Popen.pid`` 8956, ``os.getpid()`` of the code 6200, its parent
8956).  The event of the PID of ``Popen`` then does not exist.  A parent that
stops its child later gives it a token (:func:`new_stop_token`,
:data:`STOP_TOKEN_ENV`), and the listener of the child makes a second event
for the token.  The end of the launcher ends the interpreter too (the
launcher puts it in a job), thus :func:`kill_hard` of ``Popen.pid`` works.
A process that another process finds by a PID that the process wrote itself
(the index lock, ``watcher.pid``) needs no token.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import sys
import threading

__all__ = [
    "HANGUP_SIGNAL",
    "MAX_PID",
    "STOP_TOKEN_ENV",
    "install_stop_listener",
    "kill_hard",
    "new_stop_token",
    "pid_exists",
    "process_argv_windows",
    "request_stop",
]

#: The signal of a closed terminal.  POSIX: SIGHUP.  Windows: SIGBREAK, the
#: signal that the C runtime gives for CTRL_BREAK_EVENT and for the close of
#: the console (CTRL_CLOSE_EVENT), after which Windows ends the process
#: within 5 s.
_log = logging.getLogger(__name__)

if sys.platform == "win32":
    HANGUP_SIGNAL: signal.Signals = signal.SIGBREAK
else:
    HANGUP_SIGNAL = signal.SIGHUP

# Global\ and not Local\: a Local\ name is in the namespace of one logon
# session, and a takeover from an SSH session or a service did not find the
# event of a run in the interactive session.  Any user can make an event in
# Global\ (the privilege SeCreateGlobalPrivilege is for file mappings and
# links), and the default DACL lets only the same user open it.
_STOP_EVENT_NAME = "Global\\fw-context-stop-{pid}"
_STOP_TOKEN_EVENT_NAME = "Global\\fw-context-stop-token-{token}"

#: The largest PID of this system: ``pid_t`` on POSIX, a DWORD on Windows.
MAX_PID = 2**32 - 1 if sys.platform == "win32" else 2**31 - 1

#: The environment variable that gives a child process its stop token.
STOP_TOKEN_ENV = "FW_CONTEXT_STOP_TOKEN"


def new_stop_token() -> str:
    """Return a new stop token for a child process (put it in :data:`STOP_TOKEN_ENV`)."""
    return secrets.token_hex(16)
_listener_lock = threading.Lock()
_listener_started = False


if sys.platform == "win32":
    import _thread
    import ctypes
    from ctypes import wintypes

    _EVENT_MODIFY_STATE = 0x0002
    _INFINITE = 0xFFFFFFFF
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_COMMAND_LINE_INFORMATION = 60
    _ERROR_INVALID_PARAMETER = 87
    _STILL_ACTIVE = 259

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
    ]
    _kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
    _WAIT_FAILED = 0xFFFFFFFF
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
    ]
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    _shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)

    class _UnicodeString(ctypes.Structure):
        """The ``UNICODE_STRING`` that ``ProcessCommandLineInformation`` gives."""

        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    def install_stop_listener() -> None:
        """Make the stop event of this process and the thread that waits for it.

        Call it in the main thread after the SIGTERM handler is installed.
        A second call does nothing.  The event lives as long as the process,
        thus a request that comes before the handler exists runs the
        default action of SIGTERM in ``interrupt_main``: nothing, because
        ``interrupt_main`` does nothing for a signal that Python does not
        handle (SIG_DFL).

        With :data:`STOP_TOKEN_ENV` the listener makes the event of the
        token too, and removes the variable: a child of this process (a
        build tool) must not get it.
        """
        global _listener_started
        with _listener_lock:
            if _listener_started:
                return
            names = [_STOP_EVENT_NAME.format(pid=os.getpid())]
            token = os.environ.pop(STOP_TOKEN_ENV, "")
            if token:
                names.append(_STOP_TOKEN_EVENT_NAME.format(token=token))
            handles = []
            for name in names:
                # Auto-reset: one SetEvent wakes one wait, thus each request
                # runs the handler, as each SIGTERM does on POSIX.
                handle = _kernel32.CreateEventW(None, False, False, name)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
            array = (wintypes.HANDLE * len(handles))(*handles)

            def wait_for_stop() -> None:
                # A loop: a handler can ignore a request (a late takeover
                # after the release of the lock), and the next request must
                # still reach the handler of that time.
                while True:
                    result = _kernel32.WaitForMultipleObjects(len(handles), array, False, _INFINITE)
                    if result == _WAIT_FAILED:
                        _log.error("The stop listener cannot wait: WinError %d", ctypes.get_last_error())
                        return
                    _thread.interrupt_main(signal.SIGTERM)

            threading.Thread(target=wait_for_stop, name="fw-context-stop", daemon=True).start()
            _listener_started = True

    def request_stop(pid: int, *, token: str | None = None) -> bool:
        """Ask *pid* to stop: set its stop event.  Return False when it has none.

        *token* selects the event of the token that the parent gave the
        child (:data:`STOP_TOKEN_ENV`), for a child that the parent knows by
        the PID of ``Popen``.  False: the process is gone, or it is not a
        process of fw-context that listens.  The caller then waits for the
        end, or uses :func:`kill_hard`.
        """
        name = _STOP_TOKEN_EVENT_NAME.format(token=token) if token else _STOP_EVENT_NAME.format(pid=pid)
        handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(_kernel32.SetEvent(handle))
        finally:
            _kernel32.CloseHandle(handle)

    def pid_exists(pid: int) -> bool:
        """Return True while a process with *pid* runs.

        WHY not ``os.kill(pid, 0)``: on Windows signal 0 is CTRL_C_EVENT,
        and ``os.kill`` gives it to ``GenerateConsoleCtrlEvent``, which sends
        Ctrl+C to the process group *pid* of this console.  ``OpenProcess``
        fails with ``ERROR_INVALID_PARAMETER`` for a PID that no process
        has; another failure, such as access denied, is a process that
        runs.  An open process runs while its exit code is ``STILL_ACTIVE``.
        A PID outside 1 .. :data:`MAX_PID` gives False: ctypes would cut it
        to 32 bits.
        """
        if not 0 < pid <= MAX_PID:
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def kill_hard(pid: int) -> None:
        """End *pid* at once (``TerminateProcess``); a process that is gone is no error.

        Raise OSError for each other failure, for example access denied to
        a process that runs elevated, as SIGKILL raises PermissionError on
        POSIX.  The caller then reports the real cause.
        """
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == _ERROR_INVALID_PARAMETER:
                return
            raise ctypes.WinError(error)
        try:
            if not _kernel32.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(handle)

    def process_argv_windows(pid: int) -> list[str] | None:
        """Return the argv of *pid*, or None when it cannot be read.

        ``NtQueryInformationProcess(ProcessCommandLineInformation)`` gives
        the command line (Windows 8.1 and later), and ``CommandLineToArgvW``
        splits it with the rules of the C runtime: an argument with a space
        stays one argument.  Measured: 0.6 ms for each call.  A process that
        is gone, or a protected process of the system, gives None.
        """
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            _ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            status = _ntdll.NtQueryInformationProcess(
                handle, _PROCESS_COMMAND_LINE_INFORMATION, buffer, size, ctypes.byref(size),
            )
            if status != 0:
                return None
            text = _UnicodeString.from_buffer(buffer)
            line = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Length else ""
        finally:
            _kernel32.CloseHandle(handle)
        # CommandLineToArgvW("") gives the path of THIS program, thus an
        # empty command line (some processes of the system) is no argv.
        if not line:
            return None
        count = ctypes.c_int(0)
        parts = _shell32.CommandLineToArgvW(line, ctypes.byref(count))
        if not parts:
            return None
        try:
            return [parts[i] for i in range(count.value)]
        finally:
            _kernel32.LocalFree(parts)

else:

    def install_stop_listener() -> None:
        """Remove :data:`STOP_TOKEN_ENV`: on POSIX the request is SIGTERM, which runs the handler itself.

        The variable goes as on Windows, thus a child of this process (a
        build tool) gets it on no system.
        """
        os.environ.pop(STOP_TOKEN_ENV, None)

    def request_stop(pid: int, *, token: str | None = None) -> bool:
        """Send SIGTERM to *pid*.  Return False when the process is gone.

        *token* has no use on POSIX: the PID of ``Popen`` is the PID of the
        interpreter there.
        """
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def pid_exists(pid: int) -> bool:
        """Return True while a process with *pid* runs.

        ``os.kill(pid, 0)`` sends no signal and checks the PID.  A process of
        a different user gives PermissionError, and it runs.  A PID outside
        1 .. :data:`MAX_PID` gives False: ``kill`` reads 0 and a negative
        PID as a process group, this one included.

        Another OSError (EINVAL from a sandbox, for example) is no answer and
        goes to the caller: a PID file treats it as a dead process, and the
        cleanup of temporary files keeps the file.
        """
        if not 0 < pid <= MAX_PID:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill_hard(pid: int) -> None:
        """Send SIGKILL to *pid*; a process that is gone is no error."""
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def process_argv_windows(pid: int) -> list[str] | None:
        """Not used on POSIX: ``_index_owner.process_argv`` reads ``/proc`` or ``ps``."""
        return None
