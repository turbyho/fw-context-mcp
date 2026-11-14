"""The value of a macro row is the definition of its name at the end of EACH unit that reads its file.

Before, each unit ran ``clang -dM -E`` and wrote its definition into every
row of the name in the build, in turn: the last unit of the run decided the
value, also of a row in a header that it does not read.  An error of a unit
or of SQLite was swallowed, and a function-like macro got no value.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer import _postprocess, macros
from fw_context_mcp.indexer.db import (
    insert_macros_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.macros import (
    MacroDefinition,
    NoMacroAnswer,
    UnitMacros,
    agreed_value,
    expanded_fields,
    parse_definitions,
    parse_dependency_file,
    store_values_of_build,
)
from fw_context_mcp.path_key import canonical_path
from tests._toolchains import unit_of

CONFIG = "testhash"


class TestTheOutputOfThePreprocessor:
    """``clang -dM -E`` writes ``#define NAME definition`` and ``#define NAME(params) definition``."""

    def test_each_form_of_a_definition(self):
        found = parse_definitions([
            "#define EMPTY ",
            "#define OBJ (1)",
            "#define F(a,b) ((a)+(b))",
            "#define G(...) g(__VA_ARGS__)",
            "#define H(x,...) h(x, ##__VA_ARGS__)",
            "#define N() n()",
            "#define FE(x) ",
            "#define $D 1",
            "int not_a_define;",
        ])

        assert found == {
            "EMPTY": MacroDefinition("", "", False),
            # A "(" after a space is the replacement text, not a list.
            "OBJ": MacroDefinition("(1)", "", False),
            # The parameters in the form of the macros table: "a, b".
            "F": MacroDefinition("((a)+(b))", "a, b", True),
            "G": MacroDefinition("g(__VA_ARGS__)", "...", True),
            "H": MacroDefinition("h(x, ##__VA_ARGS__)", "x, ...", True),
            "N": MacroDefinition("n()", "", True),
            "FE": MacroDefinition("", "x", True),
            "$D": MacroDefinition("1", "", False),
        }


class TestNoAnswer:
    """A unit with no answer raises: an empty answer read as "no macro is defined"."""

    def test_a_source_that_does_not_exist(self, tmp_path: Path):
        with pytest.raises(NoMacroAnswer, match="does not exist"):
            macros.definitions_at_end_of_unit([], tmp_path / "gone.c")

    def test_a_preprocessor_that_fails(self, tmp_path: Path, monkeypatch):
        source = tmp_path / "a.c"
        source.write_text("#error stop\n", encoding="utf-8")
        monkeypatch.setattr(
            macros, "run_clang",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b"a.c:1:2: error: stop\n"),
        )

        with pytest.raises(NoMacroAnswer, match="exit 1"):
            macros.definitions_at_end_of_unit([], source, clang_binary="clang")


def _db(tmp_path: Path):
    """An index with one header ``h.h`` that units a.c and b.c read, and one row of LEVEL in each file."""
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, CONFIG, "pid", "cc.json")
        ids = {name: upsert_file(conn, CONFIG, name, "c") for name in ("h.h", "a.c", "b.c", "c.c")}
        insert_macros_batch(conn, [
            (CONFIG, ids["h.h"], "LEVEL", "1", "", "", 3, 0),
            (CONFIG, ids["h.h"], "TWICE", "( x ) * 2", "x", "", 4, 1),
            (CONFIG, ids["c.c"], "LEVEL", "9", "", "", 1, 0),
        ])
    return conn


def _unit(tmp_path: Path, name: str):
    return SimpleNamespace(file=tmp_path / name, directory=tmp_path, clang_args=[])


#: The files that each unit reads, as its dependency file names them.
_READS = {"a.c": ("a.c", "h.h"), "b.c": ("b.c", "h.h"), "c.c": ("c.c",)}


def _store(conn, tmp_path: Path, answers: dict):
    """Run the macros step with *answers* (unit name → definitions, or an exception) as the preprocessor.

    Each unit reads the files of ``_READS``, as its dependency file gives them.
    """
    root = canonical_path(tmp_path)

    def answer_of(unit):
        answer = answers[unit.file.name]
        if isinstance(answer, Exception):
            raise answer
        return UnitMacros(answer, frozenset(root / name for name in _READS[unit.file.name]))

    units = [_unit(root, name) for name in answers]
    with transaction(conn):
        report = store_values_of_build(conn, CONFIG, units, root, answer_of=answer_of)
    return report


def _row(conn, file: str, name: str):
    return conn.execute(
        "SELECT m.* FROM macros m JOIN files f ON f.id = m.file_id WHERE f.path = ? AND m.name = ?", (file, name),
    ).fetchone()


class TestTheValuesOfABuild:
    def test_a_unit_that_does_not_read_the_file_does_not_decide_its_value(self, tmp_path: Path):
        """c.c defines LEVEL as 9 and runs last: before, every row of LEVEL said 9."""
        conn = _db(tmp_path)
        try:
            _store(conn, tmp_path, {
                "a.c": {"LEVEL": MacroDefinition("1", "", False)},
                "b.c": {"LEVEL": MacroDefinition("1", "", False)},
                "c.c": {"LEVEL": MacroDefinition("9", "", False)},
            })

            header = _row(conn, "h.h", "LEVEL")
            assert header["expanded_value"] == "1"
            assert json.loads(header["expanded_values"]) == [
                {"state": "defined", "units": 2, "value": "1", "params": "", "is_function_like": False},
            ]
            assert _row(conn, "c.c", "LEVEL")["expanded_value"] == "9"
        finally:
            conn.close()

    def test_two_units_with_two_definitions_give_two_values_and_no_single_value(self, tmp_path: Path):
        """b.c has ``-DLEVEL_OVERRIDE`` and an #undef: no single value is true for the header."""
        conn = _db(tmp_path)
        try:
            _store(conn, tmp_path, {
                "a.c": {"LEVEL": MacroDefinition("1", "", False)},
                "b.c": {"LEVEL": MacroDefinition("2", "", False)},
            })

            header = _row(conn, "h.h", "LEVEL")
            assert header["expanded_value"] == ""
            assert expanded_fields(header) == {"expanded_values": [
                {"state": "defined", "units": 1, "value": "1", "params": "", "is_function_like": False},
                {"state": "defined", "units": 1, "value": "2", "params": "", "is_function_like": False},
            ]}
            assert agreed_value([header]) is None
        finally:
            conn.close()

    def test_an_undef_is_a_state_of_its_own(self, tmp_path: Path):
        conn = _db(tmp_path)
        try:
            _store(conn, tmp_path, {"a.c": {}, "b.c": {"LEVEL": MacroDefinition("1", "", False)}})

            assert json.loads(_row(conn, "h.h", "LEVEL")["expanded_values"]) == [
                {"state": "defined", "units": 1, "value": "1", "params": "", "is_function_like": False},
                {"state": "undefined", "units": 1},
            ]
        finally:
            conn.close()

    def test_a_unit_with_no_answer_is_no_answer_for_each_row(self, tmp_path: Path):
        """With no answer the run knows neither the definitions nor the files of the unit.

        Thus it cannot say for any file that the units which read it agree:
        also c.c, which only c.c reads, can be a file of b.c.
        """
        conn = _db(tmp_path)
        try:
            report = _store(conn, tmp_path, {
                "a.c": {"LEVEL": MacroDefinition("1", "", False)},
                "b.c": NoMacroAnswer("clang -dM -E failed (exit 1): stop"),
            })

            assert json.loads(_row(conn, "h.h", "LEVEL")["expanded_values"]) == [
                {"state": "defined", "units": 1, "value": "1", "params": "", "is_function_like": False},
                {"state": "no_answer", "units": 1},
            ]
            assert json.loads(_row(conn, "c.c", "LEVEL")["expanded_values"]) == [{"state": "no_answer", "units": 1}]
            assert report.no_answer == [("b.c", "clang -dM -E failed (exit 1): stop")]
        finally:
            conn.close()

    def test_each_listing_of_a_file_counts_with_the_files_that_it_read(self, tmp_path: Path):
        """compile_commands.json lists a.c two times; only the listing with -DUSE_H reads h.h.

        The two listings shared one header list (the manifest keys its
        lists by file): the listing that does not read h.h counted as
        "undefined" for each row of h.h.  The dependency file of each
        listing names its own files.
        """
        conn = _db(tmp_path)
        try:
            root = canonical_path(tmp_path)
            answers = iter([
                UnitMacros({"LEVEL": MacroDefinition("7", "", False)}, frozenset({root / "a.c", root / "h.h"})),
                UnitMacros({}, frozenset({root / "a.c"})),
            ])
            with transaction(conn):
                store_values_of_build(
                    conn, CONFIG, [_unit(root, "a.c"), _unit(root, "a.c")], root, answer_of=lambda unit: next(answers),
                )

            assert expanded_fields(_row(conn, "h.h", "LEVEL")) == {"expanded_value": "7"}
        finally:
            conn.close()

    def test_a_function_like_macro_gets_its_value(self, tmp_path: Path):
        """``-dM`` writes ``TWICE(x)``, and the parser before skipped each such name."""
        conn = _db(tmp_path)
        try:
            twice = MacroDefinition("(x)*2", "x", True)
            _store(conn, tmp_path, {"a.c": {"TWICE": twice}, "b.c": {"TWICE": twice}})

            assert expanded_fields(_row(conn, "h.h", "TWICE")) == {"expanded_value": "(x)*2"}
        finally:
            conn.close()

    def test_a_definition_of_another_shape_shows_its_parameters(self, tmp_path: Path):
        """The row is function-like, and each unit ends with an object-like TWICE: a bare value would hide that."""
        conn = _db(tmp_path)
        try:
            other = MacroDefinition("2", "", False)
            _store(conn, tmp_path, {"a.c": {"TWICE": other}, "b.c": {"TWICE": other}})

            assert expanded_fields(_row(conn, "h.h", "TWICE")) == {"expanded_values": [
                {"state": "defined", "units": 2, "value": "2", "params": "", "is_function_like": False},
            ]}
        finally:
            conn.close()

    def test_a_row_that_the_parse_stores_again_loses_its_values(self, tmp_path: Path):
        """The old value is of the text before the parse: the macros step of the next run writes the new one."""
        conn = _db(tmp_path)
        try:
            _store(conn, tmp_path, {
                "a.c": {"LEVEL": MacroDefinition("1", "", False)},
                "b.c": {"LEVEL": MacroDefinition("1", "", False)},
            })
            file_id = _row(conn, "h.h", "LEVEL")["file_id"]
            with transaction(conn):
                insert_macros_batch(conn, [(CONFIG, file_id, "LEVEL", "5", "", "", 3, 0)])

            row = _row(conn, "h.h", "LEVEL")
            assert (row["expanded_value"], row["expanded_values"]) == ("", "")
            assert expanded_fields(row) == {}
        finally:
            conn.close()


def test_a_row_of_an_index_before_the_column_keeps_its_old_value():
    """An index of an earlier schema has no expanded_values until the next run migrates it."""
    row = {"expanded_value": "7", "is_function_like": 0, "params": ""}

    assert expanded_fields(row) == {"expanded_value": "7"}
    assert agreed_value([row]) is None


def test_the_step_names_each_unit_with_no_answer(tmp_path: Path, monkeypatch, caplog):
    """Before, ``pass`` swallowed each error of the step, and no line said so."""
    conn = _db(tmp_path)
    try:
        def no_clang(unit):
            raise NoMacroAnswer("clang cannot run: no such file")

        monkeypatch.setattr(macros, "_answer_of_unit", no_clang)
        ctx = {"config_hash": CONFIG, "units": [_unit(tmp_path, "a.c")], "project_root": tmp_path}
        with caplog.at_level(logging.WARNING, logger=_postprocess.log.name):
            _postprocess._step_expand_macros(conn, ctx)

        assert "1 of 1 units gave no definitions" in caplog.text
        assert "a.c: clang cannot run: no such file" in caplog.text
        # The runner writes the units into the marker of the run: the next
        # run asks again, and the state of the index names them.
        assert ctx["macros_no_answer"] == (("a.c", "clang cannot run: no such file"),)
    finally:
        conn.close()


def test_an_error_of_the_start_of_clang_is_no_answer_for_that_unit(tmp_path: Path, monkeypatch):
    """A directory of the compilation that is gone gave NotADirectoryError, which stopped the step for all units."""
    source = tmp_path / "a.c"
    source.write_text("#define A 1\n", encoding="utf-8")

    def gone(*args, **kwargs):
        raise NotADirectoryError(20, "Not a directory", str(tmp_path / "build"))

    monkeypatch.setattr(macros, "run_clang", gone)

    with pytest.raises(NoMacroAnswer, match="clang cannot run"):
        macros.definitions_at_end_of_unit([], source, clang_binary="clang", cwd=tmp_path / "build")


def test_a_failed_step_keeps_the_writes_of_the_steps_before(tmp_path: Path, monkeypatch):
    """The step opened its transaction on the open writes of the steps before, and its error rolled them back."""
    import sqlite3  # after fw_context_mcp: the pysqlite3 redirect

    conn = _db(tmp_path)
    try:
        conn.execute("UPDATE files SET source_hash = 'written-before' WHERE path = 'h.h'")

        def fails(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(macros, "store_values_of_build", fails)
        ctx = {"config_hash": CONFIG, "units": [], "project_root": tmp_path}
        with pytest.raises(sqlite3.OperationalError):
            _postprocess._step_expand_macros(conn, ctx)
        conn.rollback()

        assert conn.execute("SELECT source_hash FROM files WHERE path = 'h.h'").fetchone()[0] == "written-before"
    finally:
        conn.close()


class TestTheDependencyFile:
    """The files that a unit reads: the dependency file of the same run of the preprocessor (``-MD``)."""

    def test_the_escapes_of_clang(self):
        text = (
            "unit: /src/main.c /inc/with\\ space.h \\\n"
            "  /inc/hash\\#name.h /inc/dollar$$.h \\\r\n"
            "  C:\\sdk\\Program\\ Files\\x.h\n"
        )

        assert parse_dependency_file(text) == [
            "/src/main.c", "/inc/with space.h", "/inc/hash#name.h", "/inc/dollar$.h", "C:\\sdk\\Program Files\\x.h",
        ]

    def test_a_backslash_before_a_space_is_doubled_by_clang(self):
        """Windows: the name ``C:\\dir\\ x.h`` (a name of a directory entry that begins with a space).

        clang writes each backslash before a space two times and then the
        escape: ``C:\\dir\\\\\\ x.h``.  The parser kept each backslash
        that a space did not follow, thus it read ``C:\\dir\\\\ x.h``.
        """
        assert parse_dependency_file("unit: C:\\dir\\\\\\ x.h\n") == ["C:\\dir\\ x.h"]

    def test_a_text_with_no_rule_is_an_error(self):
        with pytest.raises(ValueError, match="no rule"):
            parse_dependency_file("")

    def test_the_preprocessor_names_the_header_and_the_include_of_the_flags(self, tmp_path: Path, toolchain):
        """A header of an #include, and a file of -include: the two are files that the unit reads."""
        (tmp_path / "inc").mkdir()
        (tmp_path / "inc" / "config.h").write_text("#define LEVEL 3\n", encoding="utf-8")
        (tmp_path / "forced.h").write_text("#define FORCED 1\n", encoding="utf-8")
        source = tmp_path / "main.c"
        source.write_text('#include "config.h"\nint x = LEVEL;\n', encoding="utf-8")

        unit = unit_of(source, toolchain, ["-Iinc", "-include", "forced.h"], directory=tmp_path)
        answer = macros.definitions_at_end_of_unit(unit.clang_args, source, cwd=tmp_path)

        root = canonical_path(tmp_path)
        assert {root / "main.c", root / "inc" / "config.h", root / "forced.h"} <= answer.files
        assert answer.definitions["LEVEL"].value == "3"


class TestTheFlagsOfThePreprocessor:
    """``_sanitize_flags``: each flag of the parse, without the output options (indexer/_driver_options.py)."""

    @pytest.mark.parametrize("flags", [
        ["-iprefix", "/fw/include/", "-iwithprefixbefore", "newlib/platform_include"],
        ["-isysroot", "/sys", "-imultilib", "thumb", "-iquote", "inc"],
        ["-x", "c++", "-target", "armv7m-none-eabi", "-meabi", "gnu"],
        ["-O2", "-ffreestanding", "-fno-exceptions", "-fno-rtti", "-ansi"],
        ["-Xclang", "-include", "-Xclang", "/b/cmake_pch.hxx", "-Xpreprocessor", "-DFOO"],
        ["-working-directory=/b", "-mllvm", "-inline-threshold=5", "-Werror"],
        ["-funknown-option-of-a-new-gcc", "-munknown", "value"],
    ])
    def test_each_flag_of_the_parse_reaches_the_preprocessor(self, flags):
        """A flag can change the predefined macros or the search of the headers, thus each one stays, in its order."""
        assert macros._sanitize_flags(["-mthumb", *flags, "-DX=1"]) == ["-mthumb", *flags, "-DX=1", "-w"]

    @pytest.mark.parametrize("flags", [
        ["-o", "-c.o"],
        ["-MF", "unit.d", "-MT", "unit", "-MQ", "unit", "-MJ", "unit.json"],
        ["-c", "-S", "-E", "-fsyntax-only", "-M", "-MM", "-MD", "-MMD", "-MG", "-MP"],
        ["-save-temps=obj", "--serialize-diagnostics", "unit.dia", "-dumpdir", "out/"],
    ])
    def test_the_output_options_go_with_their_values(self, flags):
        """``-o -c.o``: the value goes with the option, and is no flag ``-c.o`` of its own."""
        assert macros._sanitize_flags(["-mthumb", *flags, "-DX=1"]) == ["-mthumb", "-DX=1", "-w"]

    @pytest.mark.parametrize("flags", [
        ["-fplugin=/tmp/x.so"],
        ["-fpass-plugin=/tmp/x.so"],
        ["-Xclang", "-load", "-Xclang", "/tmp/x.so"],
        ["-Xclang", "-add-plugin", "-Xclang", "x"],
    ])
    def test_a_compiler_plugin_is_no_answer(self, flags):
        """fw-context does not run code from compile_commands.json."""
        with pytest.raises(NoMacroAnswer, match="compiler plugin"):
            macros._sanitize_flags(["-mthumb", *flags])

    def test_the_include_directories_of_a_prefix_reach_the_preprocessor(self, tmp_path: Path, toolchain):
        """The flags of the ESP32 Arduino framework: ``-iprefix <dir> -iwithprefixbefore <sub>``.

        Before, each unit of such a build had no macro values: clang read
        ``newlib/platform_include`` as a second source file.
        """
        prefixed = tmp_path / "libs" / "include" / "newlib" / "platform_include"
        prefixed.mkdir(parents=True)
        (prefixed / "platform.h").write_text("#define PLATFORM_LEVEL 7\n", encoding="utf-8")
        source = tmp_path / "main.c"
        source.write_text('#include "platform.h"\nint x = PLATFORM_LEVEL;\n', encoding="utf-8")
        prefix = str(tmp_path / "libs" / "include") + "/"

        unit = unit_of(
            source, toolchain, ["-iprefix", prefix, "-iwithprefixbefore", "newlib/platform_include"],
            directory=tmp_path,
        )
        answer = macros.definitions_at_end_of_unit(unit.clang_args, source, cwd=tmp_path)

        assert answer.definitions["PLATFORM_LEVEL"].value == "7"
        assert canonical_path(prefixed / "platform.h") in answer.files


def test_the_backfill_of_the_header_hashes_survives_a_later_rollback(tmp_path: Path, monkeypatch):
    """The manifest step left its writes open, and the first later step that rolled back took them too."""
    conn = _db(tmp_path)
    try:
        def backfill(conn, config_hash, project_root, manifest, *, hash_cache=None):
            conn.execute("UPDATE files SET source_hash = 'backfilled' WHERE path = 'h.h'")
            return 1

        monkeypatch.setattr(_postprocess, "_update_manifest_after_index", lambda **kwargs: None)
        monkeypatch.setattr(_postprocess, "_backfill_header_source_hashes", backfill)
        ctx = {
            "config_hash": CONFIG, "manifest": {"entries": []}, "units": [], "project_root": tmp_path,
            "db_dir": tmp_path, "compile_commands": tmp_path / "compile_commands.json", "updated": 0,
            "tu_headers": {}, "build_dir_patterns": None, "transient_defines": (),
            "odr_pending": frozenset(), "failed_units": frozenset(),
        }
        _postprocess._step_update_manifest(conn, ctx)

        with pytest.raises(RuntimeError), transaction(conn):
            raise RuntimeError("a later step fails")

        assert conn.execute("SELECT source_hash FROM files WHERE path = 'h.h'").fetchone()[0] == "backfilled"
    finally:
        conn.close()


def test_a_unit_with_no_answer_counts_only_for_the_files_of_its_parse(tmp_path: Path):
    """b.c has a missing header: the preprocessor gives no answer and no files.

    The parse of libclang read b.c and h.h (its manifest entry), thus only
    their rows get the no_answer.  Before, each row of the build got it, and
    c.c, which only c.c reads, lost its value too.
    """
    conn = _db(tmp_path)
    try:
        root = canonical_path(tmp_path)
        manifest = {"entries": [
            {"file": "a.c", "flags_hash": "fa", "headers": ["h.h"]},
            {"file": "b.c", "flags_hash": "fb", "headers": ["h.h"]},
            {"file": "c.c", "flags_hash": "fc", "headers": []},
        ]}

        def answer_of(unit):
            if unit.file.name == "b.c":
                raise NoMacroAnswer("clang -dM -E failed (exit 1): 'missing.h' file not found")
            files = {"a.c": ("a.c", "h.h"), "c.c": ("c.c",)}[unit.file.name]
            return UnitMacros({"LEVEL": MacroDefinition("1", "", False)}, frozenset(root / f for f in files))

        units = [_unit(root, name) for name in ("a.c", "b.c", "c.c")]
        with transaction(conn):
            store_values_of_build(
                conn, CONFIG, units, root, manifest=manifest, flags_hashes=["fa", "fb", "fc"], answer_of=answer_of,
            )

        assert json.loads(_row(conn, "h.h", "LEVEL")["expanded_values"]) == [
            {"state": "defined", "units": 1, "value": "1", "params": "", "is_function_like": False},
            {"state": "no_answer", "units": 1},
        ]
        assert expanded_fields(_row(conn, "c.c", "LEVEL")) == {"expanded_value": "1"}
    finally:
        conn.close()


def test_a_dependency_file_that_cannot_be_removed_keeps_the_answer(tmp_path: Path, monkeypatch, caplog):
    """On Windows a scanner can hold the new file: the cleanup raised, and the macros step lost each unit."""
    source = tmp_path / "a.c"
    source.write_text("#define A 1\n", encoding="utf-8")

    written: list[Path] = []

    def clang(binary, arguments, *, timeout, cwd):
        written.append(Path(arguments[arguments.index("-MF") + 1]))
        written[-1].write_text(f"unit: {source}\n", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, b"#define A 1\n", b"")

    def held(path):
        raise PermissionError(13, "The process cannot access the file", str(path))

    monkeypatch.setattr(macros, "run_clang", clang)
    monkeypatch.setattr(macros, "remove_file", held)
    with caplog.at_level(logging.WARNING, logger=macros.log.name):
        answer = macros.definitions_at_end_of_unit([], source, clang_binary="clang", cwd=tmp_path)

    assert answer.definitions == {"A": MacroDefinition("1", "", False)}
    assert "stays in the temporary directory" in caplog.text
    for leftover in written:
        leftover.unlink(missing_ok=True)
        leftover.parent.rmdir()


def test_a_dependency_file_that_clang_did_not_write_is_named(tmp_path: Path, monkeypatch):
    """clang ended with 0 and wrote no dependency file: the reason says so, not "no rule" of an empty file."""
    source = tmp_path / "a.c"
    source.write_text("#define A 1\n", encoding="utf-8")
    monkeypatch.setattr(
        macros, "run_clang",
        lambda binary, arguments, *, timeout, cwd: subprocess.CompletedProcess(arguments, 0, b"#define A 1\n", b""),
    )

    with pytest.raises(NoMacroAnswer, match="wrote no dependency file"):
        macros.definitions_at_end_of_unit([], source, clang_binary="clang", cwd=tmp_path)
