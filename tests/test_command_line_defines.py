"""The `-D` flags of a build, and only the ones every unit shares.

Those flags ARE the configuration the build states.  zbox-ecb-fw passes
`APPLICATION_ADDR=0x10200`, `APPLICATION_SIZE=0xefe00`, and
`CMSIS_VECTAB_VIRTUAL` — the memory map and the fact that the vector table
moves to RAM.

The rule the tests exist to pin: a tool that describes ONE build must not
show the defines of one FILE as the defines of the build.  Measured on
zbox-ecb-fw, 881 units: 27 names in every one and 59 in only some, because
the three assembly files get a shorter set.  HA_Boiler splits on
`ARDUINO_CORE_BUILD`, which 46 of its 114 units carry.
"""

from __future__ import annotations

import json
from pathlib import Path

from fw_context_mcp.indexer.compile_commands import (
    _defines_of_entry,
    command_line_defines,
)


def _cc(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _entry(name: str, *flags: str) -> dict:
    return {"directory": "/p", "file": name, "arguments": ["cc", *flags, "-c", name]}


class TestOneEntry:
    """Both spellings the compiler accepts."""

    def test_joined(self):
        assert _defines_of_entry(_entry("a.c", "-DNAME=1")) == {"NAME": "1"}

    def test_separate(self):
        assert _defines_of_entry(_entry("a.c", "-D", "NAME=1")) == {"NAME": "1"}

    def test_no_value(self):
        # `-DNAME` defines it as 1 to the preprocessor.  The index reports
        # the flag as written and does not derive that value.
        assert _defines_of_entry(_entry("a.c", "-DNAME")) == {"NAME": ""}

    def test_a_value_that_holds_an_equals_sign(self):
        assert _defines_of_entry(_entry("a.c", "-DA=B=C")) == {"A": "B=C"}

    def test_a_quoted_value(self):
        # Measured on FM: `BOARD_NAME=\"NUCLEO_L476RG\"`.
        found = _defines_of_entry(_entry("a.c", '-DBOARD=\\"X\\"'))
        assert found == {"BOARD": '\\"X\\"'}

    def test_a_command_string_instead_of_arguments(self):
        entry = {"directory": "/p", "file": "a.c",
                 "command": "cc -DONE=1 -DTWO -c a.c"}
        assert _defines_of_entry(entry) == {"ONE": "1", "TWO": ""}

    def test_a_flag_that_only_starts_the_same_way(self):
        # `-Dsomething` is a define; `-dM` and `-MD` are not.
        found = _defines_of_entry(_entry("a.c", "-MD", "-dM", "-DREAL=1"))
        assert found == {"REAL": "1"}

    def test_no_define_at_all(self):
        assert _defines_of_entry(_entry("a.c", "-O2")) == {}

    def test_an_entry_with_neither_field(self):
        assert _defines_of_entry({"file": "a.c"}) == {}


class TestOnlyWhatEveryUnitShares:
    def test_a_name_in_every_unit(self, tmp_path):
        cc = _cc(tmp_path, [
            _entry("a.c", "-DBOARD=X"),
            _entry("b.c", "-DBOARD=X"),
        ])
        shared, varying = command_line_defines(cc)
        assert shared == {"BOARD": "X"}
        assert varying == 0

    def test_a_name_in_only_some_units(self, tmp_path):
        # HA_Boiler: ARDUINO_CORE_BUILD reaches 46 of 114 units.
        cc = _cc(tmp_path, [
            _entry("core.c", "-DBOARD=X", "-DCORE_BUILD"),
            _entry("app.c", "-DBOARD=X"),
        ])
        shared, varying = command_line_defines(cc)
        assert shared == {"BOARD": "X"}
        assert varying == 1

    def test_a_name_with_two_different_values(self, tmp_path):
        # Reporting either value would be a guess about which unit the
        # caller meant.
        cc = _cc(tmp_path, [
            _entry("a.c", "-DLEVEL=1"),
            _entry("b.c", "-DLEVEL=2"),
        ])
        shared, varying = command_line_defines(cc)
        assert shared == {}
        assert varying == 1

    def test_the_assembly_unit_with_a_shorter_set(self, tmp_path):
        # The measured shape on zbox-ecb-fw: three .S files out of 881 carry
        # 27 of the 86 names.
        cc = _cc(tmp_path, [
            _entry("a.cpp", "-DA", "-DB", "-DC"),
            _entry("b.cpp", "-DA", "-DB", "-DC"),
            _entry("startup.S", "-DA"),
        ])
        shared, varying = command_line_defines(cc)
        assert set(shared) == {"A"}
        assert varying == 2

    def test_one_unit_only(self, tmp_path):
        cc = _cc(tmp_path, [_entry("a.c", "-DONE=1", "-DTWO")])
        shared, varying = command_line_defines(cc)
        assert shared == {"ONE": "1", "TWO": ""}
        assert varying == 0

    def test_a_unit_with_no_define_empties_the_set(self, tmp_path):
        # Correct and worth pinning: a build where one unit carries no
        # define has no define that EVERY unit shares.
        cc = _cc(tmp_path, [
            _entry("a.c", "-DBOARD=X"),
            _entry("b.c"),
        ])
        shared, varying = command_line_defines(cc)
        assert shared == {}
        assert varying == 1


class TestWhatItRefuses:
    def test_a_file_that_is_not_there(self, tmp_path):
        shared, varying = command_line_defines(tmp_path / "absent.json")
        assert (shared, varying) == ({}, 0)

    def test_a_file_that_is_not_json(self, tmp_path):
        path = tmp_path / "compile_commands.json"
        path.write_text("not json at all", encoding="utf-8")
        assert command_line_defines(path) == ({}, 0)

    def test_json_that_is_not_a_list(self, tmp_path):
        path = tmp_path / "compile_commands.json"
        path.write_text('{"file": "a.c"}', encoding="utf-8")
        assert command_line_defines(path) == ({}, 0)

    def test_an_empty_list(self, tmp_path):
        assert command_line_defines(_cc(tmp_path, [])) == ({}, 0)

    def test_an_entry_that_is_not_an_object(self, tmp_path):
        path = tmp_path / "compile_commands.json"
        path.write_text('["a", {"file": "b.c", "arguments": ["cc", "-DX"]}]',
                        encoding="utf-8")
        shared, _ = command_line_defines(path)
        assert shared == {"X": ""}


class TestTheCostIsWhyItIsNotStored:
    """The reason this is computed on demand.

    `compile_commands.parse` normalizes every flag for libclang and costs
    6966 ms on zbox-ecb-fw against 19 ms for the direct read — measured.
    `get_active_build` is the mandatory first call, thus the difference is
    paid by every session.
    """

    def test_it_does_not_call_the_normalizing_parser(self, tmp_path, monkeypatch):
        import fw_context_mcp.indexer.compile_commands as module

        called = False

        def fail(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("command_line_defines must not call parse()")

        monkeypatch.setattr(module, "parse", fail)
        cc = _cc(tmp_path, [_entry("a.c", "-DONE=1")])
        assert command_line_defines(cc) == ({"ONE": "1"}, 0)
        assert not called
