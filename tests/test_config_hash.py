"""Tests for fw_context_mcp.indexer.config_hash."""

import json

import pytest

from fw_context_mcp.indexer.config_hash import _normalize_entry


class TestNormalizeEntry:
    def test_removes_compiler_binary(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["arm-none-eabi-g++", "-std=c++14", "-DFOO=1", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-std=c++14" in result["args"]
        assert "-DFOO=1" in result["args"]
        assert "arm-none-eabi-g++" not in result["args"]

    def test_removes_transient_flags(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-std=c++14", "-MD", "-MP", "-o", "build/main.o", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-MD" not in result["args"]
        assert "-MP" not in result["args"]
        assert "-o" not in result["args"]
        assert "build/main.o" not in result["args"]

    def test_keeps_compilation_flags(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-std=c++14", "-DFOO=1", "-Wall", "-Os", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-std=c++14" in result["args"]
        assert "-DFOO=1" in result["args"]
        assert "-Wall" in result["args"]
        assert "-Os" in result["args"]

    def test_args_are_sorted(self):
        """Arguments should be sorted for deterministic hashing."""
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-c", "-b", "-a", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert result["args"] == sorted(result["args"])

    def test_normalizes_file_path(self):
        entry = {"file": "./src/main.cpp", "arguments": ["g++", "src/main.cpp"]}
        result = _normalize_entry(entry)
        assert result["file"] == "src/main.cpp"

    def test_handles_command_string(self):
        entry = {
            "file": "main.c",
            "command": "gcc -std=c11 -O2 -o build/main.o main.c",
        }
        result = _normalize_entry(entry)
        assert "-std=c11" in result["args"]
        assert "-O2" in result["args"]

    def test_expands_response_files(self, tmpdir):
        rsp = tmpdir / "flags.rsp"
        rsp.write_text("-DEXTRA=1\n")
        entry = {
            "file": "main.cpp",
            "arguments": ["g++", f"@{rsp}", "-std=c++14", "main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-DEXTRA=1" in result["args"]


