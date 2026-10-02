"""Link inputs that the command gives outside the `-T` options.

`ld` reads more than the `-T` scripts: a response file can hold any
argument, and an input file that is no object or archive is an implicit
linker script.  A link input that this module does not see gives the map
of another link, thus each case here is read, or it makes the answer
"not known".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders import _link_command as lc


def _files(root: Path, **files: str) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class TestResponseFile:
    @pytest.mark.parametrize("token", ["@x.rsp", "-Wl,@x.rsp", "-Wl,--gc-sections,@x.rsp"])
    def test_a_response_file_is_unknown(self, tmp_path, token):
        # The file can hold `-T b.ld`.  With the scripts only in it, the
        # answer would be "no script", which deletes a correct map.
        _files(tmp_path, **{"a.ld": "", "x.rsp": "-T b.ld\n", "b.ld": ""})
        assert lc.resolve_link_scripts(["-T", "a.ld", token], [], tmp_path) is None

    def test_a_response_file_after_xlinker_is_unknown(self, tmp_path):
        _files(tmp_path, **{"a.ld": ""})
        assert lc.resolve_link_scripts(["-T", "a.ld", "-Xlinker", "@x.rsp"], [], tmp_path) is None


class TestInputFiles:
    """ld reads an input file that is no object or archive as a linker script.

    A text input file makes the answer "not known".  The module does not
    read it as a script: a word that looks like an input file can be the
    value of an option that it does not know, and a map file of an earlier
    build is text too.
    """

    @pytest.mark.parametrize("flags", [
        ["b.ld"], ["-Wl,b.ld"], ["-Xlinker", "b.ld"], ["--for-linker", "b.ld"], ["--for-linker=b.ld"],
        ["-Wl,--start-group,b.ld"],
    ])
    def test_a_text_input_file_is_unknown(self, tmp_path, flags):
        _files(tmp_path, **{"a.ld": "", "b.ld": "PROVIDE(_x = 1);\n"})
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) is None

    def test_an_object_or_an_archive_is_no_script(self, tmp_path):
        _files(tmp_path, **{"a.ld": ""})
        (tmp_path / "libx.a").write_bytes(b"!<arch>\nrest")
        (tmp_path / "y.o").write_bytes(b"\x7fELF\x01\x01")
        (tmp_path / "z.bc").write_bytes(b"BC\xc0\xde\x35\x14\x00\x00")
        (tmp_path / "w.obj").write_bytes(b"\x64\x86\x02\x00\x00\x00")
        assert lc.resolve_link_scripts(["-T", "a.ld", "libx.a", "-Wl,y.o", "z.bc", "w.obj"], [], tmp_path) == [
            (tmp_path / "a.ld").resolve(),
        ]

    @pytest.mark.parametrize("flags", [
        ["-Wl,--format=binary,web/index.html,--format=default"],
        ["-Wl,-b,binary,web/index.html,-b,elf32-littlearm"],
        ["-Wl,-b", "binary", "web/index.html"],
    ])
    def test_a_file_in_another_format_is_data(self, tmp_path, flags):
        # Embedding a certificate, HTML or a font with `--format=binary` is
        # a known pattern.  The file is data, and no script.
        _files(tmp_path, **{"a.ld": "", "web/index.html": "<script>x = 3;</script>\n"})
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) == [(tmp_path / "a.ld").resolve()]

    @pytest.mark.parametrize("flags", [
        ["-Wl,--format=elf32-littlearm", "extra.ld"],
        ["-Wl,-b,binary,res.bin,-b,elf32-littlearm", "extra.ld"],
        ["-Wl,-format=srec", "extra.ld"],
    ])
    def test_only_the_binary_format_makes_data(self, tmp_path, flags):
        # Measured with arm-none-eabi-ld 2.40: after `-b elf32-littlearm`
        # ld does not recognise a text file, and reads it as a script.
        _files(tmp_path, **{"a.ld": "", "res.bin": "x\n", "extra.ld": "PROVIDE(_x = 1);\n"})
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) is None

    def test_an_l_colon_file_in_the_binary_format_is_data(self, tmp_path):
        # Measured: `ld -b binary -l:extra.ld` links extra.ld as data.
        _files(tmp_path, **{"a.ld": "", "lib/extra.ld": "PROVIDE(_x = 1);\n"})
        flags = ["-L", str(tmp_path / "lib"), "-Wl,-b,binary", "-Wl,-l:extra.ld"]
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) == [(tmp_path / "a.ld").resolve()]

    def test_a_text_file_after_the_default_format_again(self, tmp_path):
        _files(tmp_path, **{"a.ld": "", "blob.txt": "x\n", "b.ld": "PROVIDE(_x = 1);\n"})
        flags = ["-Wl,-b,binary,blob.txt,-b,default,b.ld"]
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) is None

    @pytest.mark.parametrize("flags", [
        ["-u", "out.map"],
        ["-Wl,-Map", "out.map"],
        ["-o", "out.map"],
        ["-Wl,-Map,out.map"],
        ["-Wl,-Map=out.map"],
        ["-Wl,--Map,out.map"],
        ["-Wl,--dynamic-list,out.map"],
        ["-Wl,--section-ordering-file", "out.map"],
        ["-Xlinker", "-Map", "-Xlinker", "out.map"],
        ["-Xlinker", "-Map", "-Wl,out.map"],
        ["-Wl,-Map", "-Xlinker", "out.map"],
        ["-Wl,--defsym", "out.map"],
        ["-Wl,-L", "out.map"],
    ])
    def test_the_value_of_an_option_is_no_input_file(self, tmp_path, flags):
        # A map file of an earlier build is a text file on disk.  As the
        # value of an option it is no input of the link.
        _files(tmp_path, **{"a.ld": "", "out.map": "Memory Configuration\n"})
        assert lc.resolve_link_scripts(["-T", "a.ld", *flags], [], tmp_path) == [(tmp_path / "a.ld").resolve()]

    def test_a_t_script_is_not_read_twice(self, tmp_path):
        _files(tmp_path, **{"a.ld": "", "d.ld": ""})
        assert lc.resolve_link_scripts(
            ["-T", "a.ld", "-Wl,--default-script", "d.ld"], [], tmp_path,
        ) == [(tmp_path / "a.ld").resolve()]

    def test_a_name_that_is_no_file_is_skipped(self, tmp_path):
        # An input file that is not there stops the link, thus a word that
        # names no file is the value of an option this module does not know.
        _files(tmp_path, **{"a.ld": ""})
        assert lc.resolve_link_scripts(["-T", "a.ld", "-Wl,--unknown-option", "app_main"], [], tmp_path) == [
            (tmp_path / "a.ld").resolve(),
        ]


class TestOneDashLongOptions:
    """ld reads its options with getopt_long_only: `-script` is `--script`."""

    @pytest.mark.parametrize("flags", [["-Wl,-script=b.ld"], ["-Wl,-script,b.ld"], ["-Wl,-script", "b.ld"]])
    def test_a_script(self, tmp_path, flags):
        _files(tmp_path, **{"b.ld": ""})
        assert lc.resolve_link_scripts(flags, [], tmp_path) == [(tmp_path / "b.ld").resolve()]

    def test_a_default_script(self, tmp_path):
        _files(tmp_path, **{"d.ld": ""})
        assert lc.resolve_link_scripts(["-Wl,-default-script=d.ld"], [], tmp_path) == [(tmp_path / "d.ld").resolve()]

    def test_a_defsym(self):
        assert lc.defsyms_from_flags(["-Wl,-defsym=A=1,-defsym,B=2"]) == {"A": "1", "B": "2"}

    def test_a_library_path(self):
        assert lc.library_dirs_from_flags(["-Wl,-library-path=/a"]) == ([], ["/a"])


class TestLostAliases:
    @pytest.mark.parametrize("raw", [["--library-directory=$X"], ["--for-linker=$X"], ["--library-directory", "$X"]])
    def test_an_alias_that_did_not_expand_is_unknown(self, raw):
        assert lc._lost_link_input(raw, [None if "$" in token else token for token in raw])


class TestSeparateSpecsAndAliases:
    @pytest.mark.parametrize("option", ["-specs", "--specs"])
    def test_a_specs_file_in_the_next_argument_is_read(self, tmp_path, option):
        # gcc takes `-specs FILE` and `--specs FILE` too.
        _files(tmp_path, **{"a.ld": "", "board.specs": "*link:\n+ -T extra.ld\n"})
        assert lc.resolve_link_scripts(["-T", "a.ld", option, "./board.specs"], [], tmp_path) is None

    @pytest.mark.parametrize("flags", [["--library-directory", "{lib}"], ["--library-directory={lib}"]])
    def test_library_directory_is_l(self, tmp_path, flags):
        # A `-T` name resolves in the cwd first, and then in the -L
        # directories: the alias must be one of them.
        _files(tmp_path, **{"lib/a.ld": "", "late/a.ld": ""})
        tokens = [flag.format(lib=tmp_path / "lib") for flag in flags]
        found = lc.resolve_link_scripts([*tokens, "-T", "a.ld"], [str(tmp_path / "late")], tmp_path / "none")
        assert found == [(tmp_path / "lib" / "a.ld").resolve()]


class TestLibraryAtTheEndOfAToken:
    @pytest.mark.parametrize("flags", [["-Wl,-l", ":b.ld"], ["-Wl,--library", ":b.ld"]])
    def test_the_file_in_the_next_token(self, tmp_path, flags):
        _files(tmp_path, **{"a.ld": "", "lib/b.ld": "PROVIDE(_x = 1);\n"})
        found = lc.resolve_link_scripts(["-T", "a.ld", "-L", str(tmp_path / "lib"), *flags], [], tmp_path)
        assert found == [(tmp_path / "a.ld").resolve(), (tmp_path / "lib" / "b.ld").resolve()]


class TestShellWords:
    """Words that the shell already split are not unquoted again."""

    def test_a_directory_with_an_apostrophe(self, tmp_path):
        _files(tmp_path, **{"Tom's sdk/a.ld": ""})
        directory = str(tmp_path / "Tom's sdk")
        found = lc.resolve_link_scripts(["-L" + directory, "-T", "a.ld"], [], tmp_path / "none", unquote=False)
        assert found == [(tmp_path / "Tom's sdk" / "a.ld").resolve()]


class TestWindowsCommandLine:
    """The rules of CommandLineToArgvW, which cmd.exe and the C runtime use."""

    @pytest.mark.parametrize(("text", "words"), [
        ('-L"C:/My Projects/x" -T a.ld', ["-LC:/My Projects/x", "-T", "a.ld"]),
        (r'"C:\p q\a.ld"', [r"C:\p q\a.ld"]),
        (r"C:\dir\a.ld", [r"C:\dir\a.ld"]),
        (r'a\"b', ['a"b']),
        (r'"a\\" b', ["a\\", "b"]),
        ('""', [""]),
        ("  a   b  ", ["a", "b"]),
    ])
    def test_the_words(self, text, words):
        assert lc.windows_words(text) == words
