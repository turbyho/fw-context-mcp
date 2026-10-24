"""Tests for prompt templates and response parsers."""

from __future__ import annotations

import json

from fw_context_mcp.indexer.prompts import parse_analysis_response


class TestParseAnalysisResponse:
    def test_valid_json_no_escapes(self):
        resp = '{"summary":"does x","inputs":"a","outputs":"b"}'
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert len(result) == 1
        assert result[0]["summary"] == "does x"

    def test_json_array_with_backslash_in_value(self):
        """The LLM writes '\\' to mean a literal backslash. This is valid JSON."""
        resp = json.dumps([{
            "summary": "removes item",
            "inputs": (
                "key must not include '*' '/' '?' ':' ';' '\\\\' '\\\"' '|'"
            ),
            "outputs": "MBED_SUCCESS on success",
        }])
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert len(result) == 1
        assert "must not include" in result[0]["inputs"]

    def test_a_trailing_comma_does_not_lose_the_answer(self):
        """The answer of gemma4:26b in a real analysis (2026-10-24), shortened.

        A comma after the last field made the answer invalid JSON, and the
        symbol got no analysis.
        """
        resp = (
            '```json\n[\n  {\n    "id": 1489569,\n'
            '    "summary": "A constant of 60000 ms for error timing.",\n'
            '    "inputs": "-",\n'
            '    "outputs": "The value 60000.",\n'
            '  }\n]\n```'
        )
        result = parse_analysis_response(resp, [{"id": 1489569}])
        assert result is not None
        assert result[0]["outputs"] == "The value 60000."

    def test_a_comma_and_brace_in_a_string_stay(self):
        """The removal works outside strings only."""
        resp = json.dumps([{"summary": "writes ',}' and ', ]' to the log", "inputs": "-", "outputs": "-"}])
        result = parse_analysis_response(resp, [{"id": 1}])
        assert result is not None
        assert result[0]["summary"] == "writes ',}' and ', ]' to the log"

    def test_json_array_in_markdown_fence(self):
        """Response with ```json fence should be stripped."""
        resp = '```json\n[{"summary":"test","inputs":"x","outputs":"y"}]\n```'
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert len(result) == 1
        assert result[0]["summary"] == "test"

    def test_fallback_json_array_extraction(self):
        """When JSON is embedded in prose, fallback extraction should find it."""
        resp = 'Here is the analysis:\n[{"summary":"hello","inputs":"world","outputs":""}]\nHope this helps!'
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert result[0]["summary"] == "hello"

    def test_escaped_backslash_in_real_world_scenario(self):
        """Full realistic LLM output from TDBStore analysis — must parse."""
        resp = json.dumps([{
            "summary": "Calculates the total size of a record",
            "inputs": "key (const char *): Key - must not include "
                       "'*' '/' '?' ':' ';' '\\\\' '\\\"' '|' ' ' '<' '>' '\\\\'",
            "outputs": "Returns total record size as uint32_t",
        }])
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert len(result) == 1

    def test_invalid_escapes_still_fixed(self):
        r"""Invalid escape like \' (not \\) should be fixed by stripping backslash."""
        # Raw response from LLM with invalid \' escape
        resp = r'{"summary":"test","inputs":"key must not include \'","outputs":"ok"}'
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        # The backslash before single-quote is stripped
        assert "\\'" not in result[0]["inputs"]

    def test_escaped_double_quote_in_value(self):
        """Escaped double-quote in JSON string value should parse correctly."""
        resp = json.dumps([{
            "summary": "test",
            "inputs": 'must not include \\"',
            "outputs": "ok",
        }])
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert "must not include" in result[0]["inputs"]

    def test_multiple_entries_in_batch(self):
        resp = json.dumps([
            {"summary": "first", "inputs": "a", "outputs": "b"},
            {"summary": "second", "inputs": "c", "outputs": "d"},
        ])
        batch = [{"id": 1}, {"id": 2}]
        result = parse_analysis_response(resp, batch)
        assert result is not None
        assert len(result) == 2
        assert result[0]["symbol_id"] == 1
        assert result[1]["symbol_id"] == 2

    def test_unparseable_returns_none(self):
        resp = "this is not json at all"
        batch = [{"id": 1}]
        result = parse_analysis_response(resp, batch)
        assert result is None


class TestAnalysisPrompt:
    """The prompt of the analysis: the task and a form of the answer, no example answer (decision 2026-10-22)."""

    SYMBOL = {"id": 7, "name": "f", "qualified_name": "f", "kind": "function", "file_path": "a.c",
              "signature": "int f(void)", "docstring": "", "body": "int f(void) { return 1; }"}

    def test_the_prompt_gives_the_form_of_the_answer_and_no_example_answer(self):
        """A model copies an example answer: each text field of the form is a placeholder only."""
        import re

        from fw_context_mcp.indexer.prompts import build_analysis_prompt

        prompt = build_analysis_prompt([self.SYMBOL])

        for field in ("summary", "inputs", "outputs"):
            assert f'"{field}": "<' in prompt
            assert not re.search(rf'"{field}": "[^<]', prompt), f"an example value of {field}"
        assert "(id: 7)" in prompt
