"""Edge case tests for shared utilities — resolve_project_root, abs_path, and new utils."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fw_context_mcp.utils import MTIME_TOLERANCE_S, abs_path, resolve_project_root

class TestResolveProjectRoot:
    def test_explicit_path_returns_resolved(self, tmp_path: Path):
        result = resolve_project_root(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_explicit_relative_path_resolved(self, tmp_path: Path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        result = resolve_project_root(str(sub / ".."))
        assert result == tmp_path.resolve()

    def test_none_uses_cwd_or_git_root(self):
        result = resolve_project_root(None)
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_explicit_nonexistent_path(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist"
        result = resolve_project_root(str(nonexistent))
        assert result == nonexistent.resolve()

    def test_explicit_empty_string(self):
        # empty string resolves to cwd (Path("").resolve() == cwd)
        result = resolve_project_root("")
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_finds_git_root_when_no_explicit(self, tmp_path: Path, monkeypatch):
        git_dir = tmp_path / "repo"
        git_dir.mkdir()
        (git_dir / ".git").mkdir()
        sub = git_dir / "src" / "module"
        sub.mkdir(parents=True)
        monkeypatch.setattr(os, "getcwd", lambda: str(sub))
        result = resolve_project_root(None)
        assert result == git_dir

    def test_cwd_when_no_git(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
        result = resolve_project_root(None)
        assert result == tmp_path

    def test_git_root_at_filesystem_root(self, tmp_path: Path, monkeypatch):
        # .git in tmp_path itself
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "deep" / "dir"
        sub.mkdir(parents=True)
        monkeypatch.setattr(os, "getcwd", lambda: str(sub))
        result = resolve_project_root(None)
        assert result == tmp_path


class TestAbsPath:
    def test_relative_path_joined(self, tmp_path: Path):
        root = tmp_path / "project"
        result = abs_path(root, "src/main.c")
        assert result == str(root / "src/main.c")

    def test_absolute_path_returned_unchanged(self, tmp_path: Path):
        root = tmp_path / "project"
        result = abs_path(root, "/absolute/path/file.c")
        assert result == "/absolute/path/file.c"

    def test_empty_string_returned_as_is(self, tmp_path: Path):
        result = abs_path(tmp_path, "")
        assert result == ""

    def test_root_is_file(self, tmp_path: Path):
        """Root being a file path should still join correctly."""
        root = tmp_path / "somefile.txt"
        result = abs_path(root, "relative.c")
        assert result == str(root / "relative.c")

    def test_dot_dot_in_relative_path(self, tmp_path: Path):
        root = tmp_path / "project"
        result = abs_path(root, "../outside.c")
        # abs_path uses Path / operator which preserves ".." literally
        assert result == str(root / "../outside.c")

    def test_dot_slash_relative(self, tmp_path: Path):
        root = tmp_path / "project"
        result = abs_path(root, "./src/file.c")
        assert result == str(root / "./src/file.c")

    def test_unicode_path(self, tmp_path: Path):
        root = tmp_path / "projekt"
        result = abs_path(root, "src/žluťoučký_kůň.c")
        assert result == str(root / "src/žluťoučký_kůň.c")

    def test_whitespace_only_path(self, tmp_path: Path):
        root = tmp_path / "project"
        result = abs_path(root, "   ")
        p = Path("   ")
        if p.is_absolute():
            assert result == str(p)
        else:
            assert result == str(root / "   ")

    def test_path_with_special_chars(self, tmp_path: Path):
        root = tmp_path / "project"
        # Path with spaces and special chars
        result = abs_path(root, "src/my file (copy).c")
        expected = str(root / "src/my file (copy).c")
        assert result == expected

    def test_root_with_trailing_slash(self, tmp_path: Path):
        root = tmp_path / "project"
        # Path("/foo/bar/") == Path("/foo/bar") so trailing slash doesn't matter
        result = abs_path(Path(str(root) + "/"), "src/file.c")
        assert result == str(root / "src/file.c")


class TestMtimeTolerance:
    def test_tolerance_is_positive(self):
        assert MTIME_TOLERANCE_S > 0

    def test_tolerance_is_reasonable(self):
        # Tolerance should be between 0.1 and 10 seconds
        assert 0.1 <= MTIME_TOLERANCE_S <= 10.0


class TestIsCompileCommandsStale:
    """Tests for is_compile_commands_stale — moved from cli.py."""

    def test_cc_newer_than_index(self, tmp_path: Path) -> None:
        from fw_context_mcp.utils import is_compile_commands_stale

        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        past = datetime.now(UTC).timestamp() - 3600
        created_at = datetime.fromtimestamp(past, tz=UTC).isoformat()
        stale, reason = is_compile_commands_stale(created_at, str(cc))
        assert stale is True
        assert reason == "compile_commands_newer"

    def test_index_newer_than_cc(self, tmp_path: Path) -> None:
        from fw_context_mcp.utils import is_compile_commands_stale

        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        future = datetime.now(UTC).timestamp() + 3600
        created_at = datetime.fromtimestamp(future, tz=UTC).isoformat()
        stale, reason = is_compile_commands_stale(created_at, str(cc))
        assert stale is False
        assert reason is None

    def test_missing_file_returns_stale(self, tmp_path: Path) -> None:
        from fw_context_mcp.utils import is_compile_commands_stale

        created_at = datetime.now(UTC).isoformat()
        stale, reason = is_compile_commands_stale(created_at, str(tmp_path / "nope.json"))
        assert stale is True
        assert reason == "compile_commands_missing"

    def test_bad_timestamp_returns_error(self) -> None:
        from fw_context_mcp.utils import is_compile_commands_stale

        stale, reason = is_compile_commands_stale("not-a-date", "/some/file")
        assert stale is True
        assert reason is not None

    def test_empty_timestamp_returns_error(self) -> None:
        from fw_context_mcp.utils import is_compile_commands_stale

        stale, reason = is_compile_commands_stale("", "/some/file")
        assert stale is True
        assert reason is not None


class TestTruncatePathMiddle:
    """Tests for truncate_path_middle."""

    def test_short_path_unchanged(self) -> None:
        from fw_context_mcp.utils import truncate_path_middle

        assert truncate_path_middle("/short/path", 50) == "/short/path"

    def test_long_path_truncated(self) -> None:
        from fw_context_mcp.utils import truncate_path_middle

        path = "/home/user/very/long/path/to/some/project/src/main.cpp"
        result = truncate_path_middle(path, 40)
        assert len(result) <= 40
        assert "…" in result
        assert result.startswith("/home/user/")

    def test_exact_length_not_truncated(self) -> None:
        from fw_context_mcp.utils import truncate_path_middle

        path = "/exactly/twenty/chars"
        result = truncate_path_middle(path, len(path))
        assert result == path


class TestFmtCount:
    """Tests for fmt_count."""

    def test_zero(self) -> None:
        from fw_context_mcp.utils import fmt_count

        assert fmt_count(0) == "0"

    def test_hundreds(self) -> None:
        from fw_context_mcp.utils import fmt_count

        assert fmt_count(123) == "123"

    def test_thousands(self) -> None:
        from fw_context_mcp.utils import fmt_count

        assert fmt_count(1234) == "1 234"

    def test_millions(self) -> None:
        from fw_context_mcp.utils import fmt_count

        assert fmt_count(1_234_567) == "1 234 567"

    def test_negative_thousands(self) -> None:
        from fw_context_mcp.utils import fmt_count

        assert fmt_count(-1234) == "-1 234"
