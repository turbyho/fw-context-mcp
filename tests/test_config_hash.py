"""Tests for fw_context_mcp.indexer.config_hash."""

from pathlib import Path

from fw_context_mcp.indexer.config_hash import _normalize_entry
from fw_context_mcp.indexer.manifest import build_scope, compute_config_hash


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


class _FakeUnit:
    def __init__(self, f: str):
        self.file = Path(f)
        self.clang_args = ["-I/inc", "-DFOO=1"]
        self.raw_entry = None
        self.directory = "/tmp"


class TestBuildScope:
    def test_empty_scope(self):
        assert build_scope() == []
        assert build_scope("", "", {}) == []

    def test_variant_image(self):
        assert build_scope("nrf52840", "app") == ["nrf52840", "app"]

    def test_env_is_sorted_and_prefixed(self):
        tokens = build_scope("nrf52840", "", {"B": "2", "A": "1"})
        assert tokens == ["nrf52840", 'env:{"A": "1", "B": "2"}']


class TestConfigHashScope:
    def _hash(self, scope=None):
        unit = _FakeUnit("/tmp/proj/src/main.c")
        return compute_config_hash([unit], Path("/tmp/proj"), "projid", None, scope=scope)

    def test_scope_changes_hash(self):
        base = self._hash()
        v1 = self._hash(scope=["nrf52840", "app"])
        v2 = self._hash(scope=["nrf52840", "stage0"])
        assert base != v1
        assert v1 != v2

    def test_same_scope_is_idempotent(self):
        assert self._hash(scope=["nrf52840", "app"]) == self._hash(scope=["nrf52840", "app"])


