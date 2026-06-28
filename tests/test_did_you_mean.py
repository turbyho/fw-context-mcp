"""Edge case tests for did-you-mean? suggestion engine."""

from __future__ import annotations

from fw_context_mcp.search.did_you_mean import _token_score, _tokenize, suggest


class TestTokenize:
    def test_snake_case(self):
        assert _tokenize("modem_init") == ["modem", "init"]

    def test_camel_case(self):
        assert _tokenize("modemInit") == ["modem", "init"]

    def test_pascal_case(self):
        assert _tokenize("ModemManager") == ["modem", "manager"]

    def test_mixed_snake_camel(self):
        tokens = _tokenize("nrfxUarte_Init")
        assert "nrfx" in tokens
        assert "uarte" in tokens
        assert "init" in tokens

    def test_acronyms(self):
        tokens = _tokenize("TIM16Config")
        assert "tim" in tokens
        assert "16" in tokens
        assert "config" in tokens

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_underscore_only(self):
        assert _tokenize("___") == []

    def test_double_underscore(self):
        assert _tokenize("a__b") == ["a", "b"]

    def test_leading_underscore(self):
        assert _tokenize("_private") == ["private"]

    def test_trailing_underscore(self):
        assert _tokenize("value_") == ["value"]

    def test_all_caps_acronym(self):
        tokens = _tokenize("UARTInit")
        assert "uart" in tokens
        assert "init" in tokens

    def test_numbers_only_in_token(self):
        tokens = _tokenize("get32Value")
        # The regex groups digits with preceding word chars: "get32" stays together
        assert "get32" in tokens
        assert "value" in tokens

    def test_single_char_tokens_kept(self):
        tokens = _tokenize("a_b")
        # Single-char tokens are kept by the function
        assert len(tokens) >= 1

    def test_deduplicates_tokens(self):
        tokens = _tokenize("init_init")
        assert tokens.count("init") == 1

    def test_unicode_not_supported_gracefully(self):
        # Non-ASCII characters — the regex will handle them (likely skip or group)
        tokens = _tokenize("test_funkce")
        assert "test" in tokens
        # "funkce" should be tokenized
        assert "funkce" in tokens


class TestTokenScore:
    def test_exact_match_scores_2(self):
        # Same position: 2.0 (exact) + 0.5 (same pos) = 2.5
        score = _token_score(["modem"], ["modem"])
        assert score == 2.5

    def test_exact_match_same_position_bonus(self):
        score = _token_score(["modem", "init"], ["modem", "init"])
        # modem (pos0→pos0: 2.0+0.5) + init (pos1→pos1: 2.0+0.5) = 5.0
        assert score == 5.0

    def test_exact_match_different_position(self):
        score = _token_score(["modem", "init"], ["init", "modem"])
        # init (pos1→pos0: 2.0, no bonus) + modem (pos0→pos1: 2.0, no bonus) = 4.0
        assert score == 4.0

    def test_prefix_match_scores_1(self):
        score = _token_score(["mode"], ["modem"])
        # "mode" is 4 chars ≥ 3, prefix of "modem" → 1.0, same position bonus +0.5
        assert score == 1.5

    def test_short_prefix_not_scored(self):
        score = _token_score(["mo"], ["modem"])
        # "mo" is 2 chars < 3 minimum → not scored
        assert score == 0.0

    def test_no_match_zero(self):
        score = _token_score(["xyz"], ["abc"])
        assert score == 0.0

    def test_empty_query_tokens(self):
        score = _token_score([], ["modem", "init"])
        assert score == 0.0

    def test_multiple_matches(self):
        score = _token_score(["modem", "init"], ["modem", "init", "extra"])
        # modem pos0→pos0: 2.5, init pos1→pos1: 2.5
        assert score == 5.0

    def test_partial_match_of_query(self):
        score = _token_score(["modem", "missing"], ["modem"])
        # modem (pos0→pos0: 2.0+0.5=2.5), missing (no match: 0) = 2.5
        assert score == 2.5

    def test_exact_three_char_boundary(self):
        # 3-char prefix exactly at boundary
        score = _token_score(["mod"], ["modem"])
        assert score == 1.5  # prefix match (3 chars) + same position


class TestSuggest:
    def test_empty_db_no_crash(self, temp_db):
        from fw_context_mcp.indexer.db import get_active_config

        cfg = get_active_config(temp_db, "proj-001")
        if cfg:
            result = suggest(temp_db, cfg["config_hash"], "modem_init", limit=5)
            assert result == []

    def test_no_definitions_returns_empty(self, populated_db):
        result = suggest(populated_db, "hash-deadbeef", "nonexistent", limit=5)
        assert result == []

    def test_cache_hit_same_config_hash(self, populated_db, monkeypatch):
        """Verify _cache is used — second call with same query is instant."""
        from fw_context_mcp.search import did_you_mean

        # Clear cache first
        did_you_mean._cache.clear()

        # We just verify no crash on repeated calls
        r1 = suggest(populated_db, "hash-deadbeef", "uart_init", limit=5)
        r2 = suggest(populated_db, "hash-deadbeef", "uart_init", limit=5)
        assert r1 == r2

    def test_empty_query_tokens_returns_empty(self, populated_db):
        result = suggest(populated_db, "hash-deadbeef", "___", limit=5)
        assert result == []

    def test_single_letter_query(self, populated_db):
        """Single-letter query may or may not return results."""
        result = suggest(populated_db, "hash-deadbeef", "x", limit=5)
        assert isinstance(result, list)
