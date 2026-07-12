"""Tests for Tier 3/4 stub builders — detection only, no automated build."""

import pytest

from fw_context_mcp.indexer.builders.manual import ManualBuildSystem
from fw_context_mcp.indexer.builders.stubs import (
    STM32CubeIDEStub,
    TICCSStub,
)


class TestSTM32CubeIDEStub:
    def test_detected_cproject(self, tmp_path):
        (tmp_path / ".cproject").write_text("<?xml version='1.0'?>\n")
        assert STM32CubeIDEStub.detect(tmp_path) is True

    def test_detected_project(self, tmp_path):
        (tmp_path / ".project").write_text("<?xml version='1.0'?>\n")
        assert STM32CubeIDEStub.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert STM32CubeIDEStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="STM32CubeIDE"):
            STM32CubeIDEStub().build(tmp_path, None)


class TestTICCSStub:
    def test_detected(self, tmp_path):
        (tmp_path / ".projectspec").write_text("<?xml version='1.0'?>\n")
        assert TICCSStub.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert TICCSStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="Code Composer"):
            TICCSStub().build(tmp_path, None)


class TestManualBuildSystem:
    def test_never_detected(self, tmp_path):
        # ManualBuildSystem.detect() always returns False — bare mode is
        # never auto-detected, it requires explicit user opt-in.
        assert ManualBuildSystem.detect(tmp_path) is False

    def test_build_without_source_dirs_raises(self, tmp_path):
        from fw_context_mcp.indexer.build import BuildConfig

        cfg = BuildConfig(system="bare")
        with pytest.raises(RuntimeError, match="source_dirs"):
            ManualBuildSystem().build(tmp_path, cfg)

    def test_generate_creates_compile_commands(self, tmp_path):
        from fw_context_mcp.indexer.build import BuildConfig

        # Create source files
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.c").write_text("int main() { return 0; }\n")
        (src_dir / "utils.cpp").write_text("void f() {}\n")

        import shutil

        compiler = "gcc" if shutil.which("gcc") else "cc"
        cfg = BuildConfig(
            system="bare",
            source_dirs=["src"],
            include_dirs=["include"],
            defines=["FOO", "BAR=1"],
            extra_flags=["-Wall", "-O2"],
            compiler=compiler,
        )

        cc_path = ManualBuildSystem().generate(tmp_path, cfg)

        import json
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        assert len(data) == 2

        main_entry = next(e for e in data if e["file"].endswith("main.c"))
        # New code uses "arguments" (not "command") — array form for better tool support
        args = main_entry["arguments"]
        assert compiler in " ".join(args)
        assert any("-I" in a for a in args)
        assert any("include" in a for a in args)
        assert any("-D" in a for a in args)
        assert "FOO" in args
        assert "BAR=1" in args
        assert "-Wall" in args
        assert "-O2" in args

    def test_generate_empty_source_dirs_raises(self, tmp_path):
        from fw_context_mcp.indexer.build import BuildConfig

        (tmp_path / "empty_dir").mkdir()
        cfg = BuildConfig(
            system="bare",
            source_dirs=["empty_dir"],
        )
        with pytest.raises(RuntimeError, match="No source files"):
            ManualBuildSystem().generate(tmp_path, cfg)

    def test_generate_include_dirs_become_dash_i(self, tmp_path):
        from fw_context_mcp.indexer.build import BuildConfig

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "app.c").write_text("int x;\n")

        cfg = BuildConfig(
            system="bare",
            source_dirs=["src"],
            include_dirs=["include", "lib/CMSIS/Include"],
        )
        cc_path = ManualBuildSystem().generate(tmp_path, cfg)
        import json
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        entry = data[0]
        args = entry["arguments"]
        assert "-I" in args
        assert "include" in args
        assert "lib/CMSIS/Include" in args

    def test_generate_system_include_dirs_become_isystem(self, tmp_path):
        from fw_context_mcp.indexer.build import BuildConfig

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "app.c").write_text("int x;\n")

        cfg = BuildConfig(
            system="bare",
            source_dirs=["src"],
            system_include_dirs=["/opt/toolchain/include"],
        )
        cc_path = ManualBuildSystem().generate(tmp_path, cfg)
        import json
        data = json.loads(cc_path.read_text(encoding="utf-8"))
        entry = data[0]
        args = entry["arguments"]
        assert "-isystem" in args
        assert "/opt/toolchain/include" in args

    def test_required_tools_empty(self):
        assert ManualBuildSystem().required_tools() == []
