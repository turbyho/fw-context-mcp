"""The libclang flags of a GCC build come from the GCC driver of that build.

Before ``_driver_query`` the indexer guessed the system directories from the
layout of the toolchain and used the macros of clang.  A target that libclang
has no backend for (xtensa) then got the headers of the HOST: 252 of 757
files of an ESP32 index came from ``/usr/include``, and ``#if __XTENSA__``
read as false.

The tests use a stub driver: a shell script that prints the answers of a
real ``xtensa-esp32-elf-gcc`` 8.4.0 (shortened), its own working directory
and its own arguments.  Thus they need no toolchain on the machine.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from fw_context_mcp.indexer import _driver_query
from fw_context_mcp.indexer._driver_query import (
    DriverInfo,
    clear_query_cache,
    driver_allowed,
    is_gcc_driver_name,
    libclang_args,
    parse_defines,
    parse_verbose_output,
    predefine_flags,
    proxy_target,
    query_gcc_driver,
    resolve_compiler,
)

#: The real function; the autouse fixture replaces the module attribute.
_REAL_CLANG_RESOURCE_INCLUDE = _driver_query.clang_resource_include

XTENSA_MACROS = """\
#define __XTENSA__ 1
#define __XTENSA_EL__ 1
#define __GNUC__ 8
#define __SIZEOF_INT__ 4
#define __SIZEOF_LONG__ 4
#define __SIZEOF_POINTER__ 4
#define __SIZEOF_LONG_DOUBLE__ 8
#define __SIZEOF_WCHAR_T__ 2
#define __CHAR_UNSIGNED__ 1
#define __VERSION__ "8.4.0"
#define __STDC_HOSTED__ 1
#define __has_include(STR) __has_include__(STR)
#define __INT32_C(c) c
#define __FLT32_DIG__ 6
#define __BFLT16_DIG__ 2
#define __EMPTY__
"""


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """The answers are kept per process; a test must not see the answer of another.

    The tests also run as if no clang were installed, thus the result does
    not depend on the machine.  ``TestClangResourceDir`` sets one.
    """
    # The real function: a test can replace the module attribute, and the
    # replacement is still in place when this teardown runs.
    supports = _driver_query.libclang_supports
    clear_query_cache()
    supports.cache_clear()
    monkeypatch.setattr(_driver_query, "clang_resource_include", lambda: None)
    yield
    clear_query_cache()
    supports.cache_clear()


def _stub_driver(directory: Path, name: str, *, macros: str = XTENSA_MACROS,
                 triple: str = "xtensa-esp32-elf", exit_code: int = 0) -> tuple[Path, list[Path]]:
    """Write an executable stub GCC driver and its system include directories.

    The stub also prints two macros of its own: ``__STUB_CWD__`` (its working
    directory) and ``__STUB_ARGS__`` (its arguments).
    """
    toolchain = directory / "toolchain"
    cxx = toolchain / triple / "include" / "c++" / "8.4.0"
    gcc_inc = toolchain / "lib" / "gcc" / triple / "8.4.0" / "include"
    libc = toolchain / triple / "include"
    for path in (cxx, gcc_inc, libc):
        path.mkdir(parents=True, exist_ok=True)
    (gcc_inc / "stdbool.h").write_text("#define bool _Bool\n")
    bin_dir = toolchain / "bin"
    bin_dir.mkdir(exist_ok=True)
    driver = bin_dir / name
    # The order is the order of GCC: libstdc++ first, the C library last.
    search = "\n".join(f" {p}" for p in (cxx, gcc_inc, libc))
    driver.write_text(
        "#!/bin/sh\n"
        f"[ {exit_code} -ne 0 ] && exit {exit_code}\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "-v" ]; then\n'
        "    cat >&2 <<'EOF'\n"
        f"Target: {triple}\n"
        '#include "..." search starts here:\n'
        "#include <...> search starts here:\n"
        f"{search}\n"
        "End of search list.\n"
        "EOF\n"
        "    exit 0\n"
        "  fi\n"
        '  if [ "$a" = "-dM" ]; then\n'
        "    cat <<'EOF'\n"
        f"{macros}"
        "EOF\n"
        '    echo "#define __STUB_CWD__ $(pwd)"\n'
        '    echo "#define __STUB_ARGS__ $*"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 1\n"
    )
    driver.chmod(driver.stat().st_mode | stat.S_IXUSR)
    return driver, [cxx, gcc_inc, libc]


def _xtensa_info(dirs: tuple[str, ...] = ("/tc/c++", "/tc/gcc", "/tc/libc")) -> DriverInfo:
    return DriverInfo(triple="xtensa-esp32-elf", include_dirs=dirs, macros=tuple(parse_defines(XTENSA_MACROS)))


class TestDriverName:
    @pytest.mark.parametrize("name", [
        "gcc", "g++", "cc", "c++", "gcc-12", "arm-none-eabi-gcc", "xtensa-esp32-elf-g++",
        "avr-gcc", "riscv64-zephyr-elf-gcc", "x86_64-w64-mingw32-gcc.exe",
    ])
    def test_a_gcc_driver_name_matches(self, name: str) -> None:
        assert is_gcc_driver_name(Path(name))

    @pytest.mark.parametrize("name", ["clang", "clang++", "arm-none-eabi-clang", "ccache", "armcc5", "ld"])
    def test_another_binary_does_not_match(self, name: str) -> None:
        assert not is_gcc_driver_name(Path(name))


class TestAllowlist:
    """The query runs the binary, thus only a compiler on the allowlist runs."""

    def test_a_double_star_crosses_directories(self) -> None:
        assert driver_allowed(Path("/opt/sdk/v1/bin/arm-none-eabi-gcc"), ["/opt/**"])

    def test_a_single_star_stays_in_one_directory(self) -> None:
        assert driver_allowed(Path("/usr/bin/gcc"), ["/usr/bin/*"])
        assert not driver_allowed(Path("/usr/bin/sub/gcc"), ["/usr/bin/*"])

    def test_a_dot_dot_path_does_not_escape(self) -> None:
        """``fnmatch`` would let ``/usr/bin/*`` match this path."""
        assert not driver_allowed(Path("/usr/bin/../../home/x/proj/evil-gcc"), ["/usr/bin/*"])

    def test_a_case_insensitive_host_ignores_case(self, monkeypatch) -> None:
        """macOS and Windows ignore case, thus /Opt/X is /opt/x there."""
        monkeypatch.setattr(_driver_query, "_CASE_INSENSITIVE_FS", True)
        assert driver_allowed(Path("/OPT/sdk/bin/arm-none-eabi-gcc"), ["/opt/**"])
        assert not driver_allowed(Path("/Proj/tools/x-gcc"), ["/**"], project_root=Path("/proj"))
        monkeypatch.setattr(_driver_query, "_CASE_INSENSITIVE_FS", False)
        assert not driver_allowed(Path("/OPT/sdk/bin/arm-none-eabi-gcc"), ["/opt/**"])

    @pytest.mark.parametrize("platform, expected", [
        ("linux", "~/.arduino15/packages/**"),
        ("darwin", "~/Library/Arduino15/packages/**"),
        ("win32", "~/AppData/Local/Arduino15/packages/**"),
    ])
    def test_each_host_has_its_own_default(self, platform, expected) -> None:
        from fw_context_mcp.config.settings import _default_query_driver

        defaults = _default_query_driver(platform)
        assert expected in defaults
        assert "~/.platformio/packages/**" in defaults

    def test_the_home_directory_expands(self) -> None:
        home_tool = Path.home() / ".platformio" / "packages" / "tc" / "bin" / "gcc"
        assert driver_allowed(home_tool, ["~/.platformio/packages/**"])

    def test_a_compiler_inside_the_project_never_runs(self, tmp_path: Path) -> None:
        """A broad pattern such as /opt/** also covers a project stored under /opt."""
        project = tmp_path / "proj"
        tool = project / "tools" / "x-gcc"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        assert driver_allowed(tool, [f"{tmp_path}/**"])
        assert not driver_allowed(tool, [f"{tmp_path}/**"], project_root=project)

    def test_a_link_into_the_project_does_not_pass(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        (project / "tools").mkdir(parents=True)
        (project / "tools" / "x-gcc").write_text("")
        link = tmp_path / "allowed" / "x-gcc"
        link.parent.mkdir()
        link.symlink_to(project / "tools" / "x-gcc")
        assert not driver_allowed(link, [f"{tmp_path}/allowed/*"], project_root=project)

    def test_a_compiler_inside_the_project_gets_its_own_warning(self, tmp_path: Path, caplog) -> None:
        """A glob in query_driver cannot allow it, thus the warning must not advise one."""
        from fw_context_mcp.indexer.compile_commands import parse

        project = tmp_path / "proj"
        project.mkdir()
        driver, _ = _stub_driver(project, "vendored-gcc")
        unit = next(parse(_cc(tmp_path, str(driver)), [f"{tmp_path}/**"], project))
        assert "-nostdinc" not in unit.clang_args
        assert any("inside the project" in r.message for r in caplog.records)

    def test_a_compiler_in_the_repository_is_not_run(self, tmp_path: Path, caplog) -> None:
        from fw_context_mcp.indexer.compile_commands import parse

        driver, _ = _stub_driver(tmp_path, "evil-gcc")
        cc = _cc(tmp_path, str(driver))
        unit = next(parse(cc, ["/opt/**"]))

        assert "-nostdinc" not in unit.clang_args, "a compiler outside the allowlist must not run"
        assert any("query_driver" in r.message for r in caplog.records)


class TestReadTheDriverAnswer:
    def test_the_directories_keep_the_driver_order(self, tmp_path: Path) -> None:
        stderr = (
            "Target: xtensa-esp32-elf\n"
            "#include <...> search starts here:\n"
            f" {tmp_path}/bin/../lib/c++\n"
            f" {tmp_path}/bin/../lib/gcc\n"
            "End of search list.\n"
        )
        triple, dirs = parse_verbose_output(stderr)
        assert triple == "xtensa-esp32-elf"
        # Resolved: GCC writes them relative to its binary.
        assert dirs == [str((tmp_path / "lib/c++").resolve()), str((tmp_path / "lib/gcc").resolve())]

    def test_the_macros_keep_parameters_and_empty_values(self) -> None:
        macros = dict(parse_defines(XTENSA_MACROS))
        assert macros["__XTENSA__"] == "1"
        assert macros["__VERSION__"] == '"8.4.0"'
        assert macros["__INT32_C(c)"] == "c"
        assert macros["__EMPTY__"] == ""

    def test_the_macros_that_clang_owns_are_left_out(self) -> None:
        names = {name for name, _ in parse_defines(XTENSA_MACROS)}
        assert "__STDC_HOSTED__" not in names
        assert not any(name.startswith("__has_include") for name in names)

    def test_a_clang_builtin_macro_is_not_redefined(self) -> None:
        """xtensa GCC 13 defines __FLT_EVAL_METHOD__, a builtin of clang even under -undef."""
        assert parse_defines("#define __FLT_EVAL_METHOD__ 0\n#define __FLT_DIG__ 6\n") == [("__FLT_DIG__", "6")]

    def test_the_macros_of_unparsable_types_are_left_out(self) -> None:
        """libstdc++ switches to _Float32 and bf16 literals on them, and libclang 18 fails."""
        names = {name for name, _ in parse_defines(XTENSA_MACROS)}
        assert "__FLT32_DIG__" not in names
        assert "__BFLT16_DIG__" not in names


class TestPredefineFlags:
    def test_only_flags_that_change_a_predefine_go_to_the_driver(self, tmp_path: Path) -> None:
        args = ["-std=gnu++11", "-Os", "-mlongcalls", "-fno-rtti", "-I/x", "-DA=1", "-Wall",
                "-isystem", "/y", "-c", "-ffunction-sections"]
        assert predefine_flags(args, tmp_path) == ("-std=gnu++11", "-Os", "-mlongcalls", "-fno-rtti")

    def test_a_flag_that_runs_or_writes_something_is_left_out(self, tmp_path: Path) -> None:
        """-B runs another cc1, a specs file can add -fplugin, SARIF output writes a file.

        A bare specs name too: the driver looks it up under --sysroot, which
        the repository sets (measured: a plugin from the repository loaded).
        """
        args = ["-B/x/", "-B", "/y/", "-fplugin=evil.so", "-specs=/repo/evil.specs",
                "-fdiagnostics-format=sarif-file", "-fdump-tree-all", "--specs=nano.specs"]
        assert predefine_flags(args, tmp_path) == ()

    def test_a_sysroot_inside_the_project_is_left_out(self, tmp_path: Path) -> None:
        """The driver reads files from the sysroot; the project comes from its repository."""
        project = tmp_path / "proj"
        flags = predefine_flags(["--sysroot=sr", "-isysroot", "/opt/sdk", "-Os"], project, project)
        assert flags == ("-isysroot", "/opt/sdk", "-Os")

    def test_a_signedness_synonym_reaches_the_driver(self, tmp_path: Path) -> None:
        """-fno-signed-char changes __CHAR_UNSIGNED__ as -funsigned-char does."""
        assert predefine_flags(["-fno-signed-char", "-fno-inline"], tmp_path) == ("-fno-signed-char", "-fno-inline")

    def test_a_relative_sysroot_is_made_absolute_against_the_entry(self, tmp_path: Path) -> None:
        """The query runs in the directory of the compiler, not of the build."""
        flags = predefine_flags(["--sysroot=../sr", "-isysroot", "rel"], tmp_path / "build")
        assert flags == (f"--sysroot={os.path.normpath(tmp_path / 'sr')}", "-isysroot",
                         os.path.normpath(tmp_path / "build" / "rel"))


class TestResolveCompiler:
    def test_a_bare_name_is_found_beside_another_compiler_of_the_build(self, tmp_path: Path) -> None:
        """Measured: an ESP32 build names g++ with its directory and gcc without it."""
        driver, _ = _stub_driver(tmp_path, "xtensa-esp32-elf-g++")
        sibling = driver.parent / "xtensa-esp32-elf-gcc"
        sibling.write_text("")
        known = {driver.name: driver}
        assert resolve_compiler("xtensa-esp32-elf-gcc", tmp_path, known) == sibling

    def test_a_symbolic_link_is_not_followed(self, tmp_path: Path) -> None:
        """A ccache masquerade link must run as gcc; its target ccache rejects -E."""
        target = tmp_path / "ccache"
        target.write_text("")
        link = tmp_path / "masquerade" / "gcc"
        link.parent.mkdir()
        link.symlink_to(target)
        assert resolve_compiler(str(link), tmp_path, {}) == link

    def test_a_missing_compiler_gives_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert resolve_compiler("no-such-elf-gcc", tmp_path, {}) is None


class TestProxyTarget:
    def test_an_ilp32_target_gets_i386_with_an_embedded_data_model(self) -> None:
        assert proxy_target(_xtensa_info()) == ("i386-unknown-elf", ["-malign-double", "-mlong-double-64"])

    def test_an_lp64_target_gets_x86_64(self) -> None:
        macros = (("__SIZEOF_INT__", "4"), ("__SIZEOF_POINTER__", "8"), ("__SIZEOF_LONG__", "8"),
                  ("__SIZEOF_LONG_DOUBLE__", "16"))
        info = DriverInfo(triple="foo64-elf", include_dirs=(), macros=macros)
        assert proxy_target(info) == ("x86_64-unknown-elf", ["-mlong-double-128"])

    def test_a_16_bit_int_has_no_proxy(self) -> None:
        info = DriverInfo(triple="tiny-elf", include_dirs=(), macros=(("__SIZEOF_INT__", "2"),))
        assert proxy_target(info) is None


class TestLibclangArgs:
    def test_a_target_without_a_backend_gets_the_proxy_and_the_real_macros(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: False)
        args = libclang_args(_xtensa_info(), ["-mlongcalls", "-DUNIT=1", "-I/proj"])

        assert args[:3] == ["--target=i386-unknown-elf", "-nostdinc", "-undef"]
        assert "-ferror-limit=0" in args, "a fatal error at 20 errors drops the rest of the AST"
        assert "-fshort-wchar" in args and "-funsigned-char" in args
        assert "-D__XTENSA__=1" in args and "-D__INT32_C(c)=c" in args
        assert "-mlongcalls" not in args, "the -m flags of the real target do not fit the proxy"
        # The unit flags come after the driver macros, thus a unit -D wins.
        assert args.index("-D__XTENSA__=1") < args.index("-DUNIT=1")
        # The system directories come last, in the driver order.
        assert args[-6:] == ["-isystem", "/tc/c++", "-isystem", "/tc/gcc", "-isystem", "/tc/libc"]

    def test_a_supported_target_keeps_its_triple_and_its_m_flags(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        info = DriverInfo(triple="arm-none-eabi", include_dirs=("/tc/inc",), macros=(("__GNUC__", "12"),))
        args = libclang_args(info, ["-mcpu=cortex-m4"])
        assert args[0] == "--target=arm-none-eabi"
        assert "-mcpu=cortex-m4" in args
        assert "-D__GNUC__=12" in args, "clang reports __GNUC__ 4; the real GCC version must win"

    def test_sized_deallocation_follows_the_gcc_dialect(self, monkeypatch) -> None:
        """libstdc++ of GCC 13 calls the sized delete; clang 18 needs the flag for it."""
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        cxx = DriverInfo(triple="arm-none-eabi", include_dirs=(), macros=(("__cpp_sized_deallocation", "201309L"),))
        c = DriverInfo(triple="arm-none-eabi", include_dirs=(), macros=())
        assert "-fsized-deallocation" in libclang_args(cxx, [])
        assert "-fsized-deallocation" not in libclang_args(c, [])

    def test_no_proxy_parses_for_the_host_with_the_driver_headers(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: False)
        info = DriverInfo(triple="tiny-elf", include_dirs=("/tc/inc",), macros=(("__SIZEOF_INT__", "2"),))
        args = libclang_args(info, [])
        assert not any(a.startswith("--target") for a in args)
        assert args[-2:] == ["-isystem", "/tc/inc"]


class TestClangResourceDir:
    """The GCC internal headers (arm_acle.h of ARM GCC 12) do not parse in libclang.

    Measured on one STM32 build: 2574 errors from them, 0 with clang's own.
    """

    ARM_DIRS = (
        "/tc/arm-none-eabi/include/c++/12.3.1",
        "/tc/lib/gcc/arm-none-eabi/12.3.1/include",
        "/tc/lib/gcc/arm-none-eabi/12.3.1/include-fixed",
        "/tc/arm-none-eabi/include",
    )

    def _arm(self, macros=(("__ARM_ARCH", "7"),)) -> DriverInfo:
        return DriverInfo(triple="arm-none-eabi", include_dirs=self.ARM_DIRS, macros=macros)

    def test_the_gcc_internal_directory_is_replaced_in_place(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        monkeypatch.setattr(_driver_query, "clang_resource_include", lambda: "/clang/include")
        args = libclang_args(self._arm(), [])
        isystem = [a for i, a in enumerate(args) if i and args[i - 1] == "-isystem"]
        assert isystem == [
            "/tc/arm-none-eabi/include/c++/12.3.1",
            "/clang/include",
            "/tc/lib/gcc/arm-none-eabi/12.3.1/include-fixed",
            "/tc/arm-none-eabi/include",
        ]
        # The GCC directory stays, searched last: it holds stdfix.h and gcov.h, which clang has not.
        assert args[-2:] == ["-idirafter", "/tc/lib/gcc/arm-none-eabi/12.3.1/include"]

    def test_an_arm_target_gets_the_acle_macro_that_clangs_header_needs(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        monkeypatch.setattr(_driver_query, "clang_resource_include", lambda: "/clang/include")
        assert "-D__ARM_ACLE=200" in libclang_args(self._arm(), [])

    def test_a_gcc_acle_macro_is_not_overridden(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        monkeypatch.setattr(_driver_query, "clang_resource_include", lambda: "/clang/include")
        args = libclang_args(self._arm((("__ARM_ARCH", "8"), ("__ARM_ACLE", "201"))), [])
        assert "-D__ARM_ACLE=201" in args and "-D__ARM_ACLE=200" not in args

    def test_the_real_lookup_gives_the_headers_of_the_libclang_version(self) -> None:
        """The driver path uses the same lookup as `doctor`, and nothing else."""
        from fw_context_mcp.indexer._clang_resource import find_resource_include

        found = find_resource_include()
        expected = str(found.include) if found is not None else None
        assert _REAL_CLANG_RESOURCE_INCLUDE() == expected
        if expected is not None:
            assert (Path(expected) / "stddef.h").is_file()

    def test_without_clang_the_gcc_directories_stay(self, monkeypatch) -> None:
        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: True)
        args = libclang_args(self._arm(), [])
        assert "/tc/lib/gcc/arm-none-eabi/12.3.1/include" in args
        assert "-D__ARM_ACLE=200" not in args, "GCC's own arm_acle.h does not need it"


class TestQueryTheDriver:
    def test_the_stub_driver_answers(self, tmp_path: Path) -> None:
        driver, dirs = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
        info = query_gcc_driver(driver, "c", ())
        assert info is not None
        assert info.triple == "xtensa-esp32-elf"
        assert info.include_dirs == tuple(str(d.resolve()) for d in dirs)
        assert info.macro("__XTENSA__") == "1"

    def test_the_query_runs_in_the_compiler_directory_with_the_language_and_flags(self, tmp_path: Path) -> None:
        """The build directory belongs to the repository; the compiler directory to the allowlist."""
        driver, _ = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
        info = query_gcc_driver(driver, "cpp", ("-std=gnu++11", "-Os"))
        assert info is not None
        assert info.macro("__STUB_CWD__") == str(driver.parent.resolve())
        assert info.macro("__STUB_ARGS__") == "-std=gnu++11 -Os -x c++ -dM -E -"

    def test_a_clang_that_poses_as_gcc_is_not_used(self, tmp_path: Path) -> None:
        """Its directories belong to another resource dir than the parsing libclang."""
        driver, _ = _stub_driver(tmp_path, "cc", macros="#define __GNUC__ 4\n#define __clang__ 1\n")
        assert query_gcc_driver(driver, "c", ()) is None

    def test_a_failing_driver_gives_none(self, tmp_path: Path) -> None:
        driver, _ = _stub_driver(tmp_path, "broken-gcc", exit_code=3)
        assert query_gcc_driver(driver, "c", ()) is None

    def test_a_failure_is_not_asked_again_for_each_unit(self, tmp_path: Path, monkeypatch) -> None:
        """A clang installed as cc fails each query; 2000 units must not run 4000 queries."""
        driver, _ = _stub_driver(tmp_path, "cc", macros="#define __GNUC__ 4\n#define __clang__ 1\n")
        runs: list[Path] = []
        real_run = _driver_query._run_query
        monkeypatch.setattr(_driver_query, "_run_query", lambda *a: runs.append(a[0]) or real_run(*a))
        for _ in range(5):
            assert query_gcc_driver(driver, "c", ()) is None
        assert len(runs) == 1

    def test_a_failure_is_asked_again_after_a_while(self, tmp_path: Path, monkeypatch) -> None:
        """A timeout under load must not hold for the life of an MCP server."""
        driver, _ = _stub_driver(tmp_path, "flaky-gcc", exit_code=3)
        assert query_gcc_driver(driver, "c", ()) is None
        status = driver.stat()
        _stub_driver(tmp_path, "flaky-gcc")  # the same path, now working
        os.utime(driver, ns=(status.st_atime_ns, status.st_mtime_ns))  # the same identity
        assert query_gcc_driver(driver, "c", ()) is None, "inside the time limit the failure holds"
        monkeypatch.setattr(_driver_query, "_FAILURE_TTL_S", 0.0)
        assert query_gcc_driver(driver, "c", ()) is not None

    def test_a_changed_binary_is_asked_again(self, tmp_path: Path) -> None:
        """A toolchain update in a long MCP server process must not keep the old directories."""
        driver, _ = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
        first = query_gcc_driver(driver, "c", ())
        _stub_driver(tmp_path, "xtensa-esp32-elf-gcc", macros=XTENSA_MACROS + "#define __NEW__ 1\n")
        status = driver.stat()
        os.utime(driver, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
        second = query_gcc_driver(driver, "c", ())
        assert first is not None and second is not None
        assert first.macro("__NEW__") is None and second.macro("__NEW__") == "1"


def _cc(tmp_path: Path, compiler: str) -> Path:
    src = tmp_path / "proj" / "main.c"
    src.parent.mkdir(exist_ok=True)
    src.write_text("int main(void) { return 0; }\n")
    cc = tmp_path / "compile_commands.json"
    cc.write_text(json.dumps([{
        "directory": str(src.parent),
        "file": str(src),
        "arguments": [compiler, "-std=gnu99", "-mlongcalls", "-c", str(src), "-o", "main.o"],
    }]))
    return cc


class TestParseUsesTheDriver:
    def test_a_bare_driver_name_gets_the_toolchain_headers(self, tmp_path: Path, monkeypatch) -> None:
        from fw_context_mcp.indexer.compile_commands import parse

        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: False)
        driver, dirs = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
        monkeypatch.setenv("PATH", f"{driver.parent}:/usr/bin:/bin")
        unit = next(parse(_cc(tmp_path, "xtensa-esp32-elf-gcc"), [f"{tmp_path}/**"]))

        assert "--target=i386-unknown-elf" in unit.clang_args
        assert "-nostdinc" in unit.clang_args
        assert "-D__XTENSA__=1" in unit.clang_args
        isystem = [a for i, a in enumerate(unit.clang_args) if i and unit.clang_args[i - 1] == "-isystem"]
        assert isystem == [str(d.resolve()) for d in dirs]

    def test_no_driver_keeps_the_flags_of_the_build(self, tmp_path: Path, monkeypatch) -> None:
        from fw_context_mcp.indexer.compile_commands import parse

        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        unit = next(parse(_cc(tmp_path, "nowhere-elf-gcc"), [f"{tmp_path}/**"]))
        assert "-nostdinc" not in unit.clang_args
        assert "-std=gnu99" in unit.clang_args

    def test_the_config_hash_is_stable_with_the_driver_flags(self, tmp_path: Path, monkeypatch) -> None:
        """About 350 macros of the driver enter the hash; two runs must agree."""
        from fw_context_mcp.indexer.compile_commands import parse
        from fw_context_mcp.indexer.manifest import compute_config_hash

        monkeypatch.setattr(_driver_query, "libclang_supports", lambda triple: False)
        driver, _ = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
        cc = _cc(tmp_path, str(driver))
        allow = [f"{tmp_path}/**"]
        first = compute_config_hash(list(parse(cc, allow)), tmp_path / "proj", "pid")
        clear_query_cache()
        second = compute_config_hash(list(parse(cc, allow)), tmp_path / "proj", "pid")
        without = compute_config_hash(list(parse(cc, [])), tmp_path / "proj", "pid")
        assert first == second
        assert first != without, "the driver flags are a different build identity"


def test_the_macro_preprocessor_keeps_the_driver_flags() -> None:
    """macros.py runs clang -dM -E with the unit flags; it must not drop -nostdinc or -undef."""
    from fw_context_mcp.indexer.macros import _sanitize_flags

    cmd = ["clang", "-dM", "-E", "--target=i386-unknown-elf", "-nostdinc", "-undef", "-malign-double",
           "-fshort-wchar", "-funsigned-char", "-D__XTENSA__=1", "-isystem", "/tc/inc", "main.c"]
    kept = _sanitize_flags(cmd)
    for flag in ("-nostdinc", "-undef", "-malign-double", "-fshort-wchar", "-funsigned-char", "-D__XTENSA__=1"):
        assert flag in kept, flag


@pytest.mark.libclang
def test_libclang_takes_the_branch_of_the_real_target(tmp_path: Path, monkeypatch) -> None:
    """End to end: the regression that started this module.

    Measured on an ESP32 index: ``#if __XTENSA__`` read as false, because
    libclang parsed for the host.  With the driver flags it must read as true
    and no host header may enter.
    """
    from clang import cindex

    from fw_context_mcp.indexer.compile_commands import parse

    driver, _ = _stub_driver(tmp_path, "xtensa-esp32-elf-gcc")
    src = tmp_path / "proj" / "main.c"
    src.parent.mkdir()
    src.write_text(
        "#include <stdbool.h>\n"
        "#if __XTENSA__\nint on_target;\n#else\nint on_host;\n#endif\n"
        "_Static_assert(sizeof(long) == 4, \"ILP32\");\n"
        "_Static_assert(sizeof(long double) == 8, \"64-bit long double\");\n"
    )
    cc = tmp_path / "compile_commands.json"
    cc.write_text(json.dumps([{
        "directory": str(src.parent), "file": str(src),
        "arguments": [str(driver), "-std=gnu99", "-c", str(src)],
    }]))
    unit = next(parse(cc, [f"{tmp_path}/**"]))
    tu = cindex.Index.create().parse(str(unit.file), args=unit.clang_args)

    errors = [d.spelling for d in tu.diagnostics if d.severity >= cindex.Diagnostic.Error]
    names = {c.spelling for c in tu.cursor.get_children() if c.location.file and Path(str(c.location.file)) == src}
    hosts = [str(i.include) for i in tu.get_includes() if str(i.include).startswith("/usr/")]
    assert errors == []
    assert "on_target" in names and "on_host" not in names
    assert hosts == []
