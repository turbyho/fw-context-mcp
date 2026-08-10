"""Regression tests for functional fixes from plans/funkcni_testy.md Run 3.

Covers:
- F1: incremental embeddings (no full re-analysis on every `fw-context index`)
- F2: false self-reference in the call graph (bare-call match on definition line)
- F3: wrong overload resolution (_resolve_method_usr ambiguous -> None, caller class)
- F4: callback(&Class::method) indirect target detection
- F5: FTS5 syntax errors (`#define`, backslash-quote queries) + graceful fallback
"""

from __future__ import annotations

import logging
import sqlite3
from unittest import mock

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# F5 — FTS5 query sanitization (_expand_query)
# ═══════════════════════════════════════════════════════════════════════════


class TestExpandQuerySanitization:
    """Verify _expand_query quotes FTS5-unsafe terms instead of raising."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer.db._symbols import _expand_query

        self._expand_query = _expand_query

    def test_hash_define_quoted(self):
        """'#define' must become an FTS5 phrase, not raise a syntax error."""
        assert self._expand_query("#define") == '"#define"'

    def test_hash_ifdef_quoted(self):
        """'#ifdef' must become an FTS5 phrase."""
        assert self._expand_query("#ifdef") == '"#ifdef"'

    def test_backslash_escaped_quote_repaired(self):
        """'"extern \\"C\\""' (backslash-quote) must be repaired, not error."""
        result = self._expand_query('"extern \\"C\\""')
        assert "extern*" in result and "C*" in result

    def test_plain_words_unchanged(self):
        """Ordinary multi-word queries keep OR-wildcard expansion."""
        assert self._expand_query("extern C") == "extern* OR C*"

    def test_existing_phrase_preserved(self):
        """A valid double-quoted phrase must pass through untouched."""
        assert self._expand_query('"timeout attach"') == '"timeout attach"'

    def test_dash_quoted(self):
        """'user-defined' contains an FTS5 operator char — quote as phrase."""
        assert self._expand_query("user-defined") == '"user-defined"'

    def test_underscore_identifier_expanded(self):
        """'modem_init' keeps the trailing wildcard."""
        assert self._expand_query("modem_init") == "modem_init*"


class TestSearchContentFts5GracefulFallback:
    """Verify search_content falls back to LIKE when FTS5 rejects the query."""

    def test_search_content_fts5_error_falls_back(self, tmpdir):
        """A query FTS5 cannot parse must degrade to LIKE, not raise."""
        from fw_context_mcp.config.settings import _write_project_id
        from fw_context_mcp.indexer.db._connection import open_db
        from fw_context_mcp.indexer.db._files import upsert_file
        from fw_context_mcp.indexer.db._projects import upsert_project

        project_id = "aa11bb22cc33dd44ee55ff6600112233"
        _write_project_id(tmpdir, project_id)
        db_dir = tmpdir / ".fw-context" / "index" / project_id
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "index.db"

        conn = open_db(db_path, skip_integrity_check=True)
        upsert_project(conn, project_id=project_id, name="t", root_path=str(tmpdir))
        conn.execute(
            "INSERT INTO build_configs(config_hash, project_id, compile_commands_path, "
            "manifest_verification) VALUES(?, ?, ?, 'none')",
            ("hash-F5", project_id, str(tmpdir / "compile_commands.json")),
        )
        upsert_file(conn, "hash-F5", "a.cpp", "cpp", False, 0.0)
        # Create the actual file on disk — WP4 staleness fix detects missing
        # files as stale, which would trigger daemon startup in _with_stale_recovery.
        (tmpdir / "a.cpp").write_text("#define FOO 1\nint main() { return 0; }\n")
        conn.execute(
            "UPDATE files SET content = ? WHERE config_hash = ?",
            ("#define FOO 1\nint main() { return 0; }\n", "hash-F5"),
        )
        conn.commit()
        conn.close()

        from fw_context_mcp.mcp.handlers import search as search_mod
        from fw_context_mcp.mcp.handlers._base import BaseHandler, DbContext
        from fw_context_mcp.mcp.shared.executor import SyncQueryExecutor

        db_ctx = DbContext(
            db_path=db_path,
            executor=SyncQueryExecutor(str(db_path.resolve()), db_path),
            config_hash="hash-F5",
            project_id=project_id,
            root=tmpdir,
            cfg=None,
        )
        with mock.patch.object(BaseHandler, "resolve_db_context", return_value=db_ctx):
            with mock.patch.object(search_mod, "_db_path", return_value=db_path):
                with mock.patch(
                    "fw_context_mcp.mcp.shared.stale._stale_files",
                    return_value=[],
                ):
                    result = search_mod.search_content(
                        query="#define", project_root=str(tmpdir)
                    )
        assert isinstance(result, list)
        assert result, "search_content should return at least one matching file"
        assert result[0]["file"].endswith("a.cpp")


# ═══════════════════════════════════════════════════════════════════════════
# F3 — overload resolution (_resolve_method_usr)
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveMethodUsr:
    """Verify ambiguous method names resolve correctly or to None (never wrong)."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer.symbols import _resolve_method_usr

        self._resolve = _resolve_method_usr

    def test_exact_name_wins(self):
        """A bare unqualified name that exists must win."""
        qn = {"zbox::WDT::zbox_reset": "u_self"}
        assert self._resolve("zbox_reset", qn) == "u_self"

    def test_single_candidate_suffix(self):
        """A single ::method candidate resolves even without context."""
        qn = {"zbox::WDT::zbox_reset": "u_self"}
        assert self._resolve("zbox_reset", qn) == "u_self"

    def test_caller_class_preferred_for_bare_call(self):
        """Bare method() inside a class must resolve to a sibling method."""
        qn = {
            "zbox::WDT::zbox_reset": "u_self",
            "other::zbox_reset": "u_other",
        }
        result = self._resolve("zbox_reset", qn, caller_qn="zbox::WDT::swdt_check")
        assert result == "u_self"

    def test_receiver_field_hint_preferred(self):
        """obj.attach() must prefer the receiver type over the caller class."""
        qn = {
            "zbox::WDT::attach": "u_wdt_attach",  # caller-class sibling (WRONG choice)
            "mbed::Timeout::attach": "u_timeout_attach",
            "mbed::SerialBase::attach": "u_serial_attach",
        }
        result = self._resolve("attach", qn, field_name="_timeout",
                               caller_qn="zbox::WDT::swdt_check")
        assert result == "u_timeout_attach"

    def test_ambiguous_returns_none(self):
        """Ambiguous bare call with no context must return None, not an arbitrary hit."""
        qn = {"a::attach": "u_a", "b::attach": "u_b"}
        assert self._resolve("attach", qn) is None

    def test_ambiguous_with_unknown_receiver_returns_none(self):
        """Field hint that matches no candidate must return None (no wrong edge)."""
        qn = {"a::attach": "u_a", "b::attach": "u_b"}
        result = self._resolve("attach", qn, field_name="_something")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# F2 — self-reference suppression (_run_source_line_fallback)
# ═══════════════════════════════════════════════════════════════════════════


class _FakeTU:
    """Minimal stand-in for a libclang TranslationUnit (spelling only)."""

    def __init__(self, path: str):
        self.spelling = path

    def __getattr__(self, name: str):
        return None


class TestRunSourceLineFallbackSelfRef:
    """Verify the definition line does not create a self-caller edge."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer.symbols import _run_source_line_fallback

        self._fallback = _run_source_line_fallback

    def _run(self, source: str, qn_to_usr: dict[str, str]):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write(source)
            path = f.name
        try:
            refs: list = []
            fp_assignments: list = []
            pending_dispatches: list = []
            seen: set = set()
            usr_to_qn = {u: q for q, u in qn_to_usr.items()}
            fn_spans = [("u_self", 1, len(source.splitlines()))]
            self._fallback(
                _FakeTU(path), refs, fp_assignments, pending_dispatches, seen,
                fn_spans, qn_to_usr, usr_to_qn,
                logging.getLogger("test"),
            )
            return refs, fp_assignments
        finally:
            os.unlink(path)

    def test_no_self_caller_at_definition_line(self):
        """The signature line `void WDT::zbox_reset(` must not produce a self edge."""
        src = (
            "void WDT::zbox_reset(int delay) {\n"
            "    helper(delay);\n"
            "}\n"
        )
        qn = {"zbox::WDT::zbox_reset": "u_self", "zbox::WDT::helper": "u_helper"}
        refs, _fp = self._run(src, qn)

        self_refs = [r for r in refs if r.to_usr == "u_self" and r.from_usr == "u_self"]
        assert not self_refs, (
            f"Definition line must not create a self-reference, got {self_refs}"
        )
        helper_refs = [r for r in refs if r.to_usr == "u_helper"]
        assert helper_refs, "A real bare call on a non-definition line must be kept"

    def test_real_recursion_still_detected(self):
        """A genuine self-call inside the body (not the signature) must be kept."""
        src = (
            "void WDT::zbox_reset(int delay) {\n"
            "    helper(delay);\n"
            "    zbox_reset(delay + 1);\n"
            "}\n"
        )
        qn = {"zbox::WDT::zbox_reset": "u_self", "zbox::WDT::helper": "u_helper"}
        refs, _fp = self._run(src, qn)

        body_self = [r for r in refs
                     if r.to_usr == "u_self" and r.from_usr == "u_self" and r.from_line == 3]
        assert body_self, "Real recursion inside the body must be kept"
        sig_self = [r for r in refs
                    if r.to_usr == "u_self" and r.from_usr == "u_self" and r.from_line == 1]
        assert not sig_self, "Signature line must not create a self edge"

    def test_callback_address_of_method_indirect_via_raw_text(self):
        """`&Class::method` inside a call arg must emit an indirect ref from the
        source-line fallback (F4b) — the path that also works when the AST is
        too degraded for libclang to produce CALL_EXPR cursors."""
        src = (
            "void WDT::zbox_reset() {\n"
            "    _timeout.attach(callback(&WDT::_timeout_interrupt), delay);\n"
            "}\n"
        )
        qn = {"zbox::WDT::zbox_reset": "u_self", "zbox::WDT::_timeout_interrupt": "u_ti"}
        refs, fp_assignments = self._run(src, qn)

        indirect = [
            r for r in refs
            if r.ref_kind == "indirect" and r.to_usr == "u_ti" and r.from_usr == "u_self"
        ]
        assert indirect, (
            "&WDT::_timeout_interrupt must be an indirect ref from zbox_reset via raw text"
        )
        fpa_matches = [
            f for f in fp_assignments
            if f.rhs_usr == "u_ti" and f.method == "call_arg"
        ]
        assert fpa_matches, (
            "&WDT::_timeout_interrupt must also create an FnPointerAssignment via raw text"
        )


# ═══════════════════════════════════════════════════════════════════════════
# F1 — incremental embeddings (content_hash)
# ═══════════════════════════════════════════════════════════════════════════


class TestEmbedContentHash:
    """Verify the embedding content hash changes when inputs change."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer._embedding import _embed_content_hash

        self._hash = _embed_content_hash

    def _row(self, **overrides):
        base = {
            "name": "foo", "qualified_name": "zbox::foo", "kind": "method",
            "file_path": "src/foo.cpp", "signature": "void foo(int)",
            "docstring": "doc", "summary": "sum", "source": "void foo(int) {}",
        }
        base.update(overrides)
        return base

    def test_stable_for_same_content(self):
        assert self._hash(self._row()) == self._hash(self._row())

    def test_changes_on_source_change(self):
        a = self._hash(self._row(source="void foo(int) {}"))
        b = self._hash(self._row(source="void foo(int) { return; }"))
        assert a != b

    def test_changes_on_signature_change(self):
        a = self._hash(self._row(signature="void foo(int)"))
        b = self._hash(self._row(signature="void foo(long)"))
        assert a != b

    def test_changes_on_summary_change(self):
        a = self._hash(self._row(summary="old"))
        b = self._hash(self._row(summary="new LLM summary"))
        assert a != b


class TestEmbeddingsSchemaAndUpsert:
    """Verify the embeddings schema carries content_hash end-to-end."""

    def test_schema_has_content_hash_column(self, temp_db):
        cols = [r[1] for r in temp_db.execute("PRAGMA table_info(embeddings)").fetchall()]
        assert "content_hash" in cols

    def test_legacy_embeddings_table_migrated(self, tmpdir):
        """An embeddings table created before content_hash must be upgraded in place."""
        import sqlite3 as _sql

        from fw_context_mcp.indexer.db._connection import open_db

        db_path = tmpdir / "legacy.db"
        conn = _sql.connect(str(db_path))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS embeddings (
                symbol_id    INTEGER NOT NULL,
                chunk_index  INTEGER NOT NULL DEFAULT 0,
                embedding    BLOB    NOT NULL,
                model        TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (symbol_id, chunk_index)
            )"""
        )
        conn.execute("INSERT INTO embeddings(symbol_id, chunk_index, embedding, model) "
                     "VALUES (1, 0, x'00000000', 'm')")
        conn.commit()
        conn.close()

        upgraded = open_db(db_path)
        cols = [r[1] for r in upgraded.execute("PRAGMA table_info(embeddings)").fetchall()]
        assert "content_hash" in cols, "Migration must add the content_hash column"
        row = upgraded.execute(
            "SELECT content_hash FROM embeddings WHERE symbol_id = 1"
        ).fetchone()
        assert row["content_hash"] == "", "Existing rows must default to empty hash"
        upgraded.close()

    def test_upsert_stores_content_hash(self, populated_db):
        temp_db = populated_db
        config_hash = "hash-deadbeef"
        from fw_context_mcp.indexer.db._files import upsert_file

        upsert_file(temp_db, config_hash, "a.cpp", "cpp", False, 0.0)
        temp_db.execute(
            """INSERT INTO symbols
               (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name,
                kind, line, col, end_line, is_definition, signature, docstring, enum_value,
                is_virtual, is_pure_virtual, parent_usr, is_template, template_usr,
                is_project, pagerank, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (config_hash, 1, "a.cpp", "foo", "u_foo", "foo", "zbox::foo",
             "function", 1, 1, 3, 1, "void foo()", "", None,
             0, 0, "", 0, "", 1, 1.0, "void foo() {}"),
        )
        from fw_context_mcp.indexer.db._embeddings import upsert_embeddings

        upsert_embeddings(temp_db, [(1, 0, b"\x00\x00\x00\x00", "m:desc-v5", "abc123")])
        row = temp_db.execute(
            "SELECT content_hash FROM embeddings WHERE symbol_id = 1"
        ).fetchone()
        assert row["content_hash"] == "abc123"


class _FakeEmbedder:
    """Stand-in embedder — records how many times it was asked to embed."""

    name = "test-embed-model"
    max_tokens = 512
    dim = 8

    def __init__(self):
        self.calls = 0

    def embed_documents(self, descs):
        self.calls += len(descs)
        return [[0.5] * self.dim for _ in descs]


class TestBuildEmbeddingsIncremental:
    """Verify _build_embeddings skips unchanged symbols on the second run."""

    @staticmethod
    def _seed(conn: sqlite3.Connection, config_hash: str, source: str = "void foo() {}") -> None:
        from fw_context_mcp.indexer.db._files import upsert_file

        upsert_file(conn, config_hash, "src/foo.cpp", "cpp", False, 0.0)
        file_row = conn.execute(
            "SELECT id FROM files WHERE path = ? AND config_hash = ?",
            ("src/foo.cpp", config_hash),
        ).fetchone()
        conn.execute(
            """INSERT INTO symbols
               (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name,
                kind, line, col, end_line, is_definition, signature, docstring, enum_value,
                is_virtual, is_pure_virtual, parent_usr, is_template, template_usr,
                is_project, pagerank, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (config_hash, file_row["id"], "src/foo.cpp", "foo", "u_foo", "foo", "zbox::foo",
             "function", 1, 1, 3, 1, "void foo()", "", None,
             0, 0, "", 0, "", 1, 1.0, source),
        )
        conn.commit()

    def test_second_run_embeds_nothing_when_unchanged(self, populated_db, tmpdir):
        from fw_context_mcp.indexer._embedding import _build_embeddings

        conn = populated_db
        self._seed(conn, "hash-deadbeef")
        fake = _FakeEmbedder()

        with mock.patch(
            "fw_context_mcp.indexer._embedding.get_embedder", return_value=fake,
        ):
            _build_embeddings(conn, "hash-deadbeef", None, tmpdir)
            first_calls = fake.calls
            # Second run — identical content must not re-embed.
            _build_embeddings(conn, "hash-deadbeef", None, tmpdir)

        assert first_calls > 0, "First run must embed the symbol"
        assert fake.calls == first_calls, (
            "Unchanged symbols must NOT be re-embedded on the second run "
            f"(calls: {first_calls} -> {fake.calls})"
        )

    def test_changed_source_triggers_reembed(self, populated_db, tmpdir):
        from fw_context_mcp.indexer._embedding import _build_embeddings

        conn = populated_db
        self._seed(conn, "hash-deadbeef")
        fake = _FakeEmbedder()

        with mock.patch(
            "fw_context_mcp.indexer._embedding.get_embedder", return_value=fake,
        ):
            _build_embeddings(conn, "hash-deadbeef", None, tmpdir)
            first_calls = fake.calls
            # Change the symbol's body — the hash differs, so re-embed.
            conn.execute(
                "UPDATE symbols SET source = ? WHERE usr = 'u_foo'",
                ("void foo() { return; }",),
            )
            conn.commit()
            _build_embeddings(conn, "hash-deadbeef", None, tmpdir)

        assert fake.calls > first_calls, (
            "A changed symbol must be re-embedded on the next run"
        )


# ═══════════════════════════════════════════════════════════════════════════
# F4 — callback(&Class::method) detection (libclang integration)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.libclang
class TestCallbackTargetIndirectRef:
    """Verify callback(&Class::method) yields an indirect ref (real libclang)."""

    def test_callback_address_of_method_is_indirect_ref(self, tmpdir):
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import extract_all

        src = tmpdir / "cb.cpp"
        src.write_text(
            "class WDT {\n"
            "public:\n"
            "    void attach(void (*cb)());\n"
            "    void zbox_reset();\n"
            "    void _timeout_interrupt();\n"
            "};\n"
            "void WDT::zbox_reset() {\n"
            "    attach(&WDT::_timeout_interrupt);\n"
            "}\n",
            encoding="utf-8",
        )
        unit = CompilationUnit(
            file=src, clang_args=["-std=c++14"], directory=tmpdir, language="cpp",
        )
        result = extract_all(unit, with_refs=True)

        indirect = [r for r in result.references if r.ref_kind == "indirect"]
        timeout_refs = [r for r in indirect if "timeout_interrupt" in r.to_usr]
        assert timeout_refs, (
            "callback(&WDT::_timeout_interrupt) must be detected as an indirect ref"
        )

        # F2: the definition line `void WDT::zbox_reset()` must NOT create a
        # self-caller edge (from == to == zbox_reset).
        zbox_usrs = {s.usr for s in result.symbols if s.name == "zbox_reset"}
        assert zbox_usrs, "zbox_reset symbol must be extracted"
        self_edges = [
            r for r in result.references
            if r.to_usr in zbox_usrs and r.from_usr in zbox_usrs
        ]
        assert not self_edges, (
            f"Definition line must not create a self-reference, got {self_edges}"
        )

        # The `attach(...)` call from zbox_reset must be present (libclang
        # classifies a bare member call as ref_kind "member").
        attach_refs = [
            r for r in result.references if "attach" in r.to_usr
        ]
        assert attach_refs, "The attach(&WDT::_timeout_interrupt) call must be detected"


# ═══════════════════════════════════════════════════════════════════════════
# F8 — dispatch bridge detection + deferred resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestDispatchDetectionFallback:
    """Verify dispatch PendingDispatch creation in source-line fallback."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer.symbols import _run_source_line_fallback
        self._fallback = _run_source_line_fallback

    def _run(self, source: str, qn_to_usr: dict[str, str]):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as f:
            f.write(source)
            path = f.name
        refs: list = []
        fp_assignments: list = []
        pending: list = []
        seen: set = set()
        usr_to_qn = {u: q for q, u in qn_to_usr.items()}
        fn_spans = [("u_caller", 1, len(source.splitlines()))]
        self._fallback(
            _FakeTU(path), refs, fp_assignments, pending, seen,
            fn_spans, qn_to_usr, usr_to_qn,
            logging.getLogger("test"),
        )
        return refs, fp_assignments, pending

    def test_dispatch_call_every_creates_pending(self):
        """EventQueue::call_every with &Class::method → PendingDispatch."""
        src = (
            "void ZBLE::ZBLE() {\n"
            '    _maineventQueue.call_every(1000ms, this, &ZBLE::watch_ble);\n'
            "}\n"
        )
        qn = {
            "zbox::ZBLE::ZBLE": "u_caller",
            "events::EventQueue::call_every": "u_call_every",
            "zbox::ZBLE::watch_ble": "u_watch",
        }
        _refs, _fpa, pending = self._run(src, qn)

        matches = [
            p for p in pending
            if p.callee_qn == "events::EventQueue::call_every"
            and p.target_name == "watch_ble"
        ]
        assert matches, (
            "call_every with &ZBLE::watch_ble must produce a PendingDispatch"
        )

    def test_dispatch_ignored_for_non_dispatch_method(self):
        """Regular method with same name as dispatch method but not in map → no PendingDispatch."""
        src = (
            "void App::run() {\n"
            '    _timer.attach(&App::onTick);\n'
            "}\n"
        )
        # "attach" is a dispatch method name but "App::attach" is NOT in the map
        qn = {
            "zbox::App::run": "u_caller",
            "zbox::App::attach": "u_attach",
            "zbox::App::onTick": "u_tick",
        }
        _refs, _fpa, pending = self._run(src, qn)
        assert not pending, (
            "Non-dispatch attach must not create PendingDispatch"
        )

    def test_thread_start_dispatch(self):
        """Thread::start with callback → PendingDispatch for mbed-os."""
        src = (
            "void App::run() {\n"
            '    _thread.start(&App::threadLoop);\n'
            "}\n"
        )
        qn = {
            "zbox::App::run": "u_caller",
            "rtos::Thread::start": "u_start",
            "zbox::App::threadLoop": "u_loop",
        }
        _refs, _fpa, pending = self._run(src, qn)

        matches = [
            p for p in pending
            if p.callee_qn == "rtos::Thread::start"
        ]
        assert matches, (
            "Thread::start with callback must produce a PendingDispatch"
        )


class TestCallGraphDispatchAndGlobalCtors:
    """Verify find_call_path traverses implicit_construct and dispatch edges."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer.db._callgraph import (
            find_all_callers_recursive,
            find_call_path,
            find_callees_recursive,
        )
        self._find_call_path = find_call_path
        self._find_all_callers_recursive = find_all_callers_recursive
        self._find_callees_recursive = find_callees_recursive
        self._tmpdirs: list[Path] = []
        yield
        import shutil
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def _build_db(self):
        """Build an in-memory DB with symbols and refs for call-graph testing."""
        import tempfile
        from pathlib import Path

        from fw_context_mcp.indexer.db import (
            insert_refs_batch,
            insert_symbols_batch,
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        tmpdir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(tmpdir)
        db_path = tmpdir / "test.db"
        conn = open_db(db_path)

        with transaction(conn):
            upsert_project(conn, "proj-001", "test", "/tmp/test")
            upsert_build_config(conn, "hash-deadbeef", "proj-001", "/tmp/compile_commands.json")

        CH = "hash-deadbeef"
        fid = upsert_file(conn, CH, "src/main.cpp", "cpp")

        # Symbols: (name, qn, kind, file_path, line, usr, is_project)
        _sym_data = [
            ("main",     "main",               "function",      "src/main.cpp",    1, "u_main", 1),
            ("run",      "App::run",           "method",        "src/app.cpp",    10, "u_run", 1),
            ("ZBLE_ctor","zbox::ZBLE::ZBLE",   "constructor",   "src/zble.cpp",   20, "u_zble_ctor", 1),
            ("watch_ble","zbox::ZBLE::watch_ble","method",      "src/zble.cpp",   25, "u_watch_ble", 1),
            ("dispatch_forever", "events::EventQueue::dispatch_forever", "method",
             "vendor/EventQueue.cpp", 100, "u_dispatch_forever", 0),
            ("call_every","events::EventQueue::call_every", "method",
             "vendor/EventQueue.cpp", 90, "u_call_every", 0),
            ("zbox_reset","WDT::zbox_reset",   "method",       "src/wdt.cpp",    50, "u_zbox_reset", 1),
            ("swdt_check","WDT::swdt_check",   "method",       "src/wdt.cpp",    45, "u_swdt_check", 1),
        ]
        sym_rows = [
            (CH, fid, fp, name, usr, name, qn, kind, line, 1, 0, 1, "", "", None, 0, 0, "", 0, "", ip, 0.0, "")
            for name, qn, kind, fp, line, usr, ip in _sym_data
        ]
        insert_symbols_batch(conn, sym_rows)

        # Refs: (to_usr, from_file, from_line, from_usr, ref_kind)
        refs_data = [
            # main → dispatch_forever (event loop)
            ("u_dispatch_forever", "src/main.cpp",   2,  "u_main",              "call"),
            # main → run
            ("u_run",              "src/main.cpp",   3,  "u_main",              "call"),
            # SYNTHETIC: dispatch_forever → watch_ble (dispatch bridge)
            ("u_watch_ble",        "src/zble.cpp",  26,  "u_dispatch_forever",  "dispatch"),
            # watch_ble → swdt_check
            ("u_swdt_check",       "src/zble.cpp",  27,  "u_watch_ble",         "call"),
            # swdt_check → zbox_reset
            ("u_zbox_reset",       "src/wdt.cpp",   46,  "u_swdt_check",        "call"),
            # File-scope implicit_construct: ZBLE global object ctor
            ("u_zble_ctor",        "src/zble.cpp",  20,  None,                  "implicit_construct"),
            # ZBLE ctor → watch_ble (method called from ctor body)
            ("u_watch_ble",        "src/zble.cpp",  22,  "u_zble_ctor",         "call"),
        ]
        ref_rows = [
            (CH, to_usr, fp, line, from_usr, rk)
            for to_usr, fp, line, from_usr, rk in refs_data
        ]
        insert_refs_batch(conn, ref_rows)

        return conn, CH

    def test_call_path_through_dispatch_edge(self):
        """find_call_path(main → zbox_reset) returns paths through dispatch edge."""
        conn, ch = self._build_db()
        try:
            paths = self._find_call_path(conn, ch, "main", "zbox_reset", max_depth=8)
            assert paths, (
                "Path must exist from main to zbox_reset"
            )
            chains = [p["chain"] for p in paths]
            # One path via dispatch_forever, another via implicit_construct
            any_dispatch = any("dispatch_forever" in c for c in chains)
            any_global_ctors = any("<global ctors>" in c for c in chains)
            assert any_dispatch or any_global_ctors, (
                f"Path must include dispatch_forever or <global ctors>, got: {chains}"
            )
            assert any("zbox_reset" in c for c in chains)
        finally:
            conn.close()

    def test_call_path_main_to_watch_ble_only_dispatch(self):
        """find_call_path(main → watch_ble) when no implicit_construct path exists."""
        conn, ch = self._build_db()
        try:
            # Remove the implicit_construct edge so only dispatch path works
            from fw_context_mcp.indexer.db import delete_refs_for_file
            delete_refs_for_file(conn, ch, "src/zble.cpp")
            # Re-add only the dispatch and direct call edges (no implicit_construct)
            from fw_context_mcp.indexer.db import insert_refs_batch
            insert_refs_batch(conn, [
                (ch, "u_dispatch_forever", "src/main.cpp", 2, "u_main", "call"),
                (ch, "u_watch_ble", "src/zble.cpp", 26, "u_dispatch_forever", "dispatch"),
                (ch, "u_swdt_check", "src/zble.cpp", 27, "u_watch_ble", "call"),
                (ch, "u_zbox_reset", "src/wdt.cpp", 46, "u_swdt_check", "call"),
            ])
            paths = self._find_call_path(conn, ch, "main", "watch_ble", max_depth=6)
            assert paths, (
                "main → dispatch_forever → (dispatch) → watch_ble must exist"
            )
            chains = [p["chain"] for p in paths]
            assert any("dispatch_forever" in c for c in chains), (
                f"Path must include dispatch_forever, got: {chains}"
            )
        finally:
            conn.close()

    def test_call_path_through_implicit_construct(self):
        """find_call_path(main → watch_ble) returns shortest path (dispatch or global ctor)."""
        conn, ch = self._build_db()
        try:
            paths = self._find_call_path(conn, ch, "main", "watch_ble", max_depth=6)
            assert paths, (
                "main → watch_ble path must exist (via dispatch or global ctors)"
            )
            chains = [p["chain"] for p in paths]
            assert any(
                "dispatch_forever" in c or "<global ctors>" in c for c in chains
            ), (
                f"Path must include dispatch_forever or <global ctors>, got: {chains}"
            )
        finally:
            conn.close()

    def test_all_callers_recursive_includes_main(self):
        """find_all_callers_recursive(zbox_reset) includes main via dispatch."""
        conn, ch = self._build_db()
        try:
            callers = self._find_all_callers_recursive(conn, ch, "zbox_reset", max_depth=6)
            caller_names = [c["name"] for c in callers]
            assert "main" in caller_names, (
                f"main must be in transitive callers of zbox_reset, got: {caller_names}"
            )
            assert "swdt_check" in caller_names, (
                "swdt_check must be in callers of zbox_reset"
            )
        finally:
            conn.close()

    def test_callees_recursive_includes_dispatch(self):
        """find_callees_recursive(main) includes zbox_reset via dispatch."""
        conn, ch = self._build_db()
        try:
            callees = self._find_callees_recursive(conn, ch, "main", max_depth=8, limit=50)
            callee_names = [c["name"] for c in callees]
            assert "zbox_reset" in callee_names, (
                f"zbox_reset must be in transitive callees of main, got: {callee_names}"
            )
            assert "watch_ble" in callee_names, (
                "watch_ble must be in callees of main"
            )
        finally:
            conn.close()

    def test_no_path_for_unreachable(self):
        """find_call_path returns empty for disconnected symbols."""
        from fw_context_mcp.indexer.db import insert_symbols_batch, upsert_file
        conn, ch = self._build_db()
        try:
            fid = upsert_file(conn, ch, "src/ghost.cpp", "cpp")
            insert_symbols_batch(conn, [
                (ch, fid, "src/ghost.cpp", "ghost", "u_ghost", "ghost", "ghost",
                 "function", 1, 1, 0, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
            ])
            paths = self._find_call_path(conn, ch, "ghost", "zbox_reset", max_depth=5)
            assert not paths, "Unconnected function must have no path"
        finally:
            conn.close()
