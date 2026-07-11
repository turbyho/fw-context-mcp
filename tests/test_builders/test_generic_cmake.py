"""Tests for Generic CMake builder."""

import json

from fw_context_mcp.indexer.builders.generic_cmake import GenericCMakeBuildSystem


class TestGenericCMakeDetection:
    def test_detected_with_cmakelists(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\nproject(test)\n")
        assert GenericCMakeBuildSystem.detect(tmp_path) is True

    def test_not_detected_without_markers(self, tmp_path):
        assert GenericCMakeBuildSystem.detect(tmp_path) is False

    def test_not_detected_when_esp_idf_present(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text("idf_build()\n")
        (tmp_path / "sdkconfig").write_text("CONFIG_IDF_TARGET=esp32\n")
        # ESP-IDF takes priority — generic CMake should NOT claim this project
        assert GenericCMakeBuildSystem.detect(tmp_path) is False


class TestGenericCMakeValidation:
    def test_no_issues(self, tmp_path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "main.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "main.c"]}]))
        builder = GenericCMakeBuildSystem()
        issues = builder.validate_artifacts(cc, tmp_path)
        assert len(issues) == 0


class TestGenericCMakeTools:
    def test_required_tools(self):
        builder = GenericCMakeBuildSystem()
        assert "cmake" in builder.required_tools()
