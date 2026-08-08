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
