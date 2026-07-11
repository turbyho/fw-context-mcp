"""Tests for ESP-IDF builder."""

import json

from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem


class TestESPIDFDetection:
    def test_detected_with_sdkconfig_and_idf_cmake(self, tmp_path):
        (tmp_path / "sdkconfig").write_text("CONFIG_IDF_TARGET=esp32\n")
        (tmp_path / "CMakeLists.txt").write_text("idf_build()\ncmake_minimum_required(VERSION 3.16)\n")
        assert ESPIDFBuildSystem.detect(tmp_path) is True

    def test_detected_with_sdkconfig_alone(self, tmp_path):
        (tmp_path / "sdkconfig").write_text("CONFIG_IDF_TARGET=esp32\n")
        assert ESPIDFBuildSystem.detect(tmp_path) is True

    def test_not_detected_without_markers(self, tmp_path):
        assert ESPIDFBuildSystem.detect(tmp_path) is False

    def test_not_detected_with_cmake_only(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n")
        assert ESPIDFBuildSystem.detect(tmp_path) is False


class TestESPIDFValidation:
    def test_missing_build_dir_warns(self, tmp_path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "main.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "main.c"]}]))
        builder = ESPIDFBuildSystem()
        issues = builder.validate_artifacts(cc, tmp_path)
        assert len(issues) == 1
        assert "build" in issues[0].message.lower()

    def test_build_dir_exists_no_issues(self, tmp_path):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "main.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "main.c"]}]))
        builder = ESPIDFBuildSystem()
        issues = builder.validate_artifacts(cc, tmp_path)
        assert len(issues) == 0


class TestESPIDFTools:
    def test_required_tools(self):
        builder = ESPIDFBuildSystem()
        assert "idf.py" in builder.required_tools()
