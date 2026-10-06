"""Tests for fw_context_mcp.indexer.build."""

import json
from pathlib import Path

from fw_context_mcp.indexer.build import (
    BuildConfig,
    BuildVariant,
    _mbed_target_from_custom_targets,
    _parse_mbed_dotfile,
    build_variant_config,
    check_completeness,
    default_compile_commands,
    detect_build_system,
    explicit_compile_commands,
)


class TestDetectBuildSystem:
    def test_mbed_os_detected_via_dotfile(self, tmpdir):
        (tmpdir / ".mbed").write_text("TOOLCHAIN=GCC_ARM\nTARGET=BOARD_V2_BOARD\n")
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
        dotfile.write_text("TOOLCHAIN=GCC_ARM\nTARGET=BOARD_V2_BOARD\nROOT=.\n")
        result = _parse_mbed_dotfile(tmpdir)
        assert result == {"TOOLCHAIN": "GCC_ARM", "TARGET": "BOARD_V2_BOARD", "ROOT": "."}

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
            "BOARD_V2_BOARD": {"inherits": ["MCU_NRF52840"]},
            "OTHER_BOARD": {"inherits": ["MCU_STM32"]},
        }))
        assert _mbed_target_from_custom_targets(tmpdir) == "BOARD_V2_BOARD"

    def test_skips_non_board_keys(self, tmpdir):
        ct = tmpdir / "custom_targets.json"
        ct.write_text(json.dumps({
            "some_setting": "value",
            "BOARD_V2_BOARD": {"inherits": ["MCU_NRF52840"]},
        }))
        assert _mbed_target_from_custom_targets(tmpdir) == "BOARD_V2_BOARD"

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


class TestDefaultCompileCommands:
    """The database that a run without an explicit file reads."""

    @staticmethod
    def _cfg(compile_commands: Path, system: str = "cmake", **build):
        from types import SimpleNamespace

        return SimpleNamespace(
            index=SimpleNamespace(compile_commands=compile_commands),
            build=BuildConfig(system=system, **build),
        )

    @staticmethod
    def _out(root: Path) -> Path:
        from fw_context_mcp.indexer.build_layout import BuildLayout

        return BuildLayout(root.resolve()).out_dir("")

    def test_the_legacy_root_value_means_the_database_of_the_build(self, tmp_path: Path):
        # Configs that `fw-context init` wrote before 2026-08 name the
        # project-root file.  For a build that fw-context runs, the database
        # is in the output directory of that build.
        (tmp_path / "compile_commands.json").write_text("[]", encoding="utf-8")

        result = default_compile_commands(tmp_path, self._cfg(Path("compile_commands.json")))

        assert result == self._out(tmp_path) / "compile_commands.json"

    def test_the_old_canonical_value_means_the_database_of_the_build(self, tmp_path: Path):
        old = Path(".fw-context") / "build" / "compile_commands.json"

        result = default_compile_commands(tmp_path, self._cfg(old))

        assert result == self._out(tmp_path) / "compile_commands.json"

    def test_the_database_that_the_build_system_wrote_is_found(self, tmp_path: Path):
        # Zephyr sysbuild of one program puts it one level deeper.
        nested = self._out(tmp_path) / "zephyr" / "compile_commands.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("[]", encoding="utf-8")

        result = default_compile_commands(
            tmp_path, self._cfg(Path("compile_commands.json"), system="zephyr", board="b")
        )

        assert result == nested

    def test_a_custom_relative_path_is_the_users_file(self, tmp_path: Path):
        result = default_compile_commands(tmp_path, self._cfg(Path("cmake_build/compile_commands.json")))
        assert result == (tmp_path / "cmake_build" / "compile_commands.json").resolve()

    def test_an_absolute_path_is_the_users_file(self, tmp_path: Path):
        custom = tmp_path / "ci" / "cc.json"
        assert default_compile_commands(tmp_path, self._cfg(custom)) == custom.resolve()

    def test_a_build_that_fw_context_cannot_run_keeps_the_root_file(self, tmp_path: Path):
        # An STM32CubeIDE project makes its database outside of fw-context.
        result = default_compile_commands(
            tmp_path, self._cfg(Path("compile_commands.json"), system="stm32cubeide")
        )
        assert result == (tmp_path / "compile_commands.json").resolve()

    def test_explicit_compile_commands_says_none_for_the_build_database(self, tmp_path: Path):
        assert explicit_compile_commands(tmp_path, self._cfg(Path("compile_commands.json"))) is None


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
        Measured on the Zephyr project, which declares system = "zephyr".
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

    def test_the_output_directory_is_per_variant(self):
        """Each variant builds into its own output directory, from its name."""
        from fw_context_mcp.indexer.build_layout import BuildLayout

        base = BuildConfig(system="zephyr")
        dev = build_variant_config(base, BuildVariant(name="dev"))
        rel = build_variant_config(base, BuildVariant(name="rel"))
        layout = BuildLayout(Path("/proj"))

        assert layout.out_dir(dev.variant_name) == Path("/proj/.fw-context/build/dev/out")
        assert layout.out_dir(rel.variant_name) == Path("/proj/.fw-context/build/rel/out")
        assert base.variant_name == ""
