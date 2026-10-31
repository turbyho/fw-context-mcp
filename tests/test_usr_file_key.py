"""The USR of a symbol with no external name names its file by a key of the path (``indexer._usr``).

libclang writes the base name of the file into such a USR (`c:util.h@F@helper`),
thus two files of one base name in two directories gave one symbol for two.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

SOURCES = {
    "a/util.h": "static int counter_value;\nstatic inline int helper(void) { return counter_value + 1; }\n",
    "b/util.h": "static int counter_value;\nstatic inline int helper(void) { return counter_value + 2; }\n",
    "a/x.c": '#include "util.h"\nint use_a(void) { return helper(); }\n',
    "a/x2.c": '#include "util.h"\nint use_a2(void) { return helper(); }\n',
    "b/y.c": '#include "util.h"\nint use_b(void) { return helper(); }\n',
    "a/fatal.c": (
        "static const int log_level = 3;\nint level_a(void) { return log_level; }\n"
        "static int scale(int factor) { int product = factor * 3; return product; }\n"
        "int scaled_a(void) { return scale(1); }\n"
    ),
    "b/fatal.c": (
        "static const int log_level = 4;\nint level_b(void) { return log_level; }\n"
        "static int scale(int factor) { int product = factor * 4; return product; }\n"
        "int scaled_b(void) { return scale(2); }\n"
    ),
    "a/n.cpp": "namespace { int hidden(int v) { return v + 1; } }\nint run_na() { return hidden(1); }\n",
    "b/n.cpp": "namespace { int hidden(int v) { return v + 2; } }\nint run_nb() { return hidden(2); }\n",
    "a/s.c": "static void tick(void) {}\nstatic void (*slot)(void) = tick;\nvoid fire_a(void) { slot(); }\n",
    "b/s.c": "static void tock(void) {}\nstatic void (*slot)(void) = tock;\nvoid fire_b(void) { slot(); }\n",
    # One header through `..`: the key of its real path.
    "c/z.c": '#include "../a/util.h"\nint use_z(void) { return helper(); }\n',
    # A C++ unit reads the header of a C unit: one symbol in the two languages.
    "a/x.cpp": '#include "util.h"\nint use_cpp() { return helper(); }\n',
    "a/l.cpp": "static auto pick = [](int v) { return v + 1; };\nint run_a() { return pick(1); }\n",
    "b/l.cpp": "static auto pick = [](int v) { return v + 1; };\nint run_b() { return pick(2); }\n",
}


def _project(root: Path) -> None:
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-usr"\n\n[build]\n\n[index]\ndb_dir = "{root.parent / "db"}"\n', encoding="utf-8",
    )
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    entries = []
    for name in SOURCES:
        if name.endswith(".c"):
            entries.append({"directory": str(root), "file": name, "arguments": ["cc", "-std=gnu11", "-c", name]})
        elif name.endswith(".cpp"):
            entries.append({"directory": str(root), "file": name, "arguments": ["c++", "-std=c++17", "-c", name]})
    (root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")


def _db(root: Path) -> Path:
    return root.parent / "db" / "proj-usr" / "index.db"


def _run(root: Path) -> None:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    _db(root).parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=_db(root), project_root=root,
        project_id="proj-usr", project_name="proj-usr", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )


def _rows(root: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(_db(root))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def indexed(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("usrkey") / "proj"
    root.mkdir()
    _project(root)
    _run(root)
    return root


def test_two_files_of_one_base_name_give_two_symbols(indexed):
    def files_of(name: str) -> list[str]:
        return sorted(row[0] for row in _rows(
            indexed, "SELECT file_path FROM symbols WHERE name = ? AND is_definition = 1", name,
        ))

    assert files_of("helper") == ["a/util.h", "b/util.h"]
    assert files_of("counter_value") == ["a/util.h", "b/util.h"]
    assert files_of("log_level") == ["a/fatal.c", "b/fatal.c"]
    assert files_of("scale") == ["a/fatal.c", "b/fatal.c"]
    assert files_of("product") == ["a/fatal.c", "b/fatal.c"], "a local of two static functions"
    assert files_of("hidden") == ["a/n.cpp", "b/n.cpp"], "a member of an anonymous namespace"


def test_each_caller_reaches_the_helper_of_its_own_header(indexed):
    calls = set(_rows(
        indexed,
        "SELECT a.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE r.ref_kind = 'call' AND b.name = 'helper'",
    ))
    assert calls == {
        ("use_a", "a/util.h"), ("use_a2", "a/util.h"), ("use_b", "b/util.h"),
        ("use_z", "a/util.h"), ("use_cpp", "a/util.h"),
    }


def test_a_slot_of_each_file_reaches_the_function_of_its_own_file(indexed):
    calls = set(_rows(
        indexed,
        "SELECT a.name, b.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE r.ref_kind = 'call' AND a.name LIKE 'fire_%'",
    ))
    # A call through a pointer variable is also a call edge to the variable.
    assert calls == {
        ("fire_a", "tick", "a/s.c"), ("fire_a", "slot", "a/s.c"),
        ("fire_b", "tock", "b/s.c"), ("fire_b", "slot", "b/s.c"),
    }


def test_a_header_that_two_units_read_stays_one_symbol(indexed):
    """`a/x.c` and `a/x2.c` read one `a/util.h`: one key, one symbol."""
    rows = _rows(indexed, "SELECT usr FROM symbols WHERE name = 'helper' AND file_path = 'a/util.h'")
    assert len(rows) == 1
    from fw_context_mcp.indexer._usr import file_key

    assert rows[0][0] == f"c:{file_key(str(indexed / 'a' / 'util.h'))}@F@helper"


def test_the_lambdas_of_two_files_of_one_base_name_are_two_symbols(indexed):
    rows = _rows(indexed, "SELECT file_path FROM symbols WHERE kind = 'lambda'")
    assert sorted(row[0] for row in rows) == ["a/l.cpp", "b/l.cpp"]


def test_a_header_through_a_symbolic_link_has_the_key_of_its_real_path(tmp_path):
    from fw_context_mcp.indexer._usr import file_key

    real = tmp_path / "inc"
    real.mkdir()
    (real / "util.h").write_text("static int value;\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic link on this file system")
    assert file_key(str(link / "util.h")) == file_key(str(real / "util.h"))


def test_an_external_name_keeps_the_usr_of_libclang(indexed):
    assert _rows(indexed, "SELECT usr FROM symbols WHERE name = 'use_a'") == [("c:@F@use_a",)]


class _Cursor:
    """A cursor with a USR and the file of its canonical declaration."""

    def __init__(self, usr: str, file: str) -> None:
        self.usr = usr
        self.location = type("Location", (), {"file": type("File", (), {"name": file})()})()
        self.canonical = self

    def get_usr(self) -> str:
        return self.usr


def test_the_file_of_the_canonical_declaration_names_the_key():
    from fw_context_mcp.indexer._usr import cursor_usr, file_key

    cursor = _Cursor("c:helper.c@F@helper", "/src/a/helper.c")
    assert cursor_usr(cursor) == f"c:{file_key('/src/a/helper.c')}@F@helper"


def test_a_prefix_that_is_not_the_file_of_the_canonical_declaration_stays_as_libclang_wrote_it():
    """The base name of the file of the canonical declaration is not the prefix: no guess."""
    from fw_context_mcp.indexer._usr import cursor_usr

    assert cursor_usr(_Cursor("c:missing.c@F@helper", "/src/other.c")) == "c:missing.c@F@helper"
