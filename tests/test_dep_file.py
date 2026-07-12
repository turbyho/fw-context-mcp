"""Unit tests for .d dependency file parsing, discovery, and hashing.

Tests the four private functions in ``fw_context_mcp.indexer.runner``:

- ``_parse_dep_file`` — parse GCC ``.d`` file content
- ``_find_dep_path`` — locate ``.d`` file for a translation unit
- ``resolve_dep_path_for_entry`` — locate ``.d`` file from raw cc.json entry
- ``_compute_deps_hash`` — composite SHA-256 of header content

No external dependencies — pure Python, fast.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fw_context_mcp.indexer.compile_commands import CompilationUnit
from fw_context_mcp.indexer.runner import (
    _compute_deps_hash,
    _find_dep_path,
    _parse_dep_file,
    resolve_dep_path_for_entry,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_unit(
    source_file: Path,
    directory: Path | None = None,
    raw_entry: dict | None = None,
) -> CompilationUnit:
    """Create a minimal CompilationUnit for testing."""
    return CompilationUnit(
        file=source_file,
        directory=directory or source_file.parent,
        language="c",
        clang_args=[],
        raw_entry=raw_entry,
    )


def _touch(path: Path) -> Path:
    """Create an empty file (and its parent directories)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


# ── _parse_dep_file ─────────────────────────────────────────────────────────


class TestParseDepFile:
    """Tests for ``_parse_dep_file``."""

    def test_simple_format(self, tmp_path):
        """``target.o: header.h`` returns the header."""
        d_file = tmp_path / "main.d"
        h = _touch(tmp_path / "header.h")
        d_file.write_text(f"main.o: {h}\n")
        result = _parse_dep_file(d_file)
        assert h.resolve() in result
        assert len(result) == 1

    def test_multiline_continuation(self, tmp_path):
        """Backslash-newline joins lines into one dependency list."""
        d_file = tmp_path / "main.d"
        h1 = _touch(tmp_path / "a.h")
        h2 = _touch(tmp_path / "b.h")
        d_file.write_text(f"main.o: {h1} \\\n  {h2}\n")
        result = _parse_dep_file(d_file)
        resolved = {p.resolve() for p in result}
        assert h1.resolve() in resolved
        assert h2.resolve() in resolved
        assert len(result) == 2

    def test_filter_header_extensions(self, tmp_path):
        """Only .h, .hpp, .hxx, .hh, .inl pass the filter."""
        d_file = tmp_path / "main.d"
        h = _touch(tmp_path / "a.h")
        hpp = _touch(tmp_path / "b.hpp")
        hxx = _touch(tmp_path / "c.hxx")
        hh = _touch(tmp_path / "d.hh")
        inl = _touch(tmp_path / "e.inl")
        inc = _touch(tmp_path / "f.inc")
        c = _touch(tmp_path / "g.c")
        d_file.write_text(f"main.o: {h} {hpp} {hxx} {hh} {inl} {inc} {c}\n")
        result = _parse_dep_file(d_file)
        resolved = {p.resolve() for p in result}
        assert h.resolve() in resolved
        assert hpp.resolve() in resolved
        assert hxx.resolve() in resolved
        assert hh.resolve() in resolved
        assert inl.resolve() in resolved
        assert inc.resolve() not in resolved  # .inc excluded
        assert c.resolve() not in resolved  # .c excluded
        assert len(result) == 5

    def test_filters_non_existent(self, tmp_path):
        """Headers that don't exist on disk are excluded."""
        d_file = tmp_path / "main.d"
        h_exists = _touch(tmp_path / "exists.h")
        h_missing = tmp_path / "missing.h"  # not created
        d_file.write_text(f"main.o: {h_exists} {h_missing}\n")
        result = _parse_dep_file(d_file)
        assert h_exists.resolve() in result
        assert h_missing.resolve() not in result  # file doesn't exist
        assert len(result) == 1

    def test_relative_paths_resolved(self, tmp_path):
        """.d file in subdirectory — relative paths resolved against .d parent."""
        sub = tmp_path / "build"
        sub.mkdir()
        d_file = sub / "main.d"
        h = _touch(tmp_path / "include" / "foo.h")
        # Relative path from build/ to include/
        d_file.write_text("main.o: ../include/foo.h\n")
        result = _parse_dep_file(d_file)
        assert h.resolve() in result
        assert len(result) == 1

    def test_missing_file_returns_empty(self, tmp_path):
        """Non-existent .d file → empty list."""
        result = _parse_dep_file(tmp_path / "nonexistent.d")
        assert result == []

    def test_no_colon(self, tmp_path):
        """Line without ':' — entire text treated as deps part."""
        d_file = tmp_path / "main.d"
        h = _touch(tmp_path / "header.h")
        d_file.write_text(f" {h} \n")
        result = _parse_dep_file(d_file)
        assert h.resolve() in result
        assert len(result) == 1

    def test_empty_deps(self, tmp_path):
        """"target.o:" with no dependencies → empty list."""
        d_file = tmp_path / "main.d"
        d_file.write_text("main.o:\n")
        result = _parse_dep_file(d_file)
        assert result == []

    def test_realistic_gcc_output(self, tmp_path):
        """Multi-line with system and project headers."""
        d_file = tmp_path / "main.d"
        h1 = _touch(tmp_path / "src" / "main.h")
        h2 = _touch(tmp_path / "lib" / "utils.h")
        # Simulate a realistic GCC dep file with continuation lines
        content = (
            f"CMakeFiles/test_app.dir/src/main.c.o: src/main.c \\\n"
            f"  /usr/include/stdio.h \\\n"
            f"  /usr/include/stdlib.h \\\n"
            f"  {h1} \\\n"
            f"  {h2}\n"
        )
        d_file.write_text(content)
        result = _parse_dep_file(d_file)
        resolved = {p.resolve() for p in result}
        # System headers may or may not exist — don't assert on them
        assert h1.resolve() in resolved
        assert h2.resolve() in resolved
        assert len(result) >= 2  # at minimum the two project headers

    def test_oserror_returns_empty(self, tmp_path):
        """Unreadable .d file → OSError → empty list."""
        d_file = tmp_path / "main.d"
        d_file.write_text("main.o: header.h\n")
        # Remove read permission
        d_file.chmod(0o000)
        try:
            result = _parse_dep_file(d_file)
            assert result == []
        finally:
            d_file.chmod(0o644)  # restore for cleanup


# ── resolve_dep_path_for_entry ────────────────────────────────────────────────


class TestResolveDepPathForEntry:
    """Tests for ``resolve_dep_path_for_entry``."""

    def test_mf_flag_in_arguments(self, tmp_path):
        """``-MF build/main.d`` in arguments → found."""
        d_file = _touch(tmp_path / "build" / "main.d")
        directory = tmp_path
        entry = {
            "arguments": ["gcc", "-c", "-MF", "build/main.d", "src/main.c"],
            "directory": str(directory),
        }
        result = resolve_dep_path_for_entry(entry, directory)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_mf_flag_absolute_path(self, tmp_path):
        """``-MF /abs/path/main.d`` — absolute path used directly."""
        d_file = _touch(tmp_path / "build" / "main.d")
        entry = {
            "arguments": ["gcc", "-c", "-MF", str(d_file), "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_mf_flag_from_command_string(self, tmp_path):
        """Entry uses ``command`` string instead of ``arguments`` array."""
        d_file = _touch(tmp_path / "build" / "main.d")
        entry = {
            "command": "gcc -c -MF build/main.d src/main.c",
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_output_field_replaced(self, tmp_path):
        """``output``: ``build/main.o`` → check ``build/main.d``."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        d_file = _touch(obj_dir / "main.d")
        entry = {
            "arguments": ["gcc", "-c", "-MMD", "src/main.c"],
            "directory": str(tmp_path),
            "output": "build/main.o",
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_output_field_appended(self, tmp_path):
        """CMake-style output ``main.c.o`` → check ``main.c.o.d``."""
        obj_dir = tmp_path / "build" / "CMakeFiles" / "app.dir" / "src"
        obj_dir.mkdir(parents=True)
        d_file = _touch(obj_dir / "main.c.o.d")
        entry = {
            "arguments": ["cc", "-c", "src/main.c"],
            "directory": str(tmp_path),
            "output": "build/CMakeFiles/app.dir/src/main.c.o",
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_no_dep_file_returns_none(self, tmp_path):
        """No ``-MF`` and no ``output`` field → None."""
        entry = {
            "arguments": ["gcc", "-c", "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is None

    def test_mf_nonexistent_falls_through_to_output(self, tmp_path):
        """``-MF`` points to missing file, ``output`` has existing ``.d`` → found via output."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        d_file = _touch(obj_dir / "main.d")
        entry = {
            "arguments": ["gcc", "-c", "-MF", "nonexistent/main.d", "src/main.c"],
            "directory": str(tmp_path),
            "output": "build/main.o",
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_mf_takes_priority(self, tmp_path):
        """Both ``-MF`` and ``output`` exist → ``-MF`` wins."""
        mf_dir = tmp_path / "mf_build"
        mf_dir.mkdir()
        mf_file = _touch(mf_dir / "main.d")
        out_dir = tmp_path / "out_build"
        out_dir.mkdir()
        _touch(out_dir / "main.d")  # also exists, but lower priority
        entry = {
            "arguments": ["gcc", "-c", "-MF", "mf_build/main.d", "src/main.c"],
            "directory": str(tmp_path),
            "output": "out_build/main.o",
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == mf_file.resolve()

    def test_relative_output_resolved_against_directory(self, tmp_path):
        """Relative output resolved via ``directory`` field."""
        obj_dir = tmp_path / "build" / "obj"
        obj_dir.mkdir(parents=True)
        d_file = _touch(obj_dir / "main.d")
        # directory differs from the path used for resolution
        entry = {
            "arguments": ["gcc", "-c", "-MMD", "src/main.c"],
            "directory": str(tmp_path),
            "output": "build/obj/main.o",
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_o_flag_in_arguments(self, tmp_path):
        """``-o build/main.o`` in arguments (no ``output`` field) → .d found."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        d_file = _touch(obj_dir / "main.d")
        entry = {
            "arguments": ["gcc", "-c", "-MMD", "-o", "build/main.o", "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_o_flag_absolute(self, tmp_path):
        """``-o /abs/path/main.o`` — absolute path, .d next to it."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        o_file = _touch(obj_dir / "main.o")
        d_file = _touch(obj_dir / "main.d")
        entry = {
            "arguments": ["gcc", "-c", "-MMD", "-o", str(o_file), "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_o_flag_cmake_convention(self, tmp_path):
        """``-o build/main.c.o`` → ``build/main.c.o.d`` (CMake convention)."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir(parents=True)
        d_file = _touch(obj_dir / "main.c.o.d")
        entry = {
            "arguments": ["cc", "-c", "-o", "build/main.c.o", "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_o_flag_no_d_file_returns_none(self, tmp_path):
        """``-o`` points to existing .o but .d missing → None."""
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        _touch(obj_dir / "main.o")  # .o exists but no .d
        entry = {
            "arguments": ["gcc", "-c", "-o", "build/main.o", "src/main.c"],
            "directory": str(tmp_path),
        }
        result = resolve_dep_path_for_entry(entry, tmp_path)
        assert result is None

    def test_empty_entry_returns_none(self, tmp_path):
        """Empty entry dict → None."""
        result = resolve_dep_path_for_entry({}, tmp_path)
        assert result is None


# ── _find_dep_path ──────────────────────────────────────────────────────────


class TestFindDepPath:
    """Tests for ``_find_dep_path``."""

    def test_strategy1_mf_in_raw_args(self, tmp_path):
        """``-MF build/main.d`` in raw arguments → found."""
        d_file = _touch(tmp_path / "build" / "main.d")
        src = _touch(tmp_path / "src" / "main.c")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["gcc", "-c", "-MF", "build/main.d", str(src)],
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy1_mf_relative_path_resolved(self, tmp_path):
        """Relative -MF path resolved against unit.directory."""
        sub = tmp_path / "build"
        sub.mkdir()
        d_file = _touch(sub / "main.d")
        src = _touch(tmp_path / "src" / "main.c")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["gcc", "-c", "-MF", "build/main.d", str(src)],
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy2_fallback_source_sidecar(self, tmp_path):
        """.d file next to source file (no -MF flag)."""
        src = _touch(tmp_path / "main.c")
        d_file = _touch(tmp_path / "main.d")  # same name, .d extension
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={"arguments": ["gcc", "-c", str(src)]},  # No -MF
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_fallback_output_field(self, tmp_path):
        """.d from output field (.o → .d) — PlatformIO / out-of-tree builds."""
        src = _touch(tmp_path / "src" / "main.c")
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        d_file = _touch(obj_dir / "main.d")
        # No sidecar .d next to source, no -MF, but output points to .o
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["gcc", "-c", "-MMD", str(src)],
                "output": "build/main.o",
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_fallback_output_field_cmake(self, tmp_path):
        """.d from output field with CMake convention (main.c.o → main.c.o.d)."""
        src = _touch(tmp_path / "src" / "main.c")
        obj_dir = tmp_path / "build" / "CMakeFiles" / "test_app.dir" / "src"
        obj_dir.mkdir(parents=True)
        # CMake names .d files as <source>.o.d, not <source>.d
        d_file = _touch(obj_dir / "main.c.o.d")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["cc", "-c", str(src)],
                "output": "build/CMakeFiles/test_app.dir/src/main.c.o",
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_no_dep_file_returns_none(self, tmp_path):
        """No .d file found by any strategy → None."""
        src = _touch(tmp_path / "main.c")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={"arguments": ["gcc", "-c", str(src)]},
        )
        result = _find_dep_path(unit)
        assert result is None

    def test_mf_flag_nonexistent_file_falls_through(self, tmp_path):
        """-MF points to nonexistent file → falls through to other strategies."""
        src = _touch(tmp_path / "main.c")
        # Create .d next to source (strategy 2) so it falls through
        d_file = _touch(tmp_path / "main.d")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["gcc", "-c", "-MF", "nonexistent/main.d", str(src)],
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_raw_entry_none(self, tmp_path):
        """unit.raw_entry is None — skip strategy 1, try strategy 2."""
        src = _touch(tmp_path / "main.c")
        d_file = _touch(tmp_path / "main.d")
        unit = _make_unit(source_file=src, directory=tmp_path, raw_entry=None)
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()

    def test_strategy3_relative_output_resolved(self, tmp_path):
        """Output is relative, resolved via unit.directory."""
        src = _touch(tmp_path / "src" / "main.c")
        obj_dir = tmp_path / "build" / "obj"
        obj_dir.mkdir(parents=True)
        d_file = _touch(obj_dir / "main.d")
        unit = _make_unit(
            source_file=src,
            directory=tmp_path,
            raw_entry={
                "arguments": ["gcc", "-c", "-MMD", str(src)],
                "output": "build/obj/main.o",
            },
        )
        result = _find_dep_path(unit)
        assert result is not None
        assert result.resolve() == d_file.resolve()


# ── _compute_deps_hash ──────────────────────────────────────────────────────


class TestComputeDepsHash:
    """Tests for ``_compute_deps_hash``."""

    def test_deterministic(self, tmp_path):
        """Same headers → same hash regardless of input order."""
        h1 = _touch(tmp_path / "a.h")
        h2 = _touch(tmp_path / "b.h")
        h1.write_text("content1")
        h2.write_text("content2")
        cache: dict[str, str] = {}
        hash1 = _compute_deps_hash([h1, h2], cache)
        hash2 = _compute_deps_hash([h2, h1], {})  # reversed order, fresh cache
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex

    def test_different_headers_different_hash(self, tmp_path):
        """Different header content → different composite hash."""
        h1 = _touch(tmp_path / "a.h")
        h2 = _touch(tmp_path / "b.h")
        h1.write_text("content_a")
        h2.write_text("content_b")
        cache: dict[str, str] = {}
        hash1 = _compute_deps_hash([h1], cache)
        hash2 = _compute_deps_hash([h2], {})
        assert hash1 != hash2

    def test_cache_reuse(self, tmp_path):
        """Same header appears twice → second lookup hits cache."""
        h = _touch(tmp_path / "a.h")
        h.write_text("test_content")
        cache: dict[str, str] = {}
        _compute_deps_hash([h], cache)
        # Cache now has the header hash. Modify the file and verify
        # the cached value is the old hash, not the new content.
        old_hash = cache[str(h)]
        h.write_text("modified_content")
        # A second call with the same cache should use cached old_hash
        hash2 = _compute_deps_hash([h], cache)
        expected = hashlib.sha256(old_hash.encode()).hexdigest()
        assert hash2 == expected
        # And the hash should differ from what it would be with fresh content
        hash_fresh = _compute_deps_hash([h], {})
        assert hash2 != hash_fresh

    def test_unreadable_file_sentinel(self, tmp_path):
        """Unreadable header → empty string sentinel in cache."""
        h = _touch(tmp_path / "a.h")
        h.write_text("test")
        # Make it unreadable
        h.chmod(0o000)
        cache: dict[str, str] = {}
        try:
            _compute_deps_hash([h], cache)
            # Cache should contain empty string for unreadable file
            assert cache[str(h)] == ""
        finally:
            h.chmod(0o644)  # restore for cleanup

    def test_empty_headers_returns_hash(self, tmp_path):
        """Empty header list → hash of empty string."""
        cache: dict[str, str] = {}
        result = _compute_deps_hash([], cache)
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected
