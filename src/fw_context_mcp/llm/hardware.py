"""The memory of this machine for a local model: the GPU memory and the RAM.

The recommendation of a model (``llm.recommend``) estimates from it where
a model that is not installed runs, and the key of a calibration
(``llm.calibration``) holds it: a new GPU or more RAM makes a new
calibration.  WHY total and not free memory: the free memory changes with
each other program, and the measured speed of a model tells it at the
time of the run.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MIB = 1024 * 1024

#: macOS lets the GPU use a part of the unified memory only.  Two thirds is
#: the lower limit of the default (larger machines get three quarters).
_APPLE_GPU_SHARE = 2 / 3


@dataclass(frozen=True)
class Memory:
    """The memory of this machine in MiB; None when it is not known.

    ``unified`` is True on Apple Silicon: the GPU uses a part of the RAM.
    """

    gpu_mib: int | None
    ram_mib: int | None
    unified: bool = False


def machine_memory() -> Memory:
    """Return the GPU memory and the RAM of this machine."""
    ram = _ram_mib()
    if _apple_silicon():
        return Memory(gpu_mib=int(ram * _APPLE_GPU_SHARE) if ram else None, ram_mib=ram, unified=True)
    return Memory(gpu_mib=_nvidia_mib(), ram_mib=ram)


def _nvidia_mib() -> int | None:
    """Return the memory of the largest NVIDIA GPU, from ``nvidia-smi``; None without one.

    The largest and not the sum: Ollama puts a model on one GPU when it
    fits there, and a model split across GPUs is slower.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sizes = [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    return max(sizes) if sizes else None


def _ram_mib() -> int | None:
    """Return the RAM of this machine: ``sysconf`` on Linux, ``sysctl hw.memsize`` on macOS; None on Windows."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return int(value) // _MIB if result.returncode == 0 and value.isdigit() else None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // _MIB
    except (AttributeError, ValueError, OSError):
        return None


def _apple_silicon() -> bool:
    from .auto_model import _apple_silicon_gpu

    return _apple_silicon_gpu()
