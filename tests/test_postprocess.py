"""Tests for indexer/_postprocess.py — O1: namespace-aware override detection."""

from __future__ import annotations

import pytest


class TestNormalizeTypeNamespaces:
    """Verify _normalize_type_namespaces strips namespace qualifiers."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer._postprocess import _normalize_type_namespaces

        self._fn = _normalize_type_namespaces

    def test_strips_single_qualifier(self):
        assert self._fn("const ble::ConnectionCompleteEvent &") == "const ConnectionCompleteEvent &"

    def test_multi_param(self):
        assert self._fn("int,const std::string &,size_t") == "int,const string &,size_t"

    def test_empty_string(self):
        assert self._fn("") == ""

    def test_no_namespace_unchanged(self):
        assert self._fn("int,const char *") == "int,const char *"


class TestOverrideWithNamespaceMismatch:
    """Verify two-phase comparison catches cross-namespace overrides."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from fw_context_mcp.indexer._postprocess import _extract_param_types, _normalize_type_namespaces

        self._extract = _extract_param_types
        self._normalize = _normalize_type_namespaces

    def test_literal_mismatch_normalized_match(self):
        derived_sig = "void onConnectionComplete(const ble::ConnectionCompleteEvent &)"
        base_sig = "void onConnectionComplete(const ConnectionCompleteEvent &)"
        derived_params = self._extract(derived_sig)
        base_params = self._extract(base_sig)
        assert derived_params != base_params, "Literal comparison must fail"
        assert self._normalize(derived_params) == self._normalize(base_params), (
            "Normalized comparison must match"
        )


class TestStepOrdering:
    """Verify postprocess step ordering invariants."""

    def test_llm_analysis_runs_before_embeddings(self):
        """Summaries must exist before embeddings are hashed.

        The embedding content hash includes the LLM summary.  When embeddings
        run first, the first index stores a hash without summaries, then the
        analysis step fills them — so the NEXT index re-embeds every analyzed
        symbol.  This ordering guard prevents that regression.
        """
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names.index("llm_analysis") < names.index("embeddings")

    def test_fts5_runs_before_embeddings_and_analysis(self):
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names.index("fts5") < names.index("embeddings")
        assert names.index("fts5") < names.index("llm_analysis")

    def test_cleanup_old_runs_last_before_checkpoint(self):
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names[-1] == "wal_checkpoint"
        assert names[-2] == "cleanup_old"
