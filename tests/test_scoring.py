"""Edge case tests for search result scoring."""

from __future__ import annotations

from fw_context_mcp.search.scoring import score_result, stems_from_queries


class TestScoreResult:
    def test_name_match_scores_3(self):
        row = {"name": "modem_init", "name_tokens": "modem init", "kind": "function"}
        s = score_result(row, ["modem_init"])
        assert s == 3 + 2  # 3 for name match + 2 for kind

    def test_name_tokens_match_scores_3(self):
        row = {"name": "mdm_init", "name_tokens": "modem init", "kind": "function"}
        s = score_result(row, ["modem"])
        assert s == 3 + 2

    def test_qualified_name_match_scores_2(self):
        row = {
            "name": "init",
            "qualified_name": "ns::modem::init",
            "name_tokens": "init",
            "kind": "function",
        }
        s = score_result(row, ["modem"])
        assert s == 2 + 2  # 2 for qname match + 2 for kind

    def test_file_path_match_scores_1(self):
        row = {
            "name": "helper",
            "qualified_name": "helper",
            "name_tokens": "helper",
            "file_path": "modem/driver.c",
            "kind": "function",
        }
        s = score_result(row, ["modem"])
        assert s == 1 + 2

    def test_project_local_bonus_is_project(self):
        row = {
            "name": "fn",
            "name_tokens": "fn",
            "file_path": "src/main.c",
            "kind": "function",
            "is_project": 1,
        }
        s = score_result(row, ["fn"])
        # 3 (name) + 1 (project-local via is_project) + 2 (function kind)
        assert s == 6

    def test_no_project_local_bonus_outside_project(self):
        row = {
            "name": "fn",
            "name_tokens": "fn",
            "file_path": "vendor/sdk.c",
            "kind": "function",
            "is_project": 0,
        }
        s = score_result(row, ["fn"])
        assert s == 3 + 2  # no local bonus

    def test_kind_weight_applied(self):
        row = {"name": "init", "name_tokens": "init", "kind": "struct"}
        s = score_result(row, ["init"])
        # 3 (name) + KIND_WEIGHT["struct"] (which is 2) = 5
        assert s == 5

    def test_variable_kind_zero_weight(self):
        row = {"name": "var", "name_tokens": "var", "kind": "variable"}
        s = score_result(row, ["var"])
        assert s == 3 + 0  # 3 (name) + 0 (variable weight)

    def test_unknown_kind_zero_weight(self):
        row = {"name": "zz", "name_tokens": "zz", "kind": "unknown_kind"}
        s = score_result(row, ["zz"])
        assert s == 3  # no kind bonus for unknown

    def test_empty_row_scores_zero(self):
        s = score_result({}, ["query"])
        assert s == 0

    def test_empty_query_stems(self):
        row = {"name": "fn", "name_tokens": "fn", "kind": "function"}
        s = score_result(row, [])
        assert s == 2  # only kind weight

    def test_single_char_stem_ignored(self):
        row = {"name": "a_fn", "name_tokens": "a fn", "kind": "function"}
        s = score_result(row, ["a"])
        assert s == 2  # stem "a" skipped (len < 2), only kind weight

    def test_single_char_stem_ignored_but_multi_char_counts(self):
        row = {"name": "a_init", "name_tokens": "a init", "kind": "function"}
        s = score_result(row, ["a", "init"])
        # "a" skipped (len 1), "init" matches name → +3, +2 for kind
        assert s == 5

    def test_case_insensitive_match(self):
        row = {"name": "ModemInit", "name_tokens": "modem init", "kind": "function"}
        s = score_result(row, ["modem"])
        assert s == 3 + 2  # name_tokens lowercase matches

    def test_all_none_fields(self):
        row = {"name": None, "qualified_name": None, "name_tokens": None, "kind": None}
        s = score_result(row, ["test"])
        assert s == 0

    def test_multiple_stems_sum_scores(self):
        row = {
            "name": "modem_init",
            "qualified_name": "ns::modem_init",
            "name_tokens": "modem init",
            "file_path": "modem/driver.c",
            "kind": "function",
            "is_project": 1,
        }
        s = score_result(row, ["modem", "init"])
        # modem: matches name (+3), init: matches name (+3)
        # project-local +1, function +2
        assert s == 3 + 3 + 1 + 2  # = 9


class TestStemsFromQueries:
    def test_strips_trailing_wildcard(self):
        assert stems_from_queries(["modem*", "init*"]) == ["modem", "init"]

    def test_no_wildcard_unchanged(self):
        assert stems_from_queries(["modem", "init"]) == ["modem", "init"]

    def test_empty_list(self):
        assert stems_from_queries([]) == []

    def test_empty_strings_filtered(self):
        assert stems_from_queries(["modem", "", "init"]) == ["modem", "init"]

    def test_all_empty(self):
        assert stems_from_queries(["", ""]) == []

    def test_lowercase_conversion(self):
        assert stems_from_queries(["Modem*", "INIT"]) == ["modem", "init"]

    def test_mixed_wildcards(self):
        assert stems_from_queries(["a*", "b", "c*"]) == ["a", "b", "c"]
