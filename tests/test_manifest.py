"""Tests for manifest.py — manifest generation, loading, staleness checks."""

from __future__ import annotations

import json
import os
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

    def test_different_files_produce_the_same_hash(self, tmp_path: Path):
        """The file set is not build identity — the dialect is.

        Keeping the TU list in the hash meant adding or renaming one source
        file minted a new build for every unchanged TU, whose rows then had to
        be migrated to it.  Which files exist is recorded in the manifest, and
        whether one changed is answered by ``files.source_hash``.
        """
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"))]
        u2 = [self._make_unit(str(tmp_path / "b.cpp"))]
        h1 = compute_config_hash(u1, tmp_path, "test")
        h2 = compute_config_hash(u2, tmp_path, "test")
        assert h1 == h2

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


class TestCollectStaleHeaders:
    """Unit tests for the header → TU staleness pre-pass."""

    @staticmethod
    def _manifest(tmp_path: Path, headers: list[dict], *, tu: str = "src/main.cpp") -> dict:
        import hashlib

        src = tmp_path / tu
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        return {
            "project_root": str(tmp_path),
            "entries": [
                {
                    "file": tu,
                    "source_hash": hashlib.sha256(src.read_bytes()).hexdigest(),
                    "headers": headers,
                }
            ],
        }

    def test_changed_project_header_reported(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import collect_stale_headers

        header = tmp_path / "src" / "config.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("// current content")
        stored = hashlib.sha256(b"// old content").hexdigest()

        manifest = self._manifest(
            tmp_path, [{"path": "src/config.h", "hash": stored, "generated": False}]
        )
        assert collect_stale_headers(manifest, tmp_path, []) == {"src/config.h"}

    def test_unchanged_header_not_reported(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import collect_stale_headers

        header = tmp_path / "src" / "config.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("// current content")
        stored = hashlib.sha256(header.read_bytes()).hexdigest()

        manifest = self._manifest(
            tmp_path, [{"path": "src/config.h", "hash": stored, "generated": False}]
        )
        assert collect_stale_headers(manifest, tmp_path, []) == set()

    def test_generated_and_vendor_headers_skipped(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import collect_stale_headers

        (tmp_path / "BUILD").mkdir()
        (tmp_path / "BUILD" / "mbed_config.h").write_text("// generated")
        (tmp_path / "mbed-os").mkdir()
        (tmp_path / "mbed-os" / "mbed.h").write_text("// SDK")

        manifest = self._manifest(
            tmp_path,
            [
                {"path": "BUILD/mbed_config.h", "hash": "stale", "generated": True},
                {"path": "mbed-os/mbed.h", "hash": "stale", "generated": False},
            ],
        )
        assert collect_stale_headers(manifest, tmp_path, ["mbed-os/%"]) == set()

    def test_header_outside_project_root_skipped(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import collect_stale_headers

        outside = tmp_path.parent / f"{tmp_path.name}-external.h"
        outside.write_text("// toolchain header")
        try:
            manifest = self._manifest(
                tmp_path, [{"path": str(outside), "hash": "stale", "generated": False}]
            )
            assert collect_stale_headers(manifest, tmp_path, []) == set()
        finally:
            outside.unlink(missing_ok=True)

    def test_hash_cache_is_consulted(self, tmp_path: Path):
        """A pre-filled cache entry is trusted — no re-read of the header."""
        import hashlib

        from fw_context_mcp.indexer.manifest import collect_stale_headers

        header = tmp_path / "src" / "config.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("// current content")
        stored = hashlib.sha256(b"// old content").hexdigest()

        manifest = self._manifest(
            tmp_path, [{"path": "src/config.h", "hash": stored, "generated": False}]
        )
        cache = {str(header.resolve()): stored}
        assert collect_stale_headers(manifest, tmp_path, [], hash_cache=cache) == set()

    def test_tus_affected_by_headers(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import tus_affected_by_headers

        manifest = {
            "entries": [
                {"file": "src/main.cpp", "headers": [{"path": "src/config.h"}]},
                {"file": "src/other.cpp", "headers": [{"path": "src/util.h"}]},
            ]
        }
        assert tus_affected_by_headers(manifest, {"src/config.h"}) == {"src/main.cpp"}
        assert tus_affected_by_headers(manifest, set()) == set()


class TestManifestEntryRefreshGuard:
    """``_update_manifest_after_index`` may only refresh re-parsed TUs.

    Refreshing the entry of a TU that kept its previous symbols would declare
    the index current while it still holds data parsed from the old header
    text — the staleness signal disappears and only ``--force`` recovers.
    """

    @staticmethod
    def _unit(tmp_path: Path, rel: str):
        from unittest.mock import MagicMock

        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        unit = MagicMock()
        unit.file = src
        unit.directory = tmp_path
        unit.clang_args = ["gcc", "-c", rel]
        unit.raw_entry = {"file": rel, "directory": str(tmp_path), "arguments": ["gcc", "-c", rel]}
        return unit

    def _run(self, tmp_path: Path, *, updated_count: int, reparsed: set[str] | None):
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        unit = self._unit(tmp_path, "src/main.cpp")
        manifest = {
            "project_root": str(tmp_path),
            "entries": [
                {
                    "file": "src/main.cpp",
                    "directory": str(tmp_path),
                    "arguments": unit.clang_args,
                    "source_hash": "stored-source",
                    "flags_hash": "stored-flags",
                    "headers": [{"path": "src/config.h", "hash": "STORED", "generated": False}],
                }
            ],
        }
        return _update_manifest_after_index(
            manifest=manifest,
            units=[unit],
            project_root=tmp_path,
            db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json",
            updated_count=updated_count,
            tu_headers={"src/main.cpp": [{"path": "src/config.h", "hash": "FRESH", "generated": False}]},
            config_hash="deadbeef",
            reparsed_tus=reparsed,
        )

    def test_not_reparsed_keeps_stored_hash(self, tmp_path: Path):
        (tmp_path / "index").mkdir()
        # updated_count=1 → another TU was re-parsed, so the rebuild path runs,
        # but src/main.cpp itself was not re-parsed.
        result = self._run(tmp_path, updated_count=1, reparsed=set())
        assert result is not None
        assert result["entries"][0]["headers"][0]["hash"] == "STORED"

    def test_reparsed_takes_fresh_hash(self, tmp_path: Path):
        (tmp_path / "index").mkdir()
        result = self._run(tmp_path, updated_count=1, reparsed={"src/main.cpp"})
        assert result is not None
        assert result["entries"][0]["headers"][0]["hash"] == "FRESH"

    def test_nothing_reparsed_returns_manifest_untouched(self, tmp_path: Path):
        """updated_count=0 → no rewrite at all, even with collected headers."""
        (tmp_path / "index").mkdir()
        result = self._run(tmp_path, updated_count=0, reparsed=set())
        assert result is not None
        assert result["entries"][0]["headers"][0]["hash"] == "STORED"
        assert not list((tmp_path / "index").glob("manifest.*.json")), (
            "manifest file was written for a run that re-parsed nothing"
        )


class TestLoadBuildDirPatterns:
    """``build_dir_patterns`` is read on the MCP query path, so it is cached.

    ``_stale_files`` runs on every query routed through
    ``_with_stale_recovery`` and needs nothing from the manifest but this one
    short list.  Parsing the whole file for it was the most expensive thing on
    that path: measured on zbox-ecb-fw, 94.7 ms to fetch ``['BUILD/']`` from a
    52 MB manifest.  Cached, the same call is 0.007 ms.

    The cache is keyed by the manifest's mtime, so the interesting case is not
    the speed — it is that a reindex must never be served the old value.
    """

    def _write(self, db_dir: Path, config_hash: str, patterns: list[str]) -> Path:
        from fw_context_mcp.indexer.manifest import _manifest_path

        path = _manifest_path(db_dir, config_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "_format": "fw-context-manifest/1",
                "config_hash": config_hash,
                "project_root": str(db_dir),
                "build_dir_patterns": patterns,
                "entries": [],
            }),
            encoding="utf-8",
        )
        return path

    def test_returns_the_stored_patterns(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load_build_dir_patterns

        self._write(tmp_path, "abc123", ["BUILD/", ".pio/"])
        assert load_build_dir_patterns(tmp_path, "abc123") == ["BUILD/", ".pio/"]

    def test_agrees_with_a_full_load(self, tmp_path: Path):
        """The fast path must not drift from the thing it replaces."""
        from fw_context_mcp.indexer.manifest import load as load_manifest
        from fw_context_mcp.indexer.manifest import load_build_dir_patterns

        self._write(tmp_path, "abc123", ["BUILD/"])
        full = load_manifest(tmp_path, "abc123")
        assert load_build_dir_patterns(tmp_path, "abc123") == full["build_dir_patterns"]

    def test_a_missing_manifest_gives_no_patterns(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load_build_dir_patterns

        assert load_build_dir_patterns(tmp_path, "nosuchhash") == []

    def test_a_rewritten_manifest_is_not_served_from_cache(self, tmp_path: Path):
        """The case that makes the cache safe rather than merely fast.

        A reindex rewrites the manifest under the same config_hash when the
        dialect did not change.  Serving the previous patterns would silently
        exclude, or stop excluding, whole directories.
        """
        from fw_context_mcp.indexer.manifest import load_build_dir_patterns

        path = self._write(tmp_path, "abc123", ["BUILD/"])
        assert load_build_dir_patterns(tmp_path, "abc123") == ["BUILD/"]

        self._write(tmp_path, "abc123", ["out/"])
        # Force a distinct mtime even on a coarse-grained filesystem.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        assert load_build_dir_patterns(tmp_path, "abc123") == ["out/"], (
            "the cache served patterns from before the reindex"
        )

    def test_an_empty_pattern_list_is_cached_too(self, tmp_path: Path):
        """None-vs-empty must not make every call a miss."""
        from fw_context_mcp.indexer import manifest as m

        self._write(tmp_path, "abc123", [])
        assert m.load_build_dir_patterns(tmp_path, "abc123") == []
        before = len(m._BUILD_PATTERNS_CACHE)
        assert m.load_build_dir_patterns(tmp_path, "abc123") == []
        assert len(m._BUILD_PATTERNS_CACHE) == before, "an empty list was not cached"
