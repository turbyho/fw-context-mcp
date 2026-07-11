"""Tests for build system protocol and registry."""

from pathlib import Path

from fw_context_mcp.indexer.builders import BuildSystemRegistry
from fw_context_mcp.indexer.builders.protocol import BuildIssue


class TestBuildIssue:
    def test_defaults(self):
        issue = BuildIssue(severity="error", category="test", message="Something wrong")
        assert issue.severity == "error"
        assert issue.category == "test"
        assert issue.message == "Something wrong"
        assert issue.auto_fixable is False
        assert issue.fix_hint is None

    def test_auto_fixable(self):
        issue = BuildIssue(
            severity="warning",
            category="dep_files_missing",
            message="No .d files",
            auto_fixable=True,
            fix_hint="Run --build",
        )
        assert issue.auto_fixable is True
        assert issue.fix_hint == "Run --build"


class TestRegistry:
    def test_empty_registry_returns_none(self, tmp_path):
        reg = BuildSystemRegistry()
        assert reg.detect(tmp_path) is None
        assert reg.get("mbed-os") is None
        assert reg.keys() == []

    def test_registry_with_builders_detects(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders import registry

        # No markers → None
        assert registry.detect(tmp_path) is None

        # PlatformIO marker
        (tmp_path / "platformio.ini").write_text("[env:uno]\n")
        assert registry.detect(tmp_path) == "platformio"

    def test_multiple_markers_scores_highest(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders import registry

        # mbed-os has 3 markers → scores highest even with zephyr present
        (tmp_path / ".mbed").write_text("TARGET=foo\n")
        (tmp_path / "mbed-os").mkdir()
        (tmp_path / "mbed_app.json").write_text("{}")
        (tmp_path / "west.yml").write_text("manifest:\n")
        assert registry.detect(tmp_path) == "mbed-os"
