"""Coverage and staleness must agree — regression tests for ANALYZABLE_KINDS.

The analysis pipeline, the coverage report, and the staleness check all
import ``ANALYZABLE_KINDS``.  Before that, each had its own kind list, and
the staleness check missed the analysis work for 4 of the 10 kinds.
"""

from __future__ import annotations

from fw_context_mcp.indexer._llm_analysis import _select_unanalyzed_symbols
from fw_context_mcp.indexer.db import (
    compute_analysis_coverage,
    count_pending_analysis,
    make_analysis_summary,
    transaction,
    upsert_file,
    upsert_llm_analysis_batch,
)
from fw_context_mcp.indexer.db._projects import ANALYZABLE_KINDS
from fw_context_mcp.indexer.db._symbols import insert_symbols_batch
from fw_context_mcp.mcp.handlers.maintenance import _analysis_message

CONFIG_HASH = "hash-deadbeef"


def _add_symbol(conn, file_id, name, kind, *, is_project=1, is_definition=1):
    """Insert one symbol and return its row id."""
    insert_symbols_batch(
        conn,
        [
            (CONFIG_HASH, file_id, "src/test.cpp", name, f"usr-{name}", name,
             f"ns::{name}", kind, 10, 1, 20, is_definition, f"void {name}()",
             "", None, 0, 0, "", 0, "", is_project, 0.0, "body"),
        ],
    )
    return conn.execute(
        "SELECT id FROM symbols WHERE usr = ?", (f"usr-{name}",)
    ).fetchone()["id"]


def _add_kinds(conn, *, is_project=1, prefix="sym"):
    """Insert one definition symbol for every analyzable kind."""
    file_id = upsert_file(conn, CONFIG_HASH, "/tmp/test.cpp", "cpp")
    return {
        kind: _add_symbol(conn, file_id, f"{prefix}_{kind}", kind, is_project=is_project)
        for kind in ANALYZABLE_KINDS
    }


class TestKindCoverage:
    def test_coverage_counts_every_analyzable_kind(self, populated_db):
        with transaction(populated_db):
            _add_kinds(populated_db)

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        assert coverage["project"]["total"] == len(ANALYZABLE_KINDS)
        assert coverage["project"]["analyzed"] == 0
        assert coverage["project"]["skipped"] == 0

    def test_selection_query_returns_every_counted_kind(self, populated_db):
        """The pipeline must select exactly what the coverage report counts."""
        with transaction(populated_db):
            _add_kinds(populated_db)

        selected = _select_unanalyzed_symbols(populated_db, CONFIG_HASH, True)
        assert {row["kind"] for row in selected} == set(ANALYZABLE_KINDS)

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        assert len(selected) == coverage["project"]["total"]

    def test_pending_equals_selection_size(self, populated_db):
        with transaction(populated_db):
            _add_kinds(populated_db)

        selected = _select_unanalyzed_symbols(populated_db, CONFIG_HASH, True)
        pending = count_pending_analysis(
            populated_db, CONFIG_HASH, analyze_vendor=False
        )
        assert pending == len(selected) == len(ANALYZABLE_KINDS)


class TestSkipSentinel:
    def test_skip_row_counts_as_skipped_not_analyzed(self, populated_db):
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, "/tmp/test.cpp", "cpp")
            big = _add_symbol(populated_db, file_id, "big", "function")
            small = _add_symbol(populated_db, file_id, "small", "function")
            upsert_llm_analysis_batch(
                populated_db,
                [
                    (big, "", "", "", "skip:toolarge:16384", "h1"),
                    (small, "does a thing", "", "", "qwen2.5-coder:14b", "h2"),
                ],
            )

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        assert coverage["project"] == {"analyzed": 1, "skipped": 1, "total": 2}

    def test_skip_row_does_not_keep_the_reindex_pending(self, populated_db):
        """A skipped symbol must not restart the background reindex forever."""
        with transaction(populated_db):
            file_id = upsert_file(populated_db, CONFIG_HASH, "/tmp/test.cpp", "cpp")
            big = _add_symbol(populated_db, file_id, "big", "function")
            upsert_llm_analysis_batch(
                populated_db, [(big, "", "", "", "skip:unparseable:m", "h1")]
            )

        pending = count_pending_analysis(
            populated_db, CONFIG_HASH, analyze_vendor=False
        )
        assert pending == 0

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        summary = make_analysis_summary(coverage, False, "m")
        assert summary["complete"] is True


class TestCompleteAgreesWithPending:
    def test_complete_is_false_while_work_remains(self, populated_db):
        with transaction(populated_db):
            _add_kinds(populated_db)

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        summary = make_analysis_summary(coverage, False, None)
        pending = count_pending_analysis(
            populated_db, CONFIG_HASH, analyze_vendor=False
        )
        assert summary["complete"] is (pending == 0)
        assert summary["complete"] is False

    def test_vendor_symbols_do_not_block_completeness(self, populated_db):
        with transaction(populated_db):
            _add_kinds(populated_db, is_project=0, prefix="vendor")

        coverage = compute_analysis_coverage(populated_db, CONFIG_HASH)
        assert coverage["vendor"]["total"] == len(ANALYZABLE_KINDS)
        assert coverage["project"]["total"] == 0

        assert count_pending_analysis(
            populated_db, CONFIG_HASH, analyze_vendor=False
        ) == 0
        assert count_pending_analysis(
            populated_db, CONFIG_HASH, analyze_vendor=True
        ) == len(ANALYZABLE_KINDS)

        assert make_analysis_summary(coverage, False, None)["complete"] is True
        assert make_analysis_summary(coverage, True, None)["complete"] is False


class TestNoSecondKindList:
    """Guards against a new copy of the kind list somewhere else."""

    EXPECTED_KINDS = (
        "function",
        "method",
        "constructor",
        "destructor",
        "class",
        "struct",
        "union",
        "typedef",
        "enum",
        "varglobal",
    )

    def test_analyzable_kinds_is_the_documented_set(self):
        """Change this test together with every ANALYZABLE_KINDS consumer."""
        assert set(ANALYZABLE_KINDS) == set(self.EXPECTED_KINDS)

    def test_no_module_hardcodes_the_kind_list(self):
        """Only _projects.py may name the kinds — the rest must import them."""
        import importlib
        from pathlib import Path

        modules = [
            "fw_context_mcp.mcp.background",
            "fw_context_mcp.mcp.handlers.maintenance",
            "fw_context_mcp.indexer._llm_analysis",
        ]
        for name in modules:
            source = Path(importlib.import_module(name).__file__).read_text(
                encoding="utf-8"
            )
            assert "'function', 'method'" not in source, (
                f"{name} holds a second copy of the analyzable kind list — "
                "import ANALYZABLE_KINDS instead"
            )


class TestIndexMessage:
    """The human-readable analysis line must name the skipped symbols."""

    @staticmethod
    def _analysis(
        p_analyzed, p_skipped, p_total, v_analyzed=0, v_skipped=0, v_total=0
    ):
        return make_analysis_summary(
            {
                "project": {
                    "analyzed": p_analyzed,
                    "skipped": p_skipped,
                    "total": p_total,
                },
                "vendor": {
                    "analyzed": v_analyzed,
                    "skipped": v_skipped,
                    "total": v_total,
                },
            },
            False,
            "qwen2.5-coder:14b",
        )

    def test_no_skips_vendor_disabled(self):
        """The line of a healthy project — locks the current wording."""
        message = _analysis_message(self._analysis(3372, 0, 3372, 0, 0, 22122), False)
        assert message == (
            " | LLM analysis: project 3372/3372 "
            "(vendor skipped — analyze_vendor=false)"
        )

    def test_project_skips_vendor_disabled(self):
        message = _analysis_message(self._analysis(283, 3, 288, 0, 0, 7540), False)
        assert message == (
            " | LLM analysis: project 283/288, 3 project symbols skipped "
            "(vendor skipped — analyze_vendor=false)"
        )
        assert "288" in message and "283" in message

    def test_no_skips_vendor_enabled(self):
        message = _analysis_message(self._analysis(288, 0, 288, 7540, 0, 7540), True)
        assert message == " | LLM analysis: project 288/288, vendor 7540/7540"

    def test_skips_on_both_sides_vendor_enabled(self):
        message = _analysis_message(self._analysis(283, 5, 288, 7000, 40, 7540), True)
        assert message == (
            " | LLM analysis: project 283/288, vendor 7000/7540 "
            "(5 project, 40 vendor symbols skipped)"
        )

    def test_message_starts_with_the_separator(self):
        """get_active_build appends this part to an existing message."""
        for analyze_vendor in (False, True):
            message = _analysis_message(
                self._analysis(1, 1, 2, 1, 1, 2), analyze_vendor
            )
            assert message.startswith(" | LLM analysis: ")
