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

    def test_detected_by_project_cmake_without_sdkconfig(self, tmp_path):
        """A clone of a project that keeps only sdkconfig.defaults in git: the include of project.cmake marks it."""
        (tmp_path / "sdkconfig.defaults").write_text("CONFIG_FREERTOS_HZ=1000\n")
        (tmp_path / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.22)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(app)\n"
        )
        assert ESPIDFBuildSystem.detect(tmp_path) is True

    def test_generic_cmake_leaves_out_a_project_without_sdkconfig(self, tmp_path):
        """Generic CMake asks the rule of ESP-IDF, thus the two rules never differ."""
        from fw_context_mcp.indexer.builders.generic_cmake import GenericCMakeBuildSystem

        (tmp_path / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.22)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(app)\n"
        )
        assert GenericCMakeBuildSystem.detect(tmp_path) is False

    def test_a_platformio_espidf_project_stays_platformio(self, tmp_path):
        """The registry asks PlatformIO first: its espidf projects have the same CMakeLists.txt."""
        from fw_context_mcp.indexer.builders import registry

        (tmp_path / "platformio.ini").write_text("[env:esp32]\nplatform = espressif32\nframework = espidf\n")
        (tmp_path / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(app)\n"
        )
        assert registry.detect(tmp_path) == "platformio"


class TestESPIDFValidation:
    def test_missing_build_dir_warns(self, tmp_path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "main.c", "directory": str(tmp_path), "arguments": ["gcc", "-c", "main.c"]}]))
        builder = ESPIDFBuildSystem()
        issues = builder.validate_artifacts(cc, tmp_path)
        assert len(issues) == 1
        assert "build" in issues[0].message.lower()
        assert issues[0].severity == "warning", "an explicit database still indexes"

    def test_build_dir_exists_no_issues(self, tmp_path):
        # build() leaves the database in its build directory, beside build.ninja.
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "build.ninja").write_text("", encoding="utf-8")
        cc = build_dir / "compile_commands.json"
        cc.write_text(json.dumps([{"file": "main.c", "directory": str(build_dir), "arguments": ["gcc", "-c", "main.c"]}]))
        builder = ESPIDFBuildSystem()
        issues = builder.validate_artifacts(cc, tmp_path)
        assert len(issues) == 0


class TestESPIDFTools:
    def test_required_tools(self):
        builder = ESPIDFBuildSystem()
        assert "idf.py" in builder.required_tools()
