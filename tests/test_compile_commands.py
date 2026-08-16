"""Tests for fw_context_mcp.indexer.compile_commands."""

import json
from pathlib import Path

from fw_context_mcp.indexer.compile_commands import (
    _SOURCE_EXTS,
    _detect_language,
    _detect_target_triple,
    _gcc_system_includes,
    _is_source_file,
    expand_response_file,
    normalize_args,
    parse,
)


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


class TestDetectLanguage:
    def test_cpp_by_extension(self):
        assert _detect_language(Path("foo.cpp"), []) == "cpp"
        assert _detect_language(Path("foo.cc"), []) == "cpp"

    def test_cpp_by_std_flag(self):
        assert _detect_language(Path("foo.c"), ["-std=c++14"]) == "cpp"

    def test_c_by_default(self):
        assert _detect_language(Path("foo.c"), []) == "c"
        assert _detect_language(Path("foo.s"), []) == "c"


class TestInferTarget:
    def test_cortex_m4(self):
        assert _detect_target_triple(None, ["-mcpu=cortex-m4", "-std=c++14"]) == "arm-none-eabi"

    def test_cortex_m33(self):
        assert _detect_target_triple(None, ["-mcpu=cortex-m33"]) == "arm-none-eabi"

    def test_no_mcpu(self):
        assert _detect_target_triple(None, ["-std=c99"]) is None

    def test_empty_args(self):
        assert _detect_target_triple(None, []) is None

    def test_arm_compiler_name(self):
        assert _detect_target_triple(Path("arm-none-eabi-g++"), []) == "arm-none-eabi"

    def test_riscv_compiler_name(self):
        assert _detect_target_triple(Path("riscv32-unknown-elf-gcc"), []) == "riscv32-unknown-elf"


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

    def test_injects_target_for_cortex(self):
        result = normalize_args(["-mcpu=cortex-m4", "-std=c++14", "main.cpp"], Path.cwd())
        assert result[0] == "--target=arm-none-eabi"

    def test_no_target_without_mcpu(self):
        result = normalize_args(["-std=c99", "main.c"], Path.cwd())
        assert "--target" not in result[0]

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


class TestGccSystemIncludes:
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
    """_SOURCE_EXTS should cover all common C/C++ extensions."""
    def test_c(self):
        assert ".c" in _SOURCE_EXTS

    def test_cpp(self):
        assert ".cpp" in _SOURCE_EXTS

    def test_cc(self):
        assert ".cc" in _SOURCE_EXTS

    def test_cxx(self):
        assert ".cxx" in _SOURCE_EXTS


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

    def test_detect_language_empty_file(self):
        assert _detect_language(Path(""), []) == "c"

    def test_normalize_args_empty_list(self):
        result = normalize_args([], Path("/tmp"), source_file="test.c")
        assert isinstance(result, list)

    def test_parse_utf8_bom(self, tmpdir):
        """compile_commands.json with UTF-8 BOM — current behavior: fails.

        Path.read_text() uses utf-8 (not utf-8-sig), so BOM is not stripped.
        This test documents the current behavior; the code could be fixed
        to use encoding='utf-8-sig' if needed.
        """
        data = [
            {
                "directory": str(tmpdir),
                "file": "src/main.c",
                "arguments": ["gcc", "-c", "src/main.c"],
            },
        ]
        path = tmpdir / "compile_commands_bom.json"
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

    def test_detect_target_triple_no_args(self):
        assert _detect_target_triple(None, []) is None
