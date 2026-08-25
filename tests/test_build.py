"""Tests for fw_context_mcp.indexer.build."""

import json

from pathlib import Path

from fw_context_mcp.indexer.build import (
    BuildConfig,
    BuildVariant,
    build_variant_config,
    _mbed_target_from_custom_targets,
    _parse_mbed_dotfile,
    check_completeness,
    detect_build_system,
    resolve_reuse_compile_commands,
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


class TestResolveReuseCompileCommands:
    def test_legacy_root_value_self_heals_to_canonical(self, tmpdir):
        # Legacy config: compile_commands = "compile_commands.json" (root).
        # A previous --build produced the canonical file.
        canonical = tmpdir / ".fw-context" / "build" / "compile_commands.json"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("[]")

        result = resolve_reuse_compile_commands(tmpdir, Path("compile_commands.json"))
        assert result == canonical

    def test_legacy_root_value_without_canonical_keeps_root(self, tmpdir):
        result = resolve_reuse_compile_commands(tmpdir, Path("compile_commands.json"))
        assert result == (tmpdir / "compile_commands.json").resolve()

    def test_custom_relative_path_unchanged(self, tmpdir):
        result = resolve_reuse_compile_commands(tmpdir, Path("cmake_build/compile_commands.json"))
        assert result == (tmpdir / "cmake_build" / "compile_commands.json").resolve()

    def test_absolute_path_unchanged(self, tmpdir):
        custom = tmpdir / "ci" / "cc.json"
        result = resolve_reuse_compile_commands(tmpdir, custom)
        assert result == custom.resolve()

    def test_default_canonical_path_unchanged(self, tmpdir):
        default = Path(".fw-context") / "build" / "compile_commands.json"
        result = resolve_reuse_compile_commands(tmpdir, default)
        assert result == (tmpdir / default).resolve()


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


class TestTheConfiguredSystemWins:
    """``[build] system`` decides which builder runs, not the project markers."""

    def test_a_freestanding_zephyr_app_is_not_taken_for_cmake(self, tmp_path: Path):
        """The config also decides who generates and validates compile_commands.json.

        A freestanding NCS application has CMakeLists.txt and no west.yml.  A
        marker scan calls it a CMake project, so GenericCMakeBuildSystem
        validated its artifacts and answered for its build directories.
        Measured on zbox-ecb-fw-v5, which declares system = "zephyr".
        """
        from fw_context_mcp.config import load as load_config
        from fw_context_mcp.indexer.builders import registry

        (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
        cfg_dir = tmp_path / ".fw-context"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text('[build]\nsystem = "zephyr"\n')

        cfg = load_config(project_root=tmp_path)
        system = cfg.build.system or detect_build_system(tmp_path)

        assert detect_build_system(tmp_path) == "cmake"
        assert system == "zephyr"

        builder_cls = registry.get(system)
        assert builder_cls is not None
        assert builder_cls().get_build_dir_patterns(tmp_path) == ["build/"]
        assert registry.get("cmake")().get_build_dir_patterns(tmp_path) == [
            "build/", "cmake-build-",
        ]


class TestBuildVariantConfig:
    def test_an_unknown_variant_override_warns(self, caplog):
        """A typo in [[build.variants]] must not disappear without a message.

        An unknown key was dropped in silence, so ``vendor_pathz = [...]``
        had no effect and no warning.  The user then reads the config as
        applied when it is not.
        """
        import logging

        base = BuildConfig(system="zephyr")
        variant = BuildVariant(name="dev", overrides={"vendor_pathz": ["x"]})

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.indexer.build"):
            cfg = build_variant_config(base, variant)

        assert cfg.system == "zephyr"
        assert "vendor_pathz" in caplog.text
        assert "dev" in caplog.text

    def test_a_known_variant_override_does_not_warn(self, caplog):
        """The warning must fire on a typo only, never on a valid key."""
        import logging

        base = BuildConfig(system="zephyr", profile="develop")
        variant = BuildVariant(name="rel", overrides={"profile": "release"})

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.indexer.build"):
            cfg = build_variant_config(base, variant)

        assert cfg.profile == "release"
        assert caplog.text == ""

    def test_the_build_dir_pattern_is_per_variant(self):
        """``build_dir`` IS per-variant, and this is the boundary that measurement supports.

        Measured on zbox-ecb-fw-v5: 9 builds and 2 different build_dir values.
        The vendor patterns have no such measured trigger, so do not read this
        test as evidence for a per-variant vendor set.
        """
        base = BuildConfig(system="zephyr", build_dir="build")
        dev = build_variant_config(base, BuildVariant(name="dev", build_dir="build/dev"))
        rel = build_variant_config(base, BuildVariant(name="rel", build_dir="build/rel"))

        assert dev.build_dir == "build/dev"
        assert rel.build_dir == "build/rel"
        assert base.build_dir == "build"
