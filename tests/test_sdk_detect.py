"""Tests for indexer/sdk_detect.py — low-level SDK path detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fw_context_mcp.indexer.build import detect_build_system
from fw_context_mcp.indexer.builders.zephyr import _zephyr_vendor_patterns
from fw_context_mcp.indexer.sdk_detect import (
    _build_sdk_excludes,
    _normalize_path_pattern,
    _normalize_patterns,
    _path_matches,
)


@dataclass
class _FakeUnit:
    """The part of a translation unit that a builder reads.

    Only ``clang_args`` matters here.  A real TranslationUnit carries a
    parsed compile_commands.json entry, and to build one in a test would
    add a dependency on that format for no gain.
    """

    clang_args: list[str] = field(default_factory=list)


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

    def test_a_manifest_repo_keeps_its_own_modules(self, tmp_path: Path, monkeypatch):
        """A T2 manifest repository owns its ``modules/``, so no pattern hides it.

        This test had the opposite sign before.  The old answer was the fixed
        pair ``["zephyr/%", "modules/%"]``, which describes a T3 workspace
        root.  The marker ``west.yml`` also matches a T2 manifest repository,
        where ``modules/`` holds the TEAM's own Zephyr modules — measured as a
        false positive that hid them from every project_only query.

        With the SDK outside the project the correct answer is an empty list:
        a path outside project_root already gets is_project=0.
        """
        repo = tmp_path / "repo"
        (repo / "modules" / "my_driver" / "src").mkdir(parents=True)
        (repo / "west.yml").write_text("")
        sdk = tmp_path / "ncs" / "v3.4.0" / "zephyr"
        sdk.mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(sdk))

        excludes = _build_sdk_excludes(repo)

        assert excludes == []
        assert not any(
            _path_matches("modules/my_driver/src/drv.c", pat) for pat in excludes
        )

    def test_zephyr_no_build_dir(self, tmp_path: Path):
        """build/% should NOT be in Zephyr excludes — it's build output, not SDK."""
        (tmp_path / "west.yml").write_text("")
        excludes = _build_sdk_excludes(tmp_path)
        assert "build/%" not in excludes

    def test_pio_libdeps_stays_vendor(self, tmp_path: Path):
        """``.pio/libdeps`` is vendor, ``.pio/build`` is not.

        This test asserted ``.pio/%`` before, which covers the whole
        directory including ``.pio/build``.  Build output has
        get_build_dir_patterns(), and generated code counts as project code,
        so the pattern narrows to the libraries PlatformIO downloads.
        """
        (tmp_path / "platformio.ini").write_text("")
        excludes = _build_sdk_excludes(tmp_path)

        assert ".pio/libdeps/%" in excludes
        assert ".pio/%" not in excludes
        assert any(_path_matches(".pio/libdeps/Foo/foo.cpp", p) for p in excludes)
        assert not any(_path_matches(".pio/build/env/gen.h", p) for p in excludes)

    def test_pio_reads_a_moved_libdeps_dir(self, tmp_path: Path):
        """``libdeps_dir`` in platformio.ini moves the directory, and the pattern follows."""
        (tmp_path / "platformio.ini").write_text(
            "[platformio]\nlibdeps_dir = vendor/libs\n"
        )
        excludes = _build_sdk_excludes(tmp_path)

        assert "vendor/libs/%" in excludes
        assert ".pio/libdeps/%" not in excludes

    def test_unknown_build_system(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert excludes == []

    def test_all_empty(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert isinstance(excludes, list)
        assert len(excludes) == 0

    def test_esp_idf_marks_managed_components(self, tmp_path: Path):
        """``managed_components/`` is vendor, ``components/`` is the team's own."""
        (tmp_path / "CMakeLists.txt").write_text('include($ENV{IDF_PATH}/tools/cmake/project.cmake)\n')
        (tmp_path / "sdkconfig").write_text("")

        excludes = _build_sdk_excludes(tmp_path, "esp-idf")

        assert "managed_components/%" in excludes
        assert not any(_path_matches("components/my_sensor/src/x.c", p) for p in excludes)

    def test_a_builder_without_a_canonical_pattern_returns_nothing(self, tmp_path: Path):
        """Eight build systems mandate no in-tree vendor directory.

        A guess would be worse than nothing: a wrong pattern hides the team's
        own code from a project_only query, and it makes the staleness check
        trust a tree the team edits.
        """
        for system in ("cmake", "makefile", "bare", "arduino",
                       "keil-mdk", "iar-ewarm", "stm32cubeide", "ti-ccs"):
            assert _build_sdk_excludes(tmp_path, system) == [], system

    def test_the_configured_build_system_wins_over_detection(self, tmp_path: Path, monkeypatch):
        """``[build] system`` decides, even when the markers say something else.

        A freestanding NCS application has CMakeLists.txt and no west.yml, so
        a marker scan calls it a CMake project and it gets no pattern at all.
        Measured on zbox-ecb-fw-v5, which declares system = "zephyr".
        """
        (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
        workspace = tmp_path / "deps" / "ncs"
        (workspace / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(workspace / "zephyr"))

        assert detect_build_system(tmp_path) == "cmake"
        assert _build_sdk_excludes(tmp_path) == []
        assert _build_sdk_excludes(tmp_path, "zephyr") == ["deps/ncs/zephyr/%"]

    def test_detection_stays_the_fallback(self, tmp_path: Path):
        """With no configured system the markers still decide."""
        (tmp_path / "mbed-os").mkdir()

        assert _build_sdk_excludes(tmp_path, None) == ["mbed-os/%"]
        assert _build_sdk_excludes(tmp_path) == ["mbed-os/%"]

    def test_an_unknown_build_system_gives_no_pattern(self, tmp_path: Path):
        """A key no builder claims must not stop the caller."""
        assert _build_sdk_excludes(tmp_path, "no-such-build-system") == []


class TestZephyrVendorPatterns:
    """The Zephyr SDK root is derived, never guessed from a marker."""

    def test_an_out_of_tree_sdk_needs_no_pattern(self, tmp_path: Path):
        """A path outside project_root already gets is_project=0."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sdk = tmp_path / "ncs" / "v3.4.0" / "zephyr"

        assert _zephyr_vendor_patterns(repo, sdk, sdk.parent) == []

    def test_an_in_tree_workspace_is_marked(self, tmp_path: Path):
        """A WEST_TOPDIR inside the project covers zephyr/, modules/ and nrf/ at once."""
        topdir = tmp_path / "deps" / "ncs"

        patterns = _zephyr_vendor_patterns(tmp_path, topdir / "zephyr", topdir)

        assert patterns == ["deps/ncs/zephyr/%", "deps/ncs/%"]

    def test_the_project_root_itself_is_not_a_pattern(self, tmp_path: Path):
        """When the project root IS the workspace, a pattern would hide everything."""
        assert _zephyr_vendor_patterns(tmp_path, tmp_path / "zephyr", tmp_path) == [
            "zephyr/%"
        ]

    def test_the_environment_is_the_fallback(self, tmp_path: Path, monkeypatch):
        """Without the compiler flags, ZEPHYR_BASE from the environment decides."""
        (tmp_path / "west.yml").write_text("")
        workspace = tmp_path / "workspace"
        (workspace / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(workspace / "zephyr"))

        assert _build_sdk_excludes(tmp_path, "zephyr") == ["workspace/zephyr/%"]

    def test_the_app_directory_is_not_a_signal(self, tmp_path: Path, monkeypatch):
        """Two images of one build give the same patterns, whatever their source dir.

        Measured on zbox-ecb-fw-v5: four different CMAKE_SOURCE_DIR values
        across 9 builds, and the mcuboot image has its source dir INSIDE the
        SDK.  The application directory therefore proves nothing about where
        the SDK is.
        """
        (tmp_path / "west.yml").write_text("")
        workspace = tmp_path / "workspace"
        (workspace / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(workspace / "zephyr"))

        app = _build_sdk_excludes(
            tmp_path, "zephyr",
            units=[_FakeUnit([f"-fmacro-prefix-map={tmp_path}/proj/app=CMAKE_SOURCE_DIR"])],
        )
        mcuboot = _build_sdk_excludes(
            tmp_path, "zephyr",
            units=[_FakeUnit([
                f"-fmacro-prefix-map={workspace}/bootloader/mcuboot/boot/zephyr"
                "=CMAKE_SOURCE_DIR"
            ])],
        )

        assert app == mcuboot == ["workspace/zephyr/%"]
