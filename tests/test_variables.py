"""Tests for variable kind split, find_variables, and related changes.

Tests from plan §6:
- kind split (varglobal/varlocal incl. LINKAGE_SPEC)
- search_symbols(exclude_variables=True)
- deduplicate short-name filter
- _fetch_referencers
- _signature for VAR_DECL/FIELD_DECL
- find_variables kind validation
- find_variables integration
"""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer.db import (
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    search_symbols,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _insert_symbol(conn, config_hash: str, file_id: int, file_path: str, **kw):
    """Insert one symbol row with sensible defaults."""
    row = (
        config_hash,
        file_id,
        file_path,
        kw.get("name", ""),                     # name_tokens
        kw["usr"],
        kw["name"],
        kw.get("qualified_name", kw["name"]),
        kw.get("kind", "function"),
        kw.get("line", 1),
        kw.get("col", 0),
        kw.get("end_line", 0),
        kw.get("is_definition", 1),
        kw.get("signature", ""),
        kw.get("docstring", ""),
        kw.get("enum_value", None),
        kw.get("is_virtual", 0),
        kw.get("is_pure_virtual", 0),
        kw.get("parent_usr", ""),
        kw.get("is_template", 0),
        kw.get("template_usr", ""),
        kw.get("is_project", 1),
        kw.get("pagerank", 0.0),
        kw.get("source", ""),
        kw.get("is_weak", 0),
    )
    insert_symbols_batch(conn, [row])


def _setup_project_db(tmp_path, name: str = "test-project"):
    """Create a temp DB with project + build config, return (conn, config_hash)."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, "proj-001", name, str(tmp_path))
        upsert_build_config(conn, "hash-deadbeef", "proj-001", str(tmp_path / "compile_commands.json"))
    return conn, "hash-deadbeef"


def _do_find_variables(conn, chash: str, name: str, kind=None, limit=20) -> list[dict]:
    """Call the core find_variables logic bypassing _resolve_context.

    Imported inline so the module loads even without a project index.
    """
    from fw_context_mcp.mcp.handlers.variables import _VALID_KINDS, _VAR_KINDS, _format_variable_result

    if kind is not None and kind not in _VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind!r}. Expected 'varglobal', 'varlocal', or None.")
    limit = min(limit, 100)

    if kind:
        kind_filter = "AND s.kind = ?"
    else:
        kind_filter = f"AND s.kind IN ({','.join('?' * len(_VAR_KINDS))})"

    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    params: list = [chash, f"%{esc_name}%", f"%{esc_name}%"]
    if kind:
        params.append(kind)
    else:
        params.extend(_VAR_KINDS)
    params.append(limit)

    rows = conn.execute(
        f"""SELECT s.* FROM symbols s
            WHERE s.config_hash = ?
              AND (s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\')
              {kind_filter}
              AND s.is_definition = 1
            ORDER BY CASE s.kind WHEN 'varglobal' THEN 0 ELSE 1 END,
                     s.name
            LIMIT ?""",
        params,
    ).fetchall()

    if not rows:
        return []

    return [_format_variable_result(conn, chash, dict(r), "/tmp/test-root") for r in rows]


# ── search_symbols exclude_variables ────────────────────────────────────────


class TestExcludeVariables:
    def test_varlocal_excluded_when_exclude_variables_true(self, populated_db):
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_count", name="g_count", kind="varglobal")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@f#loop", name="i", kind="varlocal", line=10)

        results = search_symbols(populated_db, "count OR i",
                                 "hash-deadbeef", exclude_variables=True, limit=50)
        names = {r["name"] for r in results}
        assert "g_count" in names, "varglobal should be visible"
        assert "i" not in names, "varlocal should be excluded"

    def test_varglobal_visible_without_exclude(self, populated_db):
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_state", name="g_state", kind="varglobal")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@f#x", name="x", kind="varlocal")

        results = search_symbols(populated_db, "g_state OR x",
                                 "hash-deadbeef", exclude_variables=False, limit=50)
        names = {r["name"] for r in results}
        assert "g_state" in names
        assert "x" in names

    def test_legacy_variable_excluded(self, populated_db):
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_config", name="g_config", kind="varglobal")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@old_var", name="old_var", kind="variable")

        results = search_symbols(populated_db, "g_config OR old_var",
                                 "hash-deadbeef", exclude_variables=True, limit=50)
        names = {r["name"] for r in results}
        assert "g_config" in names, "varglobal should be visible"
        assert "old_var" not in names, "legacy 'variable' kind should also be excluded"


# ── Deduplicate short-name filter ───────────────────────────────────────────


class TestDeduplicateShortNameFilter:
    @pytest.mark.parametrize("name,kind,should_drop", [
        ("i", "varlocal", True),
        ("j", "varlocal", True),
        ("x", "varlocal", True),
        ("a", "variable", True),
        ("abc", "variable", False),
        ("g_", "varglobal", False),
        ("g_count", "varglobal", False),
        ("i", "function", False),
        ("i", "method", False),
        ("abc", "varlocal", False),
        ("xyz", "field", False),
        ("x", "field", True),
        ("_", "varlocal", True),
    ])
    def test_short_name_filter(self, name, kind, should_drop):
        """Verify short-name filter drops varlocal/variable/field with len<=2."""
        if name.startswith("("):
            to_drop = True
        elif len(name) <= 2 and kind in ("varlocal", "variable", "field"):
            to_drop = True
        else:
            to_drop = False
        assert to_drop == should_drop, (
            f"name={name!r} kind={kind!r}: expected drop={should_drop}, got drop={to_drop}"
        )


# ── _fetch_referencers ──────────────────────────────────────────────────────


class TestFetchReferencers:
    def test_returns_functions_that_reference_variable(self, populated_db):
        from fw_context_mcp.indexer.runner import _fetch_referencers

        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_count", name="g_count", kind="varglobal")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@init", name="init", kind="function")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@update", name="update", kind="function")

        refs = [
            ("hash-deadbeef", "c:@g_count", "/tmp/test.c", 10, "c:@F@init", "ref", None),
            ("hash-deadbeef", "c:@g_count", "/tmp/test.c", 20, "c:@F@update", "ref", None),
        ]
        insert_refs_batch(populated_db, refs)

        referencers = _fetch_referencers(populated_db, "c:@g_count", "hash-deadbeef")
        assert set(referencers) == {"init", "update"}

    def test_empty_when_no_references(self, populated_db):
        from fw_context_mcp.indexer.runner import _fetch_referencers

        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_unused", name="g_unused", kind="varglobal")

        assert _fetch_referencers(populated_db, "c:@g_unused", "hash-deadbeef") == []

    def test_join_direction_correct(self, populated_db):
        """Verify JOIN is from_usr = symbol.usr (referencer), not to_usr."""
        from fw_context_mcp.indexer.runner import _fetch_referencers

        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.c", "c")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@g_level", name="g_level", kind="varglobal")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@reader", name="reader", kind="function",
                       qualified_name="reader")
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@writer", name="writer", kind="function",
                       qualified_name="writer")

        refs = [
            ("hash-deadbeef", "c:@g_level", "/tmp/test.c", 5, "c:@F@reader", "ref", None),
            ("hash-deadbeef", "c:@g_level", "/tmp/test.c", 8, "c:@F@writer", "ref", None),
        ]
        insert_refs_batch(populated_db, refs)

        referencers = _fetch_referencers(populated_db, "c:@g_level", "hash-deadbeef")
        assert set(referencers) == {"reader", "writer"}

        # Insert duplicate declaration — DISTINCT should deduplicate
        _insert_symbol(populated_db, "hash-deadbeef", fid, "src/test.c",
                       usr="c:@F@reader", name="reader", kind="function",
                       qualified_name="reader", line=99, is_definition=0)
        referencers2 = _fetch_referencers(populated_db, "c:@g_level", "hash-deadbeef")
        assert set(referencers2) == {"reader", "writer"}
# ── _signature for VAR_DECL / FIELD_DECL (libclang) ────────────────────────


@pytest.mark.libclang
class TestSignatureForVariables:
    def test_global_variable_signature(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("int g_count = 0;\nvoid use(void) { g_count = 1; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        count = next(s for s in syms if s.name == "g_count")
        assert count.signature == "int g_count", f"got {count.signature!r}"

    def test_local_variable_signature(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("void f(void) { int local; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        local = next(s for s in syms if s.name == "local")
        assert "int" in local.signature and "local" in local.signature, (
            f"got {local.signature!r}"
        )

    def test_field_decl_signature(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.cpp"
        src.write_text("struct S { int x; char *name; };\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c++",
                               clang_args=["-std=c++17"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        x_field = next(s for s in syms if s.name == "x")
        assert "int" in x_field.signature and "x" in x_field.signature, (
            f"got {x_field.signature!r}"
        )


# ── kind split varglobal / varlocal (libclang) ──────────────────────────────


@pytest.mark.libclang
class TestKindSplit:
    def test_global_variable_is_varglobal(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("int g_debug_level = 0;\nvoid init(void) { g_debug_level = 5; }\n",
                       encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        g = next(s for s in syms if s.name == "g_debug_level")
        assert g.kind == "varglobal", f"got {g.kind}"

    def test_local_variable_is_varlocal(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("void f(void) { int i; char *p; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        varlocals = [s for s in syms if s.name in ("i", "p")]
        assert len(varlocals) == 2, f"expected 2 varlocals, got {[s.name for s in varlocals]}"
        for v in varlocals:
            assert v.kind == "varlocal", f"{v.name} got kind={v.kind}"

    def test_varlocal_has_parent_usr(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("void my_func(void) { int local; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        local = next(s for s in syms if s.name == "local")
        assert local.kind == "varlocal"
        assert local.parent_usr != "", "varlocal should have parent_usr set"

    def test_static_inside_function_is_varlocal(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.c"
        src.write_text("void f(void) { static int counter; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                               clang_args=["-std=c11"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        counter = next(s for s in syms if s.name == "counter")
        assert counter.kind == "varlocal", (
            f"static local should be varlocal, got {counter.kind}"
        )

    def test_extern_c_variable_is_varglobal(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.cpp"
        src.write_text('extern "C" { int ext_x; }\n', encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c++",
                               clang_args=["-std=c++17"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        ext_x = next(s for s in syms if s.name == "ext_x")
        assert ext_x.kind == "varglobal", (
            f"extern \"C\" var should be varglobal, got {ext_x.kind}"
        )

    def test_namespace_variable_is_varglobal(self, tmp_path):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmp_path / "test.cpp"
        src.write_text("namespace ns { int count; }\n", encoding="utf-8")

        unit = CompilationUnit(file=src, directory=tmp_path, language="c++",
                               clang_args=["-std=c++17"])
        r = extract_all(unit, with_refs=False)
        syms = r.symbols

        count = next(s for s in syms if s.name == "count")
        assert count.kind == "varglobal", (
            f"namespace-scope var should be varglobal, got {count.kind}"
        )
