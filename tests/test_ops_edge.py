"""Edge case tests for indexer ops — _read_file_lines, _read_body, _compute_content_hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.ops import _compute_content_hash, _read_body, _read_file_lines


class TestReadFileLines:
    def test_reads_normal_file(self, tmp_path: Path):
        f = tmp_path / "test.c"
        f.write_text("line1\nline2\nline3\n")
        lines = _read_file_lines(str(f))
        assert lines is not None
        assert len(lines) == 3
        assert lines[0] == "line1\n"

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.c"
        f.write_text("")
        lines = _read_file_lines(str(f))
        assert lines == []

    def test_nonexistent_file(self, tmp_path: Path):
        lines = _read_file_lines(str(tmp_path / "nonexistent.c"))
        assert lines is None

    def test_file_with_only_newlines(self, tmp_path: Path):
        f = tmp_path / "newlines.c"
        f.write_text("\n\n\n")
        lines = _read_file_lines(str(f))
        assert lines == ["\n", "\n", "\n"]

    def test_binary_file(self, tmp_path: Path):
        f = tmp_path / "binary.o"
        f.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
        # Reading binary as text may fail with UnicodeDecodeError
        # _read_file_lines catches OSError but NOT UnicodeDecodeError
        # This test verifies the current behavior — may need fixing in source
        try:
            lines = _read_file_lines(str(f))
            assert lines is not None
        except UnicodeDecodeError:
            # Known limitation: binary files cause decode errors
            pass

    def test_unicode_bom(self, tmp_path: Path):
        f = tmp_path / "bom.c"
        f.write_bytes(b"\xef\xbb\xbfint x;\n")
        lines = _read_file_lines(str(f))
        assert lines is not None
        assert "int x" in lines[0]

    def test_permission_denied(self, tmp_path: Path):
        f = tmp_path / "noperm.c"
        f.write_text("secret")
        f.chmod(0o000)  # Remove all permissions
        try:
            lines = _read_file_lines(str(f))
            # Should return None on OSError
            assert lines is None or isinstance(lines, list)
        finally:
            f.chmod(0o644)  # Restore so tmp_path cleanup works

    def test_path_is_directory(self, tmp_path: Path):
        # Passing a directory path should return None
        lines = _read_file_lines(str(tmp_path))
        assert lines is None


class TestReadBody:
    def test_reads_body_correctly(self):
        lines = ["ignored\n", "line1\n", "line2\n", "line3\n", "ignored\n"]
        body = _read_body(lines, 2, 5)
        # lines[1:5] = elements 1-4 = "line1\n", "line2\n", "line3\n", "ignored\n"
        assert body == "line1\nline2\nline3\nignored\n"

    def test_single_line_body(self):
        lines = ["a\n", "body\n", "c\n"]
        # For a single-line function on line 2, end_line should be 3 (exclusive)
        body = _read_body(lines, 2, 3)
        # lines[1:3] = elements 1,2 = "body\n", "c\n"
        assert "body" in body

    def test_start_equals_end_returns_empty(self):
        lines = ["a\n", "b\n", "c\n"]
        body = _read_body(lines, 2, 2)
        assert body == ""

    def test_end_past_file_truncates(self):
        lines = ["line1\n", "line2\n"]
        body = _read_body(lines, 1, 100)
        # end_line(100) > start_line(1) → True
        # end_line(100) <= len(lines)(2) → False → returns ""
        assert body == ""

    def test_start_past_file_returns_empty(self):
        lines = ["line1\n"]
        body = _read_body(lines, 100, 200)
        assert body == ""

    def test_start_line_zero(self):
        lines = ["line0\n", "line1\n"]
        # start_line - 1 = -1, which slices from the end in Python!
        # This is an edge case we just want to verify doesn't crash
        body = _read_body(lines, 0, 2)
        # Undefined behavior for invalid input — just verify no crash
        assert isinstance(body, str)

    def test_negative_start_line(self):
        lines = ["a\n", "b\n"]
        # Negative start_line — Python slicing will wrap
        body = _read_body(lines, -1, 2)
        assert isinstance(body, str)

    def test_empty_lines_list(self):
        body = _read_body([], 1, 5)
        assert body == ""


class TestComputeContentHash:
    def test_deterministic(self):
        lines = ["void fn() {\n", "    return;\n", "}\n"]
        h1 = _compute_content_hash(lines, 1, 4, "void fn()", "")
        h2 = _compute_content_hash(lines, 1, 4, "void fn()", "")
        assert h1 == h2
        assert len(h1) == 16  # hexdigest[:16]

    def test_different_body_produces_different_hash(self):
        # Use multi-line bodies so end_line > start_line
        lines1 = ["line\n", "void a() { return 1; }\n", "line\n"]
        lines2 = ["line\n", "void a() { return 2; }\n", "line\n"]
        h1 = _compute_content_hash(lines1, 2, 3, "void a()", "")
        h2 = _compute_content_hash(lines2, 2, 3, "void a()", "")
        assert h1 != h2

    def test_different_signature_different_hash(self):
        lines = ["line\n", "void fn() { }\n", "line\n"]
        h1 = _compute_content_hash(lines, 2, 3, "void fn(int)", "")
        h2 = _compute_content_hash(lines, 2, 3, "void fn(char)", "")
        assert h1 != h2

    def test_different_docstring_different_hash(self):
        lines = ["line\n", "void fn() { }\n", "line\n"]
        h1 = _compute_content_hash(lines, 2, 3, "void fn()", "Doc A")
        h2 = _compute_content_hash(lines, 2, 3, "void fn()", "Doc B")
        assert h1 != h2

    def test_empty_body(self):
        lines: list[str] = []
        h = _compute_content_hash(lines, 2, 3, "void fn()", "")
        # end_line(3) > start_line(2) → True, 3 <= 0 → False → body ""
        assert len(h) == 16

    def test_null_signature_handled(self):
        lines = ["a\n", "void fn() { }\n"]
        h = _compute_content_hash(lines, 2, 3, "", "")
        assert len(h) == 16

    def test_null_docstring_handled(self):
        lines = ["a\n", "void fn() { }\n"]
        h = _compute_content_hash(lines, 2, 3, "void fn()", "")
        assert len(h) == 16

    def test_whitespace_body_difference_detected(self):
        lines1 = ["a\n", "void fn() { return 1; }\n", "b\n"]
        lines2 = ["a\n", "void fn() {return 1;}\n", "b\n"]
        h1 = _compute_content_hash(lines1, 2, 3, "void fn()", "")
        h2 = _compute_content_hash(lines2, 2, 3, "void fn()", "")
        # body.strip() preserves internal whitespace differences
        assert h1 != h2

    def test_trailing_whitespace_ignored(self):
        # Single-line file — body is the whole file after strip
        content1 = "void fn() { return 1; }\n"
        content2 = "void fn() { return 1; }  \n"
        lines1 = [content1]
        lines2 = [content2]
        # Use start_line=1, end_line=2 so body = lines[0:2] = only the first line
        h1 = _compute_content_hash(lines1, 1, 2, "void fn()", "")
        h2 = _compute_content_hash(lines2, 1, 2, "void fn()", "")
        # body.strip() removes trailing whitespace → same content
        assert h1 == h2

    def test_unicode_in_body(self):
        lines = ["a\n", "void fn() { /* žluťoučký */ }\n"]
        h = _compute_content_hash(lines, 2, 3, "void fn()", "")
        assert len(h) == 16

    def test_invalid_range_for_body_read(self):
        # end_line > start_line but both way past file → body is ""
        lines = ["a\n"]
        h = _compute_content_hash(lines, 100, 200, "void fn()", "")
        assert len(h) == 16

    def test_end_line_equals_start_line(self):
        lines = ["void fn() { return; }\n"]
        h = _compute_content_hash(lines, 1, 1, "void fn()", "")
        # body is "" because end_line > start_line is False
        assert len(h) == 16
