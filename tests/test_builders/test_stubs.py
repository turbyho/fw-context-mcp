"""Tests for Tier 3/4 stub builders — detection only, no automated build."""

import pytest
from fw_context_mcp.indexer.builders.stubs import (
    BareGCCStub,
    IAREWARMStub,
    KeilMDKStub,
    MakefileStub,
    STM32CubeIDEStub,
    TICCSStub,
)


class TestKeilMDKStub:
    def test_detected(self, tmp_path):
        (tmp_path / "test.uvprojx").write_text("<Project>\n")
        assert KeilMDKStub.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert KeilMDKStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="Keil MDK"):
            KeilMDKStub().build(tmp_path, None)


class TestIAREWARMStub:
    def test_detected_ewp(self, tmp_path):
        (tmp_path / "test.ewp").write_text("<project>\n")
        assert IAREWARMStub.detect(tmp_path) is True

    def test_detected_eww(self, tmp_path):
        (tmp_path / "test.eww").write_text("<workspace>\n")
        assert IAREWARMStub.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert IAREWARMStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="IAR EWARM"):
            IAREWARMStub().build(tmp_path, None)


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


class TestMakefileStub:
    def test_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\t@echo ok\n")
        assert MakefileStub.detect(tmp_path) is True

    def test_not_detected(self, tmp_path):
        assert MakefileStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="Makefile"):
            MakefileStub().build(tmp_path, None)


class TestBareGCCStub:
    def test_never_detected(self, tmp_path):
        # BareGCCStub.detect() always returns False — it's a conscious
        # fallback, never auto-detected.
        assert BareGCCStub.detect(tmp_path) is False

    def test_build_raises_with_help(self, tmp_path):
        with pytest.raises(RuntimeError, match="No compile_commands.json"):
            BareGCCStub().build(tmp_path, None)
