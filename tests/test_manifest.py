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
    def test_deterministic(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        manifest = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp/test",
            "entries": [],
        }
        h1 = compute_config_hash(manifest)
        h2 = compute_config_hash(manifest)
        assert h1 == h2

    def test_config_hash_excluded(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [], "config_hash": "old"}
        m2 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [], "config_hash": "new"}
        assert compute_config_hash(m1) == compute_config_hash(m2)

    def test_build_dir_patterns_excluded(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [], "build_dir_patterns": ["BUILD/"]}
        m2 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [], "build_dir_patterns": [".pio/"]}
        assert compute_config_hash(m1) == compute_config_hash(m2)

    def test_different_entries_produce_different_hash(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [{"file": "a.cpp"}]}
        m2 = {"_format": "fw-context-manifest/1", "project_root": "/tmp", "entries": [{"file": "b.cpp"}]}
        assert compute_config_hash(m1) != compute_config_hash(m2)

    def test_source_hash_does_not_affect_config_hash(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-c", "a.cpp"], "source_hash": "abc123", "headers": []}
            ],
        }
        m2 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-c", "a.cpp"], "source_hash": "different", "headers": []}
            ],
        }
        assert compute_config_hash(m1) == compute_config_hash(m2)

    def test_headers_do_not_affect_config_hash(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-c", "a.cpp"], "source_hash": "abc", "headers": [{"path": "x.h", "hash": "hash1"}]}
            ],
        }
        m2 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-c", "a.cpp"], "source_hash": "abc", "headers": [{"path": "x.h", "hash": "hash2"}]}
            ],
        }
        assert compute_config_hash(m1) == compute_config_hash(m2)

    def test_arguments_change_affects_config_hash(self):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        m1 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-c", "a.cpp"], "source_hash": "abc", "headers": []}
            ],
        }
        m2 = {
            "_format": "fw-context-manifest/1",
            "project_root": "/tmp",
            "entries": [
                {"file": "a.cpp", "directory": "/tmp", "arguments": ["gcc", "-O2", "-c", "a.cpp"], "source_hash": "abc", "headers": []}
            ],
        }
        assert compute_config_hash(m1) != compute_config_hash(m2)


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load, save

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
        config_hash = save(manifest, tmp_path)
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
        stale, new_hash = check_tu_staleness(entry, tmp_path, [tmp_path / "src"])
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
        stale, new_hash = check_tu_staleness(entry, tmp_path, [tmp_path / "src"])
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
        stale, _ = check_tu_staleness(entry, tmp_path, [tmp_path / "src"])
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
        stale, _ = check_tu_staleness(entry, tmp_path, [tmp_path / "src"])
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
        # source_roots only includes src/ — mbed-os/ is SDK, should be trusted
        stale, _ = check_tu_staleness(entry, tmp_path, [tmp_path / "src"])
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

        assert _is_generated_header("BUILD/mbed_config.h") is True
        assert _is_generated_header("build/config.h") is True
        assert _is_generated_header(".pio/lib/foo.h") is True
        assert _is_generated_header("src/main.h") is False
        assert _is_generated_header("lib/utils.h") is False


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
