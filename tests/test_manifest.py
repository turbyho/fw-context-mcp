"""Tests for manifest.py — manifest generation, loading, staleness checks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fw_context_mcp.indexer.config_hash import DEFAULT_TRANSIENT_DEFINES
from fw_context_mcp.indexer.manifest import MANIFEST_FORMAT
from fw_context_mcp.utils import compute_source_hash


def _parsed(root: Path, tus: set[str] | None) -> dict[str, str] | None:
    """``reparsed_tus`` as the runner gives it: each re-parsed TU with its source hash."""
    if tus is None:
        return None
    return {tu: compute_source_hash(root / tu) for tu in tus}


class TestComputeConfigHash:
    @staticmethod
    def _make_unit(file_path: str, clang_args: list[str] | None = None):
        """Create a fake CompilationUnit for testing."""
        from unittest.mock import MagicMock

        if clang_args is None:
            clang_args = ["gcc", "-c", file_path]
        unit = MagicMock()
        # A real CompilationUnit.file IS a Path, and a MagicMock here hides
        # its basename from anything that does not call .resolve().
        unit.file = Path(file_path)
        unit.clang_args = clang_args
        return unit

    def test_deterministic(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        units = []
        h1 = compute_config_hash(units, tmp_path, "test-project", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(units, tmp_path, "test-project", transient_defines=DEFAULT_TRANSIENT_DEFINES)
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
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 == h2

    def test_different_flags_produce_different_hash(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-O2", "-c", "a.cpp"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-Os", "-c", "a.cpp"])]
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 != h2

    def test_flag_order_independent(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-O2", "-DFOO=1", "-c", "a.cpp"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"), ["gcc", "-DFOO=1", "-O2", "-c", "a.cpp"])]
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 == h2  # arguments are sorted alphabetically

    def test_hash_is_hex_string(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash

        h = compute_config_hash([], tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
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
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
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
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 == h2  # space-separated form is also stripped

    def test_transient_mbed_build_timestamp_value_only(self, tmp_path: Path):
        """-DMBED_BUILD_TIMESTAMP (no =value) should also be stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DMBED_BUILD_TIMESTAMP"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp")])]
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        assert h1 == h2  # with and without the transient define must match

    def test_other_defines_preserved(self, tmp_path: Path):
        """Non-transient -D macros must NOT be stripped."""
        from fw_context_mcp.indexer.manifest import compute_config_hash

        u1 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"), "-DDEBUG=1"])]
        u2 = [self._make_unit(str(tmp_path / "a.cpp"),
              ["gcc", "-c", str(tmp_path / "a.cpp"),
               "-DNDEBUG"])]
        h1 = compute_config_hash(u1, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        h2 = compute_config_hash(u2, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
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
            hashes.add(compute_config_hash(u, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES))
        # Without any transient macros
        u_base = [self._make_unit(str(tmp_path / "a.cpp"), base_args)]
        hashes.add(compute_config_hash(u_base, tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES))
        assert len(hashes) == 1  # all produce the same hash


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import compute_config_hash, load, save

        manifest = {
            "_format": MANIFEST_FORMAT,
            "project_root": str(tmp_path),
            "compile_commands_path": str(tmp_path / "compile_commands.json"),
            "arg_sets": [["gcc", "-c", "src/main.cpp"]],
            "headers": {
                "src/config.h": {"hash": "def456", "generated": False},
                "build/mbed_config.h": {"hash": "ghi789", "generated": True},
            },
            "entries": [
                {
                    "file": "src/main.cpp",
                    "directory": str(tmp_path),
                    "arg_set": 0,
                    "source_hash": "abc123",
                    "headers": ["src/config.h", "build/mbed_config.h"],
                }
            ],
        }
        # Compute config_hash from a mock unit list (backward compat path)
        from unittest.mock import MagicMock

        mock_unit = MagicMock()
        mock_unit.file.resolve.return_value = Path(str(tmp_path / "src/main.cpp"))
        mock_unit.clang_args = ["gcc", "-c", "src/main.cpp"]
        config_hash = compute_config_hash([mock_unit], tmp_path, "test", transient_defines=DEFAULT_TRANSIENT_DEFINES)
        config_hash = save(manifest, tmp_path, config_hash)
        assert len(config_hash) == 64

        loaded = load(tmp_path)
        assert loaded is not None
        assert loaded["_format"] == MANIFEST_FORMAT
        assert len(loaded["entries"]) == 1
        assert loaded["entries"][0]["file"] == "src/main.cpp"
        # The shared tables survive the round trip and still resolve.
        from fw_context_mcp.indexer.manifest import tu_arguments, tu_headers

        entry = loaded["entries"][0]
        assert tu_arguments(loaded, entry) == ["gcc", "-c", "src/main.cpp"]
        assert tu_headers(loaded, entry) == [
            {"path": "src/config.h", "hash": "def456", "generated": False},
            {"path": "build/mbed_config.h", "hash": "ghi789", "generated": True},
        ]

    def test_load_nonexistent(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import load

        loaded = load(tmp_path)
        assert loaded is None

    def test_a_manifest_from_an_older_format_is_ignored(self, tmp_path: Path):
        """An unreadable manifest must read as absent, not as empty.

        A /1 manifest keeps its header records inside each entry.  Handing it
        to a /2 reader would resolve every path against a missing table, and
        the entry would look like a TU with no headers — nothing marks stale
        and the index freezes.  Returning None sends the caller down the
        reindex path instead.
        """
        import json

        from fw_context_mcp.indexer.manifest import load

        legacy = {
            "_format": "fw-context-manifest/1",
            "project_root": str(tmp_path),
            "entries": [{
                "file": "src/main.cpp",
                "arguments": ["gcc"],
                "source_hash": "abc",
                "headers": [{"path": "src/a.h", "hash": "111", "generated": False}],
            }],
        }
        (tmp_path / "manifest.deadbeef.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        assert load(tmp_path, "deadbeef") is None
        assert load(tmp_path) is None


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
        stale, new_hash = check_tu_staleness(entry, tmp_path)
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
        stale, new_hash = check_tu_staleness(entry, tmp_path)
        assert stale is False
        assert new_hash is None

    def test_project_header_changed(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness, tu_headers

        # Create source file
        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        # Create header with OLD hash
        header = tmp_path / "src" / "config.h"
        header.write_text("// original config")
        old_header_hash = hashlib.sha256(b"// different content").hexdigest()

        manifest = {
            "headers": {
                "src/config.h": {"hash": old_header_hash, "generated": False},
            },
            "entries": [{
                "file": "src/main.cpp",
                "source_hash": source_hash,
                "headers": ["src/config.h"],
            }],
        }
        entry = manifest["entries"][0]
        stale, _ = check_tu_staleness(
            entry, tmp_path, headers=tu_headers(manifest, entry)
        )
        assert stale is True  # header hash differs

    def test_a_changed_generated_header_makes_the_unit_stale(self, tmp_path: Path):
        """mbed_config.h carries mbed_app.json, and the #ifdefs of the unit follow it.

        The rule that trusted a generated header said that its content
        changes with each build.  Measured on 2026-10-07 it does not: a
        header of the build changes only with the configuration.
        """
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": ["BUILD/mbed_config.h"],
        }
        # The records go in as `headers=`.  check_tu_staleness never reads
        # entry["headers"]: that field holds paths, and the records live in
        # the manifest's shared map.
        stale, _ = check_tu_staleness(
            entry, tmp_path,
            headers=[{"path": "BUILD/mbed_config.h", "hash": "nonexistent_hash", "generated": True}],
        )
        assert stale is True

    def test_an_in_tree_vendor_header_is_not_trusted(self, tmp_path: Path):
        """A changed vendor header makes its dependent TU stale.

        This test asserted ``stale is False`` before, on the reasoning that
        re-hashing the SDK would dominate the index run.  Measured: 45 ms
        against 42 minutes, so cost was never the reason.

        External code reaches the index THROUGH the project's own units: an
        inline function, a macro, a constexpr, a template or a struct layout
        expands from the header INTO every unit that includes it.  A changed
        vendor header therefore changes the indexed symbols of PROJECT files,
        so a full reparse is the correct answer and not a conservative one.
        """
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
            "headers": ["mbed-os/mbed.h"],
        }
        stale, _ = check_tu_staleness(
            entry, tmp_path,
            headers=[{"path": "mbed-os/mbed.h", "hash": "nonexistent_hash", "generated": False}],
        )
        assert stale is True

    def test_an_out_of_tree_header_is_not_trusted(self, tmp_path: Path):
        """A changed toolchain header makes its dependent TU stale.

        This is the defect A4 measured from the other side: a toolchain
        update moved no source mtime, every toolchain header sat outside
        project_root, and nothing queued a single TU for reparse.
        """
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        outside = tmp_path / "toolchain" / "stdint.h"
        outside.parent.mkdir(parents=True)
        outside.write_text("// toolchain header")

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": [str(outside)],
        }
        stale, _ = check_tu_staleness(
            entry, tmp_path,
            headers=[{"path": str(outside), "hash": "nonexistent_hash", "generated": False}],
        )
        assert stale is True

    def test_a_missing_header_reads_as_changed(self, tmp_path: Path):
        """A path that no longer exists is stale, not trusted.

        compute_source_hash returns "" on OSError, so a toolchain path that
        disappeared cannot match a stored hash.
        """
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "main.cpp"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        source_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        entry = {
            "file": "src/main.cpp",
            "source_hash": source_hash,
            "headers": ["/gone/stdint.h"],
        }
        stale, _ = check_tu_staleness(
            entry, tmp_path,
            headers=[{"path": "/gone/stdint.h", "hash": "some_hash", "generated": False}],
        )
        assert stale is True


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
        header_table: dict[str, dict] = {}
        headers = _collect_headers_from_tokens(unit, tmp_path, None, header_table)

        # Paths come back; the hash and the generated flag land in the table.
        assert isinstance(headers, list)
        assert all(isinstance(path, str) for path in headers)
        assert len(headers) > 0
        assert any("config.h" in path for path in headers)
        assert set(headers) == set(header_table)
        for record in header_table.values():
            assert "hash" in record
            assert "generated" in record

    def test_a_shared_header_is_hashed_once(self, tmp_path: Path):
        """Two TUs including the same header produce one table record.

        This is what makes the manifest 74% smaller on the Mbed project: the old shape
        stored one record per (TU, header) pair — 86 686 of them for 1 037
        files.
        """
        from fw_context_mcp.indexer.manifest import _collect_headers_from_tokens

        src_dir = tmp_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "config.h").write_text("// config header")
        first = src_dir / "a.cpp"
        first.write_text('#include "config.h"\nint a() { return 0; }')
        second = src_dir / "b.cpp"
        second.write_text('#include "config.h"\nint b() { return 0; }')

        header_table: dict[str, dict] = {}
        args = ["-std=c++14", "-I" + str(src_dir)]
        for source in (first, second):
            class FakeUnit:
                file = source
                clang_args = args

            paths = _collect_headers_from_tokens(
                FakeUnit(), tmp_path, None, header_table
            )
            assert "src/config.h" in paths

        assert len([p for p in header_table if p.endswith("config.h")]) == 1


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
        update_entry(manifest, 0, "new_hash", new_headers, flags_hash="new_flags", arguments=["-DNEW"])
        assert manifest["entries"][0]["source_hash"] == "new_hash"
        assert manifest["entries"][0]["headers"] == new_headers
        assert manifest["entries"][0]["flags_hash"] == "new_flags"
        assert manifest["arg_sets"][manifest["entries"][0]["arg_set"]] == ["-DNEW"]
        assert manifest["entries"][1]["source_hash"] == "old"  # unchanged

    def test_the_reparse_removes_the_needs_reparse_mark(self):
        """check_tu_staleness reads a marked entry as stale whatever its hashes say."""
        from fw_context_mcp.indexer.manifest import update_entry

        manifest = {"entries": [{"file": "a.c", "source_hash": "old", "headers": [], "needs_reparse": True}]}
        update_entry(manifest, 0, "new", [], flags_hash="f", arguments=[])
        assert "needs_reparse" not in manifest["entries"][0]

    def test_update_out_of_range(self):
        from fw_context_mcp.indexer.manifest import update_entry

        manifest = {"entries": []}
        update_entry(manifest, 5, "hash", [], flags_hash="f", arguments=[])
        # Should not raise — silently no-op
        assert len(manifest["entries"]) == 0


class TestCollectStaleHeaders:
    """Unit tests for the header → TU staleness pre-pass."""

    @staticmethod
    def _manifest(tmp_path: Path, headers: list[dict], *, tu: str = "src/main.cpp") -> dict:
        """Build a manifest from header records, in the stored /2 shape.

        The tests express their input as records because that is what a parse
        produces; fold_headers puts them where the manifest keeps them.
        """
        import hashlib

        from fw_context_mcp.indexer.manifest import fold_headers

        src = tmp_path / tu
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        header_table: dict[str, dict] = {}
        paths = fold_headers(headers, header_table)
        return {
            "project_root": str(tmp_path),
            "headers": header_table,
            "entries": [
                {
                    "file": tu,
                    "source_hash": hashlib.sha256(src.read_bytes()).hexdigest(),
                    "headers": paths,
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
        assert collect_stale_headers(manifest, tmp_path) == {"src/config.h"}

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
        assert collect_stale_headers(manifest, tmp_path) == set()

    def test_a_changed_generated_header_is_stale(self, tmp_path: Path):
        """No header is trusted.  A pristine build gives the same generated headers.

        Measured on 2026-10-07: 0 of 325 headers of a real sysbuild project
        changed between two pristine builds, thus to re-hash them costs no
        reparse when nothing changed.
        """
        from fw_context_mcp.indexer.manifest import collect_stale_headers

        (tmp_path / "BUILD").mkdir()
        (tmp_path / "BUILD" / "mbed_config.h").write_text("// generated")

        manifest = self._manifest(
            tmp_path,
            [{"path": "BUILD/mbed_config.h", "hash": "stale", "generated": True}],
        )
        assert collect_stale_headers(manifest, tmp_path) == {"BUILD/mbed_config.h"}

    def test_an_in_tree_vendor_header_change_is_detected(self, tmp_path: Path):
        """The other half of the old test, with its sign inverted.

        A vendor header used to keep its stored hash and its dependents were
        never queued.  See test_an_in_tree_vendor_header_is_not_trusted for
        why external code changes the indexed symbols of project files.
        """
        from fw_context_mcp.indexer.manifest import collect_stale_headers

        (tmp_path / "mbed-os").mkdir()
        (tmp_path / "mbed-os" / "mbed.h").write_text("// SDK")

        manifest = self._manifest(
            tmp_path,
            [{"path": "mbed-os/mbed.h", "hash": "stale", "generated": False}],
        )
        assert collect_stale_headers(manifest, tmp_path) == {"mbed-os/mbed.h"}

    def test_a_vendor_change_queues_its_dependents(self, tmp_path: Path):
        """Every TU that includes the changed vendor header is queued."""
        from fw_context_mcp.indexer.manifest import (
            collect_stale_headers,
            tus_affected_by_headers,
        )

        (tmp_path / "mbed-os").mkdir()
        (tmp_path / "mbed-os" / "mbed.h").write_text("// SDK")

        manifest = self._manifest(
            tmp_path,
            [{"path": "mbed-os/mbed.h", "hash": "stale", "generated": False}],
        )
        manifest["entries"].append({
            "file": "src/other.cpp",
            "source_hash": "x",
            "headers": ["mbed-os/mbed.h"],
        })

        stale = collect_stale_headers(manifest, tmp_path)
        assert tus_affected_by_headers(manifest, stale) == {
            "src/main.cpp", "src/other.cpp",
        }

    def test_an_out_of_tree_header_change_is_detected(self, tmp_path: Path):
        """A changed toolchain or system header is stale, not trusted.

        This test asserted ``== set()`` before.  It is the defect A4 measured
        from the other side: a toolchain update moves no source mtime, so if
        nothing here reports the header, no TU is ever queued.
        """
        from fw_context_mcp.indexer.manifest import collect_stale_headers

        outside = tmp_path.parent / f"{tmp_path.name}-external.h"
        outside.write_text("// toolchain header")
        try:
            manifest = self._manifest(
                tmp_path, [{"path": str(outside), "hash": "stale", "generated": False}]
            )
            assert collect_stale_headers(manifest, tmp_path) == {str(outside)}
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
        assert collect_stale_headers(manifest, tmp_path, hash_cache=cache) == set()

    def test_tus_affected_by_headers(self, tmp_path: Path):
        from fw_context_mcp.indexer.manifest import tus_affected_by_headers

        manifest = {
            "entries": [
                {"file": "src/main.cpp", "headers": ["src/config.h"]},
                {"file": "src/other.cpp", "headers": ["src/util.h"]},
            ]
        }
        assert tus_affected_by_headers(manifest, {"src/config.h"}) == {"src/main.cpp"}
        assert tus_affected_by_headers(manifest, set()) == set()


class TestVendorPatternCarrier:
    """The manifest carries the vendor set the index run applied.

    The query layer cannot derive the same set: the strongest source for
    Zephyr is ``-fmacro-prefix-map`` in the compiler flags, and only the
    indexer has those.  Without the carrier the two layers disagree, and a
    narrower set on the query side reports the index as permanently stale.
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

    def _run(self, tmp_path: Path, *, manifest, vendor_patterns):
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        unit = self._unit(tmp_path, "src/main.cpp")
        (tmp_path / "index").mkdir(exist_ok=True)
        return _update_manifest_after_index(
            manifest=manifest,
            units=[unit],
            project_root=tmp_path,
            db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json",
            updated_count=1,
            tu_headers={"src/main.cpp": [
                {"path": "src/config.h", "hash": "FRESH", "generated": False}
            ]},
            vendor_patterns=vendor_patterns,
            config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, {"src/main.cpp"}),
            transient_defines=DEFAULT_TRANSIENT_DEFINES,
        )

    @staticmethod
    def _base(tmp_path: Path, **extra) -> dict:
        manifest = {
            "_format": MANIFEST_FORMAT,
            "project_root": str(tmp_path),
            "arg_sets": [["gcc", "-c", "src/main.cpp"]],
            "headers": {"src/config.h": {"hash": "STORED", "generated": False}},
            "entries": [
                {
                    "file": "src/main.cpp",
                    "directory": str(tmp_path),
                    "arg_set": 0,
                    "source_hash": "stored-source",
                    "flags_hash": "stored-flags",
                    "headers": ["src/config.h"],
                }
            ],
        }
        manifest.update(extra)
        return manifest

    def test_the_manifest_carries_the_vendor_patterns(self, tmp_path: Path):
        result = self._run(
            tmp_path,
            manifest=self._base(tmp_path),
            vendor_patterns=["deps/ncs/zephyr/%", "deps/ncs/%"],
        )

        assert result is not None
        assert result["vendor_patterns"] == ["deps/ncs/zephyr/%", "deps/ncs/%"]

    def test_an_incremental_run_inherits_the_patterns(self, tmp_path: Path):
        """A run that computes no patterns keeps what the build was indexed with.

        Without it the next staleness check reads a manifest that describes a
        different boundary from the one the rows were written under.

        The SIGNAL for "I did not compute one" is None, not [].  This test
        used [] before, and so did the writer — which is why an empty set,
        the correct answer for every project whose SDK lives outside it, was
        never stored at all.  See test_an_empty_set_is_STORED_as_an_answer.
        """
        old = self._base(tmp_path, vendor_patterns=["deps/ncs/%"])

        result = self._run(tmp_path, manifest=old, vendor_patterns=None)

        assert result is not None
        assert result["vendor_patterns"] == ["deps/ncs/%"]

    def test_an_empty_set_is_STORED_as_an_answer(self, tmp_path: Path):
        """[] must reach the file.  A missing key means something else.

        This test had the opposite sign, and the writer agreed with it: the
        key was written under a truthiness test, so an empty set was dropped.
        Measured on all 9 builds of the Zephyr project — every one keeps its SDK
        outside the project, so every one computes the empty set — and not
        one of their manifests carried the key.  vendor_patterns_for_build()
        then fell back to detection, which is the disagreement between the
        two layers that this carrier exists to remove.
        """
        result = self._run(tmp_path, manifest=self._base(tmp_path), vendor_patterns=[])

        assert result is not None
        assert result["vendor_patterns"] == []

    def test_none_means_inherit_and_writes_nothing_new(self, tmp_path: Path):
        """None is the caller saying "I did not compute one"."""
        result = self._run(
            tmp_path, manifest=self._base(tmp_path), vendor_patterns=None
        )

        assert result is not None
        assert "vendor_patterns" not in result

    def test_an_absent_key_forces_a_rewrite(self, tmp_path: Path):
        """The no-op early return must not keep a manifest without the key.

        Nothing else would ever rewrite it: the config_hash does not change,
        and the TU count does not change either.
        """
        from fw_context_mcp.indexer._manifest_updater import _patterns_changed

        assert _patterns_changed({}, None, []) is True
        assert _patterns_changed({"vendor_patterns": []}, None, []) is False
        assert _patterns_changed({"vendor_patterns": []}, None, ["a/%"]) is True
        assert _patterns_changed({"vendor_patterns": ["a/%"]}, None, None) is False


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
            "_format": MANIFEST_FORMAT,
            "project_root": str(tmp_path),
            "arg_sets": [list(unit.clang_args)],
            "headers": {"src/config.h": {"hash": "STORED", "generated": False}},
            "entries": [
                {
                    "file": "src/main.cpp",
                    "directory": str(tmp_path),
                    "arg_set": 0,
                    "source_hash": "stored-source",
                    "flags_hash": "stored-flags",
                    "headers": ["src/config.h"],
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
            reparsed_tus=_parsed(tmp_path, reparsed),
            transient_defines=DEFAULT_TRANSIENT_DEFINES,
        )

    @staticmethod
    def _stored_hash(result: dict) -> str:
        """The hash the first entry's only header resolves to."""
        from fw_context_mcp.indexer.manifest import tu_headers

        headers = tu_headers(result, result["entries"][0])
        assert len(headers) == 1, f"expected one header, got {headers}"
        return headers[0]["hash"]

    def test_not_reparsed_keeps_stored_hash(self, tmp_path: Path):
        (tmp_path / "index").mkdir()
        # updated_count=1 → another TU was re-parsed, so the rebuild path runs,
        # but src/main.cpp itself was not re-parsed.
        result = self._run(tmp_path, updated_count=1, reparsed=set())
        assert result is not None
        assert self._stored_hash(result) == "STORED"

    def test_reparsed_takes_fresh_hash(self, tmp_path: Path):
        (tmp_path / "index").mkdir()
        result = self._run(tmp_path, updated_count=1, reparsed={"src/main.cpp"})
        assert result is not None
        assert self._stored_hash(result) == "FRESH"

    def test_nothing_reparsed_returns_manifest_untouched(self, tmp_path: Path):
        """updated_count=0 → no rewrite at all, even with collected headers."""
        (tmp_path / "index").mkdir()
        result = self._run(tmp_path, updated_count=0, reparsed=set())
        assert result is not None
        assert self._stored_hash(result) == "STORED"
        assert not list((tmp_path / "index").glob("manifest.*.json")), (
            "manifest file was written for a run that re-parsed nothing"
        )

    def test_a_reused_entry_keeps_pointing_at_its_own_arguments(self, tmp_path: Path):
        """``arg_set`` is an index, so a carried-over entry must be re-interned.

        The rebuild starts a fresh ``arg_sets`` table.  Copying an entry with
        its old index would silently attach it to whatever argument list
        happens to sit at that position, or to none at all.
        """
        from fw_context_mcp.indexer.manifest import tu_arguments

        (tmp_path / "index").mkdir()
        result = self._run(tmp_path, updated_count=1, reparsed=set())
        assert result is not None
        entry = result["entries"][0]
        assert tu_arguments(result, entry) == ["gcc", "-c", "src/main.cpp"]


class TestOneEntryForEachListing:
    """A file that compile_commands.json lists more than once keeps one entry for each listing.

    Zephyr compiles ``misc/empty_file.c`` three times in one image, with two
    sets of flags.  The carry-over looked the old entry up by file alone,
    thus each listing got the last entry of the file: A, A, B became B, B, B.
    """

    @staticmethod
    def _unit(tmp_path: Path, rel: str, define: str):
        from unittest.mock import MagicMock

        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int x;\n")
        args = ["gcc", define, "-c", rel]
        unit = MagicMock()
        unit.file = src
        unit.directory = tmp_path
        unit.clang_args = args[1:]
        unit.raw_entry = {"file": rel, "directory": str(tmp_path), "arguments": args}
        return unit

    @staticmethod
    def _flags(unit) -> str:
        from fw_context_mcp.indexer.config_hash import compute_flags_hash

        return compute_flags_hash(unit.raw_entry, transient_defines=DEFAULT_TRANSIENT_DEFINES)

    def _manifest(self, tmp_path: Path, units: list) -> dict:
        """The manifest that the previous run wrote: one correct entry for each unit."""
        arg_sets: list[list[str]] = []
        entries = []
        for unit in units:
            if unit.clang_args not in arg_sets:
                arg_sets.append(list(unit.clang_args))
            entries.append({
                "file": str(unit.file.relative_to(tmp_path)),
                "directory": str(tmp_path),
                "arg_set": arg_sets.index(unit.clang_args),
                "source_hash": "stored-source",
                "flags_hash": self._flags(unit),
                "headers": [],
            })
        return {
            "_format": MANIFEST_FORMAT, "project_root": str(tmp_path),
            "arg_sets": arg_sets, "headers": {}, "entries": entries,
        }

    def _update(self, tmp_path: Path, manifest: dict, units: list, *, with_tu_headers: bool) -> dict:
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        (tmp_path / "index").mkdir(exist_ok=True)
        result = _update_manifest_after_index(
            manifest=manifest,
            units=units,
            project_root=tmp_path,
            db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json",
            updated_count=1,
            tu_headers={"src/main.c": []} if with_tu_headers else None,
            config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, {"src/main.c"}),
            transient_defines=DEFAULT_TRANSIENT_DEFINES,
        )
        assert result is not None
        return result

    @pytest.mark.parametrize("with_tu_headers", [True, False], ids=["tu_headers", "incremental"])
    def test_each_listing_keeps_its_own_flags(self, tmp_path: Path, with_tu_headers: bool):
        from fw_context_mcp.indexer.manifest import tu_arguments

        a1 = self._unit(tmp_path, "misc/empty.c", "-DA")
        a2 = self._unit(tmp_path, "misc/empty.c", "-DA")
        b = self._unit(tmp_path, "misc/empty.c", "-DB")
        main = self._unit(tmp_path, "src/main.c", "-DA")
        units = [a1, a2, b, main]

        result = self._update(tmp_path, self._manifest(tmp_path, units), units, with_tu_headers=with_tu_headers)

        empty = [e for e in result["entries"] if e["file"] == "misc/empty.c"]
        assert [e["flags_hash"] for e in empty] == [self._flags(a1), self._flags(a2), self._flags(b)]
        assert [tu_arguments(result, e)[0] for e in empty] == ["-DA", "-DA", "-DB"]

    @pytest.mark.parametrize("with_tu_headers", [True, False], ids=["tu_headers", "incremental"])
    def test_a_new_listing_with_known_flags_gets_a_copy(self, tmp_path: Path, with_tu_headers: bool):
        """The build now lists the file with flags A three times; B is gone."""
        a = self._unit(tmp_path, "misc/empty.c", "-DA")
        b = self._unit(tmp_path, "misc/empty.c", "-DB")
        main = self._unit(tmp_path, "src/main.c", "-DA")
        manifest = self._manifest(tmp_path, [a, b, main])
        units = [a, self._unit(tmp_path, "misc/empty.c", "-DA"), self._unit(tmp_path, "misc/empty.c", "-DA"), main]

        result = self._update(tmp_path, manifest, units, with_tu_headers=with_tu_headers)

        empty = [e for e in result["entries"] if e["file"] == "misc/empty.c"]
        assert [e["flags_hash"] for e in empty] == [self._flags(a)] * 3

    @pytest.mark.parametrize("with_tu_headers", [True, False], ids=["tu_headers", "incremental"])
    def test_an_entry_keeps_the_flags_that_the_index_parsed(self, tmp_path: Path, with_tu_headers: bool):
        """The flags of each listing changed, and no listing was parsed again.

        The entries keep the old flags hash: the index still holds what the
        old flags gave, and the decision for the next run must see that.
        """
        a = self._unit(tmp_path, "misc/empty.c", "-DA")
        b = self._unit(tmp_path, "misc/empty.c", "-DB")
        main = self._unit(tmp_path, "src/main.c", "-DA")
        manifest = self._manifest(tmp_path, [a, b, main])
        units = [self._unit(tmp_path, "misc/empty.c", "-DC"), self._unit(tmp_path, "misc/empty.c", "-DD"), main]

        result = self._update(tmp_path, manifest, units, with_tu_headers=with_tu_headers)

        empty = [e for e in result["entries"] if e["file"] == "misc/empty.c"]
        assert [e["flags_hash"] for e in empty] == [self._flags(a), self._flags(b)]

    def test_a_manifest_that_lost_a_flag_set_keeps_what_was_parsed(self, tmp_path: Path):
        """A manifest of the old carry-over holds B, B, B for the listings A, A, B.

        Without a parse of the file, the index holds what B gave, and the
        entries say so.  The decision by flags hash then parses the two
        listings of A again, and their fresh entries hold A.
        """
        a = self._unit(tmp_path, "misc/empty.c", "-DA")
        b = self._unit(tmp_path, "misc/empty.c", "-DB")
        main = self._unit(tmp_path, "src/main.c", "-DA")
        manifest = self._manifest(tmp_path, [b, b, b, main])
        units = [a, self._unit(tmp_path, "misc/empty.c", "-DA"), b, main]

        result = self._update(tmp_path, manifest, units, with_tu_headers=True)

        empty = [e for e in result["entries"] if e["file"] == "misc/empty.c"]
        assert [e["flags_hash"] for e in empty] == [self._flags(b)] * 3


class TestManifestCheckpoint:
    """The write of the manifest during the run (``checkpoint_reached``)."""

    def test_a_unit_without_an_entry_gets_none_and_no_tokenizing(self, tmp_path: Path, monkeypatch):
        """The end of the run gives it an entry; a checkpoint must not parse it for one."""
        import fw_context_mcp.indexer.manifest as manifest_mod

        def no_tokenizing(*args, **kwargs):
            raise AssertionError("a checkpoint read a unit from the token stream")

        monkeypatch.setattr(manifest_mod, "_collect_headers_from_tokens", no_tokenizing)
        listing = TestOneEntryForEachListing()
        main = listing._unit(tmp_path, "src/main.c", "-DA")
        new = listing._unit(tmp_path, "src/new.c", "-DA")
        manifest = listing._manifest(tmp_path, [main])

        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        (tmp_path / "index").mkdir()
        result = _update_manifest_after_index(
            manifest=manifest, units=[main, new], project_root=tmp_path, db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json", updated_count=1,
            tu_headers={"src/main.c": []}, config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, {"src/main.c"}),
            transient_defines=DEFAULT_TRANSIENT_DEFINES, checkpoint_reached=2,
        )

        assert result is not None
        assert [e["file"] for e in result["entries"]] == ["src/main.c"]
        assert result["entries"][0]["source_hash"] == compute_source_hash(main.file)

    def test_a_listing_after_the_reached_position_keeps_its_old_entry(self, tmp_path: Path):
        """A file listed twice: the run stored the first listing, and stopped before the second.

        tu_headers and reparsed_tus are keyed by file, thus the second
        listing got a fresh entry with ITS new flags, and the next run kept
        both listings as unchanged while the rows came from the first one.
        """
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        listing = TestOneEntryForEachListing()
        a = listing._unit(tmp_path, "misc/empty.c", "-DA")
        b_old = listing._unit(tmp_path, "misc/empty.c", "-DB")
        b_new = listing._unit(tmp_path, "misc/empty.c", "-DC")
        manifest = listing._manifest(tmp_path, [a, b_old])

        (tmp_path / "index").mkdir()
        result = _update_manifest_after_index(
            manifest=manifest, units=[a, b_new], project_root=tmp_path, db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json", updated_count=1,
            tu_headers={"misc/empty.c": []}, config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, {"misc/empty.c"}),
            transient_defines=DEFAULT_TRANSIENT_DEFINES, checkpoint_reached=1,
        )

        assert result is not None
        assert [e["flags_hash"] for e in result["entries"]] == [listing._flags(a), listing._flags(b_old)]

    def test_an_unchanged_listing_after_the_reached_position_is_behind(self, tmp_path: Path):
        """Only the flags of the first listing changed, and the run stopped before the second.

        The old entry of the second listing is current by its own hashes,
        but the rows of the file are now the rows of the first listing.
        """
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        listing = TestOneEntryForEachListing()
        a_old = listing._unit(tmp_path, "misc/empty.c", "-DA")
        b = listing._unit(tmp_path, "misc/empty.c", "-DB")
        a_new = listing._unit(tmp_path, "misc/empty.c", "-DX")
        manifest = listing._manifest(tmp_path, [a_old, b])

        (tmp_path / "index").mkdir()
        result = _update_manifest_after_index(
            manifest=manifest, units=[a_new, b], project_root=tmp_path, db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json", updated_count=1,
            tu_headers={"misc/empty.c": []}, config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, {"misc/empty.c"}),
            transient_defines=DEFAULT_TRANSIENT_DEFINES, checkpoint_reached=1,
        )

        assert result is not None
        first, second = result["entries"]
        assert first["flags_hash"] == listing._flags(a_new) and "needs_reparse" not in first
        assert second["flags_hash"] == listing._flags(b) and second["needs_reparse"] is True

    def test_without_a_manifest_there_is_no_checkpoint(self, tmp_path: Path):
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        unit = TestOneEntryForEachListing()._unit(tmp_path, "src/main.c", "-DA")
        (tmp_path / "index").mkdir()
        result = _update_manifest_after_index(
            manifest=None, units=[unit], project_root=tmp_path, db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json", updated_count=1,
            tu_headers=None, config_hash="deadbeef", reparsed_tus={},
            transient_defines=DEFAULT_TRANSIENT_DEFINES, checkpoint_reached=2,
        )

        assert result is None
        assert list((tmp_path / "index").iterdir()) == []

    def test_the_manifest_is_written_through_a_rename(self, tmp_path: Path, monkeypatch):
        """A stop during the write must leave the old manifest, not a cut file."""
        import fw_context_mcp.indexer.manifest as manifest_mod

        manifest_mod.save({"_format": MANIFEST_FORMAT, "entries": []}, tmp_path, "cafe")
        before = (tmp_path / "manifest.cafe.json").read_text(encoding="utf-8")

        def stop(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("fw_context_mcp.utils.os.replace", stop)
        with pytest.raises(KeyboardInterrupt):
            manifest_mod.save({"_format": MANIFEST_FORMAT, "entries": [{"file": "a.c"}]}, tmp_path, "cafe")

        assert (tmp_path / "manifest.cafe.json").read_text(encoding="utf-8") == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.cafe.json"]


class TestOldEntriesTake:
    """The order of preference of ``_OldEntries.take``, one case for each rule."""

    @staticmethod
    def _take_all(old: list[str], new: list[str]) -> list[int]:
        """Return, for each new listing, the position of the old entry that it got."""
        from fw_context_mcp.indexer._manifest_updater import _OldEntries

        entries = [{"file": "f.c", "flags_hash": flags, "pos": i} for i, flags in enumerate(old)]
        old_entries = _OldEntries({"entries": entries})
        return [old_entries.take("f.c", flags)["pos"] for flags in new]

    @pytest.mark.parametrize(
        ("old", "new", "expected"),
        [
            (["A", "A", "B"], ["A", "A", "B"], [0, 1, 2]),  # 1: each listing, its own entry
            (["A", "B"], ["B", "A"], [1, 0]),  # 1: the order of the listings changed
            (["A"], ["A", "A"], [0, 0]),  # 2: one more listing with known flags
            (["A", "B"], ["C", "A"], [0, 0]),  # 3 then 2: C takes the unused A first
            (["A", "B"], ["C", "D"], [0, 1]),  # 3: the flags of each listing changed
            (["A", "B"], ["C", "D", "E"], [0, 1, 0]),  # 4: no entry is left
            (["A", "A", "B"], ["B"], [2]),  # fewer listings
        ],
    )
    def test_the_order_of_preference(self, old, new, expected):
        assert self._take_all(old, new) == expected

    def test_a_file_without_an_entry_is_not_in_it(self):
        from fw_context_mcp.indexer._manifest_updater import _OldEntries

        old_entries = _OldEntries({"entries": [{"file": "f.c", "flags_hash": "A"}]})
        assert "f.c" in old_entries
        assert "g.c" not in old_entries
        assert "f.c" not in _OldEntries(None)


class TestLoadBuildDirPatterns:
    """``build_dir_patterns`` is read on the MCP query path, so it is cached.

    ``_stale_files`` runs on every query routed through
    ``_with_stale_recovery`` and needs nothing from the manifest but this one
    short list.  Parsing the whole file for it was the most expensive thing on
    that path: measured on the Mbed project, 94.7 ms to fetch ``['BUILD/']`` from a
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
                "_format": MANIFEST_FORMAT,
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


class TestMtimeBumpIsSafe:
    """The mtime may move forward only for a header the pipeline trusts.

    This is the site that is easy to forget.  It used to return True for a
    vendor, a generated and an out-of-tree header with no check of the
    content.  Left alone, a header with changed content would get a new
    mtime, _count_modified_files would stop seeing the change, and the end
    of trust in the other four callers would have no effect.
    """

    def test_the_mtime_bump_verifies_a_vendor_header(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer._manifest_updater import _mtime_bump_is_safe

        header = tmp_path / "mbed-os" / "mbed.h"
        header.parent.mkdir(parents=True)
        header.write_text("// changed content")
        stored = hashlib.sha256(b"// old content").hexdigest()

        assert _mtime_bump_is_safe(
            header, {"hash": stored, "generated": False}, None
        ) is False

    def test_the_mtime_bump_verifies_an_out_of_tree_header(self, tmp_path: Path):
        import hashlib

        from fw_context_mcp.indexer._manifest_updater import _mtime_bump_is_safe

        header = tmp_path / "toolchain" / "stdint.h"
        header.parent.mkdir(parents=True)
        header.write_text("// changed content")
        stored = hashlib.sha256(b"// old content").hexdigest()

        assert _mtime_bump_is_safe(
            header, {"hash": stored, "generated": False}, None
        ) is False

    def test_an_unchanged_vendor_header_may_bump(self, tmp_path: Path):
        """Content-identical still qualifies — this is what fixes VCS mtime drift."""
        import hashlib

        from fw_context_mcp.indexer._manifest_updater import _mtime_bump_is_safe

        header = tmp_path / "mbed-os" / "mbed.h"
        header.parent.mkdir(parents=True)
        header.write_text("// same content")
        stored = hashlib.sha256(header.read_bytes()).hexdigest()

        assert _mtime_bump_is_safe(
            header, {"hash": stored, "generated": False}, None
        ) is True

    def test_a_changed_generated_header_keeps_its_stale_mtime(self, tmp_path: Path):
        """A bump would hide the change of the configuration from the next run."""
        import hashlib

        from fw_context_mcp.indexer._manifest_updater import _mtime_bump_is_safe

        header = tmp_path / "BUILD" / "mbed_config.h"
        header.parent.mkdir(parents=True)
        header.write_text("// generated")

        assert _mtime_bump_is_safe(
            header, {"hash": "whatever", "generated": True}, None
        ) is False
        same = hashlib.sha256(header.read_bytes()).hexdigest()
        assert _mtime_bump_is_safe(header, {"hash": same, "generated": True}, None) is True


class TestMergeHeaderRecords:
    """``generated`` goes from False to True, never the other way.

    ``generated`` is a property of the PATH, not of one parse.  A caller with
    no build_dir_patterns computes False for every header, and the query
    layer and the coverage purge read the flag — to let False overwrite True
    gives them a wrong answer in silence.
    """

    def test_update_entry_does_not_downgrade_generated(self):
        from fw_context_mcp.indexer.manifest import update_entry

        manifest = {
            "headers": {"build/autoconf.h": {"hash": "OLD", "generated": True}},
            "entries": [{"file": "src/main.c", "source_hash": "x", "headers": []}],
        }
        update_entry(
            manifest, 0, "new-source", ["build/autoconf.h"],
            {"build/autoconf.h": {"hash": "NEW", "generated": False}},
            flags_hash="f", arguments=[],
        )

        assert manifest["headers"]["build/autoconf.h"]["generated"] is True
        assert manifest["headers"]["build/autoconf.h"]["hash"] == "NEW"

    def test_a_new_entry_does_not_downgrade_generated(self):
        """The second merge site, which bypasses update_entry completely.

        _update_manifest_after_reindex has an else branch for a TU the
        manifest has never seen.  It called .update() directly, so the fix in
        update_entry alone would not reach it.
        """
        from fw_context_mcp.indexer.manifest import merge_header_records

        manifest = {"headers": {"build/autoconf.h": {"hash": "OLD", "generated": True}}}
        merge_header_records(
            manifest, {"build/autoconf.h": {"hash": "NEW", "generated": False}}
        )

        assert manifest["headers"]["build/autoconf.h"]["generated"] is True
        assert manifest["headers"]["build/autoconf.h"]["hash"] == "NEW"

    def test_a_promotion_to_generated_goes_through(self):
        """False to True is the direction that must still work."""
        from fw_context_mcp.indexer.manifest import merge_header_records

        manifest = {"headers": {"build/autoconf.h": {"hash": "OLD", "generated": False}}}
        merge_header_records(
            manifest, {"build/autoconf.h": {"hash": "NEW", "generated": True}}
        )

        assert manifest["headers"]["build/autoconf.h"]["generated"] is True

    def test_a_new_header_is_added(self):
        from fw_context_mcp.indexer.manifest import merge_header_records

        manifest: dict = {}
        merge_header_records(
            manifest, {"src/config.h": {"hash": "H", "generated": False}}
        )

        assert manifest["headers"] == {"src/config.h": {"hash": "H", "generated": False}}

    def test_the_caller_record_is_not_mutated(self):
        """The merge must not write True back into the caller's own dict."""
        from fw_context_mcp.indexer.manifest import merge_header_records

        manifest = {"headers": {"build/autoconf.h": {"hash": "OLD", "generated": True}}}
        records = {"build/autoconf.h": {"hash": "NEW", "generated": False}}
        merge_header_records(manifest, records)

        assert records["build/autoconf.h"]["generated"] is False


class TestNoHeaderIsTrusted:
    """No staleness check skips a header because the manifest calls it generated.

    A trust rule did that until 2026-10-07: it said that a generated header
    changes with each build.  Measured, it does not, and a generated header
    carries the configuration of the build (autoconf.h, sdkconfig.h,
    mbed_config.h).  The rule had copies in five functions once, and four
    rounds of review missed the fifth, thus the guard greps and does not
    list functions.
    """

    def test_only_the_merge_of_the_header_table_reads_generated(self):
        """Grep src/: the flag is kept for the query layer, and no check reads it."""
        import re

        src = Path(__file__).resolve().parent.parent / "src" / "fw_context_mcp"
        offenders = []
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                # The rule read as: skip when the record says generated.
                if re.search(r'\.get\("generated"\)', line):
                    offenders.append(f"{path.relative_to(src)}:{lineno}: {line.strip()}")

        allowed = {
            # merge_header_records keeps the flag; it decides nothing.
            "indexer/manifest.py",
        }
        unexpected = [
            o for o in offenders
            if not any(o.startswith(a + ":") for a in allowed)
        ]
        assert unexpected == [], (
            "a check reads `generated` again, and no header is trusted:\n"
            + "\n".join(unexpected)
        )

    def test_the_vendor_patterns_parameter_is_gone(self):
        """The staleness signatures must not take a vendor set again.

        A signature that still accepts it states a rule the body no longer
        applies, and the next caller passes one and believes it matters.
        """
        import inspect

        from fw_context_mcp.indexer._manifest_updater import _mtime_bump_is_safe
        from fw_context_mcp.indexer.manifest import (
            check_tu_staleness,
            collect_stale_headers,
        )

        for fn in (
            check_tu_staleness,
            collect_stale_headers,
            _mtime_bump_is_safe,
        ):
            assert "vendor_patterns" not in inspect.signature(fn).parameters, fn.__name__


class TestNeedsReparse:
    """The shared header table erases the staleness of a unit nobody re-parsed.

    Repro: shared.h changes, the runner queues a.c and b.c.  a.c re-parses,
    b.c ends as "skipped" and is therefore NOT in reparsed_tus.  a.c writes
    the fresh hash of shared.h into the shared map.  Next run:
    collect_stale_headers finds nothing and Tier 1 waves b.c through, so
    b.c holds rows built from the OLD header text until --force.

    In format /1 every entry carried its own copy of the hashes.  The
    manifest dedup lost that property in silence.
    """

    @staticmethod
    def _unit(tmp_path: Path, rel: str):
        from unittest.mock import MagicMock

        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        unit = MagicMock()
        unit.file = Path(src)
        unit.directory = tmp_path
        unit.clang_args = ["-std=c11"]
        unit.raw_entry = {"file": rel, "directory": str(tmp_path)}
        return unit

    def _run(self, tmp_path: Path, *, reparsed, generated=False):
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        (tmp_path / "index").mkdir(exist_ok=True)
        units = [self._unit(tmp_path, "src/a.c"), self._unit(tmp_path, "src/b.c")]
        manifest = {
            "_format": MANIFEST_FORMAT,
            "project_root": str(tmp_path),
            "arg_sets": [["-std=c11"]],
            "headers": {"src/shared.h": {"hash": "OLD", "generated": generated}},
            "entries": [
                {"file": "src/a.c", "directory": str(tmp_path), "arg_set": 0,
                 "source_hash": "sa", "flags_hash": "f", "headers": ["src/shared.h"]},
                {"file": "src/b.c", "directory": str(tmp_path), "arg_set": 0,
                 "source_hash": "sb", "flags_hash": "f", "headers": ["src/shared.h"]},
            ],
        }
        return _update_manifest_after_index(
            manifest=manifest,
            units=units,
            project_root=tmp_path,
            db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json",
            updated_count=1,
            tu_headers={
                "src/a.c": [{"path": "src/shared.h", "hash": "FRESH", "generated": generated}],
                "src/b.c": [{"path": "src/shared.h", "hash": "FRESH", "generated": generated}],
            },
            config_hash="deadbeef",
            reparsed_tus=_parsed(tmp_path, reparsed),
            transient_defines=DEFAULT_TRANSIENT_DEFINES,
        )

    @staticmethod
    def _entry(result: dict, name: str) -> dict:
        return next(e for e in result["entries"] if e["file"] == name)

    def test_a_skipped_tu_stays_queued(self, tmp_path: Path):
        result = self._run(tmp_path, reparsed={"src/a.c"})

        assert result is not None
        assert self._entry(result, "src/b.c").get("needs_reparse") is True

    def test_a_reparsed_tu_clears_the_flag(self, tmp_path: Path):
        """A re-parsed TU gets a fresh dict with no key, so the flag is gone."""
        result = self._run(tmp_path, reparsed={"src/a.c"})

        assert result is not None
        assert "needs_reparse" not in self._entry(result, "src/a.c")

    def test_a_changed_generated_header_queues_the_other_unit(self, tmp_path: Path):
        """A generated header is a header like any other: no header is trusted."""
        result = self._run(tmp_path, reparsed={"src/a.c"}, generated=True)

        assert result is not None
        assert self._entry(result, "src/b.c").get("needs_reparse") is True

    def test_an_unchanged_header_does_not_flag_anything(self, tmp_path: Path):
        """No difference in the shared table means nothing to mark."""
        from fw_context_mcp.indexer._manifest_updater import _headers_moved_on

        entry = {"headers": ["src/shared.h"]}
        table = {"src/shared.h": {"hash": "SAME", "generated": False}}

        assert _headers_moved_on(entry, table, table) is False

    def test_a_second_failure_keeps_the_flag(self, tmp_path: Path):
        """The mark is sticky: it must hold until a real reparse clears it.

        carry_over() copies the whole entry, so the key survives every
        further carry-over.  That is the intent — a TU that keeps failing
        must keep being queued.
        """
        from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index

        (tmp_path / "index").mkdir(exist_ok=True)
        units = [self._unit(tmp_path, "src/b.c")]
        manifest = {
            "_format": MANIFEST_FORMAT,
            "project_root": str(tmp_path),
            "arg_sets": [["-std=c11"]],
            "headers": {"src/shared.h": {"hash": "FRESH", "generated": False}},
            "entries": [
                {"file": "src/b.c", "directory": str(tmp_path), "arg_set": 0,
                 "source_hash": "sb", "flags_hash": "f",
                 "headers": ["src/shared.h"], "needs_reparse": True},
            ],
        }
        result = _update_manifest_after_index(
            manifest=manifest,
            units=units,
            project_root=tmp_path,
            db_dir=tmp_path / "index",
            compile_commands=tmp_path / "compile_commands.json",
            updated_count=1,
            tu_headers={},
            config_hash="deadbeef",
            reparsed_tus={},
            transient_defines=DEFAULT_TRANSIENT_DEFINES,
        )

        assert result is not None
        assert self._entry(result, "src/b.c").get("needs_reparse") is True

    def test_the_staleness_check_reads_the_flag(self, tmp_path: Path):
        """A flagged entry is stale whatever its hashes say."""
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "b.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        entry = {
            "file": "src/b.c",
            "source_hash": hashlib.sha256(src.read_bytes()).hexdigest(),
            "headers": [],
            "needs_reparse": True,
        }

        stale, _ = check_tu_staleness(entry, tmp_path)
        assert stale is True

    def test_an_unflagged_entry_with_the_same_hashes_is_not_stale(self, tmp_path: Path):
        """The negative control for the test above."""
        import hashlib

        from fw_context_mcp.indexer.manifest import check_tu_staleness

        src = tmp_path / "src" / "b.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        entry = {
            "file": "src/b.c",
            "source_hash": hashlib.sha256(src.read_bytes()).hexdigest(),
            "headers": [],
        }

        stale, _ = check_tu_staleness(entry, tmp_path)
        assert stale is False


class TestMarkEntriesBehind:
    """``reindex_file`` refreshes the shared hashes and must flag the rest."""

    @staticmethod
    def _manifest() -> dict:
        return {
            "headers": {
                "src/shared.h": {"hash": "FRESH", "generated": False},
                "build/autoconf.h": {"hash": "FRESH", "generated": True},
            },
            "entries": [
                {"file": "src/a.c", "headers": ["src/shared.h"]},
                {"file": "src/b.c", "headers": ["src/shared.h"]},
                {"file": "src/c.c", "headers": ["build/autoconf.h"]},
            ],
        }

    def test_reindex_file_flags_other_dependents(self):
        from fw_context_mcp.indexer.manifest import mark_entries_behind

        manifest = self._manifest()
        flagged = mark_entries_behind(manifest, {"src/shared.h"}, {0})

        assert flagged == 1
        by_file = {e["file"]: e for e in manifest["entries"]}
        assert "needs_reparse" not in by_file["src/a.c"]
        assert by_file["src/b.c"]["needs_reparse"] is True
        assert "needs_reparse" not in by_file["src/c.c"]

    def test_nothing_changed_flags_nobody(self):
        from fw_context_mcp.indexer.manifest import mark_entries_behind

        manifest = self._manifest()
        assert mark_entries_behind(manifest, set(), {0}) == 0
        assert all("needs_reparse" not in e for e in manifest["entries"])

    def test_an_already_flagged_entry_is_not_counted_twice(self):
        from fw_context_mcp.indexer.manifest import mark_entries_behind

        manifest = self._manifest()
        manifest["entries"][1]["needs_reparse"] = True

        assert mark_entries_behind(manifest, {"src/shared.h"}, {0}) == 0

    def test_a_listing_that_was_not_parsed_is_flagged(self):
        """A file listed twice: the run parsed one listing, and the other is behind.

        A set of files kept both entries, and the rows of the other listing
        stayed from the old header text with no mark that a run could see.
        """
        from fw_context_mcp.indexer.manifest import mark_entries_behind

        manifest = self._manifest()
        manifest["entries"].insert(1, {"file": "src/a.c", "headers": ["src/shared.h"]})

        assert mark_entries_behind(manifest, {"src/shared.h"}, {0}) == 2
        assert "needs_reparse" not in manifest["entries"][0]
        assert manifest["entries"][1]["needs_reparse"] is True


class TestTheFlagReachesTheReport:
    """``_check_header_staleness`` must count a flagged entry.

    The window is the trap here.  _check_header_staleness scans
    manifest["entries"][:max_files] with max_files=200, so an entry past
    index 200 is invisible to the report — the test would then measure the
    window and not the flag.  The entry below sits inside it, and
    test_an_entry_past_the_window_is_not_seen records the boundary.
    """

    @staticmethod
    def _write(tmp_path: Path, *, flagged_at: int, total: int) -> tuple[Path, str]:
        import hashlib
        import json

        db_dir = tmp_path / "index"
        db_dir.mkdir(exist_ok=True)
        src_dir = tmp_path / "src"
        src_dir.mkdir(exist_ok=True)

        entries = []
        for i in range(total):
            src = src_dir / f"u{i}.c"
            src.write_text("int main() { return 0; }")
            entry = {
                "file": f"src/u{i}.c",
                "directory": str(tmp_path),
                "arg_set": 0,
                "source_hash": hashlib.sha256(src.read_bytes()).hexdigest(),
                "flags_hash": "f",
                "headers": [],
            }
            if i == flagged_at:
                entry["needs_reparse"] = True
            entries.append(entry)

        config_hash = "cafe1234"
        manifest = {
            "_format": MANIFEST_FORMAT,
            "compile_commands_path": str(tmp_path / "compile_commands.json"),
            "project_root": str(tmp_path),
            "arg_sets": [["-std=c11"]],
            "headers": {},
            "entries": entries,
        }
        (db_dir / f"manifest.{config_hash}.json").write_text(json.dumps(manifest))
        return db_dir, config_hash

    @staticmethod
    def _count(tmp_path: Path, db_dir: Path, config_hash: str) -> int:
        import sqlite3

        from fw_context_mcp.mcp.shared.stale import _check_header_staleness

        conn = sqlite3.connect(db_dir / "index.db")
        conn.row_factory = sqlite3.Row
        try:
            count, _ = _check_header_staleness(
                conn, config_hash, tmp_path, use_cache=False
            )
        finally:
            conn.close()
        return count

    def test_the_stale_report_counts_a_flagged_tu(self, tmp_path: Path):
        db_dir, config_hash = self._write(tmp_path, flagged_at=3, total=10)

        assert self._count(tmp_path, db_dir, config_hash) == 1

    def test_an_unflagged_manifest_reports_nothing(self, tmp_path: Path):
        db_dir, config_hash = self._write(tmp_path, flagged_at=-1, total=10)

        assert self._count(tmp_path, db_dir, config_hash) == 0

    def test_an_entry_past_the_window_is_not_seen(self, tmp_path: Path):
        """A recorded limit, not a defect to fix here.

        max_files exists for the latency of get_active_build.  On the Mbed project (876
        TUs) a flagged entry past index 200 is invisible to this report,
        while the index run itself still queues it.
        """
        db_dir, config_hash = self._write(tmp_path, flagged_at=250, total=260)

        assert self._count(tmp_path, db_dir, config_hash) == 0

    def test_a_file_with_two_stale_listings_counts_once(self, tmp_path: Path):
        """The report is about files; a file listed twice has two entries."""
        import json
        import sqlite3

        from fw_context_mcp.mcp.shared.stale import _check_header_staleness

        db_dir, config_hash = self._write(tmp_path, flagged_at=3, total=10)
        path = db_dir / f"manifest.{config_hash}.json"
        manifest = json.loads(path.read_text())
        manifest["entries"].insert(4, dict(manifest["entries"][3]))
        path.write_text(json.dumps(manifest))

        conn = sqlite3.connect(db_dir / "index.db")
        conn.row_factory = sqlite3.Row
        try:
            count, affected = _check_header_staleness(conn, config_hash, tmp_path, use_cache=False)
        finally:
            conn.close()
        assert (count, affected) == (1, ["src/u3.c"])


class TestTusToRequeue:
    """Tier 1 must not wave a flagged TU through.

    Tier 1 compares the source mtime.  A header change moves no source mtime,
    and neither does another unit's re-parse, so the only way a flagged TU
    reaches the parser is through this set.
    """

    @staticmethod
    def _manifest(tmp_path: Path, *, flagged: bool) -> dict:
        for rel in ("src/a.c", "src/b.c"):
            src = tmp_path / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("int main() { return 0; }")
        entries = [
            {"file": "src/a.c", "headers": ["src/shared.h"]},
            {"file": "src/b.c", "headers": ["src/shared.h"]},
        ]
        if flagged:
            entries[1]["needs_reparse"] = True
        return {"project_root": str(tmp_path), "entries": entries}

    def test_tier1_does_not_wave_a_flagged_tu_through(self, tmp_path: Path):
        """The mark must reach the set even when NO header reads as changed.

        This is the probe trap: built inside an ``if stale_headers:`` the set
        stays empty here and the mark reaches nothing.
        """
        from fw_context_mcp.indexer.runner import _tus_to_requeue

        result = _tus_to_requeue(
            self._manifest(tmp_path, flagged=True), set(), tmp_path
        )

        assert any(r.endswith("src/b.c") for r in result), result
        assert not any(r.endswith("src/a.c") for r in result), result

    def test_nothing_stale_and_nothing_flagged_queues_nobody(self, tmp_path: Path):
        from fw_context_mcp.indexer.runner import _tus_to_requeue

        assert _tus_to_requeue(
            self._manifest(tmp_path, flagged=False), set(), tmp_path
        ) == frozenset()

    def test_a_changed_header_still_queues_every_dependent(self, tmp_path: Path):
        """The original source must keep working next to the new one."""
        from fw_context_mcp.indexer.runner import _tus_to_requeue

        result = _tus_to_requeue(
            self._manifest(tmp_path, flagged=False), {"src/shared.h"}, tmp_path
        )

        assert len(result) == 2


class TestFilesGeneratedColumn:
    """``files.generated`` must agree with ``headers[path].generated``.

    No caller of upsert_file() passed the flag, so every row held 0 from the
    default while the manifest of the same build said True for the same
    paths.  Measured on the Zephyr project 52840/app: 28 files under
    build/.../generated/ with files.generated = 0.

    A disagreement between the two means somebody wrote the manifest with
    different build_dir_patterns from the ones the index used — which is the
    defect D8 fixes, seen from the other side.
    """

    def test_generated_matches_the_manifest(self):
        """Both columns read the same predicate with the same patterns."""
        from fw_context_mcp.indexer.manifest import _is_generated_header

        patterns = ["build/"]
        generated = "build/zephyr/include/generated/autoconf.h"
        owned = "src/main.c"

        assert _is_generated_header(generated, patterns) is True
        assert _is_generated_header(owned, patterns) is False

    def test_no_patterns_means_nothing_is_generated(self):
        """The negative control, and the reason D8 mattered.

        With None every header reads as not generated.  That is correct for
        a build with no output directory, and it is exactly what
        reindex_file used to pass for every build.
        """
        from fw_context_mcp.indexer.manifest import _is_generated_header

        assert _is_generated_header("build/gen/autoconf.h", None) is False

    def test_the_generated_flag_does_not_change_is_project(self, tmp_path: Path):
        """Generated code is PROJECT code (decision 1).

        is_project comes from project_patterns and vendor_patterns alone.
        build_dir_patterns must stay out of it, or filling this column would
        move 28 files of the Zephyr project out of every project_only query.
        """
        import inspect

        from fw_context_mcp.indexer import ops

        source = inspect.getsource(ops._store_symbol_rows)
        # The is_project block runs between these two markers.
        start = source.index("# ── Compute is_project ──")
        end = source.index("# ── Extract function body for definitions ──")
        is_project_block = source[start:end]

        assert "build_dir_patterns" not in is_project_block, (
            "build_dir_patterns must not take part in is_project"
        )
        assert "project_patterns" in is_project_block
        assert "vendor_patterns" in is_project_block


def test_an_incremental_update_keeps_the_extension_sets(tmp_path: Path):
    """The update wrote the manifest without them, and load_tu_extensions gave None."""
    import json

    from fw_context_mcp.indexer._manifest_updater import _update_manifest_after_index
    from fw_context_mcp.indexer.manifest import load_tu_extensions

    listing = TestOneEntryForEachListing()
    main = listing._unit(tmp_path, "src/main.c", "-DA")
    cc = tmp_path / "compile_commands.json"
    cc.write_text(json.dumps([
        {"file": "src/main.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "src/main.c"]},
        {"file": "src/start.S", "directory": str(tmp_path), "arguments": ["gcc", "-c", "src/start.S"]},
    ]), encoding="utf-8")
    (tmp_path / "index").mkdir()
    _update_manifest_after_index(
        manifest=listing._manifest(tmp_path, [main]), units=[main], project_root=tmp_path,
        db_dir=tmp_path / "index", compile_commands=cc, updated_count=1,
        tu_headers={"src/main.c": []}, config_hash="deadbeef",
        reparsed_tus=_parsed(tmp_path, {"src/main.c"}), transient_defines=DEFAULT_TRANSIENT_DEFINES,
    )

    assert load_tu_extensions(tmp_path / "index", "deadbeef") == frozenset({".c", ".S"})
