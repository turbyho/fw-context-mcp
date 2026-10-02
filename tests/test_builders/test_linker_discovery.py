"""Finding the linker script of a build.

The mechanisms come from measurement on the real test projects, and each
test below says which project showed the case.  A wrong script would put a
wrong memory map in front of a user who cannot tell, thus the tests cover
what the code REFUSES as closely as what it finds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders import linker_scripts
from fw_context_mcp.indexer.builders._linker import (
    from_ninja,
    ninja_link,
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

    def test_one_script_that_is_not_there_gives_nothing(self, tmp_path):
        # Measured on an ESP-IDF build: `-T memory.ld` is a name that ld
        # finds through `-L`, and the file is not in the build directory.
        # A list of the other scripts would be the map of another link.
        (tmp_path / "rom.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -T rom.ld -T memory.ld\n")
        assert from_ninja(tmp_path) == []


# The link edge that CMake writes for an executable, in the shape of the
# ESP-IDF fixture (v5.2.5): bare `-T` names, and the directories in LINK_PATH.
_RULES = (
    "rule CXX_EXECUTABLE_LINKER__app_\n"
    "  command = $PRE_LINK && /opt/xtensa-esp32-elf-g++ $FLAGS $LINK_FLAGS $in -o $TARGET_FILE"
    " $LINK_PATH $LINK_LIBRARIES && $POST_BUILD\n"
    "  description = Linking CXX executable $TARGET_FILE\n"
    "rule C_STATIC_LIBRARY_LINKER__lib_\n"
    "  command = ar qc $TARGET_FILE $LINK_FLAGS $in\n"
)


def _link_edge(output: str, link_flags: str, link_path: str = "", libraries: str = "") -> str:
    return (
        f"build {output}: CXX_EXECUTABLE_LINKER__app_ main.o | deps\n"
        "  FLAGS = -mlongcalls\n"
        f"  LINK_FLAGS = {link_flags}\n"
        f"  LINK_PATH = {link_path}\n"
        f"  LINK_LIBRARIES = {libraries}\n"
        "  POST_BUILD = :\n"
        "  PRE_LINK = :\n"
        f"  TARGET_FILE = {output}\n"
    )


def _cmake_ninja(build_dir: Path, *edges: str) -> Path:
    (build_dir / "CMakeFiles").mkdir(parents=True, exist_ok=True)
    (build_dir / "CMakeFiles" / "rules.ninja").write_text(_RULES, encoding="utf-8")
    body = "include CMakeFiles/rules.ninja\n\n" + "\n".join(edges)
    body += "build lib/libx.a: C_STATIC_LIBRARY_LINKER__lib_ x.o\n  LINK_FLAGS = -T nope.ld\n"
    (build_dir / "build.ninja").write_text(body, encoding="utf-8")
    return build_dir


class TestNinjaLink:
    """The link edge of the executable, with its `-L` directories."""

    def _sdk(self, tmp_path: Path) -> tuple[Path, Path]:
        rom = tmp_path / "sdk" / "rom"
        gen = tmp_path / "build" / "esp-idf" / "esp_system" / "ld"
        for directory, names in ((rom, ("esp32.rom.ld",)), (gen, ("memory.ld", "sections.ld"))):
            directory.mkdir(parents=True)
            for name in names:
                (directory / name).write_text("MEMORY { }", encoding="utf-8")
        return rom, gen

    def test_bare_names_through_link_path(self, tmp_path):
        rom, gen = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge(
            "app.elf", "-Wl,--gc-sections -T esp32.rom.ld -T memory.ld -T sections.ld -Wl,--defsym=X=1",
            f"-L{rom}   -L{gen}", "-lc -lgcc -u app_main",
        ))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert [p.name for p in record.scripts] == ["esp32.rom.ld", "memory.ld", "sections.ld"]
        assert record.defsyms == {"X": "1"}

    def test_a_relative_link_path_is_relative_to_the_build_dir(self, tmp_path, monkeypatch):
        # ninja runs the link in the build directory.
        self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T memory.ld", "-Lesp-idf/esp_system/ld"))
        monkeypatch.chdir(tmp_path)
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert record.scripts == [(build / "esp-idf/esp_system/ld/memory.ld").resolve()]

    def test_ninja_escapes(self, tmp_path):
        # `$ ` is a space, `$:` a colon and `$$` a dollar sign in ninja.
        spaced = tmp_path / "my sdk"
        spaced.mkdir()
        (spaced / "memory.ld").write_text("MEMORY { }", encoding="utf-8")
        build = _cmake_ninja(tmp_path / "build", _link_edge(
            "app.elf", '-T memory.ld -Wl,--defsym=P=1$$', f'-L"{str(spaced).replace(" ", "$ ")}"',
        ))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert record.scripts == [(spaced / "memory.ld").resolve()]

    def test_a_line_continuation(self, tmp_path):
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T $\n      esp32.rom.ld", f"-L{rom}"))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert [p.name for p in record.scripts] == ["esp32.rom.ld"]

    def test_a_script_that_is_not_found_is_unknown(self, tmp_path):
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T esp32.rom.ld -T gone.ld", f"-L{rom}"))
        assert ninja_link(build, "app.elf") is None

    def test_a_ninja_variable_in_a_link_input_is_unknown(self, tmp_path):
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T $script", f"-L{rom}"))
        assert ninja_link(build, "app.elf") is None

    def test_a_link_with_no_script(self, tmp_path):
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-Wl,--gc-sections"))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert record.scripts == []

    def test_the_target_selects_the_edge(self, tmp_path):
        rom, gen = self._sdk(tmp_path)
        build = _cmake_ninja(
            tmp_path / "build",
            _link_edge("test.elf", "-T esp32.rom.ld", f"-L{rom}"),
            _link_edge("app.elf", "-T memory.ld", f"-L{gen}"),
        )
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert [p.name for p in record.scripts] == ["memory.ld"]

    def test_two_executables_and_no_target_is_unknown(self, tmp_path):
        rom, gen = self._sdk(tmp_path)
        build = _cmake_ninja(
            tmp_path / "build",
            _link_edge("test.elf", "-T esp32.rom.ld", f"-L{rom}"),
            _link_edge("app.elf", "-T memory.ld", f"-L{gen}"),
        )
        assert ninja_link(build) is None
        assert ninja_link(build, "other.elf") is None

    def test_one_executable_and_no_target(self, tmp_path):
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T esp32.rom.ld", f"-L{rom}"))
        record = ninja_link(build)
        assert record is not None
        assert [p.name for p in record.scripts] == ["esp32.rom.ld"]

    def test_no_ninja_file_is_unknown(self, tmp_path):
        assert ninja_link(tmp_path, "app.elf") is None

    def test_the_variables_of_a_response_file(self, tmp_path):
        # CMake puts LINK_PATH and LINK_LIBRARIES in a response file when the
        # command line is too long, which is the normal case for ESP-IDF on
        # Windows.  The rule names them in `rspfile_content`.
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("app.elf", "-T esp32.rom.ld", f"-L{rom}"))
        rules = build / "CMakeFiles" / "rules.ninja"
        rules.write_text(
            "rule CXX_EXECUTABLE_LINKER__app_\n"
            "  command = g++ $FLAGS $LINK_FLAGS @$RSP_FILE -o $TARGET_FILE\n"
            "  rspfile = $RSP_FILE\n"
            "  rspfile_content = $in_newline $LINK_PATH $LINK_LIBRARIES\n",
            encoding="utf-8",
        )
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert [p.name for p in record.scripts] == ["esp32.rom.ld"]

    def test_the_file_name_of_the_target_in_another_directory(self, tmp_path):
        # With CMAKE_RUNTIME_OUTPUT_DIRECTORY the edge names `bin/app.elf`,
        # and project_description.json names `app.elf`.
        rom, _ = self._sdk(tmp_path)
        build = _cmake_ninja(tmp_path / "build", _link_edge("bin/app.elf", "-T esp32.rom.ld", f"-L{rom}"))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert [p.name for p in record.scripts] == ["esp32.rom.ld"]

    @pytest.mark.skipif(os.name == "nt", reason="a backslash separates directories on Windows")
    def test_a_directory_with_a_backslash(self, tmp_path):
        # The ninja words come from a shell split.  A second unquote removed
        # the backslash, and the search went on in the next directory.
        odd = tmp_path / "back\\slash"
        odd.mkdir()
        (odd / "memory.ld").write_text("MEMORY { }", encoding="utf-8")
        later = tmp_path / "later"
        later.mkdir()
        (later / "memory.ld").write_text("MEMORY { }", encoding="utf-8")
        build = _cmake_ninja(tmp_path / "build", _link_edge(
            "app.elf", "-T memory.ld", f"'-L{odd}' -L{later}",
        ))
        record = ninja_link(build, "app.elf")
        assert record is not None
        assert record.scripts == [(odd / "memory.ld").resolve()]


class TestFromNinjaVariables:
    def test_a_script_in_a_ninja_variable_gives_nothing(self, tmp_path):
        # The variable names a script that this side cannot resolve.  The
        # other scripts would be the map of another link.
        (tmp_path / "app.ld").write_text("MEMORY { }", encoding="utf-8")
        _ninja(tmp_path, "cmd = ld -T app.ld -T $script\n")
        assert from_ninja(tmp_path) == []


class TestEspIdfLinkRecord:
    def test_the_app_elf_of_the_project_description(self, tmp_path):
        from fw_context_mcp.indexer.builders import link_record
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        rom = tmp_path / "sdk"
        rom.mkdir()
        (rom / "memory.ld").write_text("MEMORY { }", encoding="utf-8")
        build = _cmake_ninja(
            tmp_path / "build",
            _link_edge("bootloader_test.elf", "-T gone.ld"),
            _link_edge("fw.elf", "-T memory.ld", f"-L{rom}"),
        )
        (build / "project_description.json").write_text('{"app_elf": "fw.elf"}', encoding="utf-8")
        (build / "compile_commands.json").write_text("[]", encoding="utf-8")
        builder = ESPIDFBuildSystem()
        record = link_record(builder, tmp_path, compile_commands=build / "compile_commands.json")
        assert record is not None
        assert [p.name for p in record.scripts] == ["memory.ld"]
        assert builder.get_linker_scripts(tmp_path, compile_commands=build / "compile_commands.json") == record.scripts

    def _copied_build(self, tmp_path: Path, copy_content: str) -> tuple[Path, list]:
        """A build directory, and the copy of its database that `build()` publishes."""
        rom = tmp_path / "sdk"
        rom.mkdir()
        (rom / "memory.ld").write_text("MEMORY { }", encoding="utf-8")
        build = _cmake_ninja(tmp_path / "proj" / "build", _link_edge("fw.elf", "-T memory.ld", f"-L{rom}"))
        (build / "project_description.json").write_text('{"app_elf": "fw.elf"}', encoding="utf-8")
        (build / "compile_commands.json").write_text('[{"file": "a.c"}]', encoding="utf-8")
        copy = tmp_path / "proj" / ".fw-context" / "build" / "compile_commands.json"
        copy.parent.mkdir(parents=True)
        copy.write_text(copy_content, encoding="utf-8")
        units = [FakeUnit(build / "esp-idf" / "main"), FakeUnit(build)]
        return copy, units

    def test_the_build_directory_of_a_published_copy(self, tmp_path):
        # `fw-context index --build` indexes the copy in .fw-context/build/,
        # where no build.ninja is.  The units name the build directory, and
        # its database has the content of the copy.
        from fw_context_mcp.indexer.builders import link_record
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        copy, units = self._copied_build(tmp_path, '[{"file": "a.c"}]')
        record = link_record(ESPIDFBuildSystem(), tmp_path / "proj", compile_commands=copy, units=units)
        assert record is not None
        assert [p.name for p in record.scripts] == ["memory.ld"]

    def test_a_build_directory_with_another_database_is_not_used(self, tmp_path):
        # A later build changed the database there: its link is not the
        # link of the copy that the index holds.
        from fw_context_mcp.indexer.builders import link_record
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        copy, units = self._copied_build(tmp_path, '[{"file": "other.c"}]')
        assert link_record(ESPIDFBuildSystem(), tmp_path / "proj", compile_commands=copy, units=units) is None

    def test_no_build_directory_is_unknown(self, tmp_path):
        from fw_context_mcp.indexer.builders import link_record
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        assert link_record(ESPIDFBuildSystem(), tmp_path, compile_commands=None) is None


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

    def test_platformio_without_a_recorded_link(self, tmp_path):
        # SCons writes no file with the link command.  Only a build records
        # it, see test_platformio_link.py, thus with no build there is no
        # answer.
        assert PlatformIOBuildSystem().get_linker_scripts(tmp_path) == []
