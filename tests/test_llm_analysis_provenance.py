"""Model-written text must not sit among indexed facts.

``summary``, ``inputs``, and ``outputs`` come from an LLM.  They used to be
top-level keys of a search result, next to ``signature`` and ``docstring``,
which come from the code.  Nothing in the result separated the two, and a
reader quoted a guess — measured on one project, a summary said that an
identifier was "possibly related to battery status or level".

They now go under ``llm_analysis``.  These tests pin the shape, and pin
that the reranker still reads the summary through it.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.search.reranker import CrossEncoderReranker
from fw_context_mcp.search.shared_fallbacks import _symbol_row_to_dict

ROOT = Path("/proj")


def _row(**overrides) -> dict:
    base = {
        "name": "get_bv",
        "qualified_name": "zbox::ZRTDATA::get_bv",
        "kind": "method",
        "file_path": "src/zrtdata.cpp",
        "line": 282,
        "is_definition": 1,
        "signature": "bool get_bv(uint16_t & val)",
        "docstring": "",
    }
    base.update(overrides)
    return base


class TestAnalysisIsNested:
    def test_the_three_fields_go_under_one_key(self) -> None:
        d = _symbol_row_to_dict(
            _row(summary="Reads the battery voltage.", inputs="val", outputs="true"),
            ROOT,
        )
        assert d["llm_analysis"] == {
            "summary": "Reads the battery voltage.",
            "inputs": "val",
            "outputs": "true",
        }

    def test_no_flat_key_survives(self) -> None:
        """A flat key is the defect — a reader cannot tell it from a fact."""
        d = _symbol_row_to_dict(_row(summary="s", inputs="i", outputs="o"), ROOT)
        for leaked in ("summary", "inputs", "outputs"):
            assert leaked not in d, f"{leaked} must live under llm_analysis"

    def test_the_key_is_absent_without_an_analysis(self) -> None:
        assert "llm_analysis" not in _symbol_row_to_dict(_row(), ROOT)

    def test_an_empty_field_is_dropped(self) -> None:
        d = _symbol_row_to_dict(_row(summary="s", inputs="", outputs=None), ROOT)
        assert d["llm_analysis"] == {"summary": "s"}

    def test_the_facts_from_the_code_stay_at_the_top(self) -> None:
        d = _symbol_row_to_dict(_row(summary="s"), ROOT)
        assert d["signature"] == "bool get_bv(uint16_t & val)"
        assert d["kind"] == "method"
        assert d["line"] == 282


class TestRerankerReadsTheWrapper:
    """The reranker used the flat key; the wrapper must not blind it."""

    def test_a_nested_summary_reaches_the_description(self) -> None:
        desc = CrossEncoderReranker._describe(
            {"kind": "method", "name": "get_bv", "llm_analysis": {"summary": "battery voltage"}}
        )
        assert "battery voltage" in desc

    def test_a_flat_summary_still_reaches_it(self) -> None:
        # Phase code hands raw symbol rows in, and those stay flat.
        desc = CrossEncoderReranker._describe(
            {"kind": "method", "name": "get_bv", "summary": "battery voltage"}
        )
        assert "battery voltage" in desc

    def test_no_summary_gives_no_crash(self) -> None:
        assert "get_bv" in CrossEncoderReranker._describe({"kind": "method", "name": "get_bv"})
