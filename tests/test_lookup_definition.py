"""Tests for _lookup_definition — preferred_kinds priority ordering.

Verifies that the ``preferred_kinds`` parameter correctly resolves
class+constructor name collisions while not breaking other lookups,
and that ``preferred_kinds=None`` disables kind priority.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    split_tokens,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.mcp.handlers.source import _lookup_definition

# ── helpers ──────────────────────────────────────────────────────────────────

def _insert_class_and_constructor(
    conn,
    config_hash: str,
    class_line: int = 5,    # .h file — later line
    ctor_line: int = 3,     # .cpp file — earlier line (the bug trigger)
    ctor_is_definition: int = 1,
    class_is_definition: int = 1,
    namespace: str = "",
):
    """Insert a class Foo and its constructor Foo::Foo into *conn*.

    By default the constructor has a lower line number (3 < 5) which was
    the original bug trigger — ``ORDER BY s.line`` returned the constructor.
    """
    ns_prefix = f"{namespace}::" if namespace else ""
    class_qname = f"{ns_prefix}Foo"
    ctor_qname = f"{class_qname}::Foo"

    file_h = upsert_file(conn, config_hash, "include/foo.h", "cpp")
    file_cpp = upsert_file(conn, config_hash, "src/foo.cpp", "cpp")

    insert_symbols_batch(conn, [
        # Class definition in header
        (config_hash, file_h, "include/foo.h",
         split_tokens("Foo", class_qname),
         "U_Foo_class", "Foo", class_qname, "class",
         class_line, 1, 30, class_is_definition,
         "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        # Constructor definition in .cpp
        (config_hash, file_cpp, "src/foo.cpp",
         split_tokens("Foo", ctor_qname),
         "U_Foo_ctor", "Foo", ctor_qname, "constructor",
         ctor_line, 1, 15, ctor_is_definition,
         "Foo(int x)", "", None, 0, 0, "U_Foo_class", 0, "", 0, 0.0, ""),
    ])


def _setup_db(tmp_path, db_filename="test.db"):
    """Create a test database with a project and build config."""
    db_path = tmp_path / db_filename
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, "proj-001", "test-project", str(tmp_path))
        upsert_build_config(conn, "hash-deadbeef", "proj-001", "/tmp/compile_commands.json")
    return conn, "hash-deadbeef"


def _init_project_config(root, db_dir_override=None):
    """Create .fw-context/config.toml with a project ID — required by tools
    that call _resolve_context() (get_inheritance_chain, get_class_members, etc.)."""
    config_dir = root / ".fw-context"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_toml = config_dir / "config.toml"

    # By default, point db_dir to the temp directory so our test DB is found.
    # _resolve_context computes: db_dir / project_id / "index.db"
    if db_dir_override is None:
        db_dir_override = str(root)

    config_toml.write_text(
        "[project]\n"
        "id = \"proj-001\"\n"
        "\n"
        "[build]\n"
        "\n"
        "[index]\n"
        f"db_dir = \"{db_dir_override}\"\n",
        encoding="utf-8",
    )


# ── direct _lookup_definition tests ──────────────────────────────────────────

class TestLookupDefinitionClassVsConstructor:
    """Core fix: class/struct must win over constructor with same name."""

    def test_class_wins_over_constructor_same_name_definition(self, tmp_path):
        """When both class Foo and Foo::Foo() match name='Foo' with is_definition=1,
        _lookup_definition returns the class, not the constructor."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=5, ctor_line=3)

            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "class", (
                f"Expected 'class', got '{row['kind']}' — "
                f"constructor at line {row['line'] if row['kind'] == 'constructor' else '?'} "
                f"should not beat class at line 5"
            )
            assert row["qualified_name"] in ("Foo", "ns::Foo")
        finally:
            conn.close()

    def test_class_wins_when_constructor_has_lower_line(self, tmp_path):
        """Constructor in .cpp at line 3 < class in .h at line 10."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=10, ctor_line=3)

            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_class_wins_when_constructor_has_higher_line(self, tmp_path):
        """Even when constructor line > class line, class should still win."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=3, ctor_line=20)

            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_struct_wins_over_constructor(self, tmp_path):
        """Same priority for struct as class."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_h = upsert_file(conn, ch, "include/bar.h", "cpp")
            file_cpp = upsert_file(conn, ch, "src/bar.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_h, "include/bar.h",
                 split_tokens("Bar", "Bar"),
                 "U_Bar_struct", "Bar", "Bar", "struct",
                 8, 1, 30, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_cpp, "src/bar.cpp",
                 split_tokens("Bar", "Bar::Bar"),
                 "U_Bar_ctor", "Bar", "Bar::Bar", "constructor",
                 2, 1, 10, 1, "Bar()", "", None, 0, 0, "U_Bar_struct", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "Bar")
            assert row is not None
            assert row["kind"] == "struct"
        finally:
            conn.close()


class TestLookupDefinitionFallbackNamespace:
    """Fallback path with ``::`` in name — suffix-filtered lookup."""

    def test_namespace_class_wins_over_constructor(self, tmp_path):
        """Looking up 'ns::Foo' with fallback should return class, not ctor."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, namespace="ns")

            row = _lookup_definition(conn, ch, "ns::Foo")
            assert row is not None
            assert row["kind"] == "class", (
                f"Fallback lookup 'ns::Foo' returned {row['kind']} "
                f"(qualified_name={row['qualified_name']})"
            )
        finally:
            conn.close()

    def test_qualified_constructor_still_found(self, tmp_path):
        """Looking up fully qualified 'Foo::Foo' should still find the constructor
        (only the constructor's qualified_name matches exactly)."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch)

            row = _lookup_definition(conn, ch, "Foo::Foo")
            assert row is not None
            assert row["kind"] == "constructor", (
                f"Fully qualified 'Foo::Foo' should return constructor, got {row['kind']}"
            )
        finally:
            conn.close()

    def test_namespace_qualified_constructor(self, tmp_path):
        """'ns::Foo::Foo' should find the constructor via qualified_name match."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, namespace="ns")

            row = _lookup_definition(conn, ch, "ns::Foo::Foo")
            assert row is not None
            assert row["kind"] == "constructor"
        finally:
            conn.close()


class TestLookupDefinitionNoCollision:
    """Regular lookups without class/constructor collision — no regression."""

    def test_function_lookup_unchanged(self, tmp_path):
        """Lookup of a plain function still works."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/main.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/main.cpp",
                 split_tokens("main", "main"),
                 "U_main", "main", "main", "function",
                 1, 1, 5, 1, "int main()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "main")
            assert row is not None
            assert row["kind"] == "function"
            assert row["name"] == "main"
        finally:
            conn.close()

    def test_method_lookup_unchanged(self, tmp_path):
        """Lookup of a method (without class collision) still works."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/widget.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/widget.cpp",
                 split_tokens("render", "Widget::render"),
                 "U_render", "render", "Widget::render", "method",
                 42, 3, 50, 1, "void render()", "", None, 0, 0, "U_Widget", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "render")
            assert row is not None
            assert row["kind"] == "method"
        finally:
            conn.close()

    def test_enum_lookup_unchanged(self, tmp_path):
        """Lookup of an enum still works."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/types.h", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/types.h",
                 split_tokens("Color", "Color"),
                 "U_Color", "Color", "Color", "enum",
                 5, 1, 10, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "Color")
            assert row is not None
            assert row["kind"] == "enum"
        finally:
            conn.close()

    def test_class_only_no_constructor(self, tmp_path):
        """Class without a constructor in the index returns the class."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "include/baz.h", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "include/baz.h",
                 split_tokens("Baz", "Baz"),
                 "U_Baz", "Baz", "Baz", "class",
                 3, 1, 40, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "Baz")
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_constructor_only_no_class(self, tmp_path):
        """Constructor without a class in the index returns the constructor
        (it's the only match — CASE priority doesn't filter, just orders)."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/qux.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/qux.cpp",
                 split_tokens("Qux", "Qux::Qux"),
                 "U_Qux_ctor", "Qux", "Qux::Qux", "constructor",
                 5, 1, 15, 1, "Qux(int x)", "", None, 0, 0, "U_Qux_class", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "Qux")
            assert row is not None
            assert row["kind"] == "constructor", (
                f"Constructor is the only match — should be returned, got {row['kind']}"
            )
        finally:
            conn.close()


class TestLookupDefinitionEdgeCases:
    """Edge cases and potential side effects."""

    def test_prefers_definition_over_declaration_then_kind(self, tmp_path):
        """is_definition=1 sorts before CASE priority.
        A declaration-class should NOT beat a definition-constructor.
        But with is_definition=1 on both, CASE priority applies."""
        conn, ch = _setup_db(tmp_path)
        try:
            # Class declaration (is_definition=0) + constructor definition
            _insert_class_and_constructor(
                conn, ch, class_is_definition=0, ctor_is_definition=1,
            )

            row = _lookup_definition(conn, ch, "Foo")
            # is_definition=1 constructor wins because is_definition sorts first
            # in the non-restricted query, and in the restricted query
            # (is_definition=1) the constructor is the only match.
            # But the restricted query runs first, so the constructor is the only match.
            assert row is not None
            assert row["kind"] == "constructor", (
                "Constructor is the only definition — should win"
            )
        finally:
            conn.close()

    def test_definition_filter_removes_class_declaration(self, tmp_path):
        """When the class is only a declaration (is_definition=0) and the
        constructor is a definition, the constructor wins because the first
        query filters to is_definition=1."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(
                conn, ch,
                class_line=3, ctor_line=20,
                class_is_definition=0, ctor_is_definition=1,
            )

            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "constructor", (
                "Constructor is the only is_definition=1 match in restricted query"
            )
        finally:
            conn.close()

    def test_class_wins_over_function_with_same_name(self, tmp_path):
        """C code: struct X and function X() — struct wins due to CASE priority.
        This is the potential side effect for C projects noted in review."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/stat.c", "c")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/stat.c",
                 split_tokens("stat", "stat"),
                 "U_stat_struct", "stat", "stat", "struct",
                 10, 1, 20, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_id, "src/stat.c",
                 split_tokens("stat", "stat"),
                 "U_stat_func", "stat", "stat", "function",
                 30, 1, 40, 1, "int stat()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])

            # struct wins because of CASE priority — this is intentional
            # for C++ class/constructor, but may surprise C users.
            row = _lookup_definition(conn, ch, "stat")
            assert row is not None
            assert row["kind"] == "struct", (
                f"struct should win over function due to CASE priority; "
                f"got {row['kind']}. If C users need the function, they should "
                f"use the qualified name or a kind-aware lookup."
            )
        finally:
            conn.close()

    def test_typedef_does_not_beat_class(self, tmp_path):
        """typedef Foo and class Foo — class wins."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/types.h", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/types.h",
                 split_tokens("Foo", "Foo"),
                 "U_Foo_class", "Foo", "Foo", "class",
                 5, 1, 10, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_id, "src/types.h",
                 split_tokens("Foo", "Foo"),
                 "U_Foo_typedef", "Foo", "Foo", "typedef",
                 1, 1, 0, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()


class TestPreferredKindsParameter:
    """Tests for the preferred_kinds parameter of _lookup_definition."""

    def test_default_uses_class_struct_priority(self, tmp_path):
        """Default preferred_kinds=("class", "struct") — class wins."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=5, ctor_line=3)
            # Default — no explicit preferred_kinds
            row = _lookup_definition(conn, ch, "Foo")
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_explicit_class_struct_priority(self, tmp_path):
        """Explicit preferred_kinds=("class", "struct") works same as default."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=5, ctor_line=3)
            row = _lookup_definition(conn, ch, "Foo",
                                     preferred_kinds=("class", "struct"))
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_none_disables_priority_constructor_wins_by_line(self, tmp_path):
        """preferred_kinds=None — no CASE priority, constructor wins by lower line."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=5, ctor_line=3)
            row = _lookup_definition(conn, ch, "Foo", preferred_kinds=None)
            assert row is not None
            assert row["kind"] == "constructor", (
                f"Without kind priority, constructor at line 3 should win over class at line 5; "
                f"got {row['kind']} at line {row['line']}"
            )
        finally:
            conn.close()

    def test_custom_preferred_kinds(self, tmp_path):
        """Custom preferred_kinds=("function",) prefers functions over classes."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/stat.c", "c")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/stat.c",
                 split_tokens("stat", "stat"),
                 "U_stat_struct", "stat", "stat", "struct",
                 10, 1, 20, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_id, "src/stat.c",
                 split_tokens("stat", "stat"),
                 "U_stat_func", "stat", "stat", "function",
                 30, 1, 40, 1, "int stat()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ])
            # Default: struct wins
            row_default = _lookup_definition(conn, ch, "stat")
            assert row_default["kind"] == "struct"

            # Custom: function wins
            row_custom = _lookup_definition(conn, ch, "stat",
                                            preferred_kinds=("function",))
            assert row_custom["kind"] == "function", (
                f"With preferred_kinds=('function',), function should win; "
                f"got {row_custom['kind']}"
            )
        finally:
            conn.close()

    def test_single_preferred_kind(self, tmp_path):
        """Single-element preferred_kinds tuple works."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch, class_line=5, ctor_line=3)
            row = _lookup_definition(conn, ch, "Foo",
                                     preferred_kinds=("class",))
            assert row is not None
            assert row["kind"] == "class"
        finally:
            conn.close()

    def test_invalid_kind_with_quote_raises(self, tmp_path):
        """preferred_kinds value containing a quote raises ValueError."""
        conn, ch = _setup_db(tmp_path)
        try:
            _insert_class_and_constructor(conn, ch)
            with pytest.raises(ValueError, match="Invalid preferred_kinds"):
                _lookup_definition(conn, ch, "Foo",
                                   preferred_kinds=("cl'ass",))
        finally:
            conn.close()


# ── integration helpers ──────────────────────────────────────────────────────


def _setup_integration(tmp_path):
    """Set up a project that _resolve_context() can find.

    _resolve_context computes ``db_dir / project_id / "index.db"`` from config.
    We point db_dir at tmp_path so the DB lands at ``tmp_path / proj-001 / index.db``.
    """
    _init_project_config(tmp_path, db_dir_override=str(tmp_path))
    # Create the DB and close immediately — tools open their own connection
    conn, ch = _setup_db(tmp_path, db_filename="proj-001/index.db")
    conn.close()
    return ch


def _with_data(tmp_path, insert_fn):
    """Reopen the integration DB, call *insert_fn(conn, config_hash)*, checkpoint, close."""
    db_path = tmp_path / "proj-001" / "index.db"
    from fw_context_mcp.indexer.db import open_db as _open
    conn = _open(db_path)
    try:
        insert_fn(conn, "hash-deadbeef")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


# ── integration tests — tools that call _lookup_definition ───────────────────

class TestIntegrationInheritanceChain:
    """get_inheritance_chain uses _lookup_definition and checks kind."""

    def test_get_inheritance_chain_with_class_and_constructor(self, tmp_path):
        """get_inheritance_chain('Foo') should succeed when both class and
        constructor exist in the index."""
        from fw_context_mcp.mcp.handlers.inheritance import get_inheritance_chain

        _setup_integration(tmp_path)
        _with_data(tmp_path, _insert_class_and_constructor)

        result = get_inheritance_chain(class_name="Foo", project_root=str(tmp_path))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["kind"] == "class"
        assert "bases" in result
        assert "derived" in result

    def test_get_class_members_with_class_and_constructor(self, tmp_path):
        """get_class_members('Foo') should succeed when both class and
        constructor exist in the index."""
        from fw_context_mcp.mcp.handlers.inheritance import get_class_members

        def _insert(conn, ch):
            file_h = upsert_file(conn, ch, "include/foo.h", "cpp")
            file_cpp = upsert_file(conn, ch, "src/foo.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_h, "include/foo.h",
                 split_tokens("Foo", "Foo"),
                 "U_Foo_class", "Foo", "Foo", "class",
                 5, 1, 30, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_cpp, "src/foo.cpp",
                 split_tokens("Foo", "Foo::Foo"),
                 "U_Foo_ctor", "Foo", "Foo::Foo", "constructor",
                 3, 1, 15, 1, "Foo()", "", None, 0, 0, "U_Foo_class", 0, "", 0, 0.0, ""),
                (ch, file_h, "include/foo.h",
                 split_tokens("init", "Foo::init"),
                 "U_Foo_init", "init", "Foo::init", "method",
                 8, 3, 10, 1, "int init()", "", None, 0, 0, "U_Foo_class", 0, "", 0, 0.0, ""),
            ])

        _setup_integration(tmp_path)
        _with_data(tmp_path, _insert)

        result = get_class_members(class_name="Foo", project_root=str(tmp_path))
        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["kind"] == "class"
        assert "members" in result
        all_members = []
        for kind_group in result["members"].values():
            all_members.extend(m["name"] for m in kind_group)
        assert "init" in all_members, f"Method 'init' not found in members: {all_members}"


# ── prefer_project tests ─────────────────────────────────────────────────────


class TestPreferProjectParameter:
    """``prefer_project=True`` prioritises project symbols over SDK symbols."""

    def test_prefer_project_wins_over_sdk_same_name(self, tmp_path):
        """Two 'Foo' definitions — project (is_project=1) wins over SDK (is_project=0)
        with prefer_project=True, even though the SDK symbol has a lower line number."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_sdk = upsert_file(conn, ch, "mbed-os/foo.h", "cpp")
            file_proj = upsert_file(conn, ch, "src/foo.h", "cpp")
            # SDK symbol — earlier line, is_project=0
            # Project symbol — later line, is_project=1
            insert_symbols_batch(conn, [
                (ch, file_sdk, "mbed-os/foo.h",
                 split_tokens("Foo", "Foo"),
                 "U_Foo_sdk", "Foo", "Foo", "class",
                 10, 1, 30, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_proj, "src/foo.h",
                 split_tokens("Foo", "Foo"),
                 "U_Foo_proj", "Foo", "Foo", "class",
                 50, 1, 30, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
            ])

            # prefer_project=True: project wins despite higher line
            row = _lookup_definition(conn, ch, "Foo", prefer_project=True)
            assert row is not None
            assert row["kind"] == "class"
            assert row["is_project"] == 1, (
                f"Expected project symbol (is_project=1), "
                f"got is_project={row['is_project']}, file={row['file_path']}"
            )
            assert row["file_path"] == "src/foo.h"

            # prefer_project=False (default): old behavior — SDK wins by line
            row_default = _lookup_definition(conn, ch, "Foo")
            assert row_default is not None
            assert row_default["is_project"] == 0, (
                f"Default (prefer_project=False) should return SDK symbol "
                f"(lower line), got is_project={row_default['is_project']}"
            )
            assert row_default["file_path"] == "mbed-os/foo.h"
        finally:
            conn.close()

    def test_prefer_project_qualified_name_collision(self, tmp_path):
        """Project 'hal::Device' (is_project=1) beats SDK 'hal::Device' (is_project=0)
        with prefer_project=True when looking up by qualified name."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_sdk = upsert_file(conn, ch, "mbed-os/device.h", "cpp")
            file_proj = upsert_file(conn, ch, "src/my_device.h", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_sdk, "mbed-os/device.h",
                 split_tokens("Device", "hal::Device"),
                 "U_dev_sdk", "Device", "hal::Device", "class",
                 20, 1, 100, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                (ch, file_proj, "src/my_device.h",
                 split_tokens("Device", "hal::Device"),
                 "U_dev_proj", "Device", "hal::Device", "class",
                 30, 1, 80, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "hal::Device", prefer_project=True)
            assert row is not None
            assert row["is_project"] == 1, (
                f"Expected project symbol, got is_project={row['is_project']}, "
                f"file={row['file_path']}"
            )
            assert "src/" in row["file_path"]
        finally:
            conn.close()

    def test_prefer_project_combined_with_preferred_kinds(self, tmp_path):
        """prefer_project=True + preferred_kinds: project class beats SDK class
        (same kind), but SDK class still beats project function
        (kind priority comes before project priority)."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_sdk_cls = upsert_file(conn, ch, "mbed-os/init.h", "cpp")
            file_proj_fn = upsert_file(conn, ch, "src/my_init.cpp", "cpp")
            file_proj_cls = upsert_file(conn, ch, "src/my_init.h", "cpp")
            insert_symbols_batch(conn, [
                # SDK class — is_project=0, kind=class (preferred), line=10
                (ch, file_sdk_cls, "mbed-os/init.h",
                 split_tokens("init", "init"),
                 "U_init_sdk", "init", "init", "class",
                 10, 1, 20, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
                # Project function — is_project=1, kind=function (not preferred), line=5
                (ch, file_proj_fn, "src/my_init.cpp",
                 split_tokens("init", "init"),
                 "U_init_proj_fn", "init", "init", "function",
                 5, 1, 15, 1, "int init()", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
                # Project class — is_project=1, kind=class (preferred), line=50
                (ch, file_proj_cls, "src/my_init.h",
                 split_tokens("init", "MyInit"),
                 "U_init_proj_cls", "init", "MyInit", "class",
                 50, 1, 30, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
            ])

            # With prefer_project=True + preferred_kinds=("class","struct"):
            # kind priority first → class wins over function.
            # Then within classes, is_project priority → project class wins over SDK class.
            row = _lookup_definition(conn, ch, "init",
                                     preferred_kinds=("class", "struct"),
                                     prefer_project=True)
            assert row is not None
            assert row["kind"] == "class", (
                f"Kind priority should come first: class beats function. "
                f"Got kind={row['kind']}"
            )
            assert row["is_project"] == 1, (
                f"Within same kind, project should beat SDK. "
                f"Got is_project={row['is_project']}, file={row['file_path']}"
            )

            # Without prefer_project: SDK class wins (lower line within same kind)
            row_default = _lookup_definition(conn, ch, "init",
                                             preferred_kinds=("class", "struct"))
            assert row_default is not None
            assert row_default["is_project"] == 0
            assert row_default["file_path"] == "mbed-os/init.h"
        finally:
            conn.close()

    def test_prefer_project_no_collision_still_works(self, tmp_path):
        """prefer_project=True works normally when only one symbol exists."""
        conn, ch = _setup_db(tmp_path)
        try:
            file_id = upsert_file(conn, ch, "src/main.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, file_id, "src/main.cpp",
                 split_tokens("main", "main"),
                 "U_main", "main", "main", "function",
                 1, 1, 5, 1, "int main()", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
            ])

            row = _lookup_definition(conn, ch, "main", prefer_project=True)
            assert row is not None
            assert row["kind"] == "function"
            assert row["name"] == "main"
            assert row["is_project"] == 1
        finally:
            conn.close()
