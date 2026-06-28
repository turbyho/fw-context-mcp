"""Edge case tests for _parse_search_terms — LLM output parsing."""

from __future__ import annotations

from fw_context_mcp.search.phases.refine import _parse_search_terms


class TestParseSearchTerms:
    def test_json_array(self):
        # No underscores → returned as-is with whitespace strip
        result = _parse_search_terms('["modem*", "init*", "uart*"]')
        assert result == ["modem*", "init*", "uart*"]

    def test_json_with_surrounding_text(self):
        raw = 'Here are some terms:\n["modem init*", "uart send*"]\nHope this helps!'
        result = _parse_search_terms(raw)
        assert "modem init*" in result
        assert "uart send*" in result

    def test_empty_json_array(self):
        assert _parse_search_terms("[]") == []

    def test_bogus_json_filtered(self):
        # "json" and "[]" are in the BOGUS set — filtered out
        result = _parse_search_terms("json")
        assert result == []

    def test_fallback_line_by_line(self):
        raw = "  modem init*\n  uart send*\n  spi transfer*"
        result = _parse_search_terms(raw)
        # * stripped by line fallback, but spaces preserved
        assert "modem init" in result
        assert "uart send" in result
        assert "spi transfer" in result

    def test_numbered_lines(self):
        raw = "1. modem_init*\n2. uart_send*\n3. gpio_toggle*"
        result = _parse_search_terms(raw)
        # * stripped by line fallback, _ → space
        assert "modem init" in result
        assert "uart send" in result
        assert "gpio toggle" in result

    def test_lines_with_parentheses(self):
        raw = "modem_init* (for modem)\nuart_send* (data transfer)"
        result = _parse_search_terms(raw)
        # * stripped by line-by-line fallback
        assert "modem init" in result
        assert "uart send" in result
        # The parentheses content should be stripped
        for r in result:
            assert "(" not in r

    def test_empty_response(self):
        assert _parse_search_terms("") == []

    def test_whitespace_only(self):
        assert _parse_search_terms("   \n  \n\t") == []

    def test_underscore_replaced_with_space(self):
        # * stripped by line-by-line fallback
        result = _parse_search_terms("modem_init*")
        assert result == ["modem init"]

    def test_quotes_stripped(self):
        raw = '  "modem_init*"  '
        result = _parse_search_terms(raw)
        # Fallback strips quotes, *, and replaces _ with space
        assert result == ["modem init"]

    def test_backtick_stripped(self):
        raw = "`modem_init*`"
        result = _parse_search_terms(raw)
        # Fallback strips backticks, *, and replaces _ with space
        assert result == ["modem init"]

    def test_comment_lines_ignored(self):
        raw = "# This is a comment\nmodem_init*\n# Another comment"
        result = _parse_search_terms(raw)
        # "modem_init*" is on its own line, * stripped by fallback
        assert "modem init" in result

    def test_markdown_code_block(self):
        raw = '```json\n["modem*", "init*"]\n```'
        result = _parse_search_terms(raw)
        # JSON parser should find the array within the markdown
        # JSON path preserves *
        assert "modem*" in result
        assert "init*" in result

    def test_garbage_input(self):
        result = _parse_search_terms("\x00\x01\x02\n\xff\xfe")
        assert isinstance(result, list)

    def test_json_with_bogus_entry_filtered(self):
        raw = '["modem*", "json", "[]"]'
        result = _parse_search_terms(raw)
        assert "modem*" in result
        for r in result:
            assert r not in ("json", "[]")

    def test_mixed_json_and_text(self):
        raw = 'I think you should try: ["gpio*", "pin*"] also consider: "timer*"'
        result = _parse_search_terms(raw)
        # JSON array should be parsed, * preserved in JSON path
        assert "gpio*" in result
        assert "pin*" in result

    def test_invalid_json_falls_back(self):
        raw = '["modem*", broken json here'
        # Invalid JSON → fallback to line parsing
        # The whole thing is one line, gets cleaned to just the content
        result = _parse_search_terms(raw)
        # The result won't be the clean "modem*" due to brackets etc.
        # but the function returns something without crashing
        assert isinstance(result, list)

    def test_empty_strings_in_json_ignored(self):
        assert _parse_search_terms('["modem*", "", "  "]') == ["modem*"]

    def test_single_token_no_special_chars(self):
        # Line-by-line fallback strips * via .strip("`'\"*")
        result = _parse_search_terms("uart*")
        assert result == ["uart"]

    def test_multiple_underscores(self):
        # Line-by-line fallback: underscores→spaces, * stripped
        result = _parse_search_terms("nrfx_uarte_init*")
        assert result == ["nrfx uarte init"]

    def test_trim_whitespace_result(self):
        # Line-by-line fallback: whitespace trimmed, * stripped
        result = _parse_search_terms('  modem*  ')
        assert result == ["modem"]
