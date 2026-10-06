"""The link inputs of a PlatformIO build, from `pio run -t envdump`.

The dumps in this file are SYNTHETIC.  They copy the structure of the real
output of an STM32 and an ESP32 project, and the paths are in `tmp_path`.
A real dump holds the paths of a user, which a public repository must not.

The shapes that the dumps copy, each one measured:

* One `Processing <env> (...)` line before each environment.
* A `pprint` of a dict: the first line starts with `{`, a long string is
  split into `('a' 'b')`, and most values are SCons objects.
* A shell-escaped path in `LINKFLAGS` and `LIBPATH`: `\\(` for `(`.
* `-Wl,--default-script` with the path in the NEXT token (STM32).
* `-T <name>` with no directory, found through `LIBPATH` (ESP32).
* `$BUILD_DIR` in `LIBPATH`, which the dump defines through two variables.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build_layout import BuildLayout
from fw_context_mcp.indexer.builders import _link_command as lc
from fw_context_mcp.indexer.builders import _platformio_link as pl
from fw_context_mcp.indexer.builders import link_record, linker_scripts
from fw_context_mcp.indexer.builders._linker import LinkRecord
from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem


@dataclass
class FakeUnit:
    """The part of a CompilationUnit that the environment lookup reads."""

    directory: Path
    raw_entry: dict | None = None
    clang_args: list[str] = field(default_factory=list)


def _env_block(env: str, entries: str, before: str = "") -> str:
    """Return the dump of one environment, as `pio run -t envdump` prints it."""
    return (
        f"Processing {env} (platform: fake; board: fake; framework: arduino)\n"
        "--------------------------------------------------------------------\n"
        "CONFIGURATION: https://docs.platformio.org/page/boards/fake.html\n"
        f"{before}"
        "{ 'AR': 'arm-none-eabi-gcc-ar',\n"
        "  'ARCOM': '$AR $ARFLAGS $TARGET $SOURCES',\n"
        "  'BUILDERS': {'Program': <SCons.Builder.CompositeBuilder object at 0x7f>},\n"
        f"{entries}"
        "  'LINKCOM': \"${TEMPFILE('$LINK -o $TARGET $LINKFLAGS $__RPATH $SOURCES \"\n"
        "             \"$_LIBDIRFLAGS $_LIBFLAGS', '$LINKCOMSTR')}\",\n"
        "  'LIBS': [[<SCons.Node.FS.File object at 0x55>]],\n"
        f"  'PIOENV': '{env}',\n"
        "  'ZZ_LAST': 'x'}\n"
        "==================== [SUCCESS] Took 0.38 seconds ====================\n"
    )


def _escaped(path: Path) -> str:
    """Return *path* with `(` and `)` escaped for the shell, as the dump holds them."""
    return str(path).replace("(", "\\\\(").replace(")", "\\\\)")


def _stm32_block(root: Path, env: str = "nucleo") -> str:
    variant = root / "pkg" / "variants" / "L4(C-E)T"
    return _env_block(env, (
        "  'BUILD_DIR': '$PROJECT_BUILD_DIR/$PIOENV',\n"
        f"  'LIBPATH': ['$BUILD_DIR', '{_escaped(variant)}'],\n"
        "  'LINKFLAGS': [ '-T',\n"
        f"                 '{root}/pkg/system/ldscript.ld',\n"
        "                 '-mcpu=cortex-m4',\n"
        "                 '-Wl,--gc-sections,--relax',\n"
        "                 '-Wl,--defsym=LD_MAX_SIZE=1048576',\n"
        "                 '-Wl,--defsym=LD_MAX_DATA_SIZE=98304',\n"
        "                 '-Wl,-Map=\"${BUILD_DIR}/${PROGNAME}.map\"',\n"
        "                 '-Wl,--defsym=LD_FLASH_OFFSET=0x0',\n"
        "                 '-Wl,--default-script',\n"
        f"                 '{_escaped(variant)}/ldscript.ld'],\n"
        f"  'PROJECT_BUILD_DIR': '{root}/.pio/build',\n"
        f"  'PROJECT_DIR': '{root}',\n"
    ))


def _stm32_files(root: Path) -> tuple[Path, Path]:
    """Write the two scripts: an INSERT script and the variant script."""
    system = root / "pkg" / "system" / "ldscript.ld"
    variant = root / "pkg" / "variants" / "L4(C-E)T" / "ldscript.ld"
    system.parent.mkdir(parents=True, exist_ok=True)
    variant.parent.mkdir(parents=True, exist_ok=True)
    system.write_text("SECTIONS { .noinit (NOLOAD) : { *(.noinit) } } INSERT AFTER .bss;\n", encoding="utf-8")
    variant.write_text("MEMORY { RAM : ORIGIN = 0, LENGTH = LD_MAX_DATA_SIZE }\n", encoding="utf-8")
    return system, variant


def _esp32_block(root: Path) -> str:
    return _env_block("esp32dev", (
        "  'BUILD_DIR': '$PROJECT_BUILD_DIR/$PIOENV',\n"
        "  'LIBPATH': [ '$BUILD_DIR',\n"
        f"               '{root}/sdk/lib',\n"
        f"               '{root}/sdk/ld'],\n"
        "  'LINKFLAGS': [ '-mlongcalls',\n"
        "                 '-T',\n"
        "                 'memory.ld',\n"
        "                 '-T',\n"
        "                 'sections.ld',\n"
        "                 '-T',\n"
        "                 'rom.ld'],\n"
        f"  'PROJECT_BUILD_DIR': '{root}/.pio/build',\n"
        f"  'PROJECT_DIR': '{root}',\n"
    ))


def _esp32_files(root: Path) -> None:
    (root / "sdk" / "ld").mkdir(parents=True, exist_ok=True)
    for name in ("memory.ld", "sections.ld", "rom.ld"):
        (root / "sdk" / "ld" / name).write_text("", encoding="utf-8")


# ── envdump ────────────────────────────────────────────────────────────────


class TestParseEnvdump:
    def test_one_environment(self, tmp_path):
        envs = pl.parse_envdump(_stm32_block(tmp_path))
        assert list(envs) == ["nucleo"]
        link = envs["nucleo"]
        assert link.linkflags[0] == "-T"
        assert link.cwd == str(tmp_path)
        assert link.build_dir == f"{tmp_path}/.pio/build/nucleo"

    def test_two_environments(self, tmp_path):
        text = _stm32_block(tmp_path, "one") + _stm32_block(tmp_path, "two")
        envs = pl.parse_envdump(text)
        assert sorted(envs) == ["one", "two"]
        assert envs["two"].build_dir.endswith("/two")

    def test_libpath_is_expanded(self, tmp_path):
        link = pl.parse_envdump(_esp32_block(tmp_path))["esp32dev"]
        assert link.libpath == [
            f"{tmp_path}/.pio/build/esp32dev", f"{tmp_path}/sdk/lib", f"{tmp_path}/sdk/ld",
        ]

    def test_an_unexpandable_libpath_entry_stays_as_none(self, tmp_path):
        text = _env_block("x", "  'LIBPATH': ['$NOPE/ld', '/sdk/ld'],\n  'LINKFLAGS': ['-T', 'a.ld'],\n")
        assert pl.parse_envdump(text)["x"].libpath == [None, "/sdk/ld"]

    def test_an_unexpandable_flag_stays_in_its_place(self, tmp_path):
        # `${PROGNAME}` is not in the dump.  The token becomes None and does
        # not move the tokens after it.  It names no link input, thus the
        # link stays readable.
        link = pl.parse_envdump(_stm32_block(tmp_path))["nucleo"]
        assert None in link.linkflags
        index = link.linkflags.index(None)
        assert link.linkflags[index + 2] == "-Wl,--default-script"
        assert link.unknown == ""

    # An environment whose link cannot be read stays in the result with a
    # reason.  If it disappeared, `select_env` could give the one environment
    # that is left to units of the lost one.

    def test_linkflags_that_are_an_object_give_an_unknown_environment(self, tmp_path):
        text = _env_block("x", "  'LINKFLAGS': <SCons.Util.CLVar object at 0x1>,\n")
        assert "LINKFLAGS" in pl.parse_envdump(text)["x"].unknown

    def test_no_linkflags_gives_an_unknown_environment(self, tmp_path):
        # SCons always defines LINKFLAGS, thus a missing one is not empty.
        assert "LINKFLAGS" in pl.parse_envdump(_env_block("x", ""))["x"].unknown

    def test_a_libpath_that_is_an_object_gives_an_unknown_environment(self, tmp_path):
        text = _env_block("x", "  'LIBPATH': <SCons.Node.FS.Dir object at 0x1>,\n  'LINKFLAGS': ['-T', 'a.ld'],\n")
        assert "LIBPATH" in pl.parse_envdump(text)["x"].unknown

    def test_no_libpath_is_an_empty_list(self, tmp_path):
        link = pl.parse_envdump(_env_block("x", "  'LINKFLAGS': ['-T', 'a.ld'],\n"))["x"]
        assert (link.libpath, link.unknown) == ([], "")

    def test_a_string_value_is_split_like_a_clvar(self, tmp_path):
        text = _env_block("x", "  'LINKFLAGS': '-T a.ld -Os',\n")
        assert pl.parse_envdump(text)["x"].linkflags == ["-T", "a.ld", "-Os"]

    def test_no_dict_gives_an_unknown_environment(self):
        envs = pl.parse_envdump("Processing x (a)\nError: no platform\n")
        assert list(envs) == ["x"]
        assert envs["x"].unknown

    def test_no_header_gives_nothing(self, tmp_path):
        assert pl.parse_envdump("{ 'LINKFLAGS': ['-T', 'a.ld'], 'PIOENV': 'x'}\n") == {}

    def test_colour_sequences_are_removed(self, tmp_path):
        # Measured: with PLATFORMIO_FORCE_ANSI=true each line starts with
        # `\x1b[0m`, and no line starts with `{`.
        text = "".join(f"\x1b[0m{line}\n" for line in _stm32_block(tmp_path).splitlines())
        assert list(pl.parse_envdump(text)) == ["nucleo"]

    def test_a_dict_that_an_extra_script_prints_first_is_not_the_dump(self, tmp_path):
        # A pre: extra_script prints a dict before the platform adds its
        # link options.  The dump of the environment is the last dict with
        # this PIOENV.
        before = "{ 'LINKFLAGS': [], 'PIOENV': 'nucleo'}\n{ 'OTHER': 1}\n"
        text = _env_block("nucleo", "  'LINKFLAGS': ['-T', 'real.ld'],\n", before=before)
        assert pl.parse_envdump(text)["nucleo"].linkflags == ["-T", "real.ld"]

    def test_a_split_string_below_the_top_level(self, tmp_path):
        # pprint splits a long string into two adjacent literals, and below
        # the top level it writes no parentheses.  A path with a space.
        text = _env_block("x", (
            "  'LINKFLAGS': ['-T', 'a.ld'],\n"
            "  'PROJECT_BUILD_DIR': '/home/u/My Firmware '\n"
            "                       'Projects/.pio/build',\n"
            "  'BUILD_DIR': '$PROJECT_BUILD_DIR/$PIOENV',\n"
        ))
        assert pl.parse_envdump(text)["x"].build_dir == "/home/u/My Firmware Projects/.pio/build/x"

    def test_an_int_variable_expands_as_its_decimal_text(self, tmp_path):
        # The PlatformIO core defines UNIX_TIME=int(time()), and the Teensy
        # platform puts `--defsym=__rtc_localtime=$UNIX_TIME` in LINKFLAGS.
        # SCons gives the decimal text.  A refusal gave every Teensy
        # project no memory map.
        text = _env_block("x", (
            "  'LINKFLAGS': ['-Wl,--defsym=__rtc_localtime=$UNIX_TIME', '-T', 'a.ld'],\n"
            "  'UNIX_TIME': 1700000000,\n"
            "  'FLAG': True,\n"
        ))
        link = pl.parse_envdump(text)["x"]
        assert link.linkflags[0] == "-Wl,--defsym=__rtc_localtime=1700000000"
        assert link.unknown == ""

    def test_a_bool_variable_does_not_expand(self, tmp_path):
        # SCons gives `True`, and no link option takes that text.
        text = _env_block("x", "  'LINKFLAGS': ['-Wl,-Map=$FLAG.map'],\n  'FLAG': True,\n")
        assert pl.parse_envdump(text)["x"].linkflags == [None]

    def test_a_value_with_white_space_is_several_arguments(self, tmp_path):
        # SCons splits the value of a variable in LINKFLAGS at white space,
        # thus `$EXTRA` gives two arguments here, and one is a script.  One
        # token would hide `-Tcustom.ld`, and the answer would be "no script".
        text = _env_block("x", (
            "  'EXTRA': '-mcpu=cortex-m4 -Tcustom.ld',\n"
            "  'LINKFLAGS': ['-mthumb', '$EXTRA'],\n"
        ))
        link = pl.parse_envdump(text)["x"]
        assert link.linkflags == ["-mthumb", None]
        assert link.unknown
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_a_libpath_value_with_white_space_stays_one_directory(self, tmp_path):
        # SCons makes a directory node of each LIBPATH entry and splits
        # nothing there.
        text = _env_block("x", (
            "  'LINKFLAGS': ['-T', 'a.ld'],\n"
            "  'PROJECT_BUILD_DIR': '/home/u/My Firmware/.pio/build',\n"
            "  'BUILD_DIR': '$PROJECT_BUILD_DIR/$PIOENV',\n"
            "  'LIBPATH': ['$BUILD_DIR'],\n"
        ))
        assert pl.parse_envdump(text)["x"].libpath == ["/home/u/My Firmware/.pio/build/x"]

    def test_a_dict_with_a_list_key_does_not_raise(self, tmp_path):
        text = _env_block("x", "  'ODD': {[1]: 2},\n  'LINKFLAGS': ['-T', 'a.ld'],\n")
        assert pl.parse_envdump(text)["x"].linkflags == ["-T", "a.ld"]


class TestExpand:
    VARIABLES = {"A": "$B/x", "B": "/root", "LIST": "", "LOOP": "$LOOP"}

    def test_a_chain(self):
        assert pl.expand("$A/y", self.VARIABLES) == "/root/x/y"

    def test_the_brace_form(self):
        assert pl.expand("${B}/z", self.VARIABLES) == "/root/z"

    def test_a_missing_variable(self):
        assert pl.expand("$NOPE/z", self.VARIABLES) is None

    def test_a_cycle(self):
        assert pl.expand("$LOOP", self.VARIABLES) is None

    def test_a_function_call(self):
        assert pl.expand("${TEMPFILE('$B')}", self.VARIABLES) is None

    def test_a_double_dollar_is_a_literal(self):
        assert pl.expand("a$$b", self.VARIABLES) == "a$b"

    def test_no_variable(self):
        assert pl.expand("-Os", {}) == "-Os"

    def test_one_word_refuses_a_value_with_white_space(self):
        variables = {"A": "x y", "B": "$A", "C": "z"}
        assert pl.expand("-p$A", variables) == "-px y"
        assert pl.expand("-p$A", variables, one_word=True) is None
        assert pl.expand("-p$B", variables, one_word=True) is None
        assert pl.expand("-p$C", variables, one_word=True) == "-pz"

    def test_one_word_keeps_a_quoted_value(self):
        # SCons splits `-T"/a b/x.ld"` into two words, and the shell joins
        # them again: ld gets one argument.  Arduino-ESP32 writes
        # `-Wl,-Map="${BUILD_DIR}/${PROGNAME}.map"` for a path with a space.
        variables = {"D": "/a b", "Q": '"/a b/x.ld"'}
        assert pl.expand('-T"$D/x.ld"', variables, one_word=True) == '-T"/a b/x.ld"'
        assert pl.expand("-T$Q", variables, one_word=True) == '-T"/a b/x.ld"'
        assert pl.expand('-T"$D', variables, one_word=True) is None


# ── Link options ───────────────────────────────────────────────────────────


class TestScriptOptions:
    @pytest.mark.parametrize(("tokens", "expected"), [
        (["-T", "a.ld"], [("T", "a.ld")]),
        (["-Ta.ld"], [("T", "a.ld")]),
        (["-Wl,-T,a.ld"], [("T", "a.ld")]),
        (["-Wl,--script=a.ld"], [("T", "a.ld")]),
        (["-Wl,--script,a.ld"], [("T", "a.ld")]),
        (["-Wl,-dT,a.ld"], [("dT", "a.ld")]),
        (["-Wl,--default-script=a.ld"], [("dT", "a.ld")]),
        (["-Wl,--default-script,a.ld"], [("dT", "a.ld")]),
        (["-Wl,--default-script", "a.ld"], [("dT", "a.ld")]),
        (["-Wl,-Ta.ld"], [("T", "a.ld")]),
    ])
    def test_each_form(self, tokens, expected):
        assert lc.script_options(tokens) == expected

    def test_the_order_is_kept(self):
        tokens = ["-T", "one.ld", "-Os", "-Wl,--default-script", "two.ld", "-T", "three.ld"]
        assert lc.scripts_from_flags(tokens) == ["one.ld", "two.ld", "three.ld"]

    def test_other_flags_give_nothing(self):
        tokens = ["-Target=x", "-Wl,--gc-sections,--relax", "-mthumb", "-Wl,-Map=a.map"]
        assert lc.scripts_from_flags(tokens) == []

    @pytest.mark.parametrize("token", [
        "-Ttext=0x8000", "-Tdata=0x2000", "-Tbss", "-Ttext-segment=0x400000",
        "-Wl,-Tbss,0x1", "-Wl,-Ttext=0x8000", "-Wl,-Trodata-segment=0x1",
    ])
    def test_a_section_address_is_not_a_script(self, token):
        assert lc.scripts_from_flags([token]) == []

    def test_a_script_whose_name_starts_like_a_section(self):
        assert lc.scripts_from_flags(["-Ttext.ld"]) == ["text.ld"]

    def test_a_value_after_the_option_is_not_a_second_option(self):
        assert lc.scripts_from_flags(["-T", "-Tweird.ld"]) == ["-Tweird.ld"]

    def test_an_option_at_the_end_gives_nothing(self):
        assert lc.scripts_from_flags(["-T"]) == []
        assert lc.scripts_from_flags(["-Wl,--default-script"]) == []


class TestLostLinkInput:
    """A token that did not expand makes the link unknown when it can name a link input.

    Before this, such a token was dropped, and the answer was another link:
    `-T $LDSCRIPT` gave the default script as the memory map, and a lone
    `-Wl,-T,$X` gave "no script", which deleted a correct stored map.
    """

    @staticmethod
    def _unknown(raw: list[str]) -> str:
        return lc._lost_link_input(raw, [None if "$" in token else token for token in raw])

    @pytest.mark.parametrize("raw", [
        ["-T", "$LDSCRIPT"],
        ["-L", "$DIR"],
        ["-Xlinker", "$X"],
        ["-Wl,--default-script", "$X"],
        ["-Wl,-dT", "$X"],
        ["-Wl,--defsym", "$X"],
        ["-Wl,-L", "$X"],
        ["-Wl,--library-path", "$X"],
        ["-T$X"],
        ["-L$X"],
        ["-$X"],
        ["$EXTRA"],
        ["foo$X.ld"],
        ["@$RSP"],
        ["-Wl,-T,$X"],
        ["-Wl,--script=$X"],
        ["-Wl,-L$X"],
        ["-Wl,--defsym=A=$X"],
        ["-Wl,$X"],
        ["-Wl,-$X"],
        ["-Wl,--$X"],
        # The whole token is lost, also the part with no variable.
        ["-Wl,-T,a.ld,-Map=$X"],
        ["-Wl,--gc-sections,-L/x,-Map=$X"],
        # Only `=` makes the text before the variable a section address.
        ["-Ttext$X"],
        ["-Wl,-Ttext$X"],
        # A specs file and `-l:<file>` can add a script.
        ["--specs=$SPECS"],
        ["-specs=$SPECS"],
        ["-l:$X"],
        ["-l", "$X"],
        ["-Wl,-l:$X"],
        ["-Wl,--library=:$X"],
    ])
    def test_a_token_that_can_be_a_link_input(self, raw):
        assert self._unknown(raw)

    @pytest.mark.parametrize("raw", [
        ["-Wl,-Map=${BUILD_DIR}/${PROGNAME}.map"],
        ['-Wl,-Map="$X"'],
        ["-Wl,--gc-sections,-Map=$X"],
        ["-mcpu=$CPU"],
        ["-Ttext=$ADDR"],
        ["-Wl,-Ttext=$ADDR"],
        ["-T", "a.ld", "-Os"],
        [],
    ])
    def test_a_token_that_cannot(self, raw):
        assert self._unknown(raw) == ""


class TestLibraryDirs:
    def test_driver_and_wl_forms(self):
        tokens = ["-L", "/a", "-L/b", "-Wl,-L,/c", "-Wl,-L/d", "-Wl,--library-path=/e", None]
        assert lc.library_dirs_from_flags(tokens) == (["/a", "/b"], ["/c", "/d", "/e"])

    @pytest.mark.parametrize("tokens", [["-L=/x"], ["-L", "=/x"], ["-Wl,-L=/x"], ["-Wl,--library-path==/x"],
                                        ["-L$SYSROOT/x"], ["-Wl,-L,$SYSROOT/x"]])
    def test_a_sysroot_directory_is_not_known(self, tokens):
        # ld replaces `=` and `$SYSROOT` with the sysroot, which the dump
        # does not state.  The search stops at that directory.
        driver, passed = lc.library_dirs_from_flags(tokens)
        assert driver + passed == [None]

    def test_a_directory_in_the_next_part_or_token(self):
        tokens = ["-Wl,--library-path,/a", "-Wl,-L", "/b", "-Wl,--library-path", "/c", "-Wl,-L"]
        assert lc.library_dirs_from_flags(tokens) == ([], ["/a", "/b", "/c", None])

    def test_the_search_order(self, tmp_path):
        link = pl.EnvLink(linkflags=["-Wl,-L/late", "-L/early"], libpath=["/middle"])
        assert pl.search_dirs(link) == [Path("/early"), Path("/middle"), Path("/late")]


class TestDefsymsFromFlags:
    def test_the_equals_form(self):
        tokens = ["-Wl,--defsym=LD_MAX_SIZE=1048576", "-Wl,--defsym=OFFSET=0x0"]
        assert lc.defsyms_from_flags(tokens) == {"LD_MAX_SIZE": "1048576", "OFFSET": "0x0"}

    def test_the_comma_form_and_a_joined_token(self):
        tokens = ["-Wl,--defsym,A=1,--defsym=B=A+1"]
        assert lc.defsyms_from_flags(tokens) == {"A": "1", "B": "A+1"}

    def test_the_next_token_form(self):
        assert lc.defsyms_from_flags(["-Wl,--defsym", "A=1", "-Os"]) == {"A": "1"}

    def test_two_different_expressions_give_no_expression(self):
        # ld evaluates in order: with `A=1, B=A, A=2` it gives B=1.  A dict
        # keeps the first position of A and would give B=2.  The name stays
        # with no expression, because ld still defines it: a PROVIDE of A in
        # a script defines nothing.
        tokens = ["-Wl,--defsym=A=1", "-Wl,--defsym=B=A", "-Wl,--defsym=A=2"]
        assert lc.defsyms_from_flags(tokens) == {"A": None, "B": "A"}

    def test_the_same_expression_two_times_is_one_value(self):
        # env.Append keeps a duplicate that the core and build_flags both add.
        tokens = ["-Wl,--defsym=A=1", "-Wl,--defsym=A=1"]
        assert lc.defsyms_from_flags(tokens) == {"A": "1"}

    def test_a_broken_definition_is_dropped(self):
        tokens = ["-Wl,--defsym=A", "-Wl,--defsym==1", "-Wl,--defsym=B=", None, "-Os"]
        assert lc.defsyms_from_flags(tokens) == {}


class TestLdPath:
    def test_an_escaped_parenthesis(self):
        assert lc.ld_path(r"/p/L475R\(C-E-G\)T/ldscript.ld", posix=True) == "/p/L475R(C-E-G)T/ldscript.ld"

    def test_double_quotes(self):
        assert lc.ld_path('"/p/a b/x.ld"', posix=True) == "/p/a b/x.ld"

    def test_a_space_is_quoted_as_scons_does(self):
        # SCons puts double quotes around an argument with a space.
        assert lc.ld_path("/home/u/My Firmware/.pio/build/x", posix=True) == "/home/u/My Firmware/.pio/build/x"

    def test_an_apostrophe_with_a_space(self):
        assert lc.ld_path("/home/u/Tom's work/x.ld", posix=True) == "/home/u/Tom's work/x.ld"

    def test_an_open_quote_is_refused(self):
        assert lc.ld_path('"/p/x.ld', posix=True) is None

    def test_windows_keeps_the_backslash(self):
        assert lc.ld_path(r'"C:\p\x.ld"', posix=False) == r"C:\p\x.ld"


class TestResolve:
    def test_the_current_directory_wins(self, tmp_path):
        (tmp_path / "cwd").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "cwd" / "a.ld").write_text("", encoding="utf-8")
        (tmp_path / "lib" / "a.ld").write_text("", encoding="utf-8")
        found = lc.resolve_script("a.ld", tmp_path / "cwd", [tmp_path / "lib"])
        assert found == (tmp_path / "cwd" / "a.ld").resolve()

    def test_the_directories_in_their_order(self, tmp_path):
        for name in ("one", "two"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "a.ld").write_text("", encoding="utf-8")
        found = lc.resolve_script("a.ld", tmp_path / "none", [tmp_path / "one", tmp_path / "two"])
        assert found == (tmp_path / "one" / "a.ld").resolve()

    def test_an_unknown_directory_stops_the_search(self, tmp_path):
        (tmp_path / "later").mkdir()
        (tmp_path / "later" / "a.ld").write_text("", encoding="utf-8")
        assert lc.resolve_script("a.ld", tmp_path / "none", [None, tmp_path / "later"]) is None

    def test_a_relative_directory_is_relative_to_the_link(self, tmp_path, monkeypatch):
        # ld runs in the directory of the link, and `-Lrel` names a
        # directory there.  The indexer can run in another directory, and
        # there the same name can hold another script.
        for name in ("proj/rel", "elsewhere/rel", "sdk"):
            (tmp_path / name).mkdir(parents=True)
            (tmp_path / name / "x.ld").write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path / "elsewhere")
        found = lc.resolve_script("x.ld", tmp_path / "proj", [Path("rel"), tmp_path / "sdk"])
        assert found == (tmp_path / "proj" / "rel" / "x.ld").resolve()

    def test_a_relative_directory_of_the_link_that_is_missing(self, tmp_path, monkeypatch):
        (tmp_path / "elsewhere" / "rel").mkdir(parents=True)
        (tmp_path / "elsewhere" / "rel" / "x.ld").write_text("", encoding="utf-8")
        (tmp_path / "sdk").mkdir()
        (tmp_path / "sdk" / "x.ld").write_text("", encoding="utf-8")
        monkeypatch.chdir(tmp_path / "elsewhere")
        found = lc.resolve_script("x.ld", tmp_path / "proj", [Path("rel"), tmp_path / "sdk"])
        assert found == (tmp_path / "sdk" / "x.ld").resolve()

    def test_a_missing_file(self, tmp_path):
        assert lc.resolve_script("a.ld", tmp_path, [tmp_path]) is None
        assert lc.resolve_script(str(tmp_path / "b.ld"), tmp_path, []) is None

    def test_the_stm32_shape(self, tmp_path):
        system, variant = _stm32_files(tmp_path)
        link = pl.parse_envdump(_stm32_block(tmp_path))["nucleo"]
        assert pl.resolve_scripts(link, tmp_path) == [system.resolve(), variant.resolve()]

    def test_the_esp32_shape(self, tmp_path):
        _esp32_files(tmp_path)
        link = pl.parse_envdump(_esp32_block(tmp_path))["esp32dev"]
        assert [p.name for p in pl.resolve_scripts(link, tmp_path) or []] == [
            "memory.ld", "sections.ld", "rom.ld",
        ]

    def test_an_escaped_libpath_directory(self, tmp_path):
        directory = tmp_path / "L4(C-E)T"
        directory.mkdir()
        (directory / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(
            linkflags=["-T", "a.ld"], libpath=[str(directory).replace("(", "\\(").replace(")", "\\)")],
            cwd=str(tmp_path / "none"),
        )
        assert pl.resolve_scripts(link, tmp_path) == [(directory / "a.ld").resolve()]

    def test_an_unexpandable_libpath_entry_stops_the_search(self, tmp_path):
        # ld looks in that directory first, and this module cannot.  A file
        # of the same name in a later directory can be another file.
        (tmp_path / "later").mkdir()
        (tmp_path / "later" / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld"], libpath=[None, str(tmp_path / "later")],
                          cwd=str(tmp_path / "none"))
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_a_driver_l_in_linkflags_comes_before_libpath(self, tmp_path):
        for name in ("proj", "sdk"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "memory.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(
            linkflags=[f"-L{tmp_path}/proj", "-T", "memory.ld"],
            libpath=[str(tmp_path / "sdk")], cwd=str(tmp_path / "none"),
        )
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "proj" / "memory.ld").resolve()]

    def test_an_unexpanded_t_script_does_not_give_the_default_script(self, tmp_path):
        # `-T $LDSCRIPT` replaces the default script.  Without it, the
        # answer would be the memory map of `variant.ld`, a wrong one.
        (tmp_path / "variant.ld").write_text("MEMORY { RAM : ORIGIN = 0, LENGTH = 8 }\n", encoding="utf-8")
        text = _env_block("x", (
            "  'LINKFLAGS': ['-T', '$LDSCRIPT', '-Wl,--default-script', 'variant.ld'],\n"
            f"  'PROJECT_DIR': '{tmp_path}',\n"
        ))
        link = pl.parse_envdump(text)["x"]
        assert link.unknown
        assert pl.resolve_scripts(link, tmp_path) is None

    @pytest.mark.parametrize("flags", ["['-T', '$LDSCRIPT']", "['-Wl,-T,$LDSCRIPT']"])
    def test_an_unexpanded_only_script_is_not_a_link_with_no_script(self, tmp_path, flags):
        # "No script" deletes the stored map.  The link names a script, and
        # the answer is "not known", which keeps the map.
        link = pl.parse_envdump(_env_block("x", f"  'LINKFLAGS': {flags},\n"))["x"]
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_an_unexpanded_l_directory_stops_the_answer(self, tmp_path):
        # ld would look in that directory first, and it can hold a script
        # of the same name.
        (tmp_path / "sdk").mkdir()
        (tmp_path / "sdk" / "x.ld").write_text("", encoding="utf-8")
        text = _env_block("x", (
            f"  'LIBPATH': ['{tmp_path}/sdk'],\n"
            "  'LINKFLAGS': ['-L$PRIVATE', '-T', 'x.ld'],\n"
            f"  'PROJECT_DIR': '{tmp_path}/none',\n"
        ))
        assert pl.resolve_scripts(pl.parse_envdump(text)["x"], tmp_path) is None

    @pytest.mark.parametrize("flags", [
        ["-Xlinker", "-T", "-Xlinker", "x.ld"],
        ["-Xlinker", "--script=x.ld"],
        ["-Xlinker", "-Lsdk"],
        ["-Xlinker", "--defsym=A=1"],
        ["-Xlinker", "-l:x.ld"],
    ])
    def test_a_link_input_through_xlinker_is_unknown(self, tmp_path, flags):
        # The module does not read -Xlinker.  A script, a directory or a
        # value that it passes would be lost, thus the link is unknown.
        (tmp_path / "x.ld").write_text("", encoding="utf-8")
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", *flags], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_a_harmless_xlinker_value(self, tmp_path):
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "-Xlinker", "-Map=out.map", "-Xlinker", "--gc-sections"],
                          cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "a.ld").resolve()]

    @pytest.mark.parametrize("specs", ["--specs=nano.specs", "-specs=nosys.specs"])
    def test_the_newlib_specs_add_no_link_input(self, tmp_path, specs):
        # Measured: 56 copies of nano.specs and nosys.specs in the PlatformIO
        # toolchains name only libraries.  STM32 projects pass both.
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", specs], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "a.ld").resolve()]

    def test_another_specs_file_that_cannot_be_found_is_unknown(self, tmp_path):
        # pid.specs and redboot.specs of the ARM toolchain add
        # `-T redboot.ld`, and gcc finds them in its own directories.
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "--specs=pid.specs"], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) is None

    @pytest.mark.parametrize(("text", "known"), [
        ("*link:\n%(old_link) -lc\n", True),
        ("*link:\n-T redboot.ld%s %(old_link)\n", False),
        ("*link:\n--defsym=_base=0x80000000 %(old_link)\n", False),
        ("*link:\n-L/opt/sdk %(old_link)\n", False),
    ])
    def test_a_specs_file_with_a_path_is_read(self, tmp_path, text, known):
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        (tmp_path / "board.specs").write_text(text, encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "--specs=./board.specs"], cwd=str(tmp_path))
        found = pl.resolve_scripts(link, tmp_path)
        assert (found == [(tmp_path / "a.ld").resolve()]) if known else (found is None)

    def test_an_implicit_script_through_l_colon(self, tmp_path):
        # ld reads a file that `-l:name` finds, and that is no object or
        # archive, as an implicit linker script.
        (tmp_path / "lib").mkdir()
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        (tmp_path / "lib" / "extra.ld").write_text("PROVIDE(_x = 1);\n", encoding="utf-8")
        (tmp_path / "lib" / "libfoo.a").write_bytes(b"!<arch>\n")
        link = pl.EnvLink(
            linkflags=["-T", "a.ld", "-l:libfoo.a", "-Wl,-l:extra.ld"],
            libpath=[str(tmp_path / "lib")], cwd=str(tmp_path),
        )
        assert pl.resolve_scripts(link, tmp_path) == [
            (tmp_path / "a.ld").resolve(), (tmp_path / "lib" / "extra.ld").resolve(),
        ]

    def test_an_l_colon_file_that_is_not_found_is_unknown(self, tmp_path):
        # ld also searches its own directories, which the dump does not state.
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "-l", ":gone.ld"], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_a_missing_script_makes_the_answer_unknown(self, tmp_path):
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "-T", "gone.ld"], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) is None

    def test_a_t_script_without_insert_replaces_the_default_script(self, tmp_path):
        # build_flags = -Wl,-T,custom.ld: ld links with custom.ld only, and
        # the ELF holds no symbol of the variant script.
        (tmp_path / "custom.ld").write_text("MEMORY { RAM : ORIGIN = 0, LENGTH = 4 }\n", encoding="utf-8")
        (tmp_path / "variant.ld").write_text("MEMORY { RAM : ORIGIN = 0, LENGTH = 8 }\n", encoding="utf-8")
        link = pl.EnvLink(
            linkflags=["-Wl,-T,custom.ld", "-Wl,--default-script", "variant.ld"], cwd=str(tmp_path),
        )
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "custom.ld").resolve()]

    def test_the_last_default_script_wins(self, tmp_path):
        for name in ("one.ld", "two.ld"):
            (tmp_path / name).write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-Wl,-dT,one.ld", "-Wl,-dT,two.ld"], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "two.ld").resolve()]

    def test_an_insert_in_a_comment_does_not_count(self, tmp_path):
        (tmp_path / "t.ld").write_text("/* INSERT AFTER .bss */\n", encoding="utf-8")
        (tmp_path / "d.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "t.ld", "-Wl,-dT,d.ld"], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "t.ld").resolve()]

    def test_a_script_named_twice_is_returned_once(self, tmp_path):
        (tmp_path / "a.ld").write_text("", encoding="utf-8")
        link = pl.EnvLink(linkflags=["-T", "a.ld", "-T", str(tmp_path / "a.ld")], cwd=str(tmp_path))
        assert pl.resolve_scripts(link, tmp_path) == [(tmp_path / "a.ld").resolve()]


# ── Sidecar and environment ────────────────────────────────────────────────


def _database(tmp_path: Path, content: str = "[]", name: str = "compile_commands.json") -> Path:
    path = BuildLayout(tmp_path).out_dir("") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _target(tmp_path: Path, variant: str = "") -> Path:
    return pl.sidecar_path(BuildLayout(tmp_path).variant_dir(variant))


class TestSidecar:
    def test_a_round_trip(self, tmp_path):
        database = _database(tmp_path)
        envs = pl.parse_envdump(_stm32_block(tmp_path))
        pl.record_link(_target(tmp_path), "", database, envs)
        assert pl.read_link(_target(tmp_path), "", database) == envs
        # The temporary file is gone.  The sidecar sits beside out/, which
        # PlatformIO can remove as a whole.
        assert {p.name for p in _target(tmp_path).parent.iterdir()} == {"out", pl.SIDECAR_NAME}

    def test_another_database_has_no_entry(self, tmp_path):
        database = _database(tmp_path)
        pl.record_link(_target(tmp_path), "", database, pl.parse_envdump(_stm32_block(tmp_path)))
        database.write_text('[{"file": "changed.c"}]', encoding="utf-8")
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_an_entry_of_an_unpublished_build_keeps_the_published_one(self, tmp_path):
        # A build stops after the sidecar write and before the database is
        # published.  The published database still finds its own entry.
        published = _database(tmp_path, '["published"]')
        pl.record_link(_target(tmp_path), "", published, pl.parse_envdump(_stm32_block(tmp_path, "old")))
        staging = _database(tmp_path, '["unpublished"]', name=".staging.json")
        pl.record_link(_target(tmp_path), "", staging, pl.parse_envdump(_stm32_block(tmp_path, "new")))
        assert list(pl.read_link(_target(tmp_path), "", published) or {}) == ["old"]

    def test_each_variant_has_its_own_entry(self, tmp_path):
        # Two variants with byte-identical databases and another link.
        database = _database(tmp_path)
        pl.record_link(_target(tmp_path), "a", database, pl.parse_envdump(_stm32_block(tmp_path, "env_a")))
        pl.record_link(_target(tmp_path), "b", database, pl.parse_envdump(_stm32_block(tmp_path, "env_b")))
        assert list(pl.read_link(_target(tmp_path), "a", database) or {}) == ["env_a"]
        assert list(pl.read_link(_target(tmp_path), "b", database) or {}) == ["env_b"]

    def test_a_failed_dump_removes_the_entry(self, tmp_path):
        # A change of the link alone keeps the hash of the database.
        database = _database(tmp_path)
        pl.record_link(_target(tmp_path), "", database, pl.parse_envdump(_stm32_block(tmp_path)))
        pl.record_link(_target(tmp_path), "", database, None)
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_a_write_that_fails_removes_the_file(self, tmp_path, monkeypatch):
        database = _database(tmp_path)
        pl.record_link(_target(tmp_path), "", database, pl.parse_envdump(_stm32_block(tmp_path)))

        def fail(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pl.os, "replace", fail)
        pl.record_link(_target(tmp_path), "", database, pl.parse_envdump(_stm32_block(tmp_path, "new")))
        assert not _target(tmp_path).exists()

    def test_a_temporary_file_of_a_stopped_build_goes(self, tmp_path):
        # SIGKILL stops a build between the write and the rename.  The name
        # holds the owner token, thus no later write reuses it.
        from fw_context_mcp.utils import owner_token

        tag = owner_token().partition("@")[2]
        target = _target(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        dead = target.with_name(f".{target.stem}.2147483646@{tag}{target.suffix}")
        foreign = target.with_name(f".{target.stem}.2147483646@another-host-1{target.suffix}")
        # The parent of this process runs: its file belongs to a build that
        # writes it now.
        live = target.with_name(f".{target.stem}.{os.getppid()}@{tag}{target.suffix}")
        for path in (dead, foreign, live):
            path.write_text("{}", encoding="utf-8")
        pl.record_link(target, "", _database(tmp_path), pl.parse_envdump(_stm32_block(tmp_path)))
        assert not dead.exists()
        # Another PID namespace can hold a live build, and this process
        # cannot see it.
        assert foreign.exists()
        assert live.exists()

    def test_a_missing_sidecar(self, tmp_path):
        database = _database(tmp_path)
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_a_missing_database(self, tmp_path):
        assert pl.read_link(_target(tmp_path), "", tmp_path / "none.json") is None

    def test_a_broken_sidecar(self, tmp_path):
        database = _database(tmp_path)
        _target(tmp_path).write_text("{not json", encoding="utf-8")
        assert pl.read_link(_target(tmp_path), "", database) is None
        # A later record replaces the broken file.
        pl.record_link(_target(tmp_path), "", database, pl.parse_envdump(_stm32_block(tmp_path)))
        assert pl.read_link(_target(tmp_path), "", database) is not None

    def test_another_format(self, tmp_path):
        database = _database(tmp_path)
        _target(tmp_path).write_text(json.dumps({
            "format": pl.SIDECAR_FORMAT + 1,
            "entries": [{"variant": "", "cc_sha256": pl.file_sha256(database), "envs": {}}],
        }), encoding="utf-8")
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_a_field_of_a_wrong_type_makes_the_entry_unknown(self, tmp_path):
        # Without the bad environment, `select_env` would give the good one
        # to units that belong to the bad one.
        database = _database(tmp_path)
        good = {"linkflags": ["-T", "a.ld"], "libpath": [None], "cwd": "", "build_dir": "", "unknown": ""}
        _target(tmp_path).write_text(json.dumps({
            "format": pl.SIDECAR_FORMAT,
            "entries": [{"variant": "", "cc_sha256": pl.file_sha256(database), "envs": {
                "good": good,
                "bad": {**good, "linkflags": "-T a.ld"},
            }}],
        }), encoding="utf-8")
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_an_entry_with_no_unknown_field_is_not_used(self, tmp_path):
        # A format 2 entry dropped a token that did not expand.
        database = _database(tmp_path)
        _target(tmp_path).write_text(json.dumps({
            "format": pl.SIDECAR_FORMAT,
            "entries": [{"variant": "", "cc_sha256": pl.file_sha256(database), "envs": {
                "x": {"linkflags": ["-T", None], "libpath": [], "cwd": "", "build_dir": ""},
            }}],
        }), encoding="utf-8")
        assert pl.read_link(_target(tmp_path), "", database) is None

    def test_an_unknown_environment_is_recorded(self, tmp_path):
        database = _database(tmp_path)
        envs = pl.parse_envdump(_stm32_block(tmp_path, "good") + "Processing bad (x)\nError\n")
        pl.record_link(_target(tmp_path), "", database, envs)
        read = pl.read_link(_target(tmp_path), "", database) or {}
        assert read == envs
        assert read["bad"].unknown and not read["good"].unknown

    def test_the_number_of_entries_is_limited(self, tmp_path):
        envs = pl.parse_envdump(_stm32_block(tmp_path))
        for index in range(pl._MAX_ENTRIES + 3):
            pl.record_link(_target(tmp_path), f"v{index}", _database(tmp_path, f'["{index}"]'), envs)
        payload = json.loads(_target(tmp_path).read_text(encoding="utf-8"))
        assert len(payload["entries"]) == pl._MAX_ENTRIES


class TestSelectEnv:
    def _envs(self, tmp_path):
        return {
            name: pl.EnvLink(build_dir=str(tmp_path / ".pio" / "build" / name))
            for name in ("l476", "f401")
        }

    def test_the_environment_of_the_object_files(self, tmp_path):
        # Measured with two environments: `compile_commands.json` holds the
        # last one only, and its units name its build directory.
        units = [
            FakeUnit(tmp_path, {"output": ".pio/build/f401/src/main.cpp.o"}),
            FakeUnit(tmp_path, {"arguments": ["gcc", "-o", ".pio/build/f401/a.o"]}),
        ]
        assert pl.select_env(self._envs(tmp_path), units, tmp_path) == "f401"

    def test_units_of_two_environments_give_none(self, tmp_path):
        units = [
            FakeUnit(tmp_path, {"output": ".pio/build/f401/a.o"}),
            FakeUnit(tmp_path, {"output": ".pio/build/l476/b.o"}),
        ]
        assert pl.select_env(self._envs(tmp_path), units, tmp_path) is None

    def test_one_object_file_outside_every_environment_gives_none(self, tmp_path):
        units = [
            FakeUnit(tmp_path, {"output": ".pio/build/f401/a.o"}),
            FakeUnit(tmp_path, {"output": "elsewhere/b.o"}),
        ]
        assert pl.select_env(self._envs(tmp_path), units, tmp_path) is None

    def test_no_units_and_one_environment(self, tmp_path):
        envs = {"only": pl.EnvLink(build_dir=str(tmp_path / "b"))}
        assert pl.select_env(envs, None, tmp_path) == "only"

    def test_no_units_and_two_environments(self, tmp_path):
        assert pl.select_env(self._envs(tmp_path), [], tmp_path) is None

    def test_no_units_and_an_environment_whose_dump_failed(self, tmp_path):
        # The failed environment counts: the database can belong to it.
        envs = pl.parse_envdump(_stm32_block(tmp_path, "good") + "Processing bad (x)\nError\n")
        assert pl.select_env(envs, [], tmp_path) is None

    def test_a_prefix_of_a_name_is_not_a_match(self, tmp_path):
        envs = {"f4": pl.EnvLink(build_dir=str(tmp_path / ".pio" / "build" / "f4"))}
        units = [FakeUnit(tmp_path, {"output": ".pio/build/f401/a.o"})]
        assert pl.select_env(envs, units, tmp_path) is None


class TestBuilder:
    """`PlatformIOBuildSystem` from the sidecar to the answer."""

    def _recorded(self, tmp_path, text, variant=""):
        database = _database(tmp_path)
        pl.record_link(_target(tmp_path, variant), variant, database, pl.parse_envdump(text))
        return database

    def test_a_record_with_scripts_and_defsyms(self, tmp_path):
        system, variant = _stm32_files(tmp_path)
        database = self._recorded(tmp_path, _stm32_block(tmp_path))
        units = [FakeUnit(tmp_path, {"output": ".pio/build/nucleo/a.o"})]
        builder = PlatformIOBuildSystem()
        record = link_record(builder, tmp_path, compile_commands=database, units=units)
        assert record == LinkRecord(
            scripts=[system.resolve(), variant.resolve()],
            defsyms={"LD_MAX_SIZE": "1048576", "LD_MAX_DATA_SIZE": "98304", "LD_FLASH_OFFSET": "0x0"},
        )
        assert linker_scripts(builder, tmp_path, compile_commands=database, units=units) == record.scripts

    def test_a_link_with_no_script_is_a_record(self, tmp_path):
        # The backend knows the link, and the link names no script: the
        # pass removes an old map.
        database = self._recorded(tmp_path, _env_block("x", "  'LINKFLAGS': ['-Os'],\n"))
        record = link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=database)
        assert record == LinkRecord(scripts=[], defsyms={})

    def test_the_database_in_the_project_root(self, tmp_path):
        # PlatformIO writes the same database to the project root.  Its hash
        # is the hash of the copy, thus the record applies.
        _stm32_files(tmp_path)
        database = self._recorded(tmp_path, _stm32_block(tmp_path))
        root_copy = tmp_path / "compile_commands.json"
        root_copy.write_bytes(database.read_bytes())
        assert len(PlatformIOBuildSystem().get_linker_scripts(tmp_path, compile_commands=root_copy)) == 2

    def test_the_variant_selects_the_entry(self, tmp_path):
        _stm32_files(tmp_path)
        database = self._recorded(tmp_path, _stm32_block(tmp_path), variant="release")
        builder = PlatformIOBuildSystem()
        assert link_record(builder, tmp_path, compile_commands=database, variant="debug") is None
        assert link_record(builder, tmp_path, compile_commands=database, variant="release") is not None

    def test_an_environment_the_index_does_not_hold_is_unknown(self, tmp_path):
        _stm32_files(tmp_path)
        database = self._recorded(tmp_path, _stm32_block(tmp_path, "one") + _stm32_block(tmp_path, "two"))
        assert link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=database, units=[]) is None

    def test_no_record_is_unknown(self, tmp_path):
        assert link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=_database(tmp_path)) is None

    def test_an_environment_whose_link_cannot_be_read_is_unknown(self, tmp_path):
        database = self._recorded(tmp_path, _env_block("x", "  'LINKFLAGS': ['-T', '$LDSCRIPT'],\n"))
        assert link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=database) is None


class TestLinkRecordProbe:
    """`builders.link_record` for a backend without `get_link_record`."""

    class ScriptsOnly:
        def __init__(self, scripts):
            self.scripts = scripts

        def get_linker_scripts(self, project_root, **_):
            return self.scripts

    def test_a_list_is_a_record(self, tmp_path):
        assert link_record(self.ScriptsOnly([tmp_path / "a.ld"]), tmp_path) == LinkRecord(
            scripts=[tmp_path / "a.ld"],
        )

    def test_an_empty_list_is_unknown(self, tmp_path):
        # A database whose directory holds no build.ninja gives an empty
        # list.  That is "not known", and it must not remove a correct map.
        assert link_record(self.ScriptsOnly([]), tmp_path) is None

    def test_a_backend_that_raises_is_unknown(self, tmp_path):
        class Raises:
            def get_link_record(self, project_root, **_):
                raise OSError("gone")

        assert link_record(Raises(), tmp_path) is None

    def test_a_wrong_type_is_unknown(self, tmp_path):
        class Wrong:
            def get_link_record(self, project_root, **_):
                return LinkRecord(scripts=["not a path"])

        assert link_record(Wrong(), tmp_path) is None

    def test_defsyms_that_are_not_a_dict_are_unknown(self, tmp_path):
        class Wrong:
            def get_link_record(self, project_root, **_):
                return LinkRecord(scripts=[], defsyms=[("A", "1")])

        assert link_record(Wrong(), tmp_path) is None

    def test_a_defsym_with_no_expression_is_kept(self, tmp_path):
        record = LinkRecord(scripts=[tmp_path / "a.ld"], defsyms={"A": None, "B": "1"})

        class Answers:
            def get_link_record(self, project_root, **_):
                return record

        assert link_record(Answers(), tmp_path) == record

    def test_no_builder(self, tmp_path):
        assert link_record(None, tmp_path) is None


class TestBuildRecordsTheLink:
    """`build()` records the link of its environment, and a failed `envdump` stops nothing."""

    def _run(
        self,
        tmp_path,
        monkeypatch,
        envdump_stdout: str | None,
        variant: str = "",
        environment: str = "nucleo",
        envdump_times_out: bool = False,
    ):
        """Run `build()` of *environment* with a fake `pio`.

        None for *envdump_stdout* is a dump that fails, and
        *envdump_times_out* makes it time out.
        """
        from fw_context_mcp.indexer.build import BuildConfig
        from fw_context_mcp.indexer.builders import platformio as module
        from fw_context_mcp.utils import BuildTimeoutError

        calls = []

        class Done:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.returncode = 0

        def fail(what):
            raise RuntimeError(
                f"Build command failed (exit 1): {what}\n"
                "stdout:\n  'ENV': {'API_KEY': 'SECRET_TOKEN'},\n"
            )

        def fake_run(cmd, cwd, description="", build_cfg=None, env=None, **_):
            calls.append((cmd, env))
            if "compiledb" in cmd:
                out = Path(env["PLATFORMIO_BUILD_DIR"]) / cmd[cmd.index("--environment") + 1]
                out.mkdir(parents=True, exist_ok=True)
                (out / "compile_commands.json").write_text("[]", encoding="utf-8")
            if "envdump" in cmd and envdump_times_out:
                raise BuildTimeoutError("Build command timed out after 1s: pio run --target envdump")
            if "envdump" in cmd:
                return Done(envdump_stdout) if envdump_stdout is not None else fail("pio run --target envdump")
            return Done()

        monkeypatch.setattr(module, "run_build_command", fake_run)
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/pio" if name == "pio" else None)
        cfg = BuildConfig(system="platformio", clean=False, environment=environment)
        cfg.variant_name = variant
        cc_path = PlatformIOBuildSystem().build(tmp_path, cfg)
        return cc_path, calls

    def test_the_link_is_recorded(self, tmp_path, monkeypatch):
        cc_path, calls = self._run(tmp_path, monkeypatch, _stm32_block(tmp_path), variant="v1")
        envdump_env = next(env for cmd, env in calls if "envdump" in cmd)
        assert envdump_env["PLATFORMIO_NO_ANSI"] == "true"
        assert list(pl.read_link(_target(tmp_path, "v1"), "v1", cc_path) or {}) == ["nucleo"]

    def test_the_dump_asks_for_the_environment_of_the_build(self, tmp_path, monkeypatch):
        """A variant builds one environment, thus the dump of the others is not its link."""
        _, calls = self._run(tmp_path, monkeypatch, _stm32_block(tmp_path))
        [dump] = [cmd for cmd, _ in calls if "envdump" in cmd]
        assert dump[dump.index("--environment") + 1] == "nucleo"

    def test_a_failed_envdump_does_not_stop_the_build(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level("WARNING"):
            cc_path, _ = self._run(tmp_path, monkeypatch, None)
        assert cc_path.is_file()
        assert pl.read_link(_target(tmp_path), "", cc_path) is None
        # The log holds the first line only: the dump holds the ENV of the
        # process, and the failure message quotes the output after it.
        assert "SECRET_TOKEN" not in caplog.text
        assert "envdump failed" in caplog.text

    def test_a_timeout_does_not_stop_the_build(self, tmp_path, monkeypatch):
        cc_path, _ = self._run(tmp_path, monkeypatch, None, envdump_times_out=True)
        assert cc_path.is_file()
        assert pl.read_link(_target(tmp_path), "", cc_path) is None

    def test_a_failed_envdump_removes_the_entry_of_an_earlier_build(self, tmp_path, monkeypatch):
        cc_path, _ = self._run(tmp_path, monkeypatch, _stm32_block(tmp_path))
        assert pl.read_link(_target(tmp_path), "", cc_path) is not None
        cc_path, _ = self._run(tmp_path, monkeypatch, None)
        assert pl.read_link(_target(tmp_path), "", cc_path) is None

    def test_a_dump_with_no_linkflags(self, tmp_path, monkeypatch):
        cc_path, _ = self._run(tmp_path, monkeypatch, "Processing x (y)\n", environment="x")
        assert cc_path.is_file()
        assert pl.read_link(_target(tmp_path), "", cc_path)["x"].unknown
        assert link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=cc_path) is None

    def test_a_sidecar_that_is_a_directory_does_not_stop_the_build(self, tmp_path, monkeypatch):
        _target(tmp_path).mkdir(parents=True)
        cc_path, _ = self._run(tmp_path, monkeypatch, None)
        assert cc_path.is_file()

    def test_a_dump_with_no_dict_of_the_environment(self, tmp_path, monkeypatch):
        cc_path, _ = self._run(tmp_path, monkeypatch, "no header at all\n")
        assert pl.read_link(_target(tmp_path), "", cc_path) is None

    def test_the_recorded_link_gives_the_map(self, tmp_path, monkeypatch):
        system, variant = _stm32_files(tmp_path)
        cc_path, _ = self._run(tmp_path, monkeypatch, _stm32_block(tmp_path))
        units = [FakeUnit(tmp_path, {"output": ".pio/build/nucleo/a.o"})]
        record = link_record(PlatformIOBuildSystem(), tmp_path, compile_commands=cc_path, units=units)
        assert record is not None
        assert record.scripts == [system.resolve(), variant.resolve()]


class TestRunEnvironments:
    def test_every_env_section(self):
        config = json.dumps([["platformio", []], ["env", []], ["env:a", []], ["env:b", []]])
        assert pl.run_environments(config) == ["a", "b"]

    def test_default_envs_win(self):
        config = json.dumps([["platformio", [["default_envs", ["b"]]]], ["env:a", []], ["env:b", []]])
        assert pl.run_environments(config) == ["b"]

    def test_default_envs_as_a_string(self):
        config = json.dumps([["platformio", [["default_envs", "a, b"]]], ["env:a", []], ["env:b", []]])
        assert pl.run_environments(config) == ["a", "b"]

    @pytest.mark.parametrize("text", ["not json", "{}", "[[1, []]]", '[["platformio", [["default_envs", [1]]]]]'])
    def test_another_shape(self, text):
        assert pl.run_environments(text) is None
