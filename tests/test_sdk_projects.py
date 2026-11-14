"""Native projects of the embedded SDKs: ``fw-context init`` and ``fw-context index --build`` over a real build.

Each project builds with the tool of its SDK, in the versions that
``tests/sdk/versions.toml`` pins (``make test-sdk`` installs them), and the
test reads the index that the run wrote:

- Zephyr ``nrf52840dk/nrf52840`` with sysbuild: the image of the
  application (``domains.yaml``), Kconfig and devicetree in generated
  headers, an interrupt handler of ``IRQ_CONNECT``, the environment of an
  upstream Zephyr workspace without NCS.  No second image (MCUboot): it
  doubles the time of the index run (decision of the user, 2026-11-13).
- PlatformIO ESP32-C3 with the Arduino framework (RISC-V): C and C++ of the
  framework, the include directories of a response file, the macro values.

WHY the environment of the index run has no variable of the shell: a build
in the background (the daemon of an MCP client) has none.  The config that
init wrote must carry the environment.  init runs with ``--quick``: without
it, init registers the MCP server in the global configs of the AI tools of
the user (``config/tools.py``), and the isolation of the tests does not
cover ``HOME``.

Marker ``sdk_project``: ``make test-all`` runs these tests, ``make test``
does not, and CI runs them in one job (each index run takes minutes).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.config import load as load_config
from fw_context_mcp.config._toml_editor import set_key
from fw_context_mcp.indexer._run_marker import read_state
from fw_context_mcp.indexer.db import open_db
from fw_context_mcp.indexer.models import RefOrigin
from fw_context_mcp.utils import venv_program
from tests import sdk
from tests._projects import (
    IndexedProject,
    InstalledSdks,
    copy_project,
    daemon_env,
    definitions,
    expanded_values,
    index_project,
    init_project,
    installed_sdks,
    manifest_verification,
    targets_of_each_unit,
    work_directory,
)

pytestmark = [pytest.mark.sdk_project]


@pytest.fixture(scope="module")
def test_sdks() -> InstalledSdks:
    return installed_sdks()


@pytest.fixture(scope="module")
def work_dir(tmp_path_factory: pytest.TempPathFactory, test_sdks: InstalledSdks) -> Iterator[Path]:
    yield from work_directory(tmp_path_factory, test_sdks)


# ── Zephyr: nrf52840dk with sysbuild ─────────────────────────────────────


@pytest.fixture(scope="module")
def zephyr(work_dir: Path, test_sdks: InstalledSdks) -> IndexedProject:
    """The Zephyr fixture as an upstream workspace gives it: init in the shell of the workspace, index without it."""
    project = copy_project("zephyr", work_dir / "app")
    shell = sdk.tool_env(test_sdks.root, test_sdks.versions, test_sdks.zephyr_sdk)
    init_project(project, shell)
    config = project / ".fw-context" / "config.toml"
    set_key(config, "build", "board", "nrf52840dk/nrf52840")
    set_key(config, "build", "sysbuild", True)
    return index_project(project, daemon_env(test_sdks.root))


class TestZephyrSysbuild:
    def test_init_writes_the_environment_of_the_workspace(self, zephyr: IndexedProject, test_sdks: InstalledSdks):
        """No NCS and no ``environment-setup-*`` (Zephyr SDK 1.0): the config carries the shell."""
        build = load_config(project_root=zephyr.root).build
        assert build.env["ZEPHYR_BASE"] == str(sdk.zephyr_workspace(test_sdks.root, test_sdks.versions) / "zephyr")
        assert build.env["ZEPHYR_SDK_INSTALL_DIR"] == str(test_sdks.zephyr_sdk)
        assert build.extra_path[0] == str(sdk.venv_bin(test_sdks.root))
        assert not build.activate

    def test_the_image_of_the_application_is_the_build(self, zephyr: IndexedProject):
        """The sysbuild names the image of the application by its directory (``domains.yaml``)."""
        assert set(zephyr.builds) == {"app"}

    def test_the_code_of_the_application(self, zephyr: IndexedProject):
        names = {"msg_init", "msg_send", "msg_recv", "msg_irq_init", "main"}
        assert definitions(zephyr, "app", names) == names

    def test_kconfig_gives_its_macros_with_their_values(self, zephyr: IndexedProject):
        """``CONFIG_PRINTK`` of ``prj.conf``, from the generated ``autoconf.h``."""
        conn = open_db(zephyr.db_path)
        try:
            row = conn.execute(
                "SELECT m.value, m.expanded_values, f.generated FROM macros m JOIN files f ON f.id = m.file_id "
                "WHERE m.config_hash = ? AND m.name = 'CONFIG_PRINTK' AND f.path LIKE '%/autoconf.h'",
                (zephyr.builds["app"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["value"] == "1"
        assert row["expanded_values"], "the macros step gave the row no value"
        assert row["generated"] == 1

    def test_the_devicetree_header_is_a_generated_file(self, zephyr: IndexedProject):
        conn = open_db(zephyr.db_path)
        try:
            rows = conn.execute(
                "SELECT generated FROM files WHERE config_hash = ? AND path LIKE '%/devicetree_generated.h'",
                (zephyr.builds["app"],),
            ).fetchall()
        finally:
            conn.close()
        assert [row["generated"] for row in rows] == [1]

    def test_the_dispatcher_of_the_interrupts_calls_the_handler_of_irq_connect(self, zephyr: IndexedProject):
        """No call of the code reaches ``swi_isr``: the registration of ``IRQ_CONNECT`` in the build does.

        Zephyr puts a handler that is not direct into the software table
        ``_sw_isr_table``, which ``_isr_wrapper`` calls through.  The index
        reads the registrations from the linked ``zephyr_pre0.elf``
        (``indexer/_isr_facts.py``), and the slot resolution gives the edge
        (origin ``RefOrigin.SLOT``).
        """
        conn = open_db(zephyr.db_path)
        try:
            rows = conn.execute(
                "SELECT caller.name AS caller, r.origins FROM refs r "
                "JOIN symbols handler ON handler.config_hash = r.config_hash AND handler.usr = r.to_usr "
                "JOIN symbols caller ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr "
                "WHERE r.config_hash = ? AND handler.name = 'swi_isr'",
                (zephyr.builds["app"],),
            ).fetchall()
        finally:
            conn.close()
        # msg_irq_init references the handler too: IRQ_CONNECT takes its
        # address there.  The edge of the dispatcher comes from the slot.
        through_slot = [row["caller"] for row in rows if row["origins"] & RefOrigin.SLOT]
        assert through_slot == ["_isr_wrapper"], [tuple(row) for row in rows]

    def test_each_unit_got_its_macro_values(self, zephyr: IndexedProject):
        """No marker of the run: the preprocessor gave each unit its macro values."""
        assert read_state(zephyr.db_path.parent, zephyr.builds["app"]) is None
        assert expanded_values(zephyr, "app", "MSG_IRQ", "src/main.c")

    def test_the_manifest_verified_each_header(self, zephyr: IndexedProject):
        assert manifest_verification(zephyr, "app") == "full"

    def test_each_unit_has_the_target_of_the_driver_alone(self, zephyr: IndexedProject):
        """The driver gives ``arm-zephyr-eabi``; ``-mcpu=cortex-m4`` gave a second target that won."""
        assert targets_of_each_unit(zephyr, "app") == {("--target=arm-zephyr-eabi",)}


# ── PlatformIO: ESP32-C3 with the Arduino framework ──────────────────────


@pytest.fixture(scope="module")
def platformio(work_dir: Path, test_sdks: InstalledSdks) -> IndexedProject:
    """The PlatformIO fixture with the PlatformIO of the test SDKs.

    init takes the venv of PlatformIO of the user (``~/.platformio/penv``)
    when it exists, else ``pio`` of PATH: the project uses the PlatformIO
    that PlatformIO defines (decision of the user, 2026-11-13).  The test
    selects the pinned PlatformIO and its core directory in ``local.toml``,
    as a user selects an other one.
    """
    project = copy_project("platformio_esp32c3", work_dir / "pio")
    init_project(project, sdk.tool_env(test_sdks.root, test_sdks.versions, test_sdks.zephyr_sdk))
    local = project / ".fw-context" / "local.toml"
    set_key(local, "build", "python", str(venv_program(test_sdks.root / "venv", "python")))
    set_key(local, "build", "env", {
        "PLATFORMIO_CORE_DIR": str(sdk.platformio_core(test_sdks.root)),
        "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "No",
    })
    return index_project(project, daemon_env(test_sdks.root))


class TestPlatformIOEsp32c3:
    def test_one_environment_is_one_build(self, platformio: IndexedProject):
        assert list(platformio.builds) == [""]

    def test_the_c_and_the_cpp_code_of_the_project(self, platformio: IndexedProject):
        names = {"sensor_init", "sensor_read", "Blinker", "toggle", "setup", "loop", "SensorState"}
        assert definitions(platformio, "", names) == names

    def test_the_code_of_the_framework(self, platformio: IndexedProject):
        """The Arduino core of the framework, which reads the include directories of its response file.

        ``pinMode`` and ``digitalWrite`` are weak aliases of these definitions
        (``esp32-hal-gpio.c``).
        """
        assert definitions(platformio, "", {"__pinMode", "__digitalWrite"}) == {"__pinMode", "__digitalWrite"}

    def test_each_unit_got_its_macro_values(self, platformio: IndexedProject):
        """``-iprefix`` + ``-iwithprefixbefore`` of the framework reach the preprocessor (each unit had none)."""
        assert read_state(platformio.db_path.parent, platformio.builds[""]) is None
        assert expanded_values(platformio, "", "SENSOR_H", "include/sensor.h")

    def test_the_manifest_verified_each_header(self, platformio: IndexedProject):
        assert manifest_verification(platformio, "") == "full"

    def test_each_unit_has_the_target_of_the_driver_alone(self, platformio: IndexedProject):
        """The driver gives ``riscv32-esp-elf``; ``-march=rv32...`` gave a second target that won."""
        assert targets_of_each_unit(platformio, "") == {("--target=riscv32-esp-elf",)}
