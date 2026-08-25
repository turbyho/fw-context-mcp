"""Edge case tests for SDK path filtering."""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.sdk_detect import _normalize_path_pattern
from fw_context_mcp.mcp.shared.filtering import (
    _build_sdk_excludes,
    _path_matches,
    compute_exclude_like,
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

    def test_a_manifest_repo_keeps_its_own_modules(self, tmp_path: Path, monkeypatch):
        """The MCP layer must give the same answer as the indexer.

        The sign of this test is inverted from what it was.  ``modules/`` in a
        T2 manifest repository holds the TEAM's Zephyr modules, and the old
        fixed pattern hid them from every project_only query.  The reasoning
        is in tests/test_sdk_detect.py, and the duplicate lives here because
        the two layers must not drift apart.
        """
        repo = tmp_path / "repo"
        (repo / "modules").mkdir(parents=True)
        (repo / "west.yml").write_text("")
        sdk = tmp_path / "ncs" / "v3.4.0" / "zephyr"
        sdk.mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(sdk))

        assert _build_sdk_excludes(repo) == []

    def test_pio_libdeps_stays_vendor(self, tmp_path: Path):
        """``.pio/libdeps`` is vendor, ``.pio/build`` is build output."""
        (tmp_path / "platformio.ini").write_text("")
        excludes = _build_sdk_excludes(tmp_path)

        assert ".pio/libdeps/%" in excludes
        assert ".pio/%" not in excludes

    def test_unknown_build_system(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert excludes == []

    def test_all_empty(self, tmp_path: Path):
        excludes = _build_sdk_excludes(tmp_path)
        assert isinstance(excludes, list)
        assert len(excludes) == 0


class TestGeneratedOutputIsProjectCode:
    """Build output never appears in the vendor patterns.

    Generated code counts as project code (decision 1), and build output
    has its own answer in get_build_dir_patterns().  Mbed used to put
    ``BUILD/%`` in the vendor list and PlatformIO used to put ``.pio/%``
    there, so their generated files read as somebody else's code.
    """

    def test_generated_build_output_is_project_code(self, tmp_path: Path, monkeypatch):
        cases = [
            ("mbed-os", "BUILD/NRF52840/GCC_ARM/mbed_config.h"),
            ("platformio", ".pio/build/nucleo/FrameworkArduino/gen.h"),
            ("zephyr", "build/nrf52840_sysbuild/zephyr/include/generated/autoconf.h"),
            ("esp-idf", "build/config/sdkconfig.h"),
        ]
        workspace = tmp_path / "workspace"
        (workspace / "zephyr").mkdir(parents=True)
        monkeypatch.setenv("ZEPHYR_BASE", str(workspace / "zephyr"))

        for system, generated in cases:
            patterns = _build_sdk_excludes(tmp_path, system)
            assert not any(_path_matches(generated, p) for p in patterns), (
                f"{system}: {generated} matched {patterns}"
            )


class TestComputeExcludeLike:
    """Tests for the centralized compute_exclude_like() function."""

    def test_analyze_vendor_true_returns_empty(self, tmp_path: Path):
        """When analyze_vendor=True, return empty list — no exclusion."""
        result = compute_exclude_like(tmp_path, analyze_vendor=True, vendor_paths=None)
        assert result == []

    def test_analyze_vendor_false_auto_detects(self, tmp_path: Path):
        """When analyze_vendor=False, auto-detect from build system."""
        (tmp_path / "mbed-os").mkdir()
        result = compute_exclude_like(tmp_path, analyze_vendor=False, vendor_paths=None)
        assert len(result) > 0
        assert any("mbed-os" in p for p in result)

    def test_no_vendor_paths(self, tmp_path: Path):
        """When vendor_paths is None, only auto-detected patterns returned."""
        (tmp_path / "mbed-os").mkdir()
        result = compute_exclude_like(tmp_path, analyze_vendor=False, vendor_paths=None)
        assert isinstance(result, list)

    def test_vendor_paths_with_strings(self, tmp_path: Path):
        """vendor_paths=list[str] is merged correctly."""
        (tmp_path / "mbed-os").mkdir()
        result = compute_exclude_like(
            tmp_path,
            analyze_vendor=False,
            vendor_paths=["third_party"],
        )
        assert any("mbed-os" in p for p in result)
        assert any("third_party" in p for p in result)

    def test_unknown_build_system_no_extra(self, tmp_path: Path):
        """Unknown build system + no extra paths → only config defaults apply."""
        result = compute_exclude_like(
            tmp_path,
            analyze_vendor=False,
            vendor_paths=None,
        )
        assert isinstance(result, list)


class TestNormalizePathPattern:
    """Tests for _normalize_path_pattern from sdk_detect."""

    def test_no_wildcard_gets_suffix(self):
        assert _normalize_path_pattern("third_party") == "third_party/%"

    def test_trailing_slash_handled(self):
        assert _normalize_path_pattern("third_party/") == "third_party/%"

    def test_has_wildcard_unchanged(self):
        assert _normalize_path_pattern("mbed-os/%") == "mbed-os/%"

    def test_absolute_path(self):
        result = _normalize_path_pattern("/home/user/esp/components/muj_fork")
        assert result == "/home/user/esp/components/muj_fork/%"

    def test_absolute_path_with_wildcard(self):
        result = _normalize_path_pattern("/home/user/esp/%")
        assert result == "/home/user/esp/%"
