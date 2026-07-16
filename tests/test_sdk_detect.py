"""Tests for indexer/sdk_detect.py — low-level SDK path detection."""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.sdk_detect import (
    _build_sdk_excludes,
    _normalize_path_pattern,
    _normalize_patterns,
    _path_matches,
)


class TestPathMatches:
    """Tests for _path_matches — regex-based LIKE pattern matching."""

    def test_exact_match(self):
        assert _path_matches("src/main.c", "src/main.c") is True

    def test_percent_prefix_glob(self):
        assert _path_matches("src/main.c", "src/%") is True

    def test_percent_only_matches_all(self):
        assert _path_matches("anything.cpp", "%") is True

    def test_no_match(self):
        assert _path_matches("src/main.c", "lib/%") is False

    def test_subdirectory_match(self):
        assert _path_matches("src/sub/dir/file.c", "src/%") is True

    def test_partial_match_rejected(self):
        assert _path_matches("other_src/main.c", "src/%") is False

    def test_empty_pattern(self):
        # Empty pattern → regex ^$, matches only empty string
        assert _path_matches("", "") is True
        assert _path_matches("anything.c", "") is False

    def test_empty_path(self):
        assert _path_matches("", "src/%") is False

    def test_complex_pattern(self):
        assert _path_matches("mbed-os/targets/TARGET_NORDIC/serial_api.c", "mbed-os/%") is True
        assert _path_matches("mbed-os/targets/TARGET_NORDIC/serial_api.c", "mbed-os/targets/%") is True

    def test_specific_subdirectory(self):
        assert _path_matches("mbed-os/hal/serial_api.c", "mbed-os/hal/%") is True
        assert _path_matches("mbed-os/drivers/serial_api.c", "mbed-os/hal/%") is False

    def test_build_directory(self):
        assert _path_matches("build/generated.h", "build/%") is True
        assert _path_matches("build/sub/dir/file.h", "build/%") is True

    def test_modules_third_party(self):
        assert _path_matches("modules/hal/nordic/nrfx/hal/nrf_gpio.h", "modules/%") is True

    def test_nested_pattern_match(self):
        assert _path_matches("mbed-os/platform/mbed_wait.c", "mbed-os/%") is True
        assert _path_matches("zephyr/kernel/sched.c", "zephyr/%") is True

    def test_regex_special_chars_escaped(self):
        """Regex special characters in paths are escaped (not treated as regex)."""
        # + is a regex quantifier — should be matched literally
        assert _path_matches("c++/test.cpp", "c++/%") is True
        assert _path_matches("a.c++", "%.c++") is True

    def test_dot_in_pattern(self):
        """Dot in pattern is escaped — should NOT match arbitrary chars."""
        assert _path_matches("src/main_c", "src/main.c") is False
        assert _path_matches("src/main.c", "src/main.c") is True

    def test_absolute_path(self):
        """Absolute paths can be matched with absolute patterns."""
        assert _path_matches("/home/user/esp/components/muj_fork/foo.cpp",
                             "/home/user/esp/components/muj_fork/%") is True
        assert _path_matches("/home/user/other/foo.cpp",
                             "/home/user/esp/components/muj_fork/%") is False

    def test_prefix_style_match(self):
        """Prefix patterns (like %mbed-os/%) work for backwards compatibility."""
        assert _path_matches("mbed-os/targets/foo.cpp", "%mbed-os/%") is True
        assert _path_matches("/absolute/path/mbed-os/targets/foo.cpp", "%mbed-os/%") is True

    def test_platformio_nested_pattern(self):
        """%.platformio/% matches platformio packages (e.g. .platformio/packages/...)."""
        assert _path_matches(".platformio/packages/framework-arduino/cores/esp32/main.cpp", "%.platformio/%") is True
        # .pio/ paths are NOT matched by %.platformio/% — they have their own pattern .pio/%

    def test_consecutive_percent_wildcards(self):
        """Multiple % wildcards work correctly."""
        assert _path_matches("mbed-os/targets/TARGET_NORDIC/serial/foo.cpp", "%mbed-os/%TARGET_NORDIC/%") is True


class TestNormalizePathPattern:
    """Tests for _normalize_path_pattern."""

    def test_no_wildcard_gets_suffix(self):
        assert _normalize_path_pattern("third_party") == "third_party/%"

    def test_trailing_slash_handled(self):
        assert _normalize_path_pattern("third_party/") == "third_party/%"

    def test_has_wildcard_unchanged(self):
        assert _normalize_path_pattern("mbed-os/%") == "mbed-os/%"

    def test_absolute_path_no_wildcard(self):
        result = _normalize_path_pattern("/home/user/esp/components/muj_fork")
        assert result == "/home/user/esp/components/muj_fork/%"

    def test_absolute_path_with_wildcard(self):
        result = _normalize_path_pattern("/home/user/esp/%")
        assert result == "/home/user/esp/%"

    def test_empty_string(self):
        assert _normalize_path_pattern("") == "/%"

    def test_only_percent(self):
        assert _normalize_path_pattern("%") == "%"


class TestNormalizePatterns:
    """Tests for _normalize_patterns — batch normalization."""

    def test_mixed_patterns(self):
        result = _normalize_patterns(["third_party", "mbed-os/%", "/abs/path"])
        assert result == ["third_party/%", "mbed-os/%", "/abs/path/%"]

    def test_empty_list(self):
        assert _normalize_patterns([]) == []

    def test_all_already_normalized(self):
        result = _normalize_patterns(["a/%", "b/%"])
        assert result == ["a/%", "b/%"]


class TestBuildSdkExcludes:
    """Tests for _build_sdk_excludes."""

    def test_mbed_os_detected(self, tmp_path: Path):
        (tmp_path / "mbed-os").mkdir()
        excludes = _build_sdk_excludes(tmp_path)
        assert "mbed-os/%" in excludes

    def test_mbed_os_from_mbed_app_json(self, tmp_path: Path):
        (tmp_path / "mbed_app.json").write_text("{}")
        excludes = _build_sdk_excludes(tmp_path)
        # mbed_app.json implies mbed-os build, but detection depends on markers
        # which are scored by BuildSystemRegistry — west.yml=zephyr, platformio.ini=platformio
        # The mbed_app.json marker is in the mbed-os builder
        assert "mbed-os/%" in excludes

    def test_zephyr_detected(self, tmp_path: Path):
        (tmp_path / "west.yml").write_text("")
        excludes = _build_sdk_excludes(tmp_path)
        assert "zephyr/%" in excludes
        assert "modules/%" in excludes

    def test_zephyr_no_build_dir(self, tmp_path: Path):
        """build/% should NOT be in Zephyr excludes — it's build output, not SDK."""
        (tmp_path / "west.yml").write_text("")
        excludes = _build_sdk_excludes(tmp_path)
        assert "build/%" not in excludes

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
