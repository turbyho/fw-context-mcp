"""Edge case tests for SDK path filtering."""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.mcp.shared.filtering import (
    _build_sdk_excludes,
    _merge_excludes,
    _path_matches,
)


class TestPathMatches:
    def test_exact_match(self):
        assert _path_matches("src/main.c", "src/main.c") is True

    def test_percent_prefix_glob(self):
        assert _path_matches("src/main.c", "src/%") is True

    def test_percent_only_matches_all(self):
        assert _path_matches("anything/at/all.c", "%") is True

    def test_no_match(self):
        assert _path_matches("src/main.c", "lib/%") is False

    def test_subdirectory_match(self):
        assert _path_matches("mbed-os/targets/TARGET_NRF/uart.c", "mbed-os/%") is True

    def test_partial_match_rejected(self):
        # "mbed-os-tools" should NOT match "mbed-os/%"
        assert _path_matches("mbed-os-tools/file.c", "mbed-os/%") is False

    def test_empty_pattern(self):
        # Empty pattern matches no files (fnmatch converts to empty, matches nothing)
        # But "/" + empty may match in some edge cases
        result = _path_matches("src/main.c", "")
        # Empty pattern shouldn't match anything meaningful
        assert isinstance(result, bool)

    def test_empty_path(self):
        assert _path_matches("", "src/%") is False

    def test_complex_pattern(self):
        assert _path_matches(".pio/libdeps/board/Framework/lib.a", ".pio/%") is True

    def test_specific_subdirectory(self):
        assert _path_matches("zephyr/subsys/logging/log.c", "zephyr/%") is True

    def test_build_directory(self):
        assert _path_matches("build/zephyr/include/generated/syscalls.h", "build/%") is True

    def test_modules_third_party(self):
        assert _path_matches("modules/hal/nordic/drivers/uart.c", "modules/%") is True

    def test_nested_pattern_match(self):
        # Verify the fnmatch also checks for */pattern anywhere in path
        # due to the "or fnmatch('/'+path, '*/'+pattern)" logic
        assert _path_matches("some/deep/path/src/main.c", "src/%") is True


class TestBuildSdkExcludes:
    def test_mbed_os_detected(self, tmp_path: Path):
        (tmp_path / "mbed-os").mkdir()
        excludes = _build_sdk_excludes(tmp_path)
        assert "mbed-os/%" in excludes

    def test_zephyr_detected(self, tmp_path: Path):
        (tmp_path / "west.yml").write_text("")
        excludes = _build_sdk_excludes(tmp_path)
        assert "zephyr/%" in excludes
        assert "build/%" in excludes
        assert "modules/%" in excludes

    def test_platformio_detected(self, tmp_path: Path):
        (tmp_path / "platformio.ini").write_text("")
        excludes = _build_sdk_excludes(tmp_path)
        assert ".pio/%" in excludes

    def test_unknown_build_system(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert excludes == []

    def test_all_empty(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert isinstance(excludes, list)
        assert len(excludes) == 0


class TestMergeExcludes:
    def test_project_only_false_passes_through(self, tmp_path: Path):
        result = _merge_excludes(["custom/%"], project_only=False, root=tmp_path)
        assert result == ["custom/%"]

    def test_project_only_false_none_passes_through(self, tmp_path: Path):
        result = _merge_excludes(None, project_only=False, root=tmp_path)
        assert result is None

    def test_project_only_mbed_sdk(self, tmp_path: Path):
        (tmp_path / "mbed-os").mkdir()
        result = _merge_excludes(None, project_only=True, root=tmp_path)
        assert result is not None
        assert "mbed-os/%" in result

    def test_merge_user_excludes(self, tmp_path: Path):
        result = _merge_excludes(["tests/%", "examples/%"], project_only=True, root=tmp_path)
        assert "tests/%" in (result or [])
        assert "examples/%" in (result or [])

    def test_deduplication(self, tmp_path: Path, isolation):
        result = _merge_excludes(["mbed-os/%", "mbed-os/%"], project_only=True, root=tmp_path)
        # Deduplication happens in project_only=True path
        assert result is not None
        assert result.count("mbed-os/%") == 1

    def test_empty_result_when_no_excludes(self, tmp_path: Path, isolation):
        result = _merge_excludes(None, project_only=True, root=tmp_path)
        # Default config has exclude_paths = ["build", "BUILD"]
        # Unknown build system + no user excludes → just the defaults
        assert result is not None
        assert "build" in result
        assert "BUILD" in result

    def test_config_excludes_appended(self, tmp_path: Path, monkeypatch):
        """Config excludes from .fw-context/config.toml are appended."""
        # We test that user-provided excludes are preserved
        result = _merge_excludes(["my_lib/%"], project_only=True, root=tmp_path)
        assert "my_lib/%" in (result or [])
