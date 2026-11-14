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

import errno
import logging
import os
import re
import secrets
import signal
import sys
import threading

__all__ = [
    "HANGUP_SIGNAL",
    "MAX_PID",
    "STOP_TOKEN_ENV",
    "install_stop_listener",
    "is_valid_pid",
    "kill_hard",
    "new_stop_token",
    "own_stop_token",
    "parse_pid",
    "pid_exists",
    "process_argv_windows",
    "request_stop",
]

_log = logging.getLogger(__name__)

#: The signal of a closed terminal.  POSIX: SIGHUP.  Windows: SIGBREAK, the
#: signal that the C runtime gives for CTRL_BREAK_EVENT and for the close of
#: the console (CTRL_CLOSE_EVENT), after which Windows ends the process
#: within 5 s.
HANGUP_SIGNAL: signal.Signals
if sys.platform == "win32":
    HANGUP_SIGNAL = signal.SIGBREAK
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


#: The form of a stop token (:func:`new_stop_token`): 32 hex digits.
_STOP_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")

#: The stop token that the parent gave this process, after
#: :func:`install_stop_listener` took it from the environment.
_own_token: str | None = None


def own_stop_token() -> str | None:
    """Return the stop token that the parent gave this process, or None.

    :func:`install_stop_listener` takes the token from :data:`STOP_TOKEN_ENV`
    and removes the variable, thus this function is the only reader after
    that call.  WHY the run needs it: the parent knows the run by the PID of
    ``Popen``, which on Windows is the PID of a launcher.  The run writes its
    own PID into a file with the token in its name
    (``indexer._index_owner.run_pid_path``), and the parent reads that file.
    """
    return _own_token


def _take_stop_token() -> str | None:
    """Take the stop token from :data:`STOP_TOKEN_ENV`, keep it for :func:`own_stop_token`, and return it.

    Raise ValueError for a value that is not a stop token: the token goes
    into the name of a file and of an event, and a value such as ``../x``
    would name a file outside the index directory.
    """
    global _own_token
    token = os.environ.pop(STOP_TOKEN_ENV, "")
    if not token:
        return None
    if not _STOP_TOKEN_PATTERN.fullmatch(token):
        raise ValueError(f"{STOP_TOKEN_ENV} holds {token!r}, which is no stop token (32 hex digits)")
    _own_token = token
    return token


def is_valid_pid(pid: int) -> bool:
    """Return True when *pid* is in 1 .. :data:`MAX_PID`, thus can name one process.

    A reader of a PID file calls it before the PID goes to
    :func:`request_stop` or :func:`kill_hard`, and reports a PID outside
    the range as a damaged file.
    """
    return 0 < pid <= MAX_PID


def parse_pid(text: str) -> int | None:
    """Return the PID that *text* holds, or None when it is not ASCII digits of a PID in 1 .. :data:`MAX_PID`.

    The one reader of a PID in a file or an answer (``watcher.pid``, the
    holder of ``index.lock``, the answer of the daemon).  WHY ASCII:
    ``str.isdigit()`` accepts digits such as '²' that ``int()`` refuses.
    WHY the range: see :func:`_require_pid`.
    """
    if not (text.isascii() and text.isdigit()):
        return None
    pid = int(text)
    return pid if is_valid_pid(pid) else None


def _require_pid(pid: int) -> None:
    """Raise ValueError when *pid* is outside 1 .. :data:`MAX_PID`.

    WHY an error and not "the process is gone": ``kill`` reads 0 as the
    process group of the caller and -1 as each process of the user, and
    ctypes cuts a Windows PID above :data:`MAX_PID` to 32 bits, which then
    names a different process.  Such a PID comes only from a damaged file,
    and a stop or a kill must never reach a group or a different process.
    """
    if not is_valid_pid(pid):
        raise ValueError(f"PID {pid} is outside 1 .. {MAX_PID}")


_listener_lock = threading.Lock()
_listener_started = False


if sys.platform == "win32":
    import _thread
    import ctypes
    from ctypes import wintypes

    from . import _win32

    def _create_own_event(name: str) -> int:
        """Make the stop event *name* and return its handle.  Raise OSError when it exists already.

        Auto-reset: one SetEvent wakes one wait, thus each request runs the
        handler, as each SIGTERM does on POSIX.

        WHY an event that exists is an error: ``CreateEventW`` then opens it,
        and this process did not make it.  Its state and its DACL are not
        known.  It can hold a request for an earlier process with the same
        PID (a requester that held its handle while the PID came back), and
        the listener then stopped a run that nobody asked to stop.  A
        different user that made the event first, with a DACL that lets each
        user set it, could stop each run of this user.  A run that cannot
        get its own event does not start: the error names the event.
        """
        handle = _win32.kernel32.CreateEventW(None, False, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == _win32.ERROR_ALREADY_EXISTS:
            _win32.kernel32.CloseHandle(handle)
            raise OSError(
                errno.EEXIST,
                f"the stop event {name} exists already, and this process did not make it",
            )
        return handle

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

        Raise OSError when an event exists already (:func:`_create_own_event`).
        The listener then closes each event that it made, and it does not
        start.  Raise ValueError when the variable holds no stop token
        (:func:`_take_stop_token`).
        """
        global _listener_started
        with _listener_lock:
            if _listener_started:
                return
            names = [_STOP_EVENT_NAME.format(pid=os.getpid())]
            token = _take_stop_token()
            if token is not None:
                names.append(_STOP_TOKEN_EVENT_NAME.format(token=token))
            handles: list[int] = []
            try:
                for name in names:
                    handles.append(_create_own_event(name))
            except OSError:
                # A listener that does not start keeps no event: an event
                # of this PID that stays would take a request that no
                # thread waits for.
                for handle in handles:
                    _win32.kernel32.CloseHandle(handle)
                raise
            array = (wintypes.HANDLE * len(handles))(*handles)

            def wait_for_stop() -> None:
                # A loop: a handler can ignore a request (a late takeover
                # after the release of the lock), and the next request must
                # still reach the handler of that time.
                while True:
                    result = _win32.kernel32.WaitForMultipleObjects(len(handles), array, False, _win32.INFINITE)
                    if result == _win32.WAIT_FAILED:
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

        Raise ValueError for a PID outside 1 .. :data:`MAX_PID`
        (:func:`_require_pid`).
        """
        _require_pid(pid)
        name = _STOP_TOKEN_EVENT_NAME.format(token=token) if token else _STOP_EVENT_NAME.format(pid=pid)
        handle = _win32.kernel32.OpenEventW(_win32.EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(_win32.kernel32.SetEvent(handle))
        finally:
            _win32.kernel32.CloseHandle(handle)

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
        if not is_valid_pid(pid):
            return False
        handle = _win32.kernel32.OpenProcess(_win32.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() != _win32.ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not _win32.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _win32.STILL_ACTIVE
        finally:
            _win32.kernel32.CloseHandle(handle)

    def kill_hard(pid: int) -> None:
        """End *pid* at once (``TerminateProcess``); a process that is gone is no error.

        Raise OSError for each other failure, for example access denied to
        a process that runs elevated, as SIGKILL raises PermissionError on
        POSIX.  The caller then reports the real cause.

        Raise ValueError for a PID outside 1 .. :data:`MAX_PID`
        (:func:`_require_pid`).
        """
        _require_pid(pid)
        handle = _win32.kernel32.OpenProcess(_win32.PROCESS_TERMINATE, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == _win32.ERROR_INVALID_PARAMETER:
                return
            raise ctypes.WinError(error)
        try:
            if not _win32.kernel32.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _win32.kernel32.CloseHandle(handle)

    def process_argv_windows(pid: int) -> list[str] | None:
        """Return the argv of *pid*, or None when it cannot be read.

        ``NtQueryInformationProcess(ProcessCommandLineInformation)`` gives
        the command line (Windows 8.1 and later), and ``CommandLineToArgvW``
        splits it with the rules of the C runtime: an argument with a space
        stays one argument.  Measured: 0.6 ms for each call.  A process that
        is gone, or a protected process of the system, gives None.  A PID
        outside 1 .. :data:`MAX_PID` names no process and gives None too:
        ctypes would cut it to 32 bits, and the argv of a different process
        came back.
        """
        if not is_valid_pid(pid):
            return None
        handle = _win32.kernel32.OpenProcess(_win32.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            _win32.ntdll.NtQueryInformationProcess(handle, _win32.PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            status = _win32.ntdll.NtQueryInformationProcess(
                handle, _win32.PROCESS_COMMAND_LINE_INFORMATION, buffer, size, ctypes.byref(size),
            )
            if status != 0:
                return None
            text = _win32.UnicodeString.from_buffer(buffer)
            line = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Length else ""
        finally:
            _win32.kernel32.CloseHandle(handle)
        # CommandLineToArgvW("") gives the path of THIS program, thus an
        # empty command line (some processes of the system) is no argv.
        if not line:
            return None
        count = ctypes.c_int(0)
        parts = _win32.shell32.CommandLineToArgvW(line, ctypes.byref(count))
        if not parts:
            return None
        try:
            return [parts[i] for i in range(count.value)]
        finally:
            _win32.kernel32.LocalFree(parts)

else:

    def install_stop_listener() -> None:
        """Take :data:`STOP_TOKEN_ENV`: on POSIX the request is SIGTERM, which runs the handler itself.

        The variable goes as on Windows, thus a child of this process (a
        build tool) gets it on no system.  :func:`own_stop_token` gives the
        token, as on Windows.  Raise ValueError when the variable holds no
        stop token (:func:`_take_stop_token`).
        """
        _take_stop_token()

    def request_stop(pid: int, *, token: str | None = None) -> bool:
        """Send SIGTERM to *pid*.  Return False when the process cannot get the request.

        False: the process is gone, or it is a process of a different user
        (PermissionError).  The Windows version gives False in the same two
        cases, because ``OpenEventW`` then fails.  WHY no exception for a
        different user: such a process is not a run of this user that
        listens.  The caller then waits, and :func:`kill_hard` reports the
        real cause.

        *token* has no use on POSIX: the PID of ``Popen`` is the PID of the
        interpreter there.

        Raise ValueError for a PID outside 1 .. :data:`MAX_PID`
        (:func:`_require_pid`).
        """
        _require_pid(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
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
        if not is_valid_pid(pid):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill_hard(pid: int) -> None:
        """Send SIGKILL to *pid*; a process that is gone is no error.

        Raise ValueError for a PID outside 1 .. :data:`MAX_PID`
        (:func:`_require_pid`).
        """
        _require_pid(pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def process_argv_windows(pid: int) -> list[str] | None:
        """Not used on POSIX: ``_index_owner.process_argv`` reads ``/proc`` or ``ps``."""
        return None
