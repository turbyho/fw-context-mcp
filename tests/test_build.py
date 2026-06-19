"""Tests for fw_context_mcp.indexer.build."""

import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import (
    BuildConfig,
    _parse_mbed_dotfile,
    _mbed_target_from_custom_targets,
    check_completeness,
    detect_build_system,
)


class TestDetectBuildSystem:
    def test_mbed_os_detected_via_dotfile(self, tmpdir):
        (tmpdir / ".mbed").write_text("TOOLCHAIN=GCC_ARM\nTARGET=P_ECB_BOARD\n")
        assert detect_build_system(tmpdir) == "mbed-os"

    def test_mbed_os_detected_via_mbed_os_dir(self, tmpdir):
        (tmpdir / "mbed-os").mkdir()
        (tmpdir / "mbed_app.json").write_text("{}")
        assert detect_build_system(tmpdir) == "mbed-os"

    def test_zephyr_detected_via_west_yml(self, tmpdir):
        (tmpdir / "west.yml").write_text("manifest:\n")
        assert detect_build_system(tmpdir) == "zephyr"

    def test_zephyr_detected_via_zephyr_dir(self, tmpdir):
        (tmpdir / "zephyr").mkdir()
        assert detect_build_system(tmpdir) == "zephyr"

    def test_platformio_detected(self, tmpdir):
        (tmpdir / "platformio.ini").write_text("[env:uno]\n")
        assert detect_build_system(tmpdir) == "platformio"

    def test_unknown_returns_none(self, tmpdir):
        assert detect_build_system(tmpdir) is None

    def test_mbed_wins_over_zephyr_when_both_present(self, tmpdir):
        """When both markers exist, the one with more matches wins."""
        (tmpdir / ".mbed").write_text("TARGET=foo\n")
        (tmpdir / "mbed-os").mkdir()
        (tmpdir / "mbed_app.json").write_text("{}")
        (tmpdir / "west.yml").write_text("manifest:\n")
        assert detect_build_system(tmpdir) == "mbed-os"


class TestParseMbedDotfile:
    def test_parses_valid_file(self, tmpdir):
        dotfile = tmpdir / ".mbed"
        dotfile.write_text("TOOLCHAIN=GCC_ARM\nTARGET=P_ECB_BOARD\nROOT=.\n")
        result = _parse_mbed_dotfile(tmpdir)
        assert result == {"TOOLCHAIN": "GCC_ARM", "TARGET": "P_ECB_BOARD", "ROOT": "."}

    def test_ignores_comments_and_empty_lines(self, tmpdir):
        dotfile = tmpdir / ".mbed"
        dotfile.write_text("# comment\nTOOLCHAIN=GCC_ARM\n\n# another\nTARGET=BOARD\n")
        result = _parse_mbed_dotfile(tmpdir)
        assert result == {"TOOLCHAIN": "GCC_ARM", "TARGET": "BOARD"}

    def test_missing_file_returns_empty_dict(self, tmpdir):
        assert _parse_mbed_dotfile(tmpdir) == {}


class TestMbedTargetFromCustomTargets:
    def test_extracts_first_board(self, tmpdir):
        ct = tmpdir / "custom_targets.json"
        ct.write_text(json.dumps({
            "P_ECB_BOARD": {"inherits": ["MCU_NRF52840"]},
            "OTHER_BOARD": {"inherits": ["MCU_STM32"]},
        }))
        assert _mbed_target_from_custom_targets(tmpdir) == "P_ECB_BOARD"

    def test_skips_non_board_keys(self, tmpdir):
        ct = tmpdir / "custom_targets.json"
        ct.write_text(json.dumps({
            "some_setting": "value",
            "P_ECB_BOARD": {"inherits": ["MCU_NRF52840"]},
        }))
        assert _mbed_target_from_custom_targets(tmpdir) == "P_ECB_BOARD"

    def test_missing_file_returns_none(self, tmpdir):
        assert _mbed_target_from_custom_targets(tmpdir) is None

    def test_invalid_json_returns_none(self, tmpdir):
        ct = tmpdir / "custom_targets.json"
        ct.write_text("not json")
        assert _mbed_target_from_custom_targets(tmpdir) is None


class TestCheckCompleteness:
    def test_empty_compile_commands_warns(self, tmpdir):
        cc = tmpdir / "compile_commands.json"
        cc.write_text("[]")
        warnings = check_completeness(cc, tmpdir)
        assert len(warnings) == 1
        assert "empty" in warnings[0].lower()

    def test_small_cc_vs_many_sources_warns(self, tmpdir):
        # Create many source files but few cc entries
        src = tmpdir / "src"
        src.mkdir()
        for i in range(50):
            (src / f"file{i}.cpp").touch()

        cc = tmpdir / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "src/file0.cpp", "directory": str(tmpdir), "arguments": ["gcc", "-c", "src/file0.cpp"]}]))
        warnings = check_completeness(cc, tmpdir)
        assert len(warnings) >= 1
        assert "incomplete" in warnings[0].lower()

    def test_large_cc_no_warning(self, tmpdir):
        src = tmpdir / "src"
        src.mkdir()
        for i in range(10):
            (src / f"file{i}.cpp").touch()

        cc = tmpdir / "compile_commands.json"
        cc.write_text(json.dumps([
            {"file": f"src/file{i}.cpp", "directory": str(tmpdir), "arguments": ["gcc", "-c", f"src/file{i}.cpp"]}
            for i in range(10)
        ]))
        warnings = check_completeness(cc, tmpdir)
        assert len(warnings) == 0

    def test_cannot_parse_returns_warning(self, tmpdir):
        cc = tmpdir / "compile_commands.json"
        cc.write_text("not json at all")
        warnings = check_completeness(cc, tmpdir)
        assert len(warnings) >= 1


class TestBuildConfig:
    def test_defaults(self):
        cfg = BuildConfig()
        assert cfg.clean is True
        assert cfg.system is None
        assert cfg.command is None
        assert cfg.profile == "develop"
        assert cfg.app_config == "mbed_app.json"
        assert cfg.extra_profiles == ["lto.json"]

    def test_override_fields(self):
        cfg = BuildConfig(
            system="mbed-os",
            clean=False,
            profile="Release",
            target="MY_BOARD",
        )
        assert cfg.system == "mbed-os"
        assert cfg.clean is False
        assert cfg.profile == "Release"
        assert cfg.target == "MY_BOARD"
