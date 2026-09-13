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
    find_refs,
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
)
from fw_context_mcp.mcp.handlers.source import (
    _ambiguity,
    _collect_callers,
    _lookup_definition,
)

CH = "hash-deadbeef"


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


def _ref_row(to_usr: str, from_file: str, from_line: int, from_usr: str | None) -> tuple:
    """Build one ``call`` row for insert_refs_batch.  The tail is slot_index."""
    return (CH, to_usr, from_file, from_line, from_usr, "call", None)


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


class TestRepairFromUsr:
    """A reference with no caller is invisible to the graph, thus it is repaired."""

    @pytest.fixture
    def db_with_gaps(self, db):
        """Add three references that the AST walk left without a caller."""
        fid = upsert_file(db, CH, "src/gap.cpp", "cpp")
        insert_symbols_batch(db, [
            # An outer method and a lambda inside it, to test the innermost pick.
            _symbol_row(fid, "src/gap.cpp", "outer", "Gap::outer", "u_outer", 10, 90),
            _symbol_row(fid, "src/gap.cpp", "inner", "Gap::outer::lambda", "u_inner", 40, 50),
            _symbol_row(fid, "src/gap.cpp", "callee", "Gap::callee", "u_callee", 200, 210),
        ])
        insert_refs_batch(db, [
            _ref_row("u_callee", "src/gap.cpp", 20, None),    # inside outer only
            _ref_row("u_callee", "src/gap.cpp", 45, None),    # inside outer AND the lambda
            _ref_row("u_callee", "src/gap.cpp", 500, None),   # inside nothing — file scope
        ])
        db.commit()
        return db

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


class TestBackfillWritesNoGuess:
    """The cross-TU backfill must not invent an edge for an ambiguous name."""

    @pytest.fixture
    def db_for_backfill(self, db):
        """Give the file text that the backfill scans, and mark it a project file."""
        source = "\n".join([
            "void top() {",                 # line 1  (u_top spans 100..110, so use
            "    obj.probe();",             # line 2   a separate span below)
            "    obj.only();",              # line 3
            "}",                            # line 4
        ])
        db.execute(
            "UPDATE files SET content = ?, is_project = 1"
            " WHERE config_hash = ? AND path = ?",
            (source, CH, "src/main.cpp"),
        )
        # A body that covers the two call lines, so the scan looks at them.
        fid = upsert_file(db, CH, "src/main.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/main.cpp", "scan", "Scan::scan", "u_scan", 1, 4),
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
        rows, _ = find_refs_with_candidates(db, CH, "probe", ref_kind=["call"], limit=3)
        assert len(rows) == 3, f"asked for 3, got {len(rows)}"

    def test_every_symbol_appears_before_any_gets_a_second_row(self, db):
        """ClassA has three call sites and ClassB one.  Both must show.

        A plain concatenation with a total cap would spend the budget on
        ClassA and hide ClassB completely.
        """
        rows, _ = find_refs_with_candidates(db, CH, "probe", ref_kind=["call"], limit=2)
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

        rows, candidates = find_refs_with_candidates(
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
