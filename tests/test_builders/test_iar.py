"""Tests for IARBuildSystem — convert .ewp via keil2clangd."""

import pytest

from fw_context_mcp.indexer.builders.iar import IARBuildSystem


class TestIARBuildSystem:
    def test_detected_with_ewp(self, tmp_path):
        (tmp_path / "test.ewp").write_text("<project>\n")
        assert IARBuildSystem.detect(tmp_path) is True

    def test_detected_with_eww(self, tmp_path):
        (tmp_path / "test.eww").write_text("<workspace>\n")
        assert IARBuildSystem.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert IARBuildSystem.detect(tmp_path) is False

    def test_config_key(self):
        assert IARBuildSystem.config_key == "iar-ewarm"

    def test_required_tools(self):
        assert "keil2clangd" in IARBuildSystem().required_tools()

    def test_convert_without_keil2clangd_raises(self, tmp_path):
        """When keil2clangd is not installed, convert gives a helpful error."""
        (tmp_path / "test.ewp").write_text("<project>\n")
        from fw_context_mcp.indexer.build import BuildConfig

        cfg = BuildConfig(system="iar-ewarm", iar_project="test.ewp")
        builder = IARBuildSystem()

        with pytest.raises(RuntimeError, match="keil2clangd"):
            builder.convert(tmp_path, cfg)

    def test_convert_no_ewp_found_raises(self, tmp_path, monkeypatch):
        """When no .ewp exists and none configured, raise helpful error."""
        import shutil

        from fw_context_mcp.indexer.build import BuildConfig
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/keil2clangd")

        cfg = BuildConfig(system="iar-ewarm")
        builder = IARBuildSystem()
        with pytest.raises(RuntimeError, match="No .ewp"):
            builder.convert(tmp_path, cfg)

    def test_convert_missing_project_file_raises(self, tmp_path, monkeypatch):
        """When specified .ewp doesn't exist, raise error."""
        import shutil

        from fw_context_mcp.indexer.build import BuildConfig
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/keil2clangd")

        cfg = BuildConfig(system="iar-ewarm", iar_project="Nonexistent.ewp")
        builder = IARBuildSystem()
        with pytest.raises(RuntimeError, match="not found"):
            builder.convert(tmp_path, cfg)
