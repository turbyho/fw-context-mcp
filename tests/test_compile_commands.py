"""Tests for fw_context_mcp.indexer.compile_commands."""

import json
import os
from pathlib import Path

import pytest

from fw_context_mcp.indexer.compile_commands import (
    _detect_target_triple,
    _gcc_system_includes,
    _is_source_file,
    expand_response_file,
    normalize_args,
    parse,
    unit_language,
)
from fw_context_mcp.utils import TU_EXTENSIONS
from tests._toolchains import write_compile_commands, write_sources


class TestIsSourceFile:
    def test_c_source(self):
        assert _is_source_file("main.c")

    def test_cpp_source(self):
        assert _is_source_file("main.cpp")
        assert _is_source_file("main.cc")
        assert _is_source_file("main.cxx")

    def test_header_is_not_source(self):
        assert not _is_source_file("header.h")
        assert not _is_source_file("header.hpp")

    def test_other_extensions(self):
        assert not _is_source_file("Makefile")
        assert not _is_source_file("foo.o")


class TestUnitLanguage:
    """The language of a unit follows the rules of the compiler (measured with GCC 14, arm-zephyr-eabi)."""

    def test_cpp_by_extension(self):
        assert unit_language(Path("foo.cpp"), []) == "cpp"
        assert unit_language(Path("foo.cc"), []) == "cpp"

    def test_std_does_not_select_cpp(self):
        """`gcc -std=c++14 -c foo.c` compiles C, with a warning."""
        assert unit_language(Path("foo.c"), ["-std=c++14"]) == "c"

    def test_c_by_default(self):
        assert unit_language(Path("foo.c"), []) == "c"
        assert unit_language(Path("foo.s"), []) == "c"

    @pytest.mark.parametrize(("args", "expected"), [
        (["-x", "c++", "-c", "foo.c"], "cpp"),
        (["-xc++", "-c", "foo.c"], "cpp"),
        (["-x", "c", "-c", "foo.cpp"], "c"),
        (["-x", "c++-header", "-c", "foo.c"], "cpp"),
        (["-x", "c++", "-x", "none", "-c", "foo.c"], "c"),
        # -x applies to the files after it: one after the source does not.
        (["-c", "foo.c", "-x", "c++"], "c"),
    ])
    def test_the_last_x_before_the_source_decides(self, tmp_path, args, expected):
        source = tmp_path / ("foo.cpp" if "foo.cpp" in args else "foo.c")
        source.write_text("", encoding="utf-8")

        assert unit_language(source, args, Path("gcc"), tmp_path) == expected

    @pytest.mark.parametrize("driver", ["g++", "arm-none-eabi-g++", "c++", "clang++", "riscv32-esp-elf-g++.EXE", "g++-12"])
    def test_a_cpp_driver_compiles_a_c_file_as_cpp(self, tmp_path, driver):
        """`man gcc`: g++ treats .c, .h and .i files as C++ source files, unless -x is used."""
        source = tmp_path / "foo.c"
        source.write_text("", encoding="utf-8")

        assert unit_language(source, ["-c", "foo.c"], Path(driver), tmp_path) == "cpp"
        assert unit_language(source, ["-x", "c", "-c", "foo.c"], Path(driver), tmp_path) == "c"

    @pytest.mark.parametrize("value", ["assembler-with-cpp", "objective-c", "objective-c++"])
    def test_an_x_of_an_other_language_stops_the_driver_rule(self, tmp_path, value):
        """g++ -x assembler-with-cpp does not compile C++: the suffix decides, and the -x stays for libclang."""
        from fw_context_mcp.indexer.compile_commands import _language_of_unit, _with_language

        source = tmp_path / "start.c"
        source.write_text("", encoding="utf-8")
        args = ["-x", value, "-c", "start.c"]

        language, explicit = _language_of_unit(source, args, Path("arm-none-eabi-g++"), tmp_path)

        assert (language, explicit) == ("c", False)
        assert _with_language(["-x", value, "-DX"], language, explicit) == ["-x", value, "-DX"]

    @pytest.mark.parametrize(("language", "kept"), [("c", ["-std=gnu11"]), ("cpp", ["--std=gnu++17"])])
    def test_a_std_of_the_other_language_leaves_the_libclang_arguments(self, language, kept):
        """GCC ignores it with a warning, clang refuses the unit: both spellings, -std= and --std=."""
        from fw_context_mcp.indexer.compile_commands import _with_language

        args = ["-std=gnu11", "--std=gnu++17"]

        assert _with_language(args, language, explicit=False) == kept

    def test_a_c_driver_keeps_a_c_file_c(self, tmp_path):
        source = tmp_path / "foo.c"
        source.write_text("", encoding="utf-8")

        assert unit_language(source, ["-c", "foo.c"], Path("arm-none-eabi-gcc"), tmp_path) == "c"


class TestInferTarget:
    """The target of a compiler that gives no answer to the driver query: from its name alone."""

    def test_no_compiler(self):
        assert _detect_target_triple(None) is None

    @pytest.mark.parametrize(("name", "triple"), [
        ("arm-none-eabi-g++", "arm-none-eabi"),
        ("riscv32-unknown-elf-gcc", "riscv32-unknown-elf"),
        ("arm-none-eabi-g++-12", "arm-none-eabi"),
        ("arm-none-eabi-gcc.exe", "arm-none-eabi"),
        ("arm-none-eabi-gcc.EXE", "arm-none-eabi"),
        ("aarch64-none-elf-clang", "aarch64-none-elf"),
        ("arm-none-eabi-c++", "arm-none-eabi"),
        ("arm-none-eabi-cc", "arm-none-eabi"),
    ])
    def test_the_name_of_a_cross_compiler_names_its_target(self, name, triple):
        """The name of a GNU cross-compiler is ``<triple>-gcc``; Windows adds ``.exe`` in the case of PATHEXT."""
        assert _detect_target_triple(Path(name)) == triple

    def test_a_bare_architecture_triple_is_supported(self):
        """`avr-g++` gives `avr`, which the prefix `avr-` did not match."""
        assert _detect_target_triple(Path("avr-g++")) == "avr"
        assert _detect_target_triple(Path("msp430-gcc")) == "msp430"

    def test_a_target_without_a_backend_gives_none(self):
        assert _detect_target_triple(Path("xtensa-esp32-elf-gcc")) is None

    @pytest.mark.parametrize("name", ["gcc", "cc", "c++", "clang"])
    def test_a_name_without_a_target_gives_none(self, name):
        assert _detect_target_triple(Path(name)) is None


class TestExpandResponseFile:
    def test_expands_valid_file(self, tmpdir):
        rsp = tmpdir / "flags.rsp"
        rsp.write_text("-DFOO=1 -DBAR=2\n")
        result = expand_response_file(f"@{rsp}", tmpdir)
        assert result == ["-DFOO=1", "-DBAR=2"]

    def test_missing_file_returns_empty(self, tmpdir):
        result = expand_response_file("@/nonexistent.rsp", tmpdir)
        assert result == []

    def test_relative_path(self, tmpdir):
        rsp = tmpdir / "flags.rsp"
        rsp.write_text("-DBAZ=3\n")
        result = expand_response_file("@flags.rsp", tmpdir)
        assert result == ["-DBAZ=3"]


class TestNormalizeArgs:
    def test_drops_output_flag(self):
        result = normalize_args(["-std=c++14", "-o", "build/main.o", "main.cpp"], Path.cwd())
        assert "-o" not in result
        assert "build/main.o" not in result

    def test_drops_dependency_flags(self):
        result = normalize_args(["-std=c++14", "-MD", "-MF", "main.d", "main.cpp"], Path.cwd())
        assert "-MD" not in result
        assert "-MF" not in result
        assert "main.d" not in result

    def test_drops_lto(self):
        result = normalize_args(["-flto", "-std=c++14", "main.cpp"], Path.cwd())
        assert "-flto" not in result

    def test_drops_pipe(self):
        result = normalize_args(["-pipe", "-std=c++14", "main.cpp"], Path.cwd())
        assert "-pipe" not in result

    def test_drops_save_temps(self):
        result = normalize_args(["-save-temps", "-std=c++14", "main.cpp"], Path.cwd())
        assert "-save-temps" not in result

    def test_drops_gcc_only_codegen_flags(self):
        """Flags clang rejects outright, which kills the whole parse.

        They change no declaration and no macro, so dropping them cannot
        change what the index sees.  Found by running clang over the flags
        of every real project at once rather than one at a time — there were
        five, not the two that first surfaced.
        """
        gcc_only = [
            "-fno-reorder-functions",
            "-fno-printf-return-value",
            "-fstrict-volatile-bitfields",
            "-fno-tree-switch-conversion",
        ]
        result = normalize_args([*gcc_only, "-std=c11", "main.c"], Path.cwd())

        for flag in gcc_only:
            assert flag not in result, f"clang rejects {flag} as an unknown argument"

    def test_drops_the_fp16_format_flag_with_any_value(self):
        """A prefix, not an exact match: the value varies by project."""
        for value in ("ieee", "alternative"):
            result = normalize_args(
                [f"-mfp16-format={value}", "-std=c11", "main.c"], Path.cwd()
            )
            assert not any(a.startswith("-mfp16-format") for a in result)

    def test_drops_the_gcc_shrink_wrap_flag(self):
        """ESP-IDF with GCC 13: clang rejected it in every unit."""
        assert "-fno-shrink-wrap" not in normalize_args(["-fno-shrink-wrap", "-O2"], Path.cwd())

    def test_drops_the_gcc_defer_pop_flag(self):
        """Zephyr/NCS mbedtls units: libclang gave an error for it in 33 units of each image."""
        assert "-fno-defer-pop" not in normalize_args(["-fno-defer-pop", "-O2"], Path.cwd())

    def test_drops_the_gcc_tuning_parameters_in_both_forms(self):
        """Zephyr/NCS: libclang gave an error for --param=min-pagesize=0 in 33 units of three images."""
        result = normalize_args(
            ["--param=min-pagesize=0", "--param", "max-inline-insns=10", "-DKEEP=1"], Path.cwd(),
        )
        assert not any("param" in arg or "pagesize" in arg or "inline-insns" in arg for arg in result), result
        assert "-DKEEP=1" in result

    @pytest.mark.parametrize("value", ["all", "unions", "standard"])
    def test_drops_the_gcc15_padding_flag(self, value):
        """ESP-IDF 6.1 (GCC 15): clang refused it, and the macros step failed in each of 968 units."""
        result = normalize_args([f"-fzero-init-padding-bits={value}", "-DKEEP=1"], Path.cwd())
        assert not any("padding" in arg for arg in result), result
        assert "-DKEEP=1" in result

    @pytest.mark.parametrize("flag", ["-fmalloc-dce", "-fno-malloc-dce"])
    def test_drops_the_gcc15_malloc_flag(self, flag):
        """ESP-IDF 6.1 (GCC 15): clang refused -fno-malloc-dce, and the macros step failed in each of 968 units."""
        result = normalize_args([flag, "-DKEEP=1"], Path.cwd())
        assert flag not in result
        assert "-DKEEP=1" in result

    def test_a_flag_that_changes_meaning_is_kept(self):
        """The strip list must stay narrow — these decide what compiles.

        The include directory is absolute in the form of this system:
        ``normalize_args`` gives ``/inc`` on Windows the drive of the
        directory of the unit, as the compiler does.
        """
        include = f"-I{os.path.abspath('/inc')}"
        result = normalize_args(
            ["-DFOO=1", include, "-std=c11", "-mcpu=cortex-m4", "main.c"], Path.cwd()
        )
        for keep in ("-DFOO=1", include, "-std=c11", "-mcpu=cortex-m4"):
            assert keep in result, f"{keep} decides what the compiler sees"

    @pytest.mark.parametrize("flags", [
        ["-iprefix", "libs/include/"],
        ["-iprefixlibs/include/"],
    ])
    def test_a_relative_prefix_becomes_absolute_and_keeps_its_separator(self, tmp_path, flags):
        """GCC joins the prefix and the value of ``-iwithprefix*`` as text, thus the separator stays.

        libclang reads a relative path from the working directory of the
        process, not of the compilation.
        """
        result = normalize_args([*flags, "-iwithprefixbefore", "newlib", "main.c"], tmp_path)
        prefix = str(tmp_path.resolve() / "libs" / "include") + "/"
        assert (["-iprefix", prefix] if len(flags) == 2 else [f"-iprefix{prefix}"]) == [
            arg for arg in result if prefix in arg or arg == "-iprefix"
        ]
        # The value of -iwithprefixbefore is relative to the prefix.
        assert result[result.index("-iwithprefixbefore") + 1] == "newlib"

    @pytest.mark.parametrize("flags", [["--sysroot", "sys"], ["--sysroot=sys"], ["-isysroot", "sys"], ["-isysrootsys"]])
    def test_a_relative_sysroot_becomes_absolute(self, tmp_path, flags):
        result = normalize_args([*flags, "main.c"], tmp_path)
        assert any(str(tmp_path.resolve() / "sys") in arg for arg in result), result

    def test_drops_specs(self):
        result = normalize_args(["-specs=nosys.specs", "-std=c++14", "main.cpp"], Path.cwd())
        assert not any(a.startswith("-specs") for a in result)

    def test_drops_specs_with_space(self):
        """-specs <file> drops both tokens."""
        result = normalize_args(["-specs", "nosys.specs", "-std=c++14", "main.cpp"], Path.cwd())
        assert "-specs" not in result
        assert "nosys.specs" not in result

    def test_drops_gcc_only_warnings(self):
        result = normalize_args(["-Wno-stringop-truncation", "-Wall", "main.cpp"], Path.cwd())
        assert "-Wno-stringop-truncation" not in result
        assert "-Wall" in result

    def test_drops_format_signedness(self):
        result = normalize_args(["-Wformat-signedness", "-Wall", "main.c"], Path.cwd())
        assert "-Wformat-signedness" not in result
        assert "-Wall" in result

    def test_drops_format_overflow_level(self):
        result = normalize_args(
            ["-Wformat-overflow=2", "-Wformat-truncation=1", "-Wall", "main.c"],
            Path.cwd(),
        )
        assert not any(a.startswith("-Wformat-overflow") for a in result)
        assert not any(a.startswith("-Wformat-truncation") for a in result)
        assert "-Wall" in result

    def test_the_value_of_an_other_option_stays_as_it_is(self, tmp_path):
        """``-include-pch``: no attached form of ``-include`` with the path ``-pch``."""
        result = normalize_args(["-include-pch", "pch.h.pch", "main.c"], tmp_path)
        assert result[result.index("-include-pch") + 1] == "pch.h.pch"

    def test_gives_no_target(self):
        """The caller adds the target: of the GCC driver, or of the compiler name and ``-mcpu``."""
        result = normalize_args(["-mcpu=cortex-m4", "-march=rv32imc", "-std=c++14", "main.cpp"], Path.cwd())
        assert not any(arg.startswith(("--target", "-target")) for arg in result)

    def test_removes_source_file(self):
        result = normalize_args(["-std=c++14", "main.cpp"], Path.cwd())
        assert "main.cpp" not in result

    def test_expands_response_file(self, tmpdir):
        rsp = tmpdir / "extra.rsp"
        rsp.write_text("-DEXTRA=1\n")
        result = normalize_args([f"@{rsp}", "-std=c++14", "main.cpp"], tmpdir)
        assert "-DEXTRA=1" in result

    def test_appends_suppress_unknown_warning(self):
        result = normalize_args(["-Wno-maybe-uninitialized", "-Wall", "main.cpp"], Path.cwd())
        assert result[-1] == "-Wno-unknown-warning-option"
        assert "-Wno-maybe-uninitialized" in result

    def test_suppress_flag_wins_after_werror(self):
        result = normalize_args(["-Werror", "-std=c99", "main.c"], Path.cwd())
        assert result[-1] == "-Wno-unknown-warning-option"
        assert result.index("-Werror") < result.index("-Wno-unknown-warning-option")

    def test_an_unused_flag_is_no_error_under_werror(self):
        """libclang 22, Zephyr/NCS mbedtls units: "argument unused during compilation: '-c'" was an error."""
        result = normalize_args(["-Werror", "-c", "main.c"], Path.cwd())
        assert result.index("-Werror") < result.index("-Wno-unused-command-line-argument")

    def test_drops_the_xtensa_long_calls_flag(self):
        """ESP32 with libclang 22: "unknown argument '-mlongcalls'" in each unit."""
        assert "-mlongcalls" not in normalize_args(["-mlongcalls", "-O2"], Path.cwd())


class TestGccSystemIncludes:
    """The fallback for a GCC driver that does not answer the query."""

    def test_no_lib_gcc_returns_empty(self, tmpdir):
        """If lib/gcc does not exist, return empty without crash."""
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()
        compiler = bin_dir / "arm-none-eabi-g++"
        compiler.touch()
        result = _gcc_system_includes(compiler, "arm-none-eabi")
        assert result == []

    def test_real_toolchain_structure(self, fake_arm_gcc_toolchain):
        toolchain, compiler = fake_arm_gcc_toolchain
        result = _gcc_system_includes(compiler, "arm-none-eabi")
        assert len(result) >= 4  # gcc include, include-fixed, libc, c++
        assert all(r.startswith("-isystem") for r in result[::2])
        assert any("include-fixed" in r for r in result)

    def test_a_driver_that_does_not_answer_keeps_the_guessed_directories(self, fake_arm_gcc_toolchain, tmpdir):
        """The fake compiler is an empty file that cannot run, thus the query gets no answer."""
        toolchain, compiler = fake_arm_gcc_toolchain
        (tmpdir / "main.c").write_text("int x;\n")
        cc = tmpdir / "cc.json"
        cc.write_text(json.dumps([{
            "directory": str(tmpdir), "file": "main.c",
            "arguments": [str(compiler), "-mcpu=cortex-m4", "-c", "main.c"],
        }]))
        unit = next(parse(Path(cc)))
        assert "--target=arm-none-eabi" in unit.clang_args
        assert any("include-fixed" in a for a in unit.clang_args)
        assert "-nostdinc" not in unit.clang_args


class TestParse:
    def test_parse_minimal(self, compile_commands_json, tmpdir):
        (tmpdir / "src").mkdir(parents=True)
        (tmpdir / "src" / "main.cpp").touch()
        (tmpdir / "lib").mkdir()
        (tmpdir / "lib" / "helper.c").touch()

        units = list(parse(compile_commands_json))
        assert len(units) == 2

        main = units[0]
        assert main.file == (tmpdir / "src" / "main.cpp").resolve()
        assert main.language == "cpp"
        assert "--target=arm-none-eabi" in main.clang_args

        helper = units[1]
        assert helper.language == "c"

    def test_parse_empty_file(self, tmpdir):
        path = tmpdir / "empty.json"
        path.write_text("[]")
        units = list(parse(path))
        assert units == []

    def test_parse_command_string(self, tmpdir):
        """Parse entry with 'command' instead of 'arguments'."""
        data = [{
            "directory": str(tmpdir),
            "file": "src/main.c",
            "command": "arm-none-eabi-gcc -std=c11 -O2 -o build/main.o src/main.c",
        }]
        (tmpdir / "src").mkdir()
        (tmpdir / "src" / "main.c").touch()
        path = tmpdir / "cc.json"
        path.write_text(json.dumps(data))
        units = list(parse(path))
        assert len(units) == 1
        assert units[0].language == "c"


class TestSourceExts:
    """TU_EXTENSIONS should cover all common C/C++ extensions.

    The set moved to utils: three modules kept their own copy and they had
    drifted — `.c++` was in two of them and not in the third, so a new
    `.c++` file was never reported as missing from compile_commands.json.
    """

    def test_c(self):
        assert ".c" in TU_EXTENSIONS

    def test_cpp(self):
        assert ".cpp" in TU_EXTENSIONS

    def test_cc(self):
        assert ".cc" in TU_EXTENSIONS

    def test_cxx(self):
        assert ".cxx" in TU_EXTENSIONS

    def test_cxx_plus_plus(self):
        assert ".c++" in TU_EXTENSIONS

    def test_uppercase_is_covered_by_the_comparison(self):
        """`.C` is a C++ source; the set is lowercase and callers lower()."""
        assert _is_source_file("a.C")
        assert _is_source_file("a.CPP")


class TestEdgeCases:
    """Edge case tests for compile_commands processing."""

    def test_is_source_file_empty_string(self):
        assert not _is_source_file("")

    def test_is_source_file_hpp(self):
        # .hpp is a header, not a source file
        assert not _is_source_file("header.hpp")

    def test_is_source_file_no_extension(self):
        assert not _is_source_file("Makefile")

    def test_is_source_file_uppercase(self):
        # .CPP should still be detected
        assert _is_source_file("main.CPP")

    def test_unit_language_empty_file(self):
        assert unit_language(Path(""), []) == "c"

    def test_normalize_args_empty_list(self):
        result = normalize_args([], Path("/tmp"), source_file="test.c")
        assert isinstance(result, list)

    def test_parse_utf8_bom(self, tmpdir, toolchain):
        """compile_commands.json with UTF-8 BOM — current behavior: fails.

        Path.read_text() uses utf-8 (not utf-8-sig), so BOM is not stripped.
        This test documents the current behavior; the code could be fixed
        to use encoding='utf-8-sig' if needed.
        """
        write_sources(Path(str(tmpdir)), {"src/main.c": "int main(void) { return 0; }\n"})
        data = [
            {
                "directory": str(tmpdir),
                "file": "src/main.c",
                "arguments": toolchain.arguments("src/main.c"),
            },
        ]
        path = tmpdir / "compile_commands_bom.json"
        write_compile_commands(Path(str(path)), data)
        content = json.dumps(data)
        path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        units = list(parse(path))
        assert len(units) == 1, f"Expected 1 unit, got {len(units)}; BOM should be handled"

    def test_parse_empty_list(self, tmpdir):
        """Empty compile_commands array should parse to no units."""
        path = tmpdir / "empty_cc.json"
        path.write_text("[]", encoding="utf-8")
        units = list(parse(path))
        assert units == []


def test_a_unit_that_the_entry_names_through_a_link_is_parsed(tmp_path, toolchain):
    """The entry names the source through a link: the unit has the canonical name, and the link must leave the flags.

    A compare of base names (``bar.c`` against ``foo.c``) kept the link in
    the flags.  libclang then got two input files and refused the unit
    (TranslationUnitLoadError).  The same holds for a different case on a
    case-insensitive file system.
    """

    from fw_context_mcp.indexer._libclang import cindex
    from fw_context_mcp.indexer.compile_commands import parse

    (tmp_path / "common").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "common" / "bar.c").write_text("int bar(void) { return 1; }\n", encoding="utf-8")
    link = tmp_path / "app" / "foo.c"
    try:
        link.symlink_to(tmp_path / "common" / "bar.c")
    except OSError as exc:
        pytest.skip(f"this system makes no link for this user: {exc}")
    cc = write_compile_commands(tmp_path / "compile_commands.json", [{
        "directory": str(tmp_path), "file": str(link), "arguments": toolchain.arguments(str(link), output="foo.o"),
    }])

    (unit,) = parse(cc)

    assert unit.file.name == "bar.c"
    assert str(link) not in unit.clang_args
    cindex.Index.create().parse(str(unit.file), args=unit.clang_args)


def _database_with_bom(tmp_path):
    """Write a compile_commands.json with a byte order mark, as a tool on Windows can write it."""
    entry = {"directory": str(tmp_path), "file": str(tmp_path / "main.c"),
             "arguments": ["cc", "-DBOARD=7", "-c", "main.c"]}
    path = tmp_path / "compile_commands.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps([entry]).encode("utf-8"))
    return path


def test_each_reader_of_the_database_reads_a_file_with_a_byte_order_mark(tmp_path):
    """parse() indexed such a file, and the other readers took it for a damaged file.

    command_line_defines gave no defines, compile_directory gave no
    directory, and the validator reported "Cannot parse".
    """
    from fw_context_mcp.indexer.build import check_completeness
    from fw_context_mcp.indexer.compile_commands import command_line_defines, compile_directory, load_entries

    path = _database_with_bom(tmp_path)

    assert len(load_entries(path)) == 1
    assert command_line_defines(path) == ({"BOARD": "7"}, 0)
    assert compile_directory(path) == tmp_path
    assert "Cannot parse compile_commands.json" not in check_completeness(path, tmp_path)


def test_a_command_with_a_quote_that_does_not_end_gives_no_defines(tmp_path, monkeypatch):
    """parse() refuses such a file (ValueError of the split), and get_build_info got the exception.

    The documented answer for a file that cannot be read is no defines.
    """
    from fw_context_mcp.indexer import compile_commands

    def unsplittable(text):
        raise ValueError("No closing quotation")

    monkeypatch.setattr(compile_commands, "command_words", unsplittable)
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps([{"directory": str(tmp_path), "file": "a.c", "command": 'cc -DX="1 -c a.c'}]),
                    encoding="utf-8")

    assert compile_commands.command_line_defines(path) == ({}, 0)


def test_a_loop_of_links_in_a_directory_of_the_flags_does_not_stop_the_parse(tmp_path, monkeypatch):
    """Path.resolve of Python 3.11 and 3.12 raises RuntimeError for a loop of links; 3.13 gives a path.

    normalize_args resolves each relative directory of -I, -isystem and the
    attached forms, and compares each positional token with the source: one
    loop stopped the parse of the whole build on 3.11 and 3.12.  The fake
    gives the behavior of those versions on each Python: the code must not
    depend on it (path_key.resolve_path).
    """

    from fw_context_mcp.path_key import canonical_path

    (tmp_path / "main.c").write_text("int x;\n", encoding="utf-8")
    try:
        (tmp_path / "loop").symlink_to(tmp_path / "loop")
    except OSError as exc:
        pytest.skip(f"this system makes no link for this user: {exc}")

    real_resolve = Path.resolve

    def resolve_of_python_311(self, strict=False):
        if "loop" in self.parts:
            raise RuntimeError(f"Symlink loop from '{self}'")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_of_python_311)

    flags = normalize_args(["-isystem", "loop", "-Iloop", "-isystemloop", "loop", "-c", "main.c"], tmp_path, "main.c")

    assert flags.count(str(tmp_path / "loop")) >= 1
    assert "main.c" not in flags
    assert canonical_path(tmp_path / "loop") == tmp_path / "loop"


_WHICH_LANGUAGE = "#ifdef __cplusplus\nint is_cxx;\n#else\nint is_c;\n#endif\n"


@pytest.mark.libclang
@pytest.mark.parametrize(("source", "driver", "flags", "expected"), [
    # g++ compiles a .c file as C++ (`man gcc`).
    ("f.c", "c++", [], "cpp"),
    ("f.c", "c", ["-x", "c++"], "cpp"),
    ("g.cpp", "c", ["-x", "c"], "c"),
    # -std= selects no language: GCC compiles C (C++), with a warning.
    ("f.c", "c", ["-std=gnu++17"], "c"),
    ("g.cpp", "c++", ["-std=c11"], "cpp"),
])
def test_the_unit_has_the_language_that_the_compiler_gives_it(tmp_path, toolchain, source, driver, flags, expected):
    """The language, the system headers of the driver query and the parse of libclang follow the compiler.

    The language came from the suffix and from `-std=`: a unit of g++ of a
    `.c` file was a C unit, the driver query gave the headers of C, and
    libclang parsed C; a `.c` file with `-std=gnu++17` was a C++ unit, and
    libclang refused it.
    """
    from fw_context_mcp.indexer._libclang import cindex
    from fw_context_mcp.indexer.compile_commands import parse

    write_sources(tmp_path, {source: _WHICH_LANGUAGE})
    entry = {
        "directory": str(tmp_path), "file": source,
        "arguments": toolchain.arguments(source, *flags, language=driver),
    }
    (unit,) = parse(write_compile_commands(tmp_path / "compile_commands.json", [entry]))

    tu = cindex.Index.create().parse(str(unit.file), args=unit.clang_args)
    names = {cursor.spelling for cursor in tu.cursor.get_children()} & {"is_c", "is_cxx"}
    cxx_headers = [arg for arg in unit.clang_args if "/c++/" in arg.replace("\\", "/")]
    assert unit.language == expected
    assert names == ({"is_cxx"} if expected == "cpp" else {"is_c"})
    assert bool(cxx_headers) == (expected == "cpp"), cxx_headers


@pytest.mark.libclang
def test_an_x_after_the_source_does_not_reach_libclang(tmp_path, toolchain):
    """GCC applies -x to the files after it; the libclang arguments hold no source token, thus it must leave."""
    from fw_context_mcp.indexer.compile_commands import parse

    write_sources(tmp_path, {"f.c": _WHICH_LANGUAGE})
    entry = {"directory": str(tmp_path), "file": "f.c", "arguments": [*toolchain.arguments("f.c"), "-x", "c++"]}
    (unit,) = parse(write_compile_commands(tmp_path / "compile_commands.json", [entry]))

    assert unit.language == "c"
    assert "-x" not in unit.clang_args


def test_a_relative_prefix_reaches_the_parse(tmp_path, toolchain):
    """libclang finds the header of an ``-iwithprefixbefore`` directory under a relative ``-iprefix``."""
    from tests._toolchains import parse_unit

    (tmp_path / "libs" / "include" / "sub").mkdir(parents=True)
    (tmp_path / "libs" / "include" / "sub" / "platform.h").write_text("int platform_level;\n", encoding="utf-8")
    source = tmp_path / "main.c"
    source.write_text('#include "platform.h"\nint x;\n', encoding="utf-8")

    unit = parse_unit(
        source, toolchain, ["-iprefix", "libs/include/", "-iwithprefixbefore", "sub"], directory=tmp_path,
    )

    assert [d.spelling for d in unit.diagnostics if d.severity >= 3] == []


def test_a_unit_of_a_gcc_driver_has_one_target(tmp_path, toolchain):
    """The target of the driver alone: a second one from ``-mcpu``/``-march`` came last and won.

    Measured on an ESP32-C3 build: ``--target=riscv32-esp-elf`` of the
    driver, then ``--target=riscv32-unknown-elf`` from ``-march=rv32imc...``.
    """
    from tests._toolchains import unit_of

    source = tmp_path / "main.c"
    source.write_text("int x;\n", encoding="utf-8")

    unit = unit_of(source, toolchain, directory=tmp_path)

    targets = [arg for arg in unit.clang_args if arg.startswith("--target=")]
    assert len(targets) == 1, targets


def _unit_of_entry(tmp_path, arguments):
    """The unit of one entry of compile_commands.json with *arguments*, for ``main.c`` in *tmp_path*."""
    (tmp_path / "main.c").write_text("int x;\n", encoding="utf-8")
    database = tmp_path / "compile_commands.json"
    database.write_text(json.dumps([{"directory": str(tmp_path), "file": "main.c", "arguments": arguments}]))
    return next(parse(database))


def test_a_unit_with_its_own_target_keeps_it_alone(tmp_path):
    """A clang build: the target of the unit, and no second one from ``-mcpu`` after it, which won."""
    unit = _unit_of_entry(tmp_path, ["clang", "--target=thumbv7em-none-eabi", "-mcpu=cortex-m4", "-c", "main.c"])

    assert [arg for arg in unit.clang_args if "target" in arg] == ["--target=thumbv7em-none-eabi"]


@pytest.mark.parametrize("flag", ["-mcpu=cortex-m4", "-mcpu=cortex-a53", "-march=rv64imac", "-march=armv7e-m"])
def test_a_cpu_flag_names_no_target(tmp_path, flag):
    """The driver does not answer (no such compiler), and its name names no target.

    ``-mcpu=`` and ``-march=`` do not name the target:
    ``arm-none-eabi-gcc -mcpu=cortex-a53`` compiles 32-bit code.  A table
    gave ``cortex-a53`` the 32-bit triple and ``rv64`` a 32-bit one.
    """
    unit = _unit_of_entry(tmp_path, [str(tmp_path / "gcc"), flag, "-c", "main.c"])

    assert not any(arg.startswith("--target=") for arg in unit.clang_args)


def test_a_clang_cross_compiler_gets_its_target_and_no_directories_of_gcc(tmp_path, fake_arm_gcc_toolchain):
    """``arm-none-eabi-clang`` beside a GCC toolchain: the target of the name, and its own headers."""
    toolchain, compiler = fake_arm_gcc_toolchain
    clang = compiler.parent / "arm-none-eabi-clang"
    clang.touch()

    unit = _unit_of_entry(tmp_path, [str(clang), "-c", "main.c"])

    assert [arg for arg in unit.clang_args if arg.startswith("--target=")] == ["--target=arm-none-eabi"]
    assert not any("include-fixed" in arg for arg in unit.clang_args)
