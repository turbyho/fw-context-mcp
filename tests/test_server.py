"""Tests for fw_context_mcp.mcp.server helper functions."""

from pathlib import Path

from fw_context_mcp.mcp.server import _abs_path


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
