"""Finding the linker script of a build.

The mechanisms come from measurement on the real test projects, and each
test below says which project showed the case.  A wrong script would put a
wrong memory map in front of a user who cannot tell, thus the tests cover
what the code REFUSES as closely as what it finds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fw_context_mcp.indexer.builders import linker_scripts
from fw_context_mcp.indexer.builders._linker import (
    from_ninja,
    output_dirs_from_units,
    single_script_in,
)
from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem
from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem


@dataclass
class FakeUnit:
    """The part of a CompilationUnit that the output lookup reads."""

    directory: Path
    raw_entry: dict | None = None
    clang_args: list[str] = field(default_factory=list)


def _ninja(tmp_path: Path, body: str) -> Path:
    (tmp_path / "build.ninja").write_text(body, encoding="utf-8")
    return tmp_path


class TestFromNinja:
    """`-T <path>` in build.ninja is what the linker receives."""

    def test_two_spaces_after_the_flag(self, tmp_path):
        # Measured on the Zephyr project, every image:
        #   LINK_LIBRARIES = … -T  zephyr/linker.cmd  -Wl,-Map,…
        # A pattern with one space finds nothing here, which is the bug this
        # test pins.
        (tmp_path / "linker.cmd").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "  LINK_LIBRARIES = a.obj  -T  linker.cmd  -Wl,-Map,x\n")
        assert [p.name for p in from_ninja(tmp_path)] == ["linker.cmd"]

    def test_one_space_after_the_flag(self, tmp_path):
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -T app.ld -o app.elf\n")
        assert [p.name for p in from_ninja(tmp_path)] == ["app.ld"]

    def test_no_space_after_the_flag(self, tmp_path):
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -Tapp.ld -o app.elf\n")
        assert [p.name for p in from_ninja(tmp_path)] == ["app.ld"]

    def test_the_compiler_driver_form(self, tmp_path):
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = gcc -Wl,-T,app.ld -o app.elf\n")
        assert [p.name for p in from_ninja(tmp_path)] == ["app.ld"]

    def test_a_relative_path_resolves_against_the_build_dir(self, tmp_path):
        nested = tmp_path / "zephyr"
        nested.mkdir()
        (nested / "linker.cmd").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -T  zephyr/linker.cmd\n")
        assert from_ninja(tmp_path) == [(nested / "linker.cmd").resolve()]

    def test_a_longer_flag_is_not_the_script_flag(self, tmp_path):
        # Without the lookbehind, `-Target=x` matches and captures `arget=x`.
        _ninja(tmp_path, "cmd = tool -Target=app.ld\n")
        assert from_ninja(tmp_path) == []

    def test_a_ninja_variable_is_skipped(self, tmp_path):
        _ninja(tmp_path, "cmd = ld -T $script\n")
        assert from_ninja(tmp_path) == []

    def test_a_token_with_no_suffix_and_no_separator_is_skipped(self, tmp_path):
        _ninja(tmp_path, "cmd = ld -T plain\n")
        assert from_ninja(tmp_path) == []

    def test_a_named_script_that_is_not_there_is_skipped(self, tmp_path):
        _ninja(tmp_path, "cmd = ld -T  absent.ld\n")
        assert from_ninja(tmp_path) == []

    def test_the_same_script_twice_gives_one_entry(self, tmp_path):
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "a = ld -T app.ld\nb = ld -T app.ld\n")
        assert len(from_ninja(tmp_path)) == 1

    def test_every_script_of_a_split_layout(self, tmp_path):
        # ESP-IDF passes about ten scripts rather than one.
        for name in ("rom.ld", "memory.ld", "sections.ld"):
            (tmp_path / name).write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -T rom.ld -T memory.ld -T sections.ld\n")
        assert [p.name for p in from_ninja(tmp_path)] == [
            "rom.ld", "memory.ld", "sections.ld",
        ]

    def test_no_ninja_file(self, tmp_path):
        assert from_ninja(tmp_path) == []


class TestOutputDirsFromUnits:
    """Which output tree did THIS build compile into?

    mbed-tools writes one tree per toolchain and profile.  Measured on
    the second Mbed project, which holds `GCC_ARM-DEBUG` and `GCC_ARM-DEVELOP`,
    each with its own `.link_script.ld`.
    """

    def _tree(self, tmp_path: Path, profile: str) -> Path:
        directory = tmp_path / "BUILD" / "BOARD" / profile
        directory.mkdir(parents=True)
        return directory

    def test_the_output_field_of_the_database(self, tmp_path):
        self._tree(tmp_path, "GCC_ARM-DEVELOP")
        unit = FakeUnit(
            directory=tmp_path,
            raw_entry={"output": "BUILD/BOARD/GCC_ARM-DEVELOP/a.o"},
        )
        found = output_dirs_from_units(tmp_path, [unit], "BUILD")
        assert found == [tmp_path / "BUILD/BOARD/GCC_ARM-DEVELOP"]

    def test_the_dash_o_argument(self, tmp_path):
        self._tree(tmp_path, "GCC_ARM-DEVELOP")
        unit = FakeUnit(
            directory=tmp_path,
            raw_entry={"arguments": ["gcc", "-c", "-o",
                                     "BUILD/BOARD/GCC_ARM-DEVELOP/a.o"]},
        )
        found = output_dirs_from_units(tmp_path, [unit], "BUILD")
        assert found == [tmp_path / "BUILD/BOARD/GCC_ARM-DEVELOP"]

    def test_the_dash_o_of_a_command_string(self, tmp_path):
        self._tree(tmp_path, "GCC_ARM-DEVELOP")
        unit = FakeUnit(
            directory=tmp_path,
            raw_entry={"command": "gcc -c -o BUILD/BOARD/GCC_ARM-DEVELOP/a.o"},
        )
        found = output_dirs_from_units(tmp_path, [unit], "BUILD")
        assert found == [tmp_path / "BUILD/BOARD/GCC_ARM-DEVELOP"]

    def test_normalized_flags_are_not_the_source(self, tmp_path):
        # `clang_args` is normalized for libclang and the normalization drops
        # the output flag: measured on the second Mbed project, 349 normalized tokens with no
        # `-o`, against 105 raw tokens that hold it.  Reading clang_args
        # would find nothing, thus raw_entry has to be the source.
        self._tree(tmp_path, "GCC_ARM-DEVELOP")
        unit = FakeUnit(
            directory=tmp_path,
            raw_entry=None,
            clang_args=["-o", "BUILD/BOARD/GCC_ARM-DEVELOP/a.o"],
        )
        assert output_dirs_from_units(tmp_path, [unit], "BUILD") == []

    def test_the_tree_most_units_compiled_into_comes_first(self, tmp_path):
        self._tree(tmp_path, "GCC_ARM-DEBUG")
        self._tree(tmp_path, "GCC_ARM-DEVELOP")
        units = [
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEBUG/a.o"}),
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEVELOP/b.o"}),
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEVELOP/c.o"}),
        ]
        found = output_dirs_from_units(tmp_path, units, "BUILD")
        assert found[0].name == "GCC_ARM-DEVELOP"

    def test_an_absolute_output_path(self, tmp_path):
        tree = self._tree(tmp_path, "GCC_ARM-DEVELOP")
        unit = FakeUnit(tmp_path, {"output": str(tree / "a.o")})
        assert output_dirs_from_units(tmp_path, [unit], "BUILD") == [tree]

    def test_an_object_outside_the_marker(self, tmp_path):
        unit = FakeUnit(tmp_path, {"output": "obj/a.o"})
        assert output_dirs_from_units(tmp_path, [unit], "BUILD") == []

    def test_a_tree_that_is_too_shallow(self, tmp_path):
        (tmp_path / "BUILD").mkdir()
        unit = FakeUnit(tmp_path, {"output": "BUILD/a.o"})
        assert output_dirs_from_units(tmp_path, [unit], "BUILD") == []

    def test_a_directory_that_is_not_there(self, tmp_path):
        unit = FakeUnit(tmp_path, {"output": "BUILD/BOARD/GONE/a.o"})
        assert output_dirs_from_units(tmp_path, [unit], "BUILD") == []

    def test_no_units(self, tmp_path):
        assert output_dirs_from_units(tmp_path, None, "BUILD") == []
        assert output_dirs_from_units(tmp_path, [], "BUILD") == []


class TestSingleScriptIn:
    """One candidate is an answer.  Two candidates are a choice, and a
    choice made by a pattern is a guess."""

    def test_the_known_name(self, tmp_path):
        (tmp_path / ".link_script.ld").write_text("MEMORY { }", encoding="utf-8")
        (tmp_path / "other.ld").write_text("MEMORY { }", encoding="utf-8")
        found = single_script_in(tmp_path, preferred=".link_script.ld")
        assert [p.name for p in found] == [".link_script.ld"]

    def test_the_only_script(self, tmp_path):
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        assert [p.name for p in single_script_in(tmp_path)] == ["app.ld"]

    def test_two_scripts_and_no_known_name(self, tmp_path):
        (tmp_path / "a.ld").write_text("MEMORY { }", encoding="utf-8")
        (tmp_path / "b.ld").write_text("MEMORY { }", encoding="utf-8")
        assert single_script_in(tmp_path) == []

    def test_no_script(self, tmp_path):
        assert single_script_in(tmp_path, preferred=".link_script.ld") == []

    def test_a_directory_that_is_not_there(self, tmp_path):
        assert single_script_in(tmp_path / "gone") == []


class TestTheAccessor:
    """`builders.linker_scripts` must never raise and never invent."""

    def test_no_builder(self, tmp_path):
        assert linker_scripts(None, tmp_path) == []

    def test_a_builder_with_no_method(self, tmp_path):
        class Bare:
            pass

        assert linker_scripts(Bare(), tmp_path) == []

    def test_a_builder_that_raises(self, tmp_path):
        class Broken:
            def get_linker_scripts(self, project_root, **kwargs):
                raise RuntimeError("no")

        assert linker_scripts(Broken(), tmp_path) == []

    def test_a_builder_that_answers_none(self, tmp_path):
        class Quiet:
            def get_linker_scripts(self, project_root, **kwargs):
                return None

        assert linker_scripts(Quiet(), tmp_path) == []


class TestTheBackends:
    """Each backend answers for its own build system."""

    def test_zephyr_reads_the_ninja_file_of_the_image(self, tmp_path):
        # A sysbuild image keeps build.ninja and compile_commands.json in one
        # directory, thus the build directory is the parent of the compile
        # commands.
        image = tmp_path / "app"
        (image / "zephyr").mkdir(parents=True)
        (image / "zephyr/linker.cmd").write_text("MEMORY { }", encoding="utf-8")
        (image / "build.ninja").write_text(
            "cmd = ld -T  zephyr/linker.cmd\n", encoding="utf-8"
        )
        found = ZephyrBuildSystem().get_linker_scripts(
            tmp_path, compile_commands=image / "compile_commands.json"
        )
        assert [p.name for p in found] == ["linker.cmd"]

    def test_zephyr_puts_the_final_script_first(self, tmp_path):
        # Zephyr links twice and ninja names the pre-pass script first, but
        # `get_source` must show the script of the real link.
        image = tmp_path / "app"
        (image / "zephyr").mkdir(parents=True)
        for name in ("linker.cmd", "linker_zephyr_pre0.cmd"):
            (image / "zephyr" / name).write_text("MEMORY { }", encoding="utf-8")
        (image / "build.ninja").write_text(
            "a = ld -T  zephyr/linker_zephyr_pre0.cmd\n"
            "b = ld -T  zephyr/linker.cmd\n",
            encoding="utf-8",
        )
        found = ZephyrBuildSystem().get_linker_scripts(
            tmp_path, compile_commands=image / "compile_commands.json"
        )
        assert [p.name for p in found] == ["linker.cmd", "linker_zephyr_pre0.cmd"]

    def test_zephyr_with_no_compile_commands(self, tmp_path):
        assert ZephyrBuildSystem().get_linker_scripts(tmp_path) == []

    def test_mbed_reads_the_directory_fw_context_built_into(self, tmp_path):
        from fw_context_mcp.utils import autobuild_dir

        build_dir = tmp_path / autobuild_dir("")
        build_dir.mkdir(parents=True)
        (build_dir / ".link_script.ld").write_text("MEMORY { }", encoding="utf-8")
        found = MbedOSBuildSystem().get_linker_scripts(tmp_path)
        assert [p.name for p in found] == [".link_script.ld"]

    def test_mbed_reads_the_tree_the_user_built_into(self, tmp_path):
        # Two profiles, each with a script.  The build says which one.
        for profile in ("GCC_ARM-DEBUG", "GCC_ARM-DEVELOP"):
            directory = tmp_path / "BUILD" / "BOARD" / profile
            directory.mkdir(parents=True)
            (directory / ".link_script.ld").write_text(
                f"/* {profile} */", encoding="utf-8"
            )
        units = [
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEVELOP/a.o"}),
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEVELOP/b.o"}),
            FakeUnit(tmp_path, {"output": "BUILD/BOARD/GCC_ARM-DEBUG/c.o"}),
        ]
        found = MbedOSBuildSystem().get_linker_scripts(tmp_path, units=units)
        assert len(found) == 1
        assert found[0].parent.name == "GCC_ARM-DEVELOP"

    def test_mbed_with_no_output_at_all(self, tmp_path):
        assert MbedOSBuildSystem().get_linker_scripts(tmp_path) == []

    def test_platformio_answers_nothing(self, tmp_path):
        # SCons records no link command the index can read, and the map file
        # never names the script.  Measured on the STM32 project and on the ESP32 project.
        assert PlatformIOBuildSystem().get_linker_scripts(tmp_path) == []
