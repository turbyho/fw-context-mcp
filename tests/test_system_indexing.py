"""System tests — fw-context init → index pipeline over real project fixtures.

Tests the full user workflow:
1. ``fw-context init`` — creates ``.fw-context/config.toml``
2. ``fw-context index`` — detects missing ``compile_commands.json``, builds, indexes

Uses the minimal project fixtures under ``tests/builds/``.

Run::

    # Detection only (always works, no tools needed)
    python3 -m pytest tests/test_system_indexing.py -x -v -k "detection or registry"

    # Full init + index tests (requires build tools)
    python3 -m pytest tests/test_system_indexing.py -x -v -k "init_index"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import detect_build_system
from fw_context_mcp.indexer.builders import registry as builder_registry
from fw_context_mcp.indexer.db import open_db

# ── Helpers ────────────────────────────────────────────────────────────────

_BUILDS = Path(__file__).resolve().parent / "builds"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cli(args: list[str], cwd: Path | None = None, timeout: int = 180,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run fw-context CLI in a subprocess."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_project_root() / "src")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "fw_context_mcp.cli"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _db_path_for_project(project_root: Path) -> Path:
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.config import load as load_config

    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _config_hash(conn, project_root):
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.indexer.db import get_active_config

    project_id = derive_project_id(project_root)
    active = get_active_config(conn, project_id)
    return active["config_hash"] if active else None


def _patch_config(config_path: Path, replacements: dict[str, str]) -> None:
    """Edit an existing TOML config in-place.

    *replacements* maps full key paths like ``"[build] system"`` to new values
    like ``"bare"``.  Uncomments existing lines; adds new keys to the correct
    section (before the next section header) when the key doesn't exist yet.
    """
    import re

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    result: list[str] = []
    current_section = ""
    # Track keys we've consumed from replacements
    seen: set[str] = set()

    for line in lines:
        section_match = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if section_match:
            # Before moving to a new section, insert any remaining
            # replacements that belong to the current section
            for full_key, value in list(replacements.items()):
                if full_key.startswith(f"[{current_section}] "):
                    key = full_key.split("] ", 1)[1]
                    result.append(f"{key} = {value}")
                    seen.add(full_key)
                    del replacements[full_key]
            current_section = section_match.group(1)
            result.append(line)
            continue

        stripped = line.strip()
        uncommented = re.sub(r"^\s*#\s*", "", stripped)
        if "=" in uncommented:
            key = uncommented.split("=")[0].strip()
            full_key = f"[{current_section}] {key}"
            if full_key in replacements:
                value = replacements[full_key]
                indent = line[:len(line) - len(line.lstrip())]
                result.append(f"{indent}{key} = {value}")
                seen.add(full_key)
                del replacements[full_key]
                continue

        result.append(line)

    # Flush any remaining replacements for the last section
    for full_key, value in list(replacements.items()):
        if full_key.startswith(f"[{current_section}] "):
            key = full_key.split("] ", 1)[1]
            result.append(f"{key} = {value}")
            seen.add(full_key)
            del replacements[full_key]

    # Any leftovers that belong to sections not in the config at all
    for full_key, value in list(replacements.items()):
        section_name = full_key[1:].split("] ")[0]
        key = full_key.split("] ", 1)[1]
        result.append("")
        result.append(f"[{section_name}]")
        result.append(f"{key} = {value}")
        del replacements[full_key]

    config_path.write_text("\n".join(result) + "\n", encoding="utf-8")


def _init_and_index(proj: Path, *, replacements: dict[str, str] | None = None,
                    timeout: int = 300,
                    extra_env: dict[str, str] | None = None) -> Path:
    """Run fw-context init, patch config, then fw-context index.

    Returns *proj* on success, fails the test on error.
    """
    if replacements is None:
        replacements = {}
    proj = proj.resolve()

    # Clean up previous state
    cc = proj / "compile_commands.json"
    if cc.exists():
        cc.unlink()
    build_dir = proj / "build"
    if build_dir.exists():
        import shutil
        shutil.rmtree(build_dir)
    fwctx_dir = proj / ".fw-context"
    if fwctx_dir.exists():
        import shutil
        shutil.rmtree(fwctx_dir)

    # Step 1: fw-context init
    result = _cli(["init", "--project", str(proj)], cwd=proj, timeout=timeout)
    if result.returncode != 0:
        pytest.fail(f"init failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    # Step 2: ensure config.toml exists and patch it
    config_path = proj / ".fw-context" / "config.toml"
    if not config_path.exists():
        _cli(["project-init", "--fix", "--project", str(proj)], cwd=proj, timeout=timeout)
    assert config_path.exists(), f"config.toml not created at {config_path}"

    if replacements:
        _patch_config(config_path, dict(replacements))

    # Step 3: fw-context index (auto-build + index)
    result = _cli(
        ["index", "--no-refs", "--no-analyze", "--no-embeddings", "--project", str(proj)],
        cwd=proj,
        timeout=timeout,
        extra_env=extra_env,
    )
    if result.returncode != 0:
        pytest.fail(f"index failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    assert cc.exists(), f"compile_commands.json not created at {cc}"
    return proj


# ── Detection tests ────────────────────────────────────────────────────────


class TestBuildSystemDetection:
    """Verify build system detection for all supported project types."""

    def test_detect_bare_is_none(self):
        assert detect_build_system(_BUILDS / "bare") is None

    def test_detect_generic_cmake(self):
        assert detect_build_system(_BUILDS / "generic_cmake") == "cmake"

    def test_detect_makefile(self):
        assert detect_build_system(_BUILDS / "makefile") == "makefile"

    def test_detect_platformio(self):
        assert detect_build_system(_BUILDS / "platformio") == "platformio"

    def test_detect_mbed_os(self):
        assert detect_build_system(_BUILDS / "mbed_os") == "mbed-os"

    def test_detect_arduino(self):
        assert detect_build_system(_BUILDS / "arduino") == "arduino"

    def test_detect_zephyr(self):
        assert detect_build_system(_BUILDS / "zephyr") == "zephyr"

    def test_detect_esp_idf(self):
        assert detect_build_system(_BUILDS / "esp_idf") == "esp-idf"

    def test_detect_keil_mdk(self):
        assert detect_build_system(_BUILDS / "keil") == "keil-mdk"

    def test_detect_iar_ewarm(self):
        assert detect_build_system(_BUILDS / "iar") == "iar-ewarm"

    def test_detect_ti_ccs(self):
        assert detect_build_system(_BUILDS / "stubs") == "ti-ccs"

    def test_detect_none_for_empty_dir(self, tmp_path):
        assert detect_build_system(tmp_path) is None


# ── Registry completeness tests ────────────────────────────────────────────


class TestBuilderRegistry:
    """Verify all registered builders have required attributes."""

    _REQUIRED_ATTRS = ["name", "config_key", "markers"]

    @pytest.mark.parametrize(
        "config_key",
        [
            "bare", "cmake", "makefile", "platformio", "mbed-os", "arduino",
            "zephyr", "esp-idf", "keil-mdk", "iar-ewarm", "ti-ccs", "stm32cubeide",
        ],
    )
    def test_builder_has_required_attrs(self, config_key):
        builder_cls = builder_registry.get(config_key)
        assert builder_cls is not None, f"Builder '{config_key}' not registered"
        for attr in self._REQUIRED_ATTRS:
            assert hasattr(builder_cls, attr), (
                f"Builder '{config_key}' missing attribute '{attr}'"
            )

    @pytest.mark.parametrize(
        "config_key,expected_marker_count",
        [
            ("bare", 0), ("cmake", 1), ("makefile", 1),
            ("platformio", 1), ("mbed-os", 3), ("arduino", 1),
            ("zephyr", 2), ("esp-idf", 1), ("keil-mdk", 1),
            ("iar-ewarm", 2), ("ti-ccs", 1), ("stm32cubeide", 2),
        ],
    )
    def test_builder_marker_count(self, config_key, expected_marker_count):
        builder_cls = builder_registry.get(config_key)
        assert builder_cls is not None
        assert len(builder_cls.markers) == expected_marker_count, (
            f"Builder '{config_key}' has {len(builder_cls.markers)} markers, "
            f"expected {expected_marker_count}: {builder_cls.markers}"
        )


# ── Init + Index tests ─────────────────────────────────────────────────────

pytestmark_libclang = pytest.mark.libclang


class TestBareInitAndIndex:
    """End-to-end: init + index for a bare (manual mode) C project."""

    @pytest.fixture(scope="class")
    def indexed(self):
        return _init_and_index(_BUILDS / "bare", replacements={
            '[build] system': '"bare"',
            '[build] source_dirs': '["src"]',
            '[build] include_dirs': '["src"]',
        })

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) >= 1

    def test_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count >= 3
        finally:
            conn.close()

    def test_key_functions_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            functions = [
                r[0] for r in conn.execute(
                    "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1",
                    (ch,),
                )
            ]
            assert "uart_init" in functions
            assert "compute_checksum" in functions
        finally:
            conn.close()

    def test_enum_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            enums = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='enum'", (ch,)
            )]
            assert "OperationMode" in enums
        finally:
            conn.close()

    def test_struct_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            structs = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='struct'", (ch,)
            )]
            assert "Config" in structs
        finally:
            conn.close()


class TestCMakeInitAndIndex:
    """End-to-end: init + index for a CMake C project (detection works out of the box)."""

    @pytest.fixture(scope="class")
    def indexed(self):
        return _init_and_index(_BUILDS / "generic_cmake")

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) >= 1

    def test_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count >= 1
        finally:
            conn.close()

    def test_factorial_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            names = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function'", (ch,)
            )]
            assert "factorial" in names
        finally:
            conn.close()


class TestMakefileInitAndIndex:
    """End-to-end: init + index for a Makefile C project (detection works out of the box)."""

    @pytest.fixture(scope="class")
    def indexed(self):
        return _init_and_index(_BUILDS / "makefile")

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) >= 1

    def test_device_functions_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            functions = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1",
                (ch,),
            )]
            assert "register_device" in functions
            assert "find_device" in functions
        finally:
            conn.close()

    def test_struct_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            structs = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='struct'", (ch,)
            )]
            assert "Device" in structs
        finally:
            conn.close()


class TestPlatformIOInitAndIndex:
    """End-to-end: init + index for a PlatformIO project."""

    @pytest.fixture(scope="class")
    def indexed(self):
        return _init_and_index(_BUILDS / "platformio", timeout=300)

    def test_compile_commands_has_entries(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) > 10

    def test_project_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count > 50
        finally:
            conn.close()

    def test_sensor_functions_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            functions = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1",
                (ch,),
            )]
            assert "sensor_init" in functions
            assert "sensor_read" in functions
        finally:
            conn.close()

    def test_enum_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            enums = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='enum'", (ch,)
            )]
            assert "SensorState" in enums
        finally:
            conn.close()


class TestArduinoInitAndIndex:
    """End-to-end: init + index for an Arduino project (needs fqbn)."""

    @pytest.fixture(scope="class")
    def indexed(self):
        return _init_and_index(_BUILDS / "arduino", replacements={
            '[build] fqbn': '"arduino:avr:uno"',
        }, timeout=120)

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) >= 1

    def test_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count >= 3
        finally:
            conn.close()


class TestMbedOSInitAndIndex:
    """End-to-end: init + index for an Mbed OS 6 project.

    Requires ARM GCC on PATH (~/.local/bin symlinks), bear, and mbed-cli (pyenv 3.11).
    Skipped when any tool is missing.
    """

    _MBED_ENV = {
        "PYENV_VERSION": "3.11.8",
        "PATH": os.path.expanduser("~/.pyenv/versions/3.11.8/bin")
                + ":" + os.path.expanduser("~/.local/bin")
                + ":" + os.environ.get("PATH", ""),
    }

    @pytest.fixture(scope="class")
    def indexed(self):
        if not shutil.which("bear"):
            pytest.skip("bear not installed")
        if not Path(os.path.expanduser("~/.local/bin/arm-none-eabi-gcc")).exists():
            pytest.skip("ARM GCC not found in ~/.local/bin")
        return _init_and_index(_BUILDS / "mbed_os", extra_env=self._MBED_ENV, timeout=600)

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) > 100, f"Expected >100 entries, got {len(entries)}"

    def test_project_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count > 500, f"Expected >500 symbols, got {count}"
        finally:
            conn.close()


class TestZephyrInitAndIndex:
    """End-to-end: init + index for a Zephyr RTOS project.

    Requires Nordic Connect SDK (ZEPHYR_BASE, ZEPHYR_SDK_INSTALL_DIR).
    Skipped when the SDK is not detected.
    """

    _ZEPHYR_BASE = Path(os.path.expanduser("~/ncs/v3.2.3/zephyr"))
    _ZEPHYR_SDK = Path(os.path.expanduser("~/ncs/toolchains/2ac5840438/opt/zephyr-sdk"))

    _ZEPHYR_ENV = {
        "PYENV_VERSION": "3.11.8",
        "ZEPHYR_BASE": str(_ZEPHYR_BASE),
        "ZEPHYR_SDK_INSTALL_DIR": str(_ZEPHYR_SDK),
        "PATH": os.environ.get("PATH", ""),
    }

    @pytest.fixture(scope="class")
    def indexed(self):
        if not self._ZEPHYR_BASE.is_dir():
            pytest.skip("Zephyr base not found")
        if not self._ZEPHYR_SDK.is_dir():
            pytest.skip("Zephyr SDK not found")
        if not shutil.which("west"):
            pytest.skip("west not installed")
        return _init_and_index(_BUILDS / "zephyr", replacements={
            '[build] board': '"nrf52840dk/nrf52840"',
        }, extra_env=self._ZEPHYR_ENV, timeout=600)

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) > 50, f"Expected >50 entries, got {len(entries)}"

    def test_project_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count > 200, f"Expected >200 symbols, got {count}"
        finally:
            conn.close()

    def test_msg_functions_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            functions = [r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1",
                (ch,),
            )]
            assert "msg_init" in functions
            assert "msg_send" in functions
            assert "msg_recv" in functions
        finally:
            conn.close()


class TestESPIDFInitAndIndex:
    """End-to-end: init + index for an ESP-IDF project.

    Requires ESP-IDF environment (idf.py on PATH, IDF_PATH set).
    Skipped when idf.py is not available.
    """

    _IDF_PATH = Path(os.path.expanduser("~/.espressif/v5.2.5/esp-idf"))

    _IDF_ENV = {
        "IDF_PATH": str(_IDF_PATH),
        "IDF_TOOLS_PATH": os.path.expanduser("~/.espressif/tools"),
        "IDF_PYTHON_ENV_PATH": os.path.expanduser("~/.espressif/tools/python/v5.2.5/venv"),
        "ESP_IDF_VERSION": "5.2",
        "PATH": os.environ.get("PATH", ""),
    }

    @pytest.fixture(scope="class")
    def indexed(self):
        if not self._IDF_PATH.is_dir():
            pytest.skip("ESP-IDF not found")
        if not shutil.which("idf.py"):
            pytest.skip("idf.py not installed")
        return _init_and_index(_BUILDS / "esp_idf", extra_env=self._IDF_ENV, timeout=600)

    def test_compile_commands_generated(self, indexed):
        entries = json.loads((indexed / "compile_commands.json").read_text())
        assert len(entries) > 100, f"Expected >100 entries, got {len(entries)}"

    def test_project_symbols_indexed(self, indexed):
        db_path = _db_path_for_project(indexed)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn, indexed)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert count > 500, f"Expected >500 symbols, got {count}"
        finally:
            conn.close()


# ── Config hash tests ──────────────────────────────────────────────────────


class TestConfigHashStability:
    """Verify config_hash is stable across repeated indexing of the same project."""

    def test_same_project_same_config_hash(self):
        from fw_context_mcp.indexer.config_hash import compute

        cc = _BUILDS / "generic_cmake" / "compile_commands.json"
        assert cc.exists(), "Run CMake init+index test first to generate cc.json"
        assert compute(cc) == compute(cc)

    def test_config_hash_changes_with_different_cc(self, tmp_path):
        from fw_context_mcp.indexer.config_hash import compute

        cc1 = tmp_path / "cc1.json"
        cc2 = tmp_path / "cc2.json"
        cc1.write_text(json.dumps([
            {"file": "a.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "a.c"]}
        ]))
        cc2.write_text(json.dumps([
            {"file": "b.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "b.c"]}
        ]))
        assert compute(cc1) != compute(cc2)
