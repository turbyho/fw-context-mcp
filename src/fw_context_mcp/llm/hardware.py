"""The memory of this machine for a local model: the GPU memory and the RAM.

The setup of a local Ollama profile recommends a chat model from the
memory that the model can use.  WHY total and not free memory: the setup
runs once, and the free memory changes with each other program.  Each
run selects the context, not the model, from the measured speed of the
model (``llm.calibration``).

The recommendation comes from the measurement of 2026-10-20 (59 variants,
30 symbols of C/C++ firmware, blind grades 1-5; speed on a Ryzen 9 9900X
with an RTX 4070 SUPER 12 GB, ``plans/eval/llm-models-2026-10-20``):

==========================  ===================  =======  ===============
memory for the model        model                grade    seconds/symbol
==========================  ===================  =======  ===============
a 12 GB GPU                 gemma4:26b (MoE)     3.73     6.2
an 8 GB GPU                 gemma4:12b           3.50     2.7 (all on GPU)
less, RAM >= 32 GiB         qwen3-coder:30b MoE  3.63     12.3 (CPU only)
less                        qwen2.5-coder:7b     2.80     -
==========================  ===================  =======  ===============

A MoE model runs few experts for each token, thus it is usable on the CPU
when the RAM holds it.  The values are a recommendation: the user selects
the model.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MIB = 1024 * 1024

#: The smallest memory of a "12 GB" and of an "8 GB" card, in MiB.  WHY not
#: 12 * 1024: the driver reports a little less than the size on the box
#: (an RTX 4070 SUPER 12 GB: 12282 MiB, measured 2026-10-21).
_CARD_12_GB = 11 * 1024 + 512
_CARD_8_GB = 7 * 1024 + 512

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


@dataclass(frozen=True)
class Recommendation:
    """A recommended local chat model, and the reason for a person."""

    model: str
    reason: str


def machine_memory() -> Memory:
    """Return the GPU memory and the RAM of this machine."""
    ram = _ram_mib()
    if _apple_silicon():
        return Memory(gpu_mib=int(ram * _APPLE_GPU_SHARE) if ram else None, ram_mib=ram, unified=True)
    return Memory(gpu_mib=_nvidia_mib(), ram_mib=ram)


def recommended_local_model(memory: Memory) -> Recommendation:
    """Return the chat model that the measurement of 2026-10-20 recommends for *memory* (see the module)."""
    gpu, ram = memory.gpu_mib or 0, memory.ram_mib or 0
    if gpu >= _CARD_12_GB:
        return Recommendation("gemma4:26b", f"{_gib(gpu)} GiB of GPU memory: the best local model of the measurement")
    if gpu >= _CARD_8_GB:
        return Recommendation("gemma4:12b", f"{_gib(gpu)} GiB of GPU memory: the model fits on the GPU")
    if ram >= 32 * 1024:
        return Recommendation(
            "qwen3-coder:30b", f"{_gib(ram)} GiB of RAM: a MoE model that is usable on the CPU (slower)",
        )
    return Recommendation("qwen2.5-coder:7b", "little memory: a small model (weaker analyses)")


def _gib(mib: int) -> str:
    return f"{mib / 1024:.0f}"


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
