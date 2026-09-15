"""Regression tests: a name that means several symbols must not collapse to one.

``find_all_callers_recursive`` once answered a question about one method
with the callers of a different method of the same name, and a fully
qualified name could not change that answer.  The tie-break ran on the
reference counts of the candidates, which have nothing to do with the name
that the caller gave.

Four parts of the repair are pinned here:

1. The resolver ranks a match on its specificity, thus an exact qualified
   name wins over a bare sibling.
2. The graph tools walk every match of the best rank and label each row.
3. A reference that the AST walk left without a caller gets one, because a
   NULL ``from_usr`` drops out of the recursive CTE.
4. The cross-TU backfill writes no edge for an ambiguous method name.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from fw_context_mcp.indexer._postprocess import _step_repair_from_usr
from fw_context_mcp.indexer.db import (
    find_refs_with_candidates,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    refs_for_symbol,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.db._callgraph import (
    _resolve_target_usrs,
    find_all_callers_recursive,
    find_call_path,
    find_callees_recursive,
)
from fw_context_mcp.indexer.db._resolve import (
    MAX_AMBIGUOUS_TARGETS,
    Candidate,
    ambiguity_notice,
    candidate_labels,
    count_candidates,
    resolve_candidates,
)
from fw_context_mcp.mcp.handlers.source import (
    _ambiguity,
    _collect_callers,
    _lookup_definition,
)

CH = "hash-deadbeef"


def find_refs(conn, config_hash, name, ref_kind=None, limit=50):
    """The rows of ``find_refs_with_candidates``, for a test that reads rows.

    The package used to ship this as a view.  Nothing in ``src/`` called it
    after the resolution and the query were split, and it dropped
    ``candidates`` and ``total`` — the two values that every real caller
    reads.  A convenience that only a test wants belongs to the test.
    """
    rows, _candidates, _total = find_refs_with_candidates(
        conn, config_hash, name, ref_kind=ref_kind, limit=limit
    )
    return rows


def _symbol_row(
    file_id: int,
    file_path: str,
    name: str,
    qualified_name: str,
    usr: str,
    line: int,
    end_line: int = 0,
    kind: str = "method",
    parent_usr: str = "",
    signature: str = "",
) -> tuple:
    """Build one row for insert_symbols_batch.

    The column order is the one that the function documents.  Only the
    fields that these tests read carry a value; the rest take the same
    neutral defaults that the other call-graph tests use.

    *parent_usr* points at the class that declares a method.  The tools
    read the class through it, thus a fixture that leaves it empty cannot
    show what a reader sees.
    """
    return (
        CH, file_id, file_path, name, usr, name, qualified_name, kind,
        line, 1, end_line, 1, signature, "", None, 0, 0, parent_usr,
        0, "", 1, 0.0, "", 0,
    )


def _ref_row(
    to_usr: str,
    from_file: str,
    from_line: int,
    from_usr: str | None,
    ref_kind: str = "call",
) -> tuple:
    """Build one reference row for insert_refs_batch.  The tail is slot_index.

    *ref_kind* is a parameter because a function-pointer table writes
    ``indirect`` rows, and that is the shape the repair below has to get
    right — see ``TestRepairFromUsr``.
    """
    return (CH, to_usr, from_file, from_line, from_usr, ref_kind, None)


@pytest.fixture
def db():
    """Two classes, each with a method named ``probe``, each with a caller.

    ``ClassA::probe`` gets more references than ``ClassB::probe`` on
    purpose.  That is the count that used to decide the answer, so a test
    that gives it to the wrong class would pass for the wrong reason.
    """
    tmpdir = Path(tempfile.mkdtemp())
    conn = open_db(tmpdir / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", "/tmp/test")
        upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
    fid = upsert_file(conn, CH, "src/main.cpp", "cpp")

    insert_symbols_batch(conn, [
        # The classes themselves, so that a method can point at its owner.
        _symbol_row(fid, "src/main.cpp", "ClassA", "ClassA", "u_class_a", 5, 25,
                    kind="class"),
        _symbol_row(fid, "src/main.cpp", "ClassB", "ClassB", "u_class_b", 26, 45,
                    kind="class"),
        _symbol_row(fid, "src/main.cpp", "Solo", "Solo", "u_class_solo", 85, 96,
                    kind="class"),
        _symbol_row(fid, "src/main.cpp", "probe", "ClassA::probe", "u_a_probe", 10, 20,
                    parent_usr="u_class_a", signature="bool probe()"),
        _symbol_row(fid, "src/main.cpp", "probe", "ClassB::probe", "u_b_probe", 30, 40,
                    parent_usr="u_class_b", signature="bool probe(int)"),
        _symbol_row(fid, "src/main.cpp", "run_a", "ClassA::run_a", "u_a_run", 50, 60,
                    parent_usr="u_class_a"),
        _symbol_row(fid, "src/main.cpp", "run_b", "ClassB::run_b", "u_b_run", 70, 80,
                    parent_usr="u_class_b"),
        _symbol_row(fid, "src/main.cpp", "only", "Solo::only", "u_solo", 90, 95,
                    parent_usr="u_class_solo"),
        _symbol_row(fid, "src/main.cpp", "top", "top", "u_top", 100, 110, kind="function"),
    ])
    insert_refs_batch(conn, [
        _ref_row("u_a_probe", "src/main.cpp", 51, "u_a_run"),
        _ref_row("u_b_probe", "src/main.cpp", 71, "u_b_run"),
        _ref_row("u_solo", "src/main.cpp", 101, "u_top"),
        # ClassA::probe gets the higher reference count, which is what the
        # old tie-break read.
        _ref_row("u_a_probe", "src/main.cpp", 52, "u_top"),
        _ref_row("u_a_probe", "src/main.cpp", 53, "u_top"),
    ])
    yield conn
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestRankedResolver:
    """The specificity of a match decides, not the reference count."""

    def test_a_qualified_name_gives_only_its_own_symbol(self, db):
        usrs, names = _resolve_target_usrs(db, CH, "ClassB::probe")
        assert names == ["ClassB::probe"], f"expected only ClassB, got: {names}"
        assert usrs == ["u_b_probe"]

    def test_the_qualified_name_wins_over_the_more_referenced_sibling(self, db):
        """ClassA::probe has more references, and must still not be chosen."""
        usrs, _ = _resolve_target_usrs(db, CH, "ClassB::probe")
        assert usrs == ["u_b_probe"], "the reference count overruled the qualified name"

    def test_a_bare_name_gives_every_match(self, db):
        usrs, names = _resolve_target_usrs(db, CH, "probe")
        assert set(names) == {"ClassA::probe", "ClassB::probe"}, f"got: {names}"
        assert len(usrs) == 2

    def test_an_unknown_name_gives_nothing(self, db):
        assert _resolve_target_usrs(db, CH, "no_such_symbol") == ([], [])

    def test_a_partial_qualified_name_reaches_the_symbol(self, db):
        """Rank 2: a name that starts at the class and not at the namespace.

        A caller types ``Dev::send_byte`` for ``hal::Dev::send_byte``, thus
        the resolver matches a SUFFIX of the qualified name.
        """
        fid = upsert_file(db, CH, "src/hal.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/hal.cpp", "Dev", "hal::Dev", "u_hal_dev", 300, 340,
                        kind="class"),
            _symbol_row(fid, "src/hal.cpp", "send_byte", "hal::Dev::send_byte",
                        "u_send_byte", 310, 320, parent_usr="u_hal_dev"),
        ])
        db.commit()
        usrs, names = _resolve_target_usrs(db, CH, "Dev::send_byte")
        assert usrs == ["u_send_byte"], f"the suffix tier missed the symbol: {names}"

    def test_an_underscore_of_the_name_is_not_a_wildcard(self, db):
        """The suffix tier is a LIKE, thus ``_`` needs the ESCAPE clause.

        An embedded name carries underscores by habit.  Without the escape
        ``Dev::send_byte`` would also reach ``Dev::sendXbyte``.
        """
        fid = upsert_file(db, CH, "src/hal.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/hal.cpp", "Dev", "hal::Dev", "u_hal_dev", 300, 340,
                        kind="class"),
            _symbol_row(fid, "src/hal.cpp", "sendXbyte", "hal::Dev::sendXbyte",
                        "u_send_x", 330, 335, parent_usr="u_hal_dev"),
        ])
        db.commit()
        usrs, names = _resolve_target_usrs(db, CH, "Dev::send_byte")
        assert usrs == [], f"the underscore matched any character: {names}"


class TestAmbiguousCallers:
    """find_all_callers_recursive answers for the symbol that was named."""

    def test_a_qualified_name_gives_only_its_own_callers(self, db):
        rows = find_all_callers_recursive(db, CH, "ClassB::probe", max_depth=3)
        names = [r["qualified_name"] for r in rows]
        assert names == ["ClassB::run_b"], f"expected only ClassB::run_b, got: {names}"

    def test_the_other_variant_is_reachable_too(self, db):
        rows = find_all_callers_recursive(db, CH, "ClassA::probe", max_depth=3)
        names = {r["qualified_name"] for r in rows}
        assert names == {"ClassA::run_a", "top"}, f"got: {names}"

    def test_a_bare_name_gives_the_callers_of_each_symbol(self, db):
        rows = find_all_callers_recursive(db, CH, "probe", max_depth=3)
        names = {r["qualified_name"] for r in rows if "warning" not in r}
        assert names == {"ClassA::run_a", "ClassB::run_b", "top"}, f"got: {names}"

    def test_a_bare_name_labels_each_row_with_its_target(self, db):
        rows = find_all_callers_recursive(db, CH, "probe", max_depth=3)
        by_caller = {
            r["qualified_name"]: r["target_qualified_name"]
            for r in rows if "warning" not in r
        }
        assert by_caller["ClassA::run_a"] == "ClassA::probe", f"got: {by_caller}"
        assert by_caller["ClassB::run_b"] == "ClassB::probe", f"got: {by_caller}"

    def test_a_bare_name_puts_one_warning_first(self, db):
        rows = find_all_callers_recursive(db, CH, "probe", max_depth=3)
        assert set(rows[0]) == {"warning"}, f"the first row is not a warning: {rows[0]}"
        assert "ClassA::probe" in rows[0]["warning"]
        assert "ClassB::probe" in rows[0]["warning"]
        assert sum(1 for r in rows if "warning" in r) == 1, "more than one warning row"

    def test_a_single_match_keeps_the_older_row_shape(self, db):
        """An unambiguous name must answer exactly as it did before."""
        rows = find_all_callers_recursive(db, CH, "Solo::only", max_depth=3)
        assert len(rows) == 1, f"expected one caller, got: {rows}"
        assert "warning" not in rows[0]
        assert "target_qualified_name" not in rows[0], "the key leaked into a clear answer"
        assert "target" not in rows[0], "the internal target column leaked"
        assert rows[0]["qualified_name"] == "top"


class TestAmbiguousCallees:
    """find_callees_recursive follows the same rule in the other direction."""

    def test_a_qualified_name_gives_only_its_own_callees(self, db):
        rows = find_callees_recursive(db, CH, "ClassB::run_b", max_depth=3)
        names = [r["qualified_name"] for r in rows]
        assert names == ["ClassB::probe"], f"got: {names}"

    def test_a_single_match_keeps_the_older_row_shape(self, db):
        rows = find_callees_recursive(db, CH, "ClassA::run_a", max_depth=3)
        assert all("target_qualified_name" not in r for r in rows), f"got: {rows}"


class TestAmbiguousCallPath:
    """find_call_path reports which of the same-name targets it reached."""

    def test_the_path_reaches_the_named_target(self, db):
        paths = find_call_path(db, CH, "ClassB::run_b", "ClassB::probe")
        assert paths, "no path found"
        assert all("warning" not in p for p in paths), f"got: {paths}"
        assert paths[0]["target_usr"] == "u_b_probe"

    def test_an_ambiguous_target_labels_each_path(self, db):
        paths = find_call_path(db, CH, "ClassB::run_b", "probe")
        payload = [p for p in paths if "warning" not in p]
        assert payload, f"no path found: {paths}"
        for path in payload:
            assert path["target_qualified_name"] == "ClassB::probe", f"got: {path}"

    def test_one_caller_of_two_targets_gives_a_path_to_each(self, db):
        """The depth-1 step returned the FIRST direct edge and stopped.

        A function that calls two same-name methods reaches both, and the
        notice above the rows names both.  One row then made the notice
        read as a promise that the answer did not keep.
        """
        fid = upsert_file(db, CH, "src/both.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/both.cpp", "both", "both", "u_both", 500, 520,
                        kind="function"),
        ])
        insert_refs_batch(db, [
            _ref_row("u_a_probe", "src/both.cpp", 505, "u_both"),
            _ref_row("u_b_probe", "src/both.cpp", 506, "u_both"),
        ])
        db.commit()

        paths = find_call_path(db, CH, "both", "probe")
        payload = [p for p in paths if "warning" not in p]
        targets = {p["target_qualified_name"] for p in payload}
        assert targets == {"ClassA::probe", "ClassB::probe"}, (
            f"the depth-1 step stopped at the first edge: {payload}"
        )
        assert all(p["depth"] == 1 for p in payload), payload

    def test_one_direct_edge_still_answers_with_one_path(self, db):
        """A name that means one symbol keeps the shape that it had."""
        paths = find_call_path(db, CH, "ClassB::run_b", "ClassB::probe")
        assert len(paths) == 1, f"got: {paths}"
        assert paths[0]["depth"] == 1


class TestRepairFromUsr:
    """A reference with no caller is invisible to the graph, thus it is repaired."""

    @pytest.fixture
    def db_with_gaps(self, db):
        """Add the references that the AST walk left without a caller.

        The last two carry the shapes that dominate a real index: a static
        function-pointer table, which is a ``varglobal`` that spans lines,
        and a member initializer inside a ``class`` body.  Measured on one
        firmware index, a repair with no kind filter named a ``varglobal``
        1219 times and a ``class`` 455 times, against 629 real callables.
        """
        fid = upsert_file(db, CH, "src/gap.cpp", "cpp")
        insert_symbols_batch(db, [
            # An outer method and a lambda inside it, to test the innermost pick.
            _symbol_row(fid, "src/gap.cpp", "outer", "Gap::outer", "u_outer", 10, 90),
            _symbol_row(fid, "src/gap.cpp", "inner", "Gap::outer::lambda", "u_inner", 40, 50),
            _symbol_row(fid, "src/gap.cpp", "callee", "Gap::callee", "u_callee", 200, 210),
            # A static table of function pointers.  It holds the callee, and
            # it calls nothing: a variable is not a caller.
            _symbol_row(fid, "src/gap.cpp", "gapFcnTable", "gapFcnTable", "u_table",
                        300, 310, kind="varglobal"),
            # A class whose body holds a reference outside of any method.
            _symbol_row(fid, "src/gap.cpp", "Holder", "Holder", "u_holder",
                        400, 450, kind="class"),
        ])
        insert_refs_batch(db, [
            _ref_row("u_callee", "src/gap.cpp", 20, None),    # inside outer only
            _ref_row("u_callee", "src/gap.cpp", 45, None),    # inside outer AND the lambda
            _ref_row("u_callee", "src/gap.cpp", 500, None),   # inside nothing — file scope
            _ref_row("u_callee", "src/gap.cpp", 305, None, ref_kind="indirect"),
            _ref_row("u_callee", "src/gap.cpp", 405, None),
        ])
        db.commit()
        return db

    @pytest.fixture
    def db_with_one_line_body(self, db_with_gaps):
        """An inline accessor whose whole body sits on one line.

        Embedded headers carry this shape by habit — ``bool ready() const
        { return r_; }`` — and such a definition has ``end_line == line``.
        Measured over seven indexed projects: 66 to 1158 of them per index,
        and 54 caller-less references that no other body can claim.
        """
        insert_symbols_batch(db_with_gaps, [
            _symbol_row(db_with_gaps.execute(
                "SELECT id FROM files WHERE config_hash=? AND path=?",
                (CH, "src/gap.cpp"),
            ).fetchone()["id"], "src/gap.cpp", "ready", "Gap::ready", "u_ready",
                600, 600),
        ])
        insert_refs_batch(db_with_gaps, [
            _ref_row("u_callee", "src/gap.cpp", 600, None),
        ])
        db_with_gaps.commit()
        return db_with_gaps

    def _callers_of(self, conn, from_line: int) -> str | None:
        row = conn.execute(
            "SELECT from_usr FROM refs WHERE config_hash = ? AND from_file = ?"
            " AND from_line = ?",
            (CH, "src/gap.cpp", from_line),
        ).fetchone()
        return row["from_usr"]

    def test_a_reference_inside_one_body_gets_that_body(self, db_with_gaps):
        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})
        assert self._callers_of(db_with_gaps, 20) == "u_outer"

    def test_the_innermost_definition_wins(self, db_with_gaps):
        """A lambda inside a method is the real caller, not the method."""
        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})
        assert self._callers_of(db_with_gaps, 45) == "u_inner"

    def test_a_reference_at_file_scope_keeps_no_caller(self, db_with_gaps):
        """A NULL there is a fact, not a gap, thus the repair leaves it."""
        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})
        assert self._callers_of(db_with_gaps, 500) is None

    def test_a_function_pointer_table_is_not_a_caller(self, db_with_gaps):
        """A static table of function pointers holds a callee; it calls none.

        The row sits inside the initializer of a ``varglobal``, thus the
        innermost definition around it is a variable.  A variable that
        reads as a caller is a false edge, and ``refs`` records no
        provenance — nothing downstream can tell it from a real one.
        """
        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})
        assert self._callers_of(db_with_gaps, 305) is None

    def test_a_class_body_is_not_a_caller(self, db_with_gaps):
        """A member initializer sits in the class, and a class calls nothing."""
        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})
        assert self._callers_of(db_with_gaps, 405) is None

    def test_the_repair_runs_twice_without_failing(self, db_with_gaps):
        """A second index run meets the rows that the first one repaired.

        ``idx_refs_unique`` covers ``from_usr`` and SQLite reads two NULLs
        as distinct, thus a NULL row and its repaired twin can both exist.
        Writing the same USR into the NULL one would then collide, and the
        UPDATE would abort the whole index run.
        """
        insert_refs_batch(db_with_gaps, [
            _ref_row("u_callee", "src/gap.cpp", 20, "u_outer"),
        ])
        db_with_gaps.commit()

        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})

        rows = db_with_gaps.execute(
            "SELECT from_usr FROM refs WHERE config_hash = ? AND from_file = ?"
            " AND from_line = ?",
            (CH, "src/gap.cpp", 20),
        ).fetchall()
        # The duplicate is dropped rather than written twice, and the row
        # that survives names the caller.
        assert [r["from_usr"] for r in rows] == ["u_outer"], f"got: {rows}"

    def test_the_repair_makes_the_caller_visible_in_the_graph(self, db_with_gaps):
        """This is the defect that the report was about, end to end.

        The recursive CTE drops a row whose ``from_usr`` is NULL, thus the
        method reads as having no caller at all until the repair runs.
        """
        before = find_all_callers_recursive(db_with_gaps, CH, "Gap::callee", max_depth=3)
        assert before == [], f"expected the gap to hide every caller, got: {before}"

        _step_repair_from_usr(db_with_gaps, {"config_hash": CH})

        after = find_all_callers_recursive(db_with_gaps, CH, "Gap::callee", max_depth=3)
        names = {r["qualified_name"] for r in after}
        assert names == {"Gap::outer", "Gap::outer::lambda"}, f"got: {names}"

    def test_a_body_on_one_line_is_a_caller_too(self, db_with_one_line_body):
        """``end_line == line`` is a whole body, not a declaration.

        ``is_definition = 1`` already keeps a declaration out, thus a span
        of one line needs no guard of its own.  The guard that asked for
        ``end_line > line`` only lost the inline accessors.
        """
        _step_repair_from_usr(db_with_one_line_body, {"config_hash": CH})
        assert self._callers_of(db_with_one_line_body, 600) == "u_ready"

    def test_a_definition_with_no_extent_is_not_a_caller(self, db_with_one_line_body):
        """``end_line = 0`` means that the extent is missing, not that it is one line.

        Such a row would otherwise claim every reference of its file that
        sits on line 0 or below, and it describes no body at all.
        """
        fid = db_with_one_line_body.execute(
            "SELECT id FROM files WHERE config_hash=? AND path=?",
            (CH, "src/gap.cpp"),
        ).fetchone()["id"]
        insert_symbols_batch(db_with_one_line_body, [
            _symbol_row(fid, "src/gap.cpp", "noextent", "Gap::noextent",
                        "u_noextent", 0, 0),
        ])
        insert_refs_batch(db_with_one_line_body, [
            _ref_row("u_callee", "src/gap.cpp", 0, None),
        ])
        db_with_one_line_body.commit()

        _step_repair_from_usr(db_with_one_line_body, {"config_hash": CH})
        assert self._callers_of(db_with_one_line_body, 0) is None

    def test_two_bodies_of_one_span_resolve_the_same_way_twice(
        self, db_with_one_line_body,
    ):
        """The repair WRITES an edge, thus it must not depend on a scan order.

        Two definitions can share a span — one line that holds both, or
        generated code.  The span alone then ties, and SQLite may return
        either row.  A run that picks differently writes a different graph.
        """
        fid = db_with_one_line_body.execute(
            "SELECT id FROM files WHERE config_hash=? AND path=?",
            (CH, "src/gap.cpp"),
        ).fetchone()["id"]
        # Inserted after ``u_ready`` and sorting before it by USR.
        insert_symbols_batch(db_with_one_line_body, [
            _symbol_row(fid, "src/gap.cpp", "also", "Gap::also", "u_also", 600, 600),
        ])
        db_with_one_line_body.commit()

        _step_repair_from_usr(db_with_one_line_body, {"config_hash": CH})
        assert self._callers_of(db_with_one_line_body, 600) == "u_also", (
            "the span tied, thus the USR must settle it"
        )


class TestBackfillWritesNoGuess:
    """The cross-TU backfill must not invent an edge for an ambiguous name."""

    @pytest.fixture
    def db_for_backfill(self, db):
        """Give the file text that the backfill scans, and mark it a project file."""
        source = "\n".join([
            "void top() {",                 # line 1  (u_top spans 100..110, so use
            "    obj.probe();",             # line 2   a separate span below)
            "    obj.only();",              # line 3
            "    obj.emit(1);",             # line 4
            "}",                            # line 5
        ])
        db.execute(
            "UPDATE files SET content = ?, is_project = 1"
            " WHERE config_hash = ? AND path = ?",
            (source, CH, "src/main.cpp"),
        )
        # A body that covers the call lines, so the scan looks at them.
        fid = upsert_file(db, CH, "src/main.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/main.cpp", "scan", "Scan::scan", "u_scan", 1, 5),
            # Two overloads.  They share a qualified name and differ in the
            # parameters, which the regex of the backfill never sees.
            _symbol_row(fid, "src/main.cpp", "emit", "Scan::emit", "u_emit_int",
                        10, 12, signature="void emit(int)"),
            _symbol_row(fid, "src/main.cpp", "emit", "Scan::emit", "u_emit_char",
                        14, 16, signature="void emit(char)"),
        ])
        db.commit()
        return db

    def _edges_from_scan(self, conn) -> set[str]:
        return {
            r["to_usr"]
            for r in conn.execute(
                "SELECT to_usr FROM refs WHERE config_hash = ? AND from_usr = ?",
                (CH, "u_scan"),
            )
        }

    def test_an_ambiguous_method_name_makes_no_edge(self, db_for_backfill):
        """``probe`` means two methods, thus the backfill must stay silent."""
        from fw_context_mcp.indexer.ops import backfill_cross_tu_refs

        backfill_cross_tu_refs(db_for_backfill, CH, Path("/tmp/test"))
        edges = self._edges_from_scan(db_for_backfill)
        assert "u_a_probe" not in edges, "the backfill guessed ClassA::probe"
        assert "u_b_probe" not in edges, "the backfill guessed ClassB::probe"

    def test_an_unambiguous_method_name_still_makes_an_edge(self, db_for_backfill):
        """The guard must not silence the resolution that was always right."""
        from fw_context_mcp.indexer.ops import backfill_cross_tu_refs

        backfill_cross_tu_refs(db_for_backfill, CH, Path("/tmp/test"))
        assert "u_solo" in self._edges_from_scan(db_for_backfill), (
            "the one clear name lost its edge too"
        )

    def test_an_overload_makes_no_edge_either(self, db_for_backfill):
        """Two overloads share a qualified name, thus the name is ambiguous.

        The symbol map keys on the qualified name, and the two overloads
        collapse to one entry in it.  ``_resolve_method_usr`` then saw ONE
        candidate and answered with it, which is a coin flip between two
        bodies — the rule that an ambiguous name gets no edge never
        reached this shape.

        The regex of the backfill reads ``obj.emit(`` and nothing more.
        It cannot tell the overloads apart, thus neither can the answer.
        """
        from fw_context_mcp.indexer.ops import backfill_cross_tu_refs

        backfill_cross_tu_refs(db_for_backfill, CH, Path("/tmp/test"))
        edges = self._edges_from_scan(db_for_backfill)
        assert "u_emit_int" not in edges, "the backfill guessed one overload"
        assert "u_emit_char" not in edges, "the backfill guessed one overload"


class TestReferenceQueries:
    """find_refs answers for every symbol the name matches, and says so."""

    def test_a_qualified_name_gives_only_its_own_references(self, db):
        rows = find_refs(db, CH, "ClassB::probe", ref_kind=["call"])
        lines = sorted(r["from_line"] for r in rows)
        assert lines == [71], f"expected only ClassB's call site, got: {lines}"

    def test_a_bare_name_gives_the_references_of_each_symbol(self, db):
        """The older version picked one symbol with LIMIT 1 and said nothing."""
        rows = find_refs(db, CH, "probe", ref_kind=["call"])
        lines = sorted(r["from_line"] for r in rows)
        assert lines == [51, 52, 53, 71], f"got: {lines}"

    def test_a_bare_name_tags_each_row_with_its_symbol(self, db):
        rows = find_refs(db, CH, "probe", ref_kind=["call"])
        by_line = {r["from_line"]: r["target_qualified_name"] for r in rows}
        assert by_line[51] == "ClassA::probe", f"got: {by_line}"
        assert by_line[71] == "ClassB::probe", f"got: {by_line}"

    def test_a_single_match_returns_plain_rows(self, db):
        """An unambiguous name must keep the row that every reader expects."""
        rows = find_refs(db, CH, "Solo::only", ref_kind=["call"])
        assert rows, "no rows"
        assert "target_qualified_name" not in rows[0].keys(), (
            "the tag leaked into a clear answer"
        )

    def test_refs_for_symbol_does_not_resolve_a_name(self, db):
        """The USR decides, thus a caller that already chose cannot be overruled."""
        rows = refs_for_symbol(db, CH, "u_b_probe", "method", ref_kind=["call"])
        lines = sorted(r["from_line"] for r in rows)
        assert lines == [71], f"got: {lines}"


class TestSymbolContextIsAboutOneSymbol:
    """The body and the callers of one answer must describe one symbol.

    ``get_symbol_context`` resolved the name twice: ``_lookup_definition``
    for the body and ``find_refs`` for the callers.  The two ranked
    differently, thus one answer could hold the body of ClassA and the call
    sites of ClassB with nothing to say so.
    """

    def test_collect_callers_follows_the_chosen_symbol(self, db):
        row_a = _lookup_definition(db, CH, "ClassA::probe", preferred_kinds=None)
        row_b = _lookup_definition(db, CH, "ClassB::probe", preferred_kinds=None)

        callers_a = _collect_callers(db, CH, row_a, Path("/tmp/test"))
        callers_b = _collect_callers(db, CH, row_b, Path("/tmp/test"))

        assert {c["line"] for c in callers_a} == {51, 52, 53}, f"got: {callers_a}"
        assert {c["line"] for c in callers_b} == {71}, f"got: {callers_b}"

    def test_the_two_symbols_do_not_share_callers(self, db):
        """The regression: one bare name used to feed both halves separately."""
        row_a = _lookup_definition(db, CH, "ClassA::probe", preferred_kinds=None)
        callers_a = _collect_callers(db, CH, row_a, Path("/tmp/test"))
        assert 71 not in {c["line"] for c in callers_a}, (
            "ClassA's answer holds ClassB's call site"
        )


class TestSingleBodyToolsOfferTheChoice:
    """A tool that returns ONE body must offer the others as rows.

    A sentence that lists the alternatives makes the reader parse text and
    ask again.  These rows carry the class, the file, the line and the
    signature, thus the reader picks the right symbol in one more call.
    """

    def test_a_bare_name_offers_every_match_as_a_row(self, db):
        row = _lookup_definition(db, CH, "probe", preferred_kinds=None)
        out = _ambiguity(db, CH, "probe", row, Path("/tmp/test"))
        names = {c["qualified_name"] for c in out["candidates"]}
        assert names == {"ClassA::probe", "ClassB::probe"}, f"got: {names}"
        assert out["candidates_total"] == 2

    def test_each_row_names_the_class(self, db):
        """The field that tells two same-name methods apart at a glance."""
        row = _lookup_definition(db, CH, "probe", preferred_kinds=None)
        out = _ambiguity(db, CH, "probe", row, Path("/tmp/test"))
        by_name = {c["qualified_name"]: c["class"] for c in out["candidates"]}
        assert by_name["ClassA::probe"] == "ClassA", f"got: {by_name}"
        assert by_name["ClassB::probe"] == "ClassB", f"got: {by_name}"

    def test_each_row_carries_what_the_choice_needs(self, db):
        row = _lookup_definition(db, CH, "probe", preferred_kinds=None)
        out = _ambiguity(db, CH, "probe", row, Path("/tmp/test"))
        assert set(out["candidates"][0]) == {
            "qualified_name", "class", "kind", "file", "line", "signature",
        }, f"got: {sorted(out['candidates'][0])}"

    def test_the_warning_names_the_symbol_that_the_answer_is_about(self, db):
        row = _lookup_definition(db, CH, "probe", preferred_kinds=None)
        out = _ambiguity(db, CH, "probe", row, Path("/tmp/test"))
        chosen = row["qualified_name"]
        assert chosen in out["ambiguous_warning"]
        assert "candidates" in out["ambiguous_warning"]

    def test_a_qualified_name_offers_nothing(self, db):
        row = _lookup_definition(db, CH, "ClassB::probe", preferred_kinds=None)
        assert _ambiguity(db, CH, "ClassB::probe", row, Path("/tmp/test")) == {}

    def test_an_unambiguous_bare_name_offers_nothing(self, db):
        row = _lookup_definition(db, CH, "only", preferred_kinds=None)
        assert _ambiguity(db, CH, "only", row, Path("/tmp/test")) == {}

    def test_a_declaration_is_not_a_second_definition(self, db):
        """A declaration shares the USR of its definition — one symbol, not two."""
        fid = upsert_file(db, CH, "src/decl.hpp", "cpp")
        declaration = list(
            _symbol_row(fid, "src/decl.hpp", "only", "Solo::only", "u_solo", 5)
        )
        declaration[11] = 0  # is_definition
        insert_symbols_batch(db, [tuple(declaration)])
        db.commit()

        row = _lookup_definition(db, CH, "only", preferred_kinds=None)
        assert _ambiguity(db, CH, "only", row, Path("/tmp/test")) == {}


class TestAmbiguousAnswerStaysWithinItsPromises:
    """Two faults that a synthetic fixture alone did not show.

    Both were found by running the tools over a real index, where one name
    matches many symbols and most of them have no reference at all.
    """

    def test_the_limit_bounds_the_total_and_not_each_symbol(self, db):
        """``limit`` is documented as the maximum, thus it must hold.

        The first version applied it per symbol, so a caller that asked for
        8 rows about an ambiguous name got 11.
        """
        rows, _c, _t = find_refs_with_candidates(db, CH, "probe", ref_kind=["call"], limit=3)
        assert len(rows) == 3, f"asked for 3, got {len(rows)}"

    def test_every_symbol_appears_before_any_gets_a_second_row(self, db):
        """ClassA has three call sites and ClassB one.  Both must show.

        A plain concatenation with a total cap would spend the budget on
        ClassA and hide ClassB completely.
        """
        rows, _c, _t = find_refs_with_candidates(db, CH, "probe", ref_kind=["call"], limit=2)
        targets = {r["target_qualified_name"] for r in rows}
        assert targets == {"ClassA::probe", "ClassB::probe"}, f"got: {targets}"

    def test_the_candidates_hold_a_symbol_that_has_no_reference(self, db):
        """The notice must count what the name MATCHED, not what had rows.

        Measured on one real index: a name matched 18 symbols and 5 of them
        had callers, and a notice built from the rows said it matched 5.
        """
        fid = upsert_file(db, CH, "src/quiet.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/quiet.cpp", "probe", "ClassC::probe", "u_c_probe", 10, 20),
        ])
        db.commit()

        rows, candidates, _total = find_refs_with_candidates(
            db, CH, "probe", ref_kind=["call"], limit=50
        )
        named = {c.qualified_name for c in candidates}
        with_rows = {r["target_qualified_name"] for r in rows}

        assert named == {"ClassA::probe", "ClassB::probe", "ClassC::probe"}, f"got: {named}"
        assert "ClassC::probe" not in with_rows, "ClassC has no call site"
        assert len(named) > len(with_rows), (
            "the fixture no longer covers a matched symbol without rows"
        )


class TestTheNoticeStaysReadableAndHonest:
    """A notice is read by a model, thus its size and its counts both matter."""

    def test_it_writes_out_only_the_first_few_names(self):
        names = [f"Class{i}::probe" for i in range(30)]
        text = ambiguity_notice("probe", names, "callers")["warning"]
        assert "Class0::probe" in text
        assert "Class7::probe" in text
        assert "Class8::probe" not in text, "the notice spelled out too many names"
        assert "and 22 more" in text, f"got: {text}"

    def test_it_says_more_than_when_the_set_was_cut(self):
        """A cut set must not report an exact count that it cannot know."""
        names = [f"Class{i}::probe" for i in range(MAX_AMBIGUOUS_TARGETS)]
        text = ambiguity_notice("probe", names, "callers")["warning"]
        assert f"more than {MAX_AMBIGUOUS_TARGETS} symbols" in text, f"got: {text}"
        assert "can be missing from the rows" in text

    def test_a_small_set_reports_its_exact_count(self, db):
        rows = find_all_callers_recursive(db, CH, "probe", max_depth=3)
        assert "matches 2 symbols" in rows[0]["warning"], f"got: {rows[0]['warning']}"

    def test_a_known_total_beats_more_than(self):
        """The walk is cut; the count is not, thus the count is reported.

        ``get_source`` answers the same name with an exact
        ``candidates_total``.  A list tool that said "more than 50" for
        the same name left the reader unable to see that the two describe
        one ambiguity.
        """
        names = [f"Class{i}::probe" for i in range(MAX_AMBIGUOUS_TARGETS)]
        text = ambiguity_notice("probe", names, "callers", total=132)["warning"]
        assert "matches 132 symbols" in text, f"got: {text}"
        assert "more than" not in text, f"got: {text}"
        # The walk was still cut, thus the rows can miss a symbol.
        assert "can be missing from the rows" in text, f"got: {text}"

    def test_a_total_larger_than_the_walk_still_warns(self):
        """Fewer names walked than matched: the rows are incomplete."""
        names = [f"Class{i}::probe" for i in range(3)]
        text = ambiguity_notice("probe", names, "callers", total=9)["warning"]
        assert "matches 9 symbols" in text, f"got: {text}"
        assert "can be missing from the rows" in text, f"got: {text}"

    def test_the_two_tool_families_report_one_number(self, db):
        """The list tools and the one-body tools must agree.

        ``count_candidates`` is the single source of that number, thus the
        notice of ``find_all_callers_recursive`` and the
        ``candidates_total`` of ``get_source`` cannot drift apart.
        """
        total = count_candidates(db, CH, "probe")
        rows = find_all_callers_recursive(db, CH, "probe", max_depth=3)
        assert f"matches {total} symbols" in rows[0]["warning"], f"got: {rows[0]}"

        row = _lookup_definition(db, CH, "probe", preferred_kinds=None)
        body = _ambiguity(db, CH, "probe", row, Path("/tmp/test"))
        assert body["candidates_total"] == total, f"got: {body}"


class TestBareNameIsNotAQualifiedName:
    """A bare name carries no disambiguator, thus it means every match.

    A free function whose qualified_name equals its bare name used to sit
    alone at rank 0, and every method of that name became unreachable.
    Measured on one real index: a bare name gave one free function while
    the body tools answered with a method of the same name, and the two
    tools then described different symbols.
    """

    def test_a_free_function_does_not_hide_the_methods(self, db):
        fid = upsert_file(db, CH, "src/free.cpp", "cpp")
        insert_symbols_batch(db, [
            # qualified_name == name, as a function at file scope carries.
            _symbol_row(fid, "src/free.cpp", "probe", "probe", "u_free_probe", 5, 8,
                        kind="function"),
        ])
        db.commit()

        _, names = _resolve_target_usrs(db, CH, "probe")
        assert set(names) == {"ClassA::probe", "ClassB::probe", "probe"}, (
            f"the free function hid the methods: {names}"
        )

    def test_a_qualified_name_still_wins_alone(self, db):
        _, names = _resolve_target_usrs(db, CH, "ClassB::probe")
        assert names == ["ClassB::probe"], f"got: {names}"


class TestALabelTellsTwoCandidatesApart:
    """A C function at file scope makes the qualified name repeat.

    Measured on one Zephyr project: a name matched three symbols and the
    notice read ``clock_stop, clock_stop, clock_stop``, which tells a
    reader nothing.  Such a name takes its file.
    """

    def test_a_repeated_qualified_name_takes_its_file(self):
        candidates = [
            Candidate("u1", "clock_stop", "function", "drivers/a.c"),
            Candidate("u2", "clock_stop", "function", "drivers/b.c"),
            Candidate("u3", "Class::other", "method", "src/x.cpp"),
        ]
        labels = candidate_labels(candidates)
        assert labels[0] == "clock_stop (drivers/a.c)", f"got: {labels}"
        assert labels[1] == "clock_stop (drivers/b.c)", f"got: {labels}"
        # A name that stands alone stays typeable, thus it keeps no file.
        assert labels[2] == "Class::other", f"got: {labels}"

    def test_a_unique_name_keeps_the_plain_qualified_name(self, db):
        """The common case must not gain noise that the caller cannot type."""
        _, labels = _resolve_target_usrs(db, CH, "probe")
        assert set(labels) == {"ClassA::probe", "ClassB::probe"}, f"got: {labels}"

    def test_a_candidate_without_a_file_keeps_its_name(self):
        """A missing file must not produce an empty bracket."""
        candidates = [
            Candidate("u1", "same", "function", ""),
            Candidate("u2", "same", "function", ""),
        ]
        assert candidate_labels(candidates) == ["same", "same"]

    def test_an_overload_takes_its_parameter_list(self):
        """Overloads share a qualified name AND a file, thus only the
        parameters separate them.

        Measured on one real project: ``CoilData::set`` has five overloads,
        and two rows of one call site read ``CoilData.cpp:183 →
        CoilData::set`` twice, differing only in a USR the reader never
        sees.
        """
        candidates = [
            Candidate("u1", "CoilData::set", "method", "src/CoilData.cpp",
                      "CoilData", 10, "bool set(uint16_t index, bool value)"),
            Candidate("u2", "CoilData::set", "method", "src/CoilData.cpp",
                      "CoilData", 20, "bool set(uint16_t index, const char * iv)"),
        ]
        labels = candidate_labels(candidates)
        assert labels[0] == "CoilData::set(uint16_t index, bool value)", f"got: {labels}"
        assert labels[1] == "CoilData::set(uint16_t index, const char * iv)", f"got: {labels}"

    def test_the_parameter_list_wins_over_the_file(self):
        """Two overloads usually share a file, thus the file separates
        nothing and the parameters must be tried first."""
        candidates = [
            Candidate("u1", "A::f", "method", "src/a.cpp", "A", 1, "void f(int)"),
            Candidate("u2", "A::f", "method", "src/a.cpp", "A", 2, "void f(char)"),
        ]
        labels = candidate_labels(candidates)
        assert labels == ["A::f(int)", "A::f(char)"], f"got: {labels}"

    def test_a_signature_without_brackets_falls_back_to_the_file(self):
        candidates = [
            Candidate("u1", "clock_stop", "function", "drivers/a.c", "", 1, ""),
            Candidate("u2", "clock_stop", "function", "drivers/b.c", "", 2, ""),
        ]
        labels = candidate_labels(candidates)
        assert labels == ["clock_stop (drivers/a.c)", "clock_stop (drivers/b.c)"]


class TestTheCandidateOrderIsStable:
    """The order of the candidates must end at a column that is unique.

    This order does more than rank a list.  It decides which symbols a
    graph query walks when the name matches more than
    ``MAX_TRAVERSED_TARGETS``, it decides the interleave that
    ``find_refs_with_candidates`` cuts with an ``offset``, and it decides
    which candidates the body tools offer.  Two pages of one walk read two
    different orders when the order ties.

    Every ranking column ties over exactly the population that this
    resolver exists for: same-name methods in different classes share the
    definition flag, the project flag, and often both reference counts.
    """

    @pytest.fixture
    def tied_db(self, db):
        """Six methods of one name that tie on every ranking column.

        None of them carries a reference, thus ``ref_count`` and
        ``out_count`` are zero for all six and only the last column of the
        order can separate them.

        The rows go in with the HIGHEST USR first, so that the rowid order
        of the table is the reverse of the USR order.  Without that, a scan
        that keeps the rowid order answers in USR order by accident, and a
        test over it would pass with no tie-break at all.
        """
        fid = upsert_file(db, CH, "src/tied.cpp", "cpp")
        rows = []
        for i in reversed(range(6)):
            rows.append(_symbol_row(
                fid, "src/tied.cpp", f"Tied{i}", f"Tied{i}", f"u_tied_class_{i}",
                200 + i * 10, 205 + i * 10, kind="class",
            ))
            rows.append(_symbol_row(
                fid, "src/tied.cpp", "reset", f"Tied{i}::reset", f"u_tied_{i}",
                201 + i * 10, 204 + i * 10, parent_usr=f"u_tied_class_{i}",
            ))
        insert_symbols_batch(db, rows)
        db.commit()
        return db

    def test_the_order_ends_at_a_unique_column(self):
        """The invariant, because no test can force the fault.

        SQLite keeps the rowid order of a small table scan every time, thus
        the fault needs a plan change and a test cannot ask for one.  What
        holds either way: the order must END at a column that is unique
        within one build.  ``usr`` is that column.
        """
        from fw_context_mcp.indexer.db._resolve import _CANDIDATE_ORDER

        assert _CANDIDATE_ORDER.rstrip().endswith("s.usr"), _CANDIDATE_ORDER

    def test_six_tied_candidates_come_back_in_one_order(self, tied_db):
        """The same question twice must give the same answer twice."""
        first = [c.usr for c in resolve_candidates(tied_db, CH, "reset")]
        again = [c.usr for c in resolve_candidates(tied_db, CH, "reset")]
        assert len(first) == 6, f"got: {first}"
        assert first == again
        assert first == sorted(first), (
            f"every ranking column tied, thus the USR must order: {first}"
        )

    def test_a_cut_list_takes_the_same_head_as_the_whole_one(self, tied_db):
        """``limit`` cuts the order, thus a cut must not change the head.

        A graph query walks at most ``MAX_TRAVERSED_TARGETS`` symbols.  When
        the order ties, which symbols it walks is a guess, and two tools
        that ask with different bounds then describe different sets.
        """
        whole = [c.usr for c in resolve_candidates(tied_db, CH, "reset")]
        cut = [c.usr for c in resolve_candidates(tied_db, CH, "reset", limit=3)]
        assert cut == whole[:3], f"the cut took another head: {cut} vs {whole[:3]}"

    def test_the_count_still_reports_every_tied_symbol(self, tied_db):
        """The order bounds the list, and it must not reach the count."""
        assert count_candidates(tied_db, CH, "reset") == 6

    def test_the_rank_still_wins_over_the_usr(self, tied_db):
        """The tie-break is the LAST column, thus it cannot beat the rank.

        ``Tied5::reset`` sorts last by USR and first by rank, because the
        caller named its qualified name.
        """
        candidates = resolve_candidates(tied_db, CH, "Tied5::reset")
        assert [c.usr for c in candidates] == ["u_tied_5"], (
            f"an exact qualified name must stand alone: {candidates}"
        )
