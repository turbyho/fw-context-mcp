"""Tests for build artifact validator."""

import json
from pathlib import Path

from fw_context_mcp.indexer.validator import is_compile_commands_stale, validate_and_fix


class TestIsCompileCommandsStale:
    def test_missing_file_is_stale(self, tmp_path):
        stale, reasons = is_compile_commands_stale(tmp_path / "compile_commands.json", tmp_path)
        assert stale is True
        assert "missing" in reasons[0].lower()

    def test_valid_cc_is_not_stale(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.cpp").write_text("// empty")

        cc.write_text(json.dumps([
            {"file": str(src / "main.cpp"), "directory": str(tmp_path), "arguments": ["gcc", "-c", str(src / "main.cpp")]}
        ]))
        stale, _ = is_compile_commands_stale(cc, tmp_path)
        assert stale is False

    def test_missing_source_files(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        src = tmp_path / "src"
        src.mkdir()
        # Create 3 source files but cc only references 1
        (src / "main.cpp").write_text("// empty")
        (src / "extra.cpp").write_text("// empty")
        (src / "utils.cpp").write_text("// empty")

        cc.write_text(json.dumps([
            {"file": str(src / "main.cpp"), "directory": str(tmp_path), "arguments": ["gcc", "-c", str(src / "main.cpp")]}
        ]))
        stale, reasons = is_compile_commands_stale(cc, tmp_path)
        assert stale is True
        assert any("source file" in r.lower() for r in reasons)

    def test_build_marker_newer(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        pio_ini = tmp_path / "platformio.ini"
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.cpp").write_text("// empty")

        cc.write_text(json.dumps([
            {"file": str(src / "main.cpp"), "directory": str(tmp_path), "arguments": ["gcc", "-c", str(src / "main.cpp")]}
        ]))
        # Make platformio.ini newer than compile_commands.json
        pio_ini.write_text("[env:uno]\n")
        # Touch pio_ini after cc
        pio_ini.touch()
        stale, reasons = is_compile_commands_stale(cc, tmp_path)
        assert stale is True
        assert any("platformio.ini" in r for r in reasons)


class TestValidateAndFix:
    def test_missing_compile_commands(self, tmp_path):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        builder = PlatformIOBuildSystem()
        issues = validate_and_fix(tmp_path / "compile_commands.json", tmp_path, builder)
        assert len(issues) == 1
        assert issues[0].category == "missing_compile_commands"

    def test_empty_compile_commands(self, tmp_path):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        builder = PlatformIOBuildSystem()
        issues = validate_and_fix(cc, tmp_path, builder)
        assert len(issues) == 1
        assert issues[0].category == "empty_compile_commands"

    def test_valid_compile_commands_no_issues(self, tmp_path):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        cc = tmp_path / "compile_commands.json"
        # Create a .d file so validate_artifacts can find it
        obj_dir = tmp_path / "build"
        obj_dir.mkdir()
        d_path = obj_dir / "main.d"
        d_path.write_text("main.o: main.cpp\n")
        cc.write_text(json.dumps([
            {
                "file": "main.cpp",
                "directory": str(tmp_path),
                "arguments": ["gcc", "-c", "-MMD", "main.cpp"],
                "output": "build/main.o",
            }
        ]))
        builder = PlatformIOBuildSystem()
        issues = validate_and_fix(cc, tmp_path, builder)
        assert len(issues) == 0

    def test_invalid_json(self, tmp_path):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        cc = tmp_path / "compile_commands.json"
        cc.write_text("not json")
        builder = PlatformIOBuildSystem()
        issues = validate_and_fix(cc, tmp_path, builder)
        assert len(issues) >= 1
        assert any("json" in i.message.lower() or "parse" in i.message.lower() for i in issues)
