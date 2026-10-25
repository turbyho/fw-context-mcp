"""The link name: what an assembly unit names when it names a symbol.

A vector slot, an alias and a branch of assembly name a symbol as the
linker does.  `symbols.name` cannot answer that: `main` of C++ has the USR
`c:@F@main#`, a C++ method `write` is no target of `bl write`, and a
`static` function is no target of another object.  The values below were
measured with libclang 22 (probe s9 of the call edge plan).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

_CPP = """\
int main() { return 0; }
extern "C" void SystemInit(void) {}
void cpp_func(int) {}
static void cpp_static() {}
namespace { void anon_fn() {} }
struct Uart { int write(int); };
int Uart::write(int) { return 0; }
int global_var;
static int static_var;
template <class T> void tmpl(T) {}
template <class T> struct Box { static int count; };
template <class T> int Box<T>::count = 0;
void use() { tmpl(1); cpp_static(); anon_fn(); }
"""

_C = """\
int main(void) { return 0; }
static void helper(void) {}
int global_var = 1;
static int static_var = 2;
void use(void) { helper(); }
"""


def _extract(tmp_path: Path, name: str, code: str, language: str) -> dict[str, str]:
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / name
    src.write_text(code, encoding="utf-8")
    args = ["-std=c++17"] if language == "cpp" else []
    unit = CompilationUnit(file=src, directory=tmp_path, language=language, clang_args=args)
    return {s.usr: s.link_name for s in extract_all(unit).symbols}


@pytest.mark.libclang
class TestExtraction:
    def test_cpp(self, tmp_path: Path):
        names = _extract(tmp_path, "a.cpp", _CPP, "cpp")

        assert names["c:@F@main#"] == "main"
        assert names["c:@F@SystemInit"] == "SystemInit"
        assert names["c:@F@cpp_func#I#"] == "_Z8cpp_funci"
        assert names["c:@S@Uart@F@write#I#"] == "_ZN4Uart5writeEi"
        assert names["c:@global_var"] == "global_var"
        # Internal linkage: no other object names it.
        assert names["c:a.cpp@F@cpp_static#"] == ""
        assert names["c:a.cpp@aN@F@anon_fn#"] == ""
        assert names["c:a.cpp@static_var"] == ""
        # A type has no link name.
        assert names["c:@S@Uart"] == ""
        # A member of a class template is a template: no object holds it.
        assert names["c:@ST>1#T@Box@count"] == ""

    def test_c(self, tmp_path: Path):
        names = _extract(tmp_path, "b.c", _C, "c")

        assert names["c:@F@main"] == "main"
        assert names["c:@global_var"] == "global_var"
        assert names["c:b.c@F@helper"] == ""
        assert names["c:b.c@static_var"] == ""


def _db(tmp_path: Path, rows: list[tuple[str, str, str, int, int]]):
    """Index *rows* as (usr, name, link_name, is_definition, is_weak)."""
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
        fid = upsert_file(conn, "ch", "src/a.c", "c")
        insert_symbols_batch(conn, [
            ("ch", fid, "src/a.c", name, usr, name, name, "function", 10, 1, 12,
             definition, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", weak, link_name)
            for usr, name, link_name, definition, weak in rows
        ])
    return conn


class TestResolution:
    @staticmethod
    def _resolve(tmp_path: Path, rows, name: str, unit_names=None):
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = _db(tmp_path, rows)
        try:
            return _resolve_handler(conn, "ch", name, unit_names or {})
        finally:
            conn.close()

    def test_main_of_cpp_is_found_by_its_link_name(self, tmp_path: Path):
        """`bl main` of a startup reaches `main` of C++, whose USR ends in `#`."""
        assert self._resolve(
            tmp_path, [("c:@F@main#", "main", "main", 1, 0)], "main"
        ) == "c:@F@main#"

    def test_a_method_of_the_same_name_is_no_target(self, tmp_path: Path):
        """A C++ method `write` is `_ZN4Uart5writeEi` to the linker."""
        assert self._resolve(
            tmp_path, [("c:@S@Uart@F@write#I#", "write", "_ZN4Uart5writeEi", 1, 0)], "write"
        ) is None

    def test_a_static_function_is_no_target(self, tmp_path: Path):
        assert self._resolve(
            tmp_path, [("c:a.c@F@helper", "helper", "", 1, 0)], "helper"
        ) is None

    def test_a_static_function_does_not_hide_the_external_one(self, tmp_path: Path):
        """Once these two were two candidates of one name, and none won."""
        assert self._resolve(tmp_path, [
            ("c:a.c@F@handler", "handler", "", 1, 0),
            ("c:@F@handler", "handler", "handler", 1, 0),
        ], "handler") == "c:@F@handler"

    def test_a_name_of_the_unit_is_its_own(self, tmp_path: Path):
        """The assembler resolves a name that its unit defines."""
        assert self._resolve(
            tmp_path, [("c:@F@Default_Handler", "Default_Handler", "Default_Handler", 1, 0)],
            "Default_Handler", {"Default_Handler": "asm:startup.S@Default_Handler"},
        ) == "asm:startup.S@Default_Handler"

    def test_a_declaration_stands_for_a_library_definition(self, tmp_path: Path):
        """`longjmp` is defined in a library: the C declaration is the symbol."""
        assert self._resolve(
            tmp_path, [("c:@F@longjmp", "longjmp", "longjmp", 0, 0)], "longjmp"
        ) == "c:@F@longjmp"

    def test_a_definition_wins_over_a_declaration(self, tmp_path: Path):
        assert self._resolve(tmp_path, [
            ("c:@F@main", "main", "main", 0, 0),
            ("c:@F@main#", "main", "main", 1, 0),
        ], "main") == "c:@F@main#"

    def test_two_declarations_of_two_symbols_are_refused(self, tmp_path: Path):
        assert self._resolve(tmp_path, [
            ("c:@F@f", "f", "f", 0, 0),
            ("asm:@f", "f", "f", 0, 0),
        ], "f") is None


class TestAssemblyLabels:
    def test_only_an_exported_label_has_a_link_name(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units

        from .test_asm_indexing import TestStoreUnits, _unit

        unit = _unit(
            tmp_path, "s.S",
            ".global Exported\nExported:\n  nop\nLocal:\n  nop\n.weak Weak\nWeak:\n  nop\n",
        )
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            names = dict(conn.execute("SELECT name, link_name FROM symbols").fetchall())
        finally:
            conn.close()

        assert names == {"Exported": "Exported", "Local": "", "Weak": "Weak"}


class TestLinkerScript:
    _SCRIPT = "SECTIONS\n{\n    end = .;\n    __StackTop = 0x20040000;\n}\n"

    def test_a_script_symbol_has_its_name_as_link_name(self, tmp_path: Path):
        from fw_context_mcp.indexer.linker_script import store_scripts

        script = tmp_path / "app.ld"
        script.write_text(self._SCRIPT, encoding="utf-8")
        conn = _db(tmp_path, [])
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [script], tmp_path)
            names = dict(conn.execute(
                "SELECT name, link_name FROM symbols WHERE usr LIKE 'ld:%'"
            ).fetchall())
        finally:
            conn.close()

        assert names["__StackTop"] == "__StackTop"

    def test_a_static_variable_of_the_same_name_does_not_hide_it(self, tmp_path: Path):
        """A `static int end` in C is no definition of the linker symbol `end`."""
        from fw_context_mcp.indexer.linker_script import store_scripts

        script = tmp_path / "app.ld"
        script.write_text(self._SCRIPT, encoding="utf-8")
        conn = _db(tmp_path, [("c:a.c@end", "end", "", 1, 0)])
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [script], tmp_path)
            stored = {row[0] for row in conn.execute(
                "SELECT name FROM symbols WHERE usr LIKE 'ld:%'"
            )}
        finally:
            conn.close()

        assert "end" in stored
