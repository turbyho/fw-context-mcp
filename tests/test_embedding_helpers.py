"""Edge case tests for embedding helper functions."""

from __future__ import annotations

import math

import pytest

from fw_context_mcp.search.phases.embedding_helpers import (
    brute_force_search,
    table_exists,
    table_has_rows,
)


class TestTableExists:
    def test_table_exists_true(self, temp_db):
        assert table_exists(temp_db, "symbols") is True

    def test_table_exists_false(self, temp_db):
        assert table_exists(temp_db, "nonexistent_table") is False

    def test_table_exists_empty(self, temp_db):
        assert table_exists(temp_db, "") is False

    def test_table_exists_case_sensitive(self, temp_db):
        # SQLite table names are case-insensitive normally, but sqlite_master
        # stores them as-is. Table names in this codebase are lowercase.
        assert table_exists(temp_db, "SYMBOLS") is False


class TestTableHasRows:
    def test_table_has_rows_true(self, populated_db):
        # populated_db has projects and build_configs tables with data
        assert table_has_rows(populated_db, "projects") is True

    def test_table_has_rows_empty(self, temp_db):
        # symbols table exists but is empty
        assert table_has_rows(temp_db, "symbols") is False

    def test_table_has_rows_missing(self, temp_db):
        assert table_has_rows(temp_db, "nonexistent") is False


class TestBruteForceSearch:
    def test_basic_cosine_similarity(self):
        query = [1.0, 0.0, 0.0]
        stored = {1: [1.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0]}
        # ID 2 has cosine = 0.0 which fails strict > 0.0
        result = brute_force_search(query, stored, threshold=-0.1)
        # ID 1: cos=1.0, ID 2: cos=0.0 → both pass threshold=-0.1
        assert len(result) == 2
        assert result[0][0] == 1
        assert abs(result[0][1] - 1.0) < 0.001
        assert result[1][0] == 2
        assert abs(result[1][1] - 0.0) < 0.001

    def test_threshold_filters(self):
        query = [1.0, 0.0]
        stored = {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [0.707, 0.707]}
        result = brute_force_search(query, stored, threshold=0.6)
        # ID 1: cos=1.0 → included, ID 2: cos=0.0 → excluded, ID 3: cos=0.707 → included
        ids = {r[0] for r in result}
        assert 1 in ids
        assert 2 not in ids
        assert 3 in ids

    def test_zero_query_vector(self):
        query = [0.0, 0.0]
        stored = {1: [1.0, 0.0]}
        # norm_a = 0, so sim = 0; strict > threshold(0) → excluded
        result = brute_force_search(query, stored, threshold=-0.1)
        assert len(result) == 1
        assert abs(result[0][1] - 0.0) < 0.001

    def test_zero_stored_vector(self):
        query = [1.0, 0.0]
        stored = {1: [0.0, 0.0]}
        # norm_b = 0, so sim = 0
        result = brute_force_search(query, stored, threshold=-0.1)
        assert len(result) == 1
        assert abs(result[0][1] - 0.0) < 0.001

    def test_both_zero_vectors(self):
        query = [0.0, 0.0]
        stored = {1: [0.0, 0.0]}
        result = brute_force_search(query, stored, threshold=-0.1)
        # Both norms = 0 → sim = 0
        assert len(result) == 1
        assert abs(result[0][1] - 0.0) < 0.001

    def test_empty_stored(self):
        query = [1.0, 0.0]
        result = brute_force_search(query, {}, threshold=0.0)
        assert result == []

    def test_identity_vectors_equal_one(self):
        query = [3.0, 4.0]  # norm = 5
        stored = {1: [6.0, 8.0]}  # scaled 2x: norm = 10
        result = brute_force_search(query, stored, threshold=0.0)
        # (18+32)/(5*10) = 50/50 = 1.0
        assert abs(result[0][1] - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        query = [1.0, 0.0]
        stored = {1: [0.0, 1.0]}
        result = brute_force_search(query, stored, threshold=-0.1)
        assert len(result) == 1
        assert abs(result[0][1] - 0.0) < 0.001

    def test_negative_values(self):
        query = [1.0, 0.0]
        stored = {1: [-1.0, 0.0]}
        result = brute_force_search(query, stored, threshold=-2.0)
        # cos = -1/(1*1) = -1.0, threshold -2.0 → passes
        assert len(result) == 1
        assert abs(result[0][1] + 1.0) < 0.001

    def test_sort_order_descending(self):
        query = [1.0, 0.0]
        stored = {
            1: [0.0, 1.0],  # cos=0.0
            2: [0.707, 0.707],  # cos=0.707
            3: [1.0, 0.0],  # cos=1.0
        }
        result = brute_force_search(query, stored, threshold=-0.5)
        # Should be sorted: 3 (1.0), 2 (0.707), 1 (0.0)
        assert result[0][0] == 3
        assert result[1][0] == 2
        assert result[2][0] == 1

    def test_high_threshold_nothing_passes(self):
        query = [1.0, 0.0]
        stored = {1: [0.707, 0.707]}  # cos=0.707
        result = brute_force_search(query, stored, threshold=0.8)
        assert result == []

    def test_mismatched_dimensions_raises(self):
        query = [1.0, 0.0, 0.0]
        stored = {1: [1.0, 0.0]}  # wrong length
        with pytest.raises(ValueError):
            brute_force_search(query, stored)

    def test_large_vectors(self):
        # Test with realistic embedding dimensions (1024-dim)
        import random
        random.seed(42)
        dim = 128  # Use smaller dim for speed
        query = [random.random() for _ in range(dim)]
        stored = {}
        for i in range(10):
            stored[i] = [random.random() for _ in range(dim)]
        result = brute_force_search(query, stored, threshold=-1.0)
        # All should be returned (very low threshold)
        assert len(result) == 10
        # Sorted descending
        sims = [r[1] for r in result]
        assert sims == sorted(sims, reverse=True)
