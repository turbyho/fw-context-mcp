"""Tests for KeilBuildSystem — convert .uvprojx via keil2clangd."""

import pytest

from fw_context_mcp.indexer.builders.keil import KeilBuildSystem


class TestKeilBuildSystem:
    def test_detected_with_uvprojx(self, tmp_path):
        (tmp_path / "test.uvprojx").write_text("<Project>\n")
        assert KeilBuildSystem.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert KeilBuildSystem.detect(tmp_path) is False

    def test_config_key(self):
        assert KeilBuildSystem.config_key == "keil-mdk"

    def test_required_tools(self):
        assert "keil2clangd" in KeilBuildSystem().required_tools()

    def test_convert_without_keil2clangd_raises(self, tmp_path):
        """When keil2clangd is not installed, convert gives a helpful error."""
        (tmp_path / "test.uvprojx").write_text("<Project>\n")
        from fw_context_mcp.indexer.build import BuildConfig

        cfg = BuildConfig(system="keil-mdk", keil_project="test.uvprojx")
        builder = KeilBuildSystem()

        # keil2clangd likely not installed in CI — convert should raise with help
        with pytest.raises(RuntimeError, match="keil2clangd"):
            builder.convert(tmp_path, cfg)

    def test_convert_no_uvprojx_found_raises(self, tmp_path, monkeypatch):
        """When no .uvprojx exists and none configured, raise helpful error."""
        # Mock shutil.which so we skip the "keil2clangd not found" check
        import shutil

        from fw_context_mcp.indexer.build import BuildConfig
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/keil2clangd")

        cfg = BuildConfig(system="keil-mdk")
        builder = KeilBuildSystem()
        with pytest.raises(RuntimeError, match="No .uvprojx"):
            builder.convert(tmp_path, cfg)

    def test_convert_missing_project_file_raises(self, tmp_path, monkeypatch):
        """When specified .uvprojx doesn't exist, raise error."""
        import shutil

        from fw_context_mcp.indexer.build import BuildConfig
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/keil2clangd")

        cfg = BuildConfig(system="keil-mdk", keil_project="Nonexistent.uvprojx")
        builder = KeilBuildSystem()
        with pytest.raises(RuntimeError, match="not found"):
            builder.convert(tmp_path, cfg)

    def test_detect_environment_returns_defaults(self, tmp_path):
        """detect_environment returns safe defaults when no Python env found."""
        result = KeilBuildSystem.detect_environment(tmp_path)
        assert result == {"python": None, "activate": None}

    def test_detect_environment_callable(self, tmp_path):
        """detect_environment does not raise on valid project root."""
        result = KeilBuildSystem.detect_environment(tmp_path)
        assert isinstance(result, dict)
        assert "python" in result
        assert "activate" in result
