"""The memory of the machine (``llm/hardware.py``) and the estimate from it (``llm/recommend.estimate``).

No test runs ``nvidia-smi`` or reads the memory of this machine: the
commands and ``/proc/meminfo`` are replaced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm import hardware
from fw_context_mcp.llm.hardware import Memory
from fw_context_mcp.llm.recommend import Fit, estimate

GPU_A = "GPU-aaaa"
GPU_B = "GPU-bbbb"


def _nvidia(monkeypatch, gpus: str, apps: str | None = "", *, returncode: int = 0,
            stderr: str = "") -> list[list[str]]:
    """Replace ``nvidia-smi``: *gpus* (uuid, total, free) for --query-gpu, *apps* for --query-compute-apps.

    *apps* None: that call fails.  ``--query-gpu=memory.total`` alone (the
    check of doctor) gets the total of each line of *gpus*.
    """
    calls: list[list[str]] = []

    def _run(args, **kwargs):
        calls.append(list(args))
        if "--query-gpu=memory.total" in args:
            totals = "".join(f"{line.split(',')[1].strip()}\n" for line in gpus.splitlines() if line.strip())
            return subprocess.CompletedProcess(args, returncode, stdout=totals, stderr=stderr)
        if any(arg.startswith("--query-gpu") for arg in args):
            return subprocess.CompletedProcess(args, returncode, stdout=gpus, stderr=stderr)
        if apps is None:
            return subprocess.CompletedProcess(args, 9, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=apps, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


# ── The NVIDIA GPU ──


def test_the_largest_nvidia_gpu_counts(monkeypatch):
    _nvidia(monkeypatch, f"{GPU_A}, 8192, 8000\n{GPU_B}, 12282, 11870\n")

    gpu = hardware.nvidia_gpu()

    assert (gpu.uuid, gpu.total_mib, gpu.free_mib) == (GPU_B, 12282, 11870)


def test_no_nvidia_smi_is_no_gpu(monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", _missing)

    assert hardware.nvidia_gpu() is None
    problem = hardware.nvidia_smi_problem()
    assert problem is not None and problem.missing


def test_a_failed_nvidia_smi_is_no_gpu_and_names_its_error(monkeypatch):
    """A driver update without a restart: nvidia-smi is installed and fails."""
    _nvidia(monkeypatch, "", returncode=18, stderr="Failed to initialize NVML: Driver/library version mismatch\n")

    assert hardware.nvidia_gpu() is None
    problem = hardware.nvidia_smi_problem()
    assert problem is not None and not problem.missing
    assert str(problem) == "Failed to initialize NVML: Driver/library version mismatch"


def test_a_free_memory_that_is_not_a_number_keeps_the_gpu(monkeypatch):
    """Before, one field that was not a number dropped the GPU, and the estimate counted no GPU."""
    _nvidia(monkeypatch, f"{GPU_A}, 12282, [N/A]\n")

    gpu = hardware.nvidia_gpu()

    assert (gpu.total_mib, gpu.free_mib) == (12282, None)
    assert hardware._others_mib(gpu) is None


def test_the_other_programs_hold_the_memory_that_no_compute_process_can_get(monkeypatch):
    """Measured 2026-10-23: 12282 MiB, 380 reserved by the driver, a model of Ollama 2278, memory.used 2320.

    The free memory was 12282 - 2320 - 380 = 9582 MiB: the desktop, the
    other programs and the reserve of the driver hold 12282 - 9582 - 2278
    = 422 MiB.
    """
    _nvidia(monkeypatch, f"{GPU_A}, 12282, 9582\n",
            f"{GPU_A}, 2278\n{GPU_B}, 999\n")  # a process of another GPU does not count

    assert hardware._others_mib(hardware.nvidia_gpu()) == 422


@pytest.mark.parametrize("apps", [None, f"{GPU_A}, [N/A]\n"], ids=["the call fails", "WDDM gives N/A"])
def test_a_process_memory_that_is_not_known_gives_no_value(monkeypatch, apps):
    """Windows (WDDM) gives "[N/A]" for the memory of a process: the estimate then uses its fixed reserve."""
    _nvidia(monkeypatch, f"{GPU_A}, 12282, 9582\n", apps)

    assert hardware._others_mib(hardware.nvidia_gpu()) is None


def test_the_machine_memory_holds_the_total_and_the_others(monkeypatch):
    _nvidia(monkeypatch, f"{GPU_A}, 12282, 9482\n", f"{GPU_A}, 2000\n")
    monkeypatch.setattr(hardware, "_apple_silicon", lambda: False)
    monkeypatch.setattr(hardware, "_ram_mib", lambda: 64000)
    monkeypatch.setattr(hardware, "_ram_available_mib", lambda: 40000)

    assert hardware.machine_memory() == Memory(gpu_mib=12282, ram_mib=64000, gpu_others_mib=800,
                                               ram_available_mib=40000)


def test_apple_silicon_has_a_share_of_the_ram_and_no_others(monkeypatch):
    monkeypatch.setattr(hardware, "_apple_silicon", lambda: True)
    monkeypatch.setattr(hardware, "_ram_mib", lambda: 32768)
    monkeypatch.setattr(hardware, "_ram_available_mib", lambda: None)

    assert hardware.machine_memory() == Memory(gpu_mib=21845, ram_mib=32768, unified=True)


# ── The RAM ──


def test_the_available_ram_comes_from_meminfo(monkeypatch, tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       65429572 kB\nMemFree:  1000 kB\nMemAvailable:   44363244 kB\n",
                       encoding="ascii")
    real = Path

    monkeypatch.setattr(hardware.sys, "platform", "linux")
    monkeypatch.setattr(hardware, "Path", lambda path: meminfo if path == "/proc/meminfo" else real(path))

    assert hardware._ram_available_mib() == 44363244 // 1024


def test_meminfo_without_the_field_gives_no_value(monkeypatch, tmp_path: Path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\n", encoding="ascii")
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    monkeypatch.setattr(hardware, "Path", lambda path: meminfo)

    assert hardware._ram_available_mib() is None


def test_windows_reads_the_memory_status(monkeypatch):
    """GlobalMemoryStatusEx: the total RAM and the RAM that is available now (before 2026-10-23: no RAM at all)."""
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    monkeypatch.setattr(hardware, "_windows_memory_status", lambda: (16 * 1024**3, 6 * 1024**3))

    assert (hardware._ram_mib(), hardware._ram_available_mib()) == (16384, 6144)


def test_windows_without_the_status_gives_no_value(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    monkeypatch.setattr(hardware, "_windows_memory_status", lambda: None)

    assert (hardware._ram_mib(), hardware._ram_available_mib()) == (None, None)


def test_the_memory_status_is_none_outside_windows(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "linux")

    assert hardware._windows_memory_status() is None


# ── The estimate ──


def test_an_unknown_other_memory_keeps_the_old_reserve_of_4096():
    """Without nvidia-smi values the estimate is the one before 2026-10-23: 4096 MiB of reserve in all."""
    old = Memory(gpu_mib=12282, ram_mib=64000)

    # 12282 - 4096 = 8186 MiB for the weights: 8.5 GB (8106 MiB) fits, 8.7 GB (8297 MiB) does not.
    assert estimate(8.5, False, old) is Fit.GPU
    assert estimate(8.7, False, old) is Fit.DENSE_IN_RAM


def test_a_free_gpu_takes_a_larger_model():
    """The GPU of the user, only for Ollama since 2026-10-23: the desktop holds nothing on it."""
    free = Memory(gpu_mib=12282, ram_mib=64000, gpu_others_mib=34)

    # 12282 - 1331 - 34 = 10917 MiB for the weights: 11.0 GB (10490 MiB) fits.
    assert estimate(11.0, False, free) is Fit.GPU
    assert estimate(12.0, False, free) is Fit.DENSE_IN_RAM


def test_a_busy_gpu_takes_a_smaller_model():
    busy = Memory(gpu_mib=12282, ram_mib=64000, gpu_others_mib=6000)

    # 12282 - 1331 - 6000 = 4951 MiB: 5.5 GB (5245 MiB) does not fit, 5.0 GB (4768 MiB) does.
    assert estimate(5.5, False, busy) is Fit.DENSE_IN_RAM
    assert estimate(5.5, True, busy) is Fit.MOE_IN_RAM
    assert estimate(5.0, False, busy) is Fit.GPU


def test_the_available_ram_decides_the_part_outside_the_gpu():
    tight = Memory(gpu_mib=12282, ram_mib=64000, gpu_others_mib=34, ram_available_mib=2000)
    roomy = Memory(gpu_mib=12282, ram_mib=64000, gpu_others_mib=34, ram_available_mib=40000)

    # 20 GB = 19073 MiB: 19073 - 10917 = 8156 MiB outside the GPU.
    assert estimate(20.0, False, tight) is None
    assert estimate(20.0, False, roomy) is Fit.DENSE_IN_RAM


def test_without_the_available_ram_the_reserve_of_8_gib_applies():
    machine = Memory(gpu_mib=12282, ram_mib=16384, gpu_others_mib=34)

    # 16384 - 8192 = 8192 MiB of RAM: 8156 MiB outside the GPU fits, 9000 more does not.
    assert estimate(20.0, False, machine) is Fit.DENSE_IN_RAM
    assert estimate(21.0, False, machine) is None


# ── The doctor check gpu-tools ──


def _pci(tmp_path: Path, *devices: tuple[str, str]) -> Path:
    """Make a /sys/bus/pci/devices of *devices* (vendor, class).

    The check reads no device name.  WHY a name without a colon: a name of
    sysfs is a PCI address such as ``0000:01:00.0``, and Windows does not
    let a file name hold a colon (WinError 123).
    """
    root = tmp_path / "devices"
    for number, (vendor, device_class) in enumerate(devices):
        device = root / f"0000-0{number}-00.0"
        device.mkdir(parents=True)
        (device / "vendor").write_text(f"{vendor}\n", encoding="ascii")
        (device / "class").write_text(f"{device_class}\n", encoding="ascii")
    root.mkdir(exist_ok=True)
    return root


#: A config whose chat and embeddings run on the Ollama of this machine.
LOCAL = LLMConfig()


@pytest.fixture
def linux(monkeypatch):
    from fw_context_mcp.deps import _checks

    monkeypatch.setattr(_checks.sys, "platform", "linux")
    return _checks


def test_an_nvidia_gpu_without_nvidia_smi_names_the_package(linux, monkeypatch, tmp_path: Path):
    """Without nvidia-smi the recommendation counts no GPU: the check names the package, and installs nothing."""
    monkeypatch.setattr(linux, "_PCI_DEVICES", _pci(tmp_path, ("0x1002", "0x030000"), ("0x10de", "0x030000")))

    def _missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", _missing)

    result = linux.check_gpu_tools(LOCAL)

    assert (result.status, result.critical, result.fix_cmd) == ("degraded", False, None)
    assert "not installed" in result.message
    assert "sudo pacman -S nvidia-utils" in result.instructions
    assert "sudo apt install nvidia-smi" in result.instructions


def test_a_failing_nvidia_smi_names_the_usual_causes_and_no_package(linux, monkeypatch, tmp_path: Path):
    """The driver of Arch and Manjaro always brings nvidia-smi: an install does not repair a failure."""
    monkeypatch.setattr(linux, "_PCI_DEVICES", _pci(tmp_path, ("0x10de", "0x030000")))
    _nvidia(monkeypatch, "", returncode=18, stderr="Failed to initialize NVML: Driver/library version mismatch\n")

    result = linux.check_gpu_tools(LOCAL)

    assert result.status == "degraded"
    assert "Driver/library version mismatch" in result.message
    assert "restart the machine" in result.instructions and "pacman -S" not in result.instructions


def test_an_nvidia_gpu_with_nvidia_smi_is_ok(linux, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(linux, "_PCI_DEVICES", _pci(tmp_path, ("0x10de", "0x030000")))
    _nvidia(monkeypatch, f"{GPU_A}, 12282, 11870\n")

    result = linux.check_gpu_tools(LOCAL)

    assert (result.status, result.message) == ("ok", "nvidia-smi: 12282 MiB")


@pytest.mark.parametrize("devices", [(("0x1002", "0x030000"),), (("0x10de", "0x040300"),), ()],
                         ids=["an AMD GPU only", "an NVIDIA audio device", "no device"])
def test_a_machine_without_an_nvidia_gpu_is_skipped(linux, monkeypatch, tmp_path: Path, devices):
    monkeypatch.setattr(linux, "_PCI_DEVICES", _pci(tmp_path, *devices))

    assert linux.check_gpu_tools(LOCAL).status == "skipped"


@pytest.mark.parametrize("cfg", [
    LLMConfig(enabled=False),
    LLMConfig(provider="deepseek", chat_api_base="https://api.deepseek.com", chat_api_format="openai",
              ollama_url="http://gpu-box:11434"),
], ids=["the LLM is off", "an API and an Ollama of another computer"])
def test_a_config_without_the_local_ollama_is_skipped(linux, monkeypatch, tmp_path: Path, cfg):
    """The recommendation reads the GPU only for the Ollama of this machine."""
    monkeypatch.setattr(linux, "_PCI_DEVICES", _pci(tmp_path, ("0x10de", "0x030000")))

    result = linux.check_gpu_tools(cfg)

    assert result.status == "skipped" and "Ollama of this machine" in result.message


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_another_system_is_skipped(monkeypatch, platform):
    from fw_context_mcp.deps import _checks

    monkeypatch.setattr(_checks.sys, "platform", platform)

    result = _checks.check_gpu_tools(LOCAL)

    assert result.status == "skipped" and "macOS" in result.message and "Windows" in result.message


# ── The setup ──


def test_the_setup_shows_the_memory_of_now(monkeypatch, capsys):
    from fw_context_mcp.cli import _llm_wizard as wizard
    from fw_context_mcp.llm import _diag

    monkeypatch.setattr(hardware, "machine_memory", lambda: Memory(gpu_mib=12282, ram_mib=63896, gpu_others_mib=412,
                                                                   ram_available_mib=43323))
    monkeypatch.setattr(_diag, "installed_models", lambda url: None)

    wizard._machine_report()

    out = capsys.readouterr().out
    assert "GPU 12.0 GiB (11.6 GiB free for models now)" in out
    assert "RAM 62.4 GiB (42.3 GiB available now)" in out


def test_a_model_that_takes_all_of_the_available_ram_does_not_fit():
    """The review of 2026-10-23: 16 GiB without a GPU, 15000 MiB available, a model of 15.0 GB (14305 MiB).

    Without the runtime reserve on the RAM side, the estimate gave "in RAM"
    with 0.7 GiB left for the KV cache, the buffers and the desktop.
    """
    idle = Memory(gpu_mib=None, ram_mib=16384, ram_available_mib=15000)

    # 15000 - 1331 = 13669 MiB.
    assert estimate(15.0, False, idle) is None
    assert estimate(14.0, False, idle) is Fit.DENSE_IN_RAM
