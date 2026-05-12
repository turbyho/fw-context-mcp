"""Tests for fw_context_mcp.indexer.config_hash."""

import json

from fw_context_mcp.indexer.config_hash import _normalize_entry, compute


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


class TestCompute:
    def test_identical_inputs_produce_same_hash(self, tmpdir):
        data = [
            {"directory": str(tmpdir), "file": "src/a.cpp",
             "arguments": ["g++", "-std=c++14", "-Os", "src/a.cpp"]},
        ]
        path1 = tmpdir / "cc1.json"
        path2 = tmpdir / "cc2.json"
        path1.write_text(json.dumps(data))
        path2.write_text(json.dumps(data))
        assert compute(path1) == compute(path2)

    def test_different_flags_produce_different_hash(self, tmpdir):
        data1 = [{"file": "a.cpp", "arguments": ["g++", "-Os", "a.cpp"]}]
        data2 = [{"file": "a.cpp", "arguments": ["g++", "-O2", "a.cpp"]}]
        p1 = tmpdir / "cc1.json"
        p2 = tmpdir / "cc2.json"
        p1.write_text(json.dumps(data1))
        p2.write_text(json.dumps(data2))
        assert compute(p1) != compute(p2)

    def test_transient_flags_dont_affect_hash(self, tmpdir):
        """-MD/-MP etc. should be ignored in hash computation."""
        data1 = [{"file": "a.cpp", "arguments": ["g++", "-O2", "-MD", "-MP", "a.cpp"]}]
        data2 = [{"file": "a.cpp", "arguments": ["g++", "-O2", "a.cpp"]}]
        p1 = tmpdir / "cc1.json"
        p2 = tmpdir / "cc2.json"
        p1.write_text(json.dumps(data1))
        p2.write_text(json.dumps(data2))
        assert compute(p1) == compute(p2)

    def test_output_path_doesnt_affect_hash(self, tmpdir):
        """Different -o paths should produce the same hash."""
        data1 = [{"file": "a.cpp", "arguments": ["g++", "-O2", "-o", "/tmp/a.o", "a.cpp"]}]
        data2 = [{"file": "a.cpp", "arguments": ["g++", "-O2", "-o", "/tmp/b.o", "a.cpp"]}]
        p1 = tmpdir / "cc1.json"
        p2 = tmpdir / "cc2.json"
        p1.write_text(json.dumps(data1))
        p2.write_text(json.dumps(data2))
        assert compute(p1) == compute(p2)

    def test_hash_is_hex_string(self, tmpdir):
        data = [{"file": "a.cpp", "arguments": ["g++", "-O2", "a.cpp"]}]
        p = tmpdir / "cc.json"
        p.write_text(json.dumps(data))
        result = compute(p)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
