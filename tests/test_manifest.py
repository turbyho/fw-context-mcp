"""Tests for manifest.py — manifest generation, loading, staleness checks."""

from __future__ import annotations

from pathlib import Path


class TestManifestEntryHash:
    def test_empty_entry(self):
        from fw_context_mcp.indexer.manifest import get_manifest_entry_hash

        h = get_manifest_entry_hash({"source_hash": "", "headers": []})
        assert len(h) == 64  # SHA-256 hex
        assert h != ""

    def test_with_source_only(self):
        from fw_context_mcp.indexer.manifest import get_manifest_entry_hash

        entry = {"source_hash": "abc123", "headers": []}
        h1 = get_manifest_entry_hash(entry)
        h2 = get_manifest_entry_hash(entry)
        assert h1 == h2  # deterministic

    def test_different_headers_produce_different_hash(self):
        from fw_context_mcp.indexer.manifest import get_manifest_entry_hash

        e1 = {"source_hash": "abc", "headers": [{"path": "a.h", "hash": "111"}]}
        e2 = {"source_hash": "abc", "headers": [{"path": "a.h", "hash": "222"}]}
        assert get_manifest_entry_hash(e1) != get_manifest_entry_hash(e2)


class TestComputeConfigHash:
    @staticmethod
    def _make_unit(file_path: str, clang_args: list[str] | None = None):
        """Create a fake CompilationUnit for testing."""
        from unittest.mock import MagicMock

        if clang_args is None:
            clang_args = ["gcc", "-c", file_path]
        unit = MagicMock()
        unit.file = MagicMock()
        unit.file.resolve.return_value = Path(file_path)
        unit.clang_args = clang_args
        return unit

    def test_deterministic(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        units = []
        h1 = compute_config_hash(units, tmp_path, "test-project")
        h2 = compute_config_hash(units, tmp_path, "test-project")
        assert h1 == h2
        assert len(h1) == 64

    def test_different_files_produce_different_hash(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"))]
        u2 = [self._make_unit(str(tmp_path / "b.cpp"))]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 != h2

    def test_different_flags_produce_different_hash(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-O2", "-c", "a.cpp"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-Os", "-c", "a.cpp"])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 != h2

    def test_flag_order_independent(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-O2", "-DFOO=1", "-c", "a.cpp"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-DFOO=1", "-O2", "-c", "a.cpp"])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 == h2  # arguments are sorted alphabetically

    def test_hash_is_hex_string(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        h = compute_config_hash([], tmp_path, "test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_transient_mbed_build_timestamp_stripped(self, tmp_path: Path):
        """-DMBED_BUILD_TIMESTAMP=<float> must be stripped — it changes every build."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DMBED_BUILD_TIMESTAMP=1784146370.289386"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DMBED_BUILD_TIMESTAMP=1784145526.0657399"])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 == h2  # timestamp value differs but macro should be stripped

    def test_transient_mbed_build_timestamp_space_form(self, tmp_path: Path):
        """-D MBED_BUILD_TIMESTAMP=<float> (space-separated) must also be stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-D", "MBED_BUILD_TIMESTAMP=1784146370.289386"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-D", "MBED_BUILD_TIMESTAMP=9999999999.0"])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 == h2  # space-separated form is also stripped

    def test_transient_mbed_build_timestamp_value_only(self, tmp_path: Path):
        """-DMBED_BUILD_TIMESTAMP (no =value) should also be stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DMBED_BUILD_TIMESTAMP"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp")])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 == h2  # with and without the transient define must match

    def test_other_defines_preserved(self, tmp_path: Path):
        """Non-transient -D macros must NOT be stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"), "-DDEBUG=1"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DNDEBUG"])]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 != h2  # different non-transient defines → different hash

    def test_generic_transient_defines_stripped(self, tmp_path: Path):
        """BUILD_TIMESTAMP, BUILD_TIME, BUILD_DATE, BUILD_ID, BUILD_NUMBER are stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        base_args = ["gcc", "-c", str(tmp_path / "a.cpp")]
        # All of these should produce the same hash — the transient macros are stripped
        hashes = set()
        for dflag in [
            "-DBUILD_TIMESTAMP=2026-07-15T12:00:00",
            "-DBUILD_TIME=12:00:00",
            "-DBUILD_DATE=2026-07-15",
            "-DBUILD_ID=12345",
            "-DBUILD_NUMBER=42",
        ]:
            u = [self._make_unit(str(tmp_path / "a.cpp"), base_args + [dflag])]
            hashes.add(compute_config_hash(u, tmp_path, "test"))
        # Without any transient macros
        u_base = [self._make_unit(str(tmp_path / "a.cpp"), base_args)]
        hashes.add(compute_config_hash(u_base, tmp_path, "test"))
        assert len(hashes) == 1  # all produce the same hash


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash, load, save

        manifest = {
            "_format": "fw-context-manifest/1",
            "project_root": str(tmp_path),
            "compile_commands_path": str(tmp_path / "compile_commands.json"),
            "entries": [
                {
                    "file": "src/main.cpp",
                    "directory": str(tmp_path),
                    "arguments": ["gcc", "-c", "src/main.cpp"],
                    "source_hash": "abc123",
                    "headers": [
                        {"path": "src/config.h", "hash": "def456", "generated": False},
                        {"path": "build/mbed_config.h", "hash": "ghi789", "generated": True},
                    ],
                }
            ],
        }
        # Compute config_hash from a mock unit list (backward compat path)
        from unittest.mock import MagicMock

        mock_unit = MagicMock()
        mock_unit.file.resolve.return_value = Path(str(tmp_path / "src/main.cpp"))
        mock_unit.clang_args = ["gcc", "-c", "src/main.cpp"]
        config_hash = compute_config_hash([mock_unit], tmp_path, "test")
        config_hash = save(manifest, tmp_path, config_hash)
        assert len(config_hash) == 64

        loaded = load(tmp_path)
        assert loaded is not None
        assert loaded["_format"] == "fw-context-manifest/1"
        assert len(loaded["entries"]) == 1
        assert loaded["entries"][0]["file"] == "src/main.cpp"

    def test_load_nonexistent(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load

        loaded = load(tmp_path)
        assert loaded is None


class TestCheckTuStaleness:
    def test_source_hash_changed(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")

        entry = {
            "file": "src/main.cpp",
            "source_hash": "old_hash_that_differs",
            "headers": [],
        }
        stale, new_hash = check_tu_staleness(entry, tmp_path, [])
        assert stale is True
        assert new_hash is not None
        assert len(new_hash) == 64

    def test_source_hash_unchanged(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        content = "int main() { return 0; }"
        src.write_text(content)
        source_hash = hashlib.sha256(content.encode()).hexdigest()

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": [],
        }
        stale, new_hash = check_tu_staleness(entry, tmp_path, [])
        assert stale is False
        assert new_hash is None

    def test_project_header_changed(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        # Create source file
        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        # Create header with OLD hash
        header = tmp_path / "src" / "config.h"
        header.write_text("// original config")
        old_header_hash = hashlib.sha256(b"// different content").hexdigest()

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": [
                {"path": "src/config.h", "hash": old_header_hash, "generated": False},
            ],
        }
        stale, _ = check_tu_staleness(entry, tmp_path, [])
        assert stale is True  # header hash differs

    def test_generated_header_skipped(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": [
                {"path": "BUILD/mbed_config.h", "hash": "nonexistent_hash", "generated": True},
            ],
        }
        stale, _ = check_tu_staleness(entry, tmp_path, [])
        assert stale is False  # generated header is skipped

    def test_sdk_header_trusted(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        mbed_os = tmp_path / "mbed-os"
        mbed_os.mkdir()
        (mbed_os / "mbed.h").write_text("// SDK header")

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": [
                {"path": "mbed-os/mbed.h", "hash": "nonexistent_hash", "generated": False},
            ],
        }
        # mbed-os/% is a vendor pattern — SDK header is trusted from manifest
        stale, _ = check_tu_staleness(entry, tmp_path, ["mbed-os/%"])
        assert stale is False  # SDK header is trusted from manifest


class TestCollectHeadersFromTokens:
    def test_returns_headers(self, tmp_path: Path):
        """Integration test — requires libclang."""
        from fw_context_mcp.indexer.manifest import _collect_headers_from_tokens

        # Create source and header in same directory
        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('#include "config.h"\nint main() { return 0; }')

        header = tmp_path / "src" / "config.h"
        header.write_text("// config header")

        # Mock a CompilationUnit-like object
        class FakeUnit:
            file = src
            clang_args = ["-std=c++14", "-I" + str(tmp_path / "src")]

        unit = FakeUnit()
        headers = _collect_headers_from_tokens(unit, tmp_path)
        assert isinstance(headers, list)
        # Should find at least the included config.h header
        for h in headers:
            assert "path" in h
            assert "hash" in h
            assert "generated" in h
        assert len(headers) > 0
        assert any("config.h" in h["path"] for h in headers)


class TestIsGeneratedHeader:
    def test_build_dir_is_generated(self):
        from fw_context_mcp.indexer.manifest import _is_generated_header

        patterns = ["BUILD/", "build/", ".pio/", "cmake-build-", "_build/"]

        assert _is_generated_header("BUILD/mbed_config.h", patterns) is True
        assert _is_generated_header("build/config.h", patterns) is True
        assert _is_generated_header(".pio/lib/foo.h", patterns) is True
        assert _is_generated_header("src/main.h", patterns) is False
        assert _is_generated_header("lib/utils.h", patterns) is False

    def test_no_patterns_returns_false(self):
        from fw_context_mcp.indexer.manifest import _is_generated_header

        assert _is_generated_header("BUILD/mbed_config.h") is False
        assert _is_generated_header("build/config.h", None) is False


class TestUpdateEntry:
    def test_update_existing_entry(self):
        from fw_context_mcp.indexer.manifest import update_entry

        manifest = {
            "entries": [
                {"file": "a.cpp", "source_hash": "old", "headers": []},
                {"file": "b.cpp", "source_hash": "old", "headers": []},
            ]
        }
        new_headers = [{"path": "new.h", "hash": "111", "generated": False}]
        update_entry(manifest, 0, "new_hash", new_headers)
        assert manifest["entries"][0]["source_hash"] == "new_hash"
        assert manifest["entries"][0]["headers"] == new_headers
        assert manifest["entries"][1]["source_hash"] == "old"  # unchanged

    def test_update_out_of_range(self):
        from fw_context_mcp.indexer.manifest import update_entry

        manifest = {"entries": []}
        update_entry(manifest, 5, "hash", [])
        # Should not raise — silently no-op
        assert len(manifest["entries"]) == 0
