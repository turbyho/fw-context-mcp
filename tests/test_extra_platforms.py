"""Native projects of the extended set of the test SDKs: ``fw-context init`` and ``index --build`` over a real build.

The extended set adds the architectures and the builders that the set of
CI does not have (``tests/sdk/versions.toml``, ``[extra]``):

- PlatformIO with the Arduino framework: ESP32 (Xtensa), Nucleo L476RG
  (ARM with the newlib of PlatformIO, the shape of a STM32 project) and
  Uno (AVR, 8 bits).
- ESP-IDF on ESP32: the builder of ESP-IDF, with the image ``bootloader``
  next to the application.
- Arduino CLI on Uno: the builder of Arduino.

Each test checks the same minimum: the code of the project is in the
index, each unit has the one target of its driver, the manifest verified
each header, and the macros step gave each unit its values.

The config of each project carries the environment of the test SDKs in
``local.toml``, as a user selects an install that is not the default one:
the index run gets no variable of the shell (``daemon_env``).

Marker ``extra_platforms``: only ``make test-platforms`` runs these tests,
after ``make test-sdk-platforms``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.config import load as load_config
from fw_context_mcp.config._toml_editor import set_key
from fw_context_mcp.indexer._run_marker import read_state
from fw_context_mcp.utils import venv_program
from tests import sdk
from tests._projects import (
    IndexedProject,
    InstalledSdks,
    copy_project,
    daemon_env,
    definitions,
    index_project,
    init_project,
    installed_sdks,
    manifest_verification,
    targets_of_each_unit,
    work_directory,
)

pytestmark = [pytest.mark.extra_platforms]


@pytest.fixture(scope="module")
def test_sdks() -> InstalledSdks:
    return installed_sdks()


@pytest.fixture(scope="module")
def work_dir(tmp_path_factory: pytest.TempPathFactory, test_sdks: InstalledSdks) -> Iterator[Path]:
    yield from work_directory(tmp_path_factory, test_sdks)


def _check_the_build(project: IndexedProject, image: str, names: set[str], target: str) -> None:
    """The minimum of each project: its code, one target for each unit, a full manifest, and the macros of each unit."""
    assert definitions(project, image, names) == names
    assert targets_of_each_unit(project, image) == {(f"--target={target}",)}
    assert manifest_verification(project, image) == "full"
    assert read_state(project.db_path.parent, project.builds[image]) is None, "the macros step failed for a unit"


# ── PlatformIO with the Arduino framework ────────────────────────────────

#: The fixture of each PlatformIO project, and the target that its driver gives.
_PLATFORMIO = {
    "platformio_esp32": "xtensa-esp-elf",
    "platformio_nucleo_l476rg": "arm-none-eabi",
    "platformio_uno": "avr",
}


@pytest.fixture(scope="module", params=sorted(_PLATFORMIO))
def platformio(request: pytest.FixtureRequest, work_dir: Path, test_sdks: InstalledSdks) -> tuple[str, IndexedProject]:
    """A PlatformIO project with the pinned PlatformIO and its core directory in ``local.toml``."""
    fixture = request.param
    project = copy_project(fixture, work_dir / fixture)
    init_project(project, sdk.tool_env(test_sdks.root, test_sdks.versions, test_sdks.zephyr_sdk))
    local = project / ".fw-context" / "local.toml"
    set_key(local, "build", "python", str(venv_program(test_sdks.root / "venv", "python")))
    set_key(local, "build", "env", {
        "PLATFORMIO_CORE_DIR": str(sdk.platformio_core(test_sdks.root)),
        "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "No",
    })
    return fixture, index_project(project, daemon_env(test_sdks.root))


def test_platformio_arduino(platformio: tuple[str, IndexedProject]):
    fixture, project = platformio
    assert list(project.builds) == [""]
    _check_the_build(
        project, "", {"blink_init", "blink_next", "BlinkState", "Led", "setup", "loop"}, _PLATFORMIO[fixture],
    )


# ── ESP-IDF on ESP32 ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def esp_idf(work_dir: Path, test_sdks: InstalledSdks) -> IndexedProject:
    """The ESP-IDF project: init takes the ESP-IDF of IDF_PATH of the shell, and ``local.toml`` adds the tools and the pinned Python.

    WHY init gets IDF_PATH: init must select the ESP-IDF of the shell, also
    on a host with an EIM install in the home directory.  WHY the Python
    first on PATH: ``export.sh`` selects the Python env of ESP-IDF by the
    version of the ``python3`` of PATH, and the provision made that env
    with the pinned Python.  WHY the venv of the test SDKs: ESP-IDF on
    Linux and macOS takes ``cmake`` and ``ninja`` from the user, and the
    venv holds the pinned ones (measured: macOS has no other ``cmake``, and
    on Linux the test used ``/usr/bin/cmake`` of the host).
    """
    project = copy_project("esp_idf", work_dir / "idf")
    shell = sdk.tool_env(test_sdks.root, test_sdks.versions, test_sdks.zephyr_sdk)
    shell["IDF_PATH"] = str(sdk.esp_idf_dir(test_sdks.root, test_sdks.versions))
    init_project(project, shell)
    local = project / ".fw-context" / "local.toml"
    assert load_config(project_root=project).build.activate == str(
        sdk.esp_idf_export_script(test_sdks.root, test_sdks.versions)
    ), "init takes the ESP-IDF of the shell"
    set_key(local, "build", "extra_path", [
        str(sdk.python_program(test_sdks.root).parent), str(sdk.venv_bin(test_sdks.root)),
    ])
    set_key(local, "build", "env", {"IDF_TOOLS_PATH": str(sdk.idf_tools_path(test_sdks.root))})
    return index_project(project, daemon_env(test_sdks.root))


def test_esp_idf_application_and_bootloader(esp_idf: IndexedProject):
    """idf.py builds the application and its bootloader: two images of one build, each with its own code."""
    assert set(esp_idf.builds) == {"fwctx_test_esp_idf", "bootloader"}
    _check_the_build(esp_idf, "fwctx_test_esp_idf", {"app_main", "counter_step", "Counter"}, "xtensa-esp-elf")
    _check_the_build(esp_idf, "bootloader", {"call_start_cpu0"}, "xtensa-esp-elf")
    assert not definitions(esp_idf, "bootloader", {"app_main"}), "the bootloader holds no code of the application"


# ── Arduino CLI on Uno ───────────────────────────────────────────────────


@pytest.fixture(scope="module")
def arduino(work_dir: Path, test_sdks: InstalledSdks) -> IndexedProject:
    """The sketch with the arduino-cli and the cores of the test SDKs in ``local.toml``.

    The config names no board: the build takes ``default_fqbn`` of
    ``sketch.yaml``, and the target ``avr`` of each unit shows that board.
    """
    project = copy_project("arduino", work_dir / "arduino")
    shell = sdk.tool_env(test_sdks.root, test_sdks.versions, test_sdks.zephyr_sdk)
    shell.update(sdk.arduino_env(test_sdks.root))
    shell["PATH"] = os.pathsep.join([str(sdk.arduino_cli_dir(test_sdks.root)), shell["PATH"]])
    init_project(project, shell)
    assert not load_config(project_root=project).build.fqbn, "the board comes from sketch.yaml"
    local = project / ".fw-context" / "local.toml"
    set_key(local, "build", "extra_path", [str(sdk.arduino_cli_dir(test_sdks.root))])
    set_key(local, "build", "env", sdk.arduino_env(test_sdks.root))
    return index_project(project, daemon_env(test_sdks.root))


def test_arduino_cli(arduino: IndexedProject):
    assert list(arduino.builds) == [""]
    _check_the_build(
        arduino, "", {"set_led_state", "set_led_interval", "update_led", "setup", "loop", "LedConfig"}, "avr",
    )
