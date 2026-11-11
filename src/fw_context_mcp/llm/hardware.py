"""The memory of this machine for a local model: the GPU memory and the RAM.

The recommendation of a model (``llm.recommend``) estimates from it where
a model that is not installed runs, and the key of a calibration
(``llm.calibration``) holds it: a new GPU or more RAM makes a new
calibration.  WHY the total memory in the key and not the free memory: the
free memory changes with each other program, and the measured speed of a
model tells it at the time of the run.

The estimate also uses the memory that other programs hold now
(``gpu_others_mib``, ``ram_available_mib``).  WHY (decision of the user,
2026-10-23): the desktop and the other programs hold another amount on each
machine and at each time, and a fixed reserve was right for one machine
only.  A value that is not known stays None, and the estimate then uses
its fixed reserve.

The sources: ``nvidia-smi`` for an NVIDIA GPU, ``sysctl`` on macOS (Apple
Silicon uses a part of the RAM for its GPU), ``/proc/meminfo`` and
``sysconf`` on Linux, ``GlobalMemoryStatusEx`` on Windows.  WHY no AMD GPU:
no API tells which GPU Ollama uses, nor whether ROCm supports a card; a
machine can have an AMD card for its desktop and an NVIDIA card for Ollama
(measured on the machine of the user, 2026-10-23).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_MIB = 1024 * 1024

#: macOS lets the GPU use a part of the unified memory only.  Two thirds is
#: the lower limit of the default (larger machines get three quarters).
_APPLE_GPU_SHARE = 2 / 3


@dataclass(frozen=True)
class Memory:
    """The memory of this machine in MiB; None when it is not known.

    ``unified`` is True on Apple Silicon: the GPU uses a part of the RAM.
    ``gpu_others_mib`` is the memory of the GPU that is not of a compute
    process now: the desktop, the other graphics programs and the driver.
    ``ram_available_mib`` is the RAM that a program can get now.  The two
    describe the moment of the call, the others the machine.
    """

    gpu_mib: int | None
    ram_mib: int | None
    unified: bool = False
    gpu_others_mib: int | None = None
    ram_available_mib: int | None = None


def machine_memory() -> Memory:
    """Return the GPU memory and the RAM of this machine, and the part of each that other programs hold now."""
    ram, available = _ram_mib(), _ram_available_mib()
    if _apple_silicon():
        return Memory(gpu_mib=int(ram * _APPLE_GPU_SHARE) if ram else None, ram_mib=ram, unified=True,
                      ram_available_mib=available)
    gpu = nvidia_gpu()
    return Memory(gpu_mib=gpu.total_mib if gpu else None, ram_mib=ram,
                  gpu_others_mib=_others_mib(gpu) if gpu else None, ram_available_mib=available)


@dataclass(frozen=True)
class NvidiaGpu:
    """The largest NVIDIA GPU: its id, its total memory and its free memory now, in MiB (None: not known)."""

    uuid: str
    total_mib: int
    free_mib: int | None


class NvidiaSmiProblem(Exception):
    """``nvidia-smi`` does not give the GPUs: *missing* when it is not installed, else the first line of its error."""

    def __init__(self, message: str, *, missing: bool) -> None:
        super().__init__(message)
        self.missing = missing


def _run_nvidia_smi(*query: str) -> list[list[str]]:
    """Run ``nvidia-smi`` with *query* in the CSV form; return the fields of each line.

    Raises:
        NvidiaSmiProblem: It is not installed, or it fails (a driver
            update without a restart, no NVIDIA kernel driver).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", *query, "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
        )
    except FileNotFoundError:
        raise NvidiaSmiProblem("nvidia-smi is not installed", missing=True) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NvidiaSmiProblem(f"nvidia-smi does not run ({type(exc).__name__})", missing=False) from None
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
        raise NvidiaSmiProblem(lines[0] if lines else f"nvidia-smi gives exit code {result.returncode}",
                               missing=False)
    return [[field.strip() for field in line.split(",")] for line in result.stdout.splitlines() if line.strip()]


def _nvidia_smi(*query: str) -> list[list[str]] | None:
    """Run ``nvidia-smi`` with *query*; return the fields of each line, or None when it does not run."""
    try:
        return _run_nvidia_smi(*query)
    except NvidiaSmiProblem:
        return None


def nvidia_gpu() -> NvidiaGpu | None:
    """Return the largest NVIDIA GPU, from ``nvidia-smi``; None without one.

    The largest and not the sum: Ollama puts a model on one GPU when it
    fits there, and a model split across GPUs is slower.  WHY
    ``memory.free`` and not ``memory.used``: the driver reserves a part
    that neither counts as used (380 MiB of 12282 here, 2026-10-23), and
    no model gets it.  A free memory that is not a number ("[N/A]") keeps
    the GPU with its total memory: only the memory of now is then unknown.
    """
    rows = _nvidia_smi("--query-gpu=uuid,memory.total,memory.free")
    gpus = [NvidiaGpu(uuid, int(total), int(free) if free.isdigit() else None)
            for uuid, total, free in (row for row in rows or [] if len(row) == 3) if total.isdigit()]
    return max(gpus, key=lambda gpu: gpu.total_mib) if gpus else None


def nvidia_smi_problem() -> NvidiaSmiProblem | None:
    """Return why ``nvidia-smi`` gives no GPU memory, or None when it gives it (for the check of ``fw-context doctor``)."""
    try:
        rows = _run_nvidia_smi("--query-gpu=memory.total")
    except NvidiaSmiProblem as exc:
        return exc
    if not any(row and row[0].isdigit() for row in rows):
        return NvidiaSmiProblem("nvidia-smi gives no memory of a GPU", missing=False)
    return None


def _others_mib(gpu: NvidiaGpu) -> int | None:
    """Return the memory of *gpu* that no compute process can get now, or None when it is not known.

    WHY each compute process and not the processes of Ollama only (decision
    of the user, 2026-10-23): a process of Ollama is known only by its name
    or path ("llama-server" now, "ollama" in older releases), and a guess
    from a name is no fact.  What remains is the memory of the desktop, of
    the other graphics programs and the reserve of the driver: the part
    that changes from machine to machine.  Another compute program on the
    same GPU is rare, and the estimate then gives a larger model.  WHY not
    ``/api/ps``: its ``size_vram`` is not the memory on the GPU
    (``llm.calibration``).  Measured 2026-10-23: a model of Ollama held
    2278 MiB, and ``memory.used`` was 2320 MiB.  On Windows (WDDM) the
    memory of a process is "[N/A]", thus None there.
    """
    if gpu.free_mib is None:
        return None
    rows = _nvidia_smi("--query-compute-apps=gpu_uuid,used_memory")
    if rows is None:
        return None
    held = 0
    for row in rows:
        if len(row) != 2 or row[0] != gpu.uuid:
            continue
        if not row[1].isdigit():
            return None
        held += int(row[1])
    return max(0, gpu.total_mib - gpu.free_mib - held)


def _ram_mib() -> int | None:
    """Return the RAM of this machine: ``sysctl hw.memsize`` on macOS, ``GlobalMemoryStatusEx`` on Windows, else ``sysconf``."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["sysctl", "-n", "hw.memsize"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return int(value) // _MIB if result.returncode == 0 and value.isdigit() else None
    if sys.platform == "win32":
        status = _windows_memory_status()
        return status[0] // _MIB if status else None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // _MIB
    except (AttributeError, ValueError, OSError):
        return None


def _ram_available_mib() -> int | None:
    """Return the RAM that a program can get now: ``MemAvailable`` on Linux, ``GlobalMemoryStatusEx`` on Windows.

    None on macOS: ``vm_stat`` gives counts of pages, and the sum that
    matches ``MemAvailable`` is not verified on a Mac; the estimate then
    uses its fixed reserve.
    """
    if sys.platform == "win32":
        status = _windows_memory_status()
        return status[1] // _MIB if status else None
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        name, _, value = line.partition(":")
        fields = value.split()
        if name == "MemAvailable" and fields and fields[0].isdigit():
            return int(fields[0]) // 1024  # the value is in kB
    return None


def _windows_memory_status() -> tuple[int, int] | None:
    """Return the total and the available physical memory in bytes (``GlobalMemoryStatusEx``), or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MemoryStatusEx),)
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _apple_silicon() -> bool:
    from .auto_model import _apple_silicon_gpu

    return _apple_silicon_gpu()
