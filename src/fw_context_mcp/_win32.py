"""The Win32 API that fw-context calls through ctypes: one binding of each DLL.  Windows only.

Each prototype (``argtypes`` and ``restype``), each structure of a
prototype, and each constant of the API is here, one time.

WHY one module: each ``ctypes.WinDLL(...)`` call gives a new object with its
own function pointers, and each module declared the prototypes of its own
object again.  ``OpenProcess`` and ``CloseHandle`` had three copies.  A
wrong ``argtypes`` in one copy (a HANDLE declared as a DWORD) cuts a value
to 32 bits in that module only, and a review had to compare the copies.

``use_last_error=True`` for ``kernel32``, ``shell32`` and ``ws2_32``: ctypes then keeps
the error of the call, and ``ctypes.get_last_error()`` reads it after
Python code that ran between (a ``GetLastError`` of a different call).
``ntdll`` returns its status as the value of the call.
"""

from __future__ import annotations

import sys

if sys.platform != "win32":
    raise ImportError("fw_context_mcp._win32 is only for Windows")

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ws2_32 = ctypes.WinDLL("ws2_32", use_last_error=True)

# ── Constants ───────────────────────────────────────────────────────────────

ERROR_INVALID_PARAMETER = 87
ERROR_LOCK_VIOLATION = 33
ERROR_ALREADY_EXISTS = 183

INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
STILL_ACTIVE = 259

EVENT_MODIFY_STATE = 0x0002
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_SUSPEND_RESUME = 0x0002

CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_COMMAND_LINE_INFORMATION = 60

LOCKFILE_FAIL_IMMEDIATELY = 0x1
LOCKFILE_EXCLUSIVE_LOCK = 0x2

#: ``_WSAIOW(IOC_VENDOR, 17)``, ``mstcpip.h``.
SIO_TCP_INITIAL_RTO = 0x98000011
#: The system keeps its initial RTT.
TCP_INITIAL_RTO_UNSPECIFIED_RTT = 0xFFFF
TCP_INITIAL_RTO_NO_SYN_RETRANSMISSIONS = 0xFE

# ── Structures ──────────────────────────────────────────────────────────────


class Overlapped(ctypes.Structure):
    """The ``OVERLAPPED`` structure.

    Only ``Offset`` and ``OffsetHigh`` have a function in fw-context: they
    give the offset of a locked byte (``file_lock``).  The union of the two
    offsets with ``Pointer`` has the same size, thus two DWORD fields are
    sufficient.
    """

    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class ThreadEntry32(ctypes.Structure):
    """The ``THREADENTRY32`` structure: one thread of a ``CreateToolhelp32Snapshot``.

    ``job_object._resume_process`` reads ``th32OwnerProcessID`` and
    ``th32ThreadID``: ``Popen`` keeps no thread ID of the suspended
    process.  Seven fields of 32 bits on 32-bit and 64-bit Windows; the
    caller sets ``dwSize``, and Windows refuses a wrong size.
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    """The ``IO_COUNTERS`` structure: a part of :class:`ExtendedLimitInformation`.

    fw-context reads no counter.  The fields are necessary because they
    give the size and the offsets of the fields that come after them.
    """

    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class BasicLimitInformation(ctypes.Structure):
    """The ``JOBOBJECT_BASIC_LIMIT_INFORMATION`` structure: a part of :class:`ExtendedLimitInformation`.

    ``job_object.BuildJob`` sets only ``LimitFlags``
    (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``).  The SIZE_T fields are
    ``c_size_t``, thus the layout is correct for 32-bit and 64-bit Python.
    """

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


class ExtendedLimitInformation(ctypes.Structure):
    """The ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` structure of ``SetInformationJobObject``.

    WHY the extended structure and not the basic one: Microsoft documents
    that ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` requires it.
    ``SetInformationJobObject`` checks only the size: a wrong size fails
    the call (``job_object.JobObjectError``), but fields in a wrong order
    with the correct size set wrong limits with no error.  The fields thus
    follow ``winnt.h`` one by one, and ``test_build_job.py`` shows that a
    stop ends each process of the job.
    """

    _fields_ = [
        ("BasicLimitInformation", BasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class UnicodeString(ctypes.Structure):
    """The ``UNICODE_STRING`` that ``ProcessCommandLineInformation`` gives.

    ``processes.process_argv_windows`` reads it from the start of the
    buffer of ``NtQueryInformationProcess``.  ``Length`` counts bytes, not
    characters, and the text has no terminating null.  ``Buffer`` is
    ``c_void_p``, thus the padding before it is correct for 32-bit and
    64-bit Python.
    """

    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class MemoryStatusEx(ctypes.Structure):
    """The ``MEMORYSTATUSEX`` structure of ``GlobalMemoryStatusEx``.

    ``llm.hardware`` reads the total and the available physical memory
    for the choice of a model.  The caller sets ``dwLength``: Windows
    refuses the call without it.
    """

    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class InitialRtoParameters(ctypes.Structure):
    """The ``TCP_INITIAL_RTO_PARAMETERS`` structure of ``mstcpip.h``.

    ``loopback`` gives it to ``WSAIoctl(SIO_TCP_INITIAL_RTO)``: a connect
    to a loopback port that nobody listens on then fails at once, and
    not after the 2 s of the SYN retransmissions of Windows.
    """

    _fields_ = [("Rtt", ctypes.c_ushort), ("MaxSynRetransmissions", ctypes.c_ubyte)]


# ── Prototypes ──────────────────────────────────────────────────────────────

# Handles and processes.
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p

# Events.
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
]
kernel32.WaitForMultipleObjects.restype = wintypes.DWORD

# Job objects and threads.
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
kernel32.Thread32Next.restype = wintypes.BOOL

# File locks.
kernel32.LockFileEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped),
]
kernel32.LockFileEx.restype = wintypes.BOOL
kernel32.UnlockFileEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped),
]
kernel32.UnlockFileEx.restype = wintypes.BOOL

# Console and memory.
kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetConsoleMode.restype = wintypes.BOOL
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatusEx)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

# The command line of a process.
ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long
shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)

# Sockets.
ws2_32.WSAIoctl.argtypes = [
    ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p,
]
ws2_32.WSAIoctl.restype = ctypes.c_int
