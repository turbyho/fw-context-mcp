"""Tests for fw_context_mcp.mcp.server helper functions."""

from pathlib import Path

from fw_context_mcp.mcp.server import _abs_path, _read_symbol_body


class TestAbsPath:
    def test_relative_joined_with_root(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "src/zble.cpp") == "/home/user/project/src/zble.cpp"

    def test_absolute_returned_unchanged(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "/abs/path/file.cpp") == "/abs/path/file.cpp"

    def test_empty_path_passthrough(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "") == ""

    def test_nested_relative(self):
        root = Path("/p")
        assert _abs_path(root, "lib/modem/zmodem_driver.cpp") == "/p/lib/modem/zmodem_driver.cpp"


class TestReadSymbolBody:
    def _write(self, tmp_path, content):
        f = tmp_path / "src.cpp"
        f.write_text(content)
        return str(f)

    def test_balances_braces_for_function(self, tmp_path):
        src = (
            "int before() { return 0; }\n"      # line 1
            "void target()\n"                    # line 2 (definition line)
            "{\n"                                # line 3
            "    if (x) {\n"                     # line 4
            "        do_thing();\n"              # line 5
            "    }\n"                            # line 6
            "}\n"                                # line 7
            "int after() { return 1; }\n"        # line 8
        )
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 2)
        assert "void target()" in body
        assert "do_thing();" in body
        assert "}" in body
        # must NOT bleed into after()
        assert "int after()" not in body

    def test_single_line_body(self, tmp_path):
        src = "void f() { return; }\nint g() {}\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1)
        assert "void f()" in body
        assert "int g()" not in body

    def test_declaration_without_braces_small_window(self, tmp_path):
        src = "int field_a;\nint field_b;\nint field_c;\nint field_d;\nint field_e;\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1)
        # no braces → small window (≤3 lines), not the whole file
        assert "field_a" in body
        assert body.count("\n") <= 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_symbol_body(str(tmp_path / "nope.cpp"), 1) == ""

    def test_out_of_range_line(self, tmp_path):
        path = self._write(tmp_path, "int x;\n")
        assert _read_symbol_body(path, 999) == ""

    def test_end_line_exact_range_preferred(self, tmp_path):
        # end_line from libclang extent → exact range, ignores brace heuristic
        src = "a\nb\nc\nd\ne\nf\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 2, end_line=4)
        # lines 2..4 = b, c, d
        assert "b" in body and "c" in body and "d" in body
        assert "a" not in body and "e" not in body

    def test_end_line_clamped_to_file(self, tmp_path):
        path = self._write(tmp_path, "x\ny\n")
        body = _read_symbol_body(path, 1, end_line=999)
        assert "x" in body and "y" in body

    def test_end_line_zero_falls_back_to_braces(self, tmp_path):
        src = "void f()\n{\n  g();\n}\nint after;\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1, end_line=0)
        assert "void f()" in body and "g();" in body
        assert "int after" not in body

