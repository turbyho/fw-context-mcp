"""A backend that redirects its build must read the result from the same place.

`-B`, `--build`, `PLATFORMIO_BUILD_DIR` — every backend that isolates its
output has a second place that must follow: the read of
``compile_commands.json``.  esp_idf moved the first and not the second, and
the backend then failed in every configuration; generic_cmake had it right
from the start and nothing tested the difference.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.utils import autobuild_dir


@pytest.fixture
def esp_idf_project(tmp_path: Path, monkeypatch) -> tuple[Path, list[list[str]]]:
    """An ESP-IDF project whose idf.py honours -B.  Returns (root, calls)."""
    import fw_context_mcp.indexer.builders.esp_idf as mod

    root = tmp_path / "proj"
    root.mkdir()
    (root / "CMakeLists.txt").write_text("x\n", encoding="utf-8")
    (root / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32s3"\n', encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
        calls.append([str(c) for c in cmd])
        if "build" in cmd and "set-target" not in cmd:
            bdir = Path(cmd[cmd.index("-B") + 1]) if "-B" in cmd else Path(cwd) / "build"
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "compile_commands.json").write_text("[]", encoding="utf-8")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(mod, "run_build_command", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(mod, "resolve_real_binary", lambda n: Path(f"/usr/bin/{n}"))
    return root, calls


class TestESPIDFIsolatedDirectory:
    def test_the_database_is_read_from_the_isolated_directory(
        self, esp_idf_project, tmp_path: Path
    ):
        """The regression: `-B` moved and the read stayed on build/."""
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project
        cfg = replace(BuildConfig(), isolated_build_dir=autobuild_dir(), clean=False)

        result = ESPIDFBuildSystem().build(root, cfg)

        assert result.exists(), "build() must return a compilation database that exists"
        assert not (root / "build").exists(), (
            "the isolated build must not create the directory of the user"
        )
        assert (root / autobuild_dir() / "compile_commands.json").exists()

    def test_the_gate_asks_about_the_isolated_directory(self, esp_idf_project):
        """A configured build/ of the user says nothing about the isolated one."""
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project
        (root / "build").mkdir()  # the user has built here before
        cfg = replace(BuildConfig(), isolated_build_dir=autobuild_dir(), clean=False)

        ESPIDFBuildSystem().build(root, cfg)

        assert any("set-target" in c for c in calls), (
            "the isolated directory holds no cmake cache, thus set-target has "
            "to run — asking about project_root/'build' skipped it and the "
            "build then failed on a missing configuration"
        )

    def test_the_default_directory_is_kept_without_isolation(self, esp_idf_project):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project
        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert all("-B" not in c for c in calls), (
            "an explicit build keeps the plain command line"
        )
        assert (root / "build" / "compile_commands.json").exists()

    def test_the_error_names_the_directory_it_looked_in(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        root.mkdir()
        (root / "CMakeLists.txt").write_text("x\n", encoding="utf-8")
        (root / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32"\n', encoding="utf-8")

        def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()  # writes nothing

        monkeypatch.setattr(mod, "run_build_command", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "resolve_real_binary", lambda n: Path(f"/usr/bin/{n}"))

        cfg = replace(BuildConfig(), isolated_build_dir=autobuild_dir(), clean=False)
        with pytest.raises(RuntimeError, match=r"autobuild"):
            ESPIDFBuildSystem().build(root, cfg)


class TestESPIDFValidation:
    """validate_artifacts has no BuildConfig, so it must accept either place."""

    def test_an_isolated_build_passes_validation(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        (root / autobuild_dir()).mkdir(parents=True)
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        assert ESPIDFBuildSystem().validate_artifacts(cc, root) == [], (
            "an isolated build produced artifacts; reporting them missing "
            "aborts the index run and arms the autobuild backoff"
        )

    def test_a_project_that_was_never_built_still_fails(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        root.mkdir()
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        issues = ESPIDFBuildSystem().validate_artifacts(cc, root)
        assert [i.category for i in issues] == ["missing_build_dir"]

    def test_the_ordinary_build_directory_still_passes(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        (root / "build").mkdir(parents=True)
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        assert ESPIDFBuildSystem().validate_artifacts(cc, root) == []


class TestEveryBackendReadsWhereItWrote:
    """Guard against the same defect appearing in another backend.

    A backend that names a build directory in its own source must not name
    it twice with different spellings.  The check is textual on purpose: the
    defect was two literals that drifted apart, and only a reader comparing
    them would have caught it.
    """

    def test_no_backend_hardcodes_a_build_directory_beside_resolve_build_dir(self):
        from fw_context_mcp.indexer import builders

        offenders: list[str] = []
        pkg_dir = Path(builders.__file__).parent
        for path in sorted(pkg_dir.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            if "resolve_build_dir(" not in src:
                continue  # backend does not redirect its output at all
            if "def build(" not in src:
                continue
            # Only the body of build(): validate_artifacts() further down
            # names project_root/"build" on purpose — it has no BuildConfig
            # and has to accept either directory.
            start = src.index("def build(")
            rest = src[start:]
            nxt = rest.find("\n    def ", 1)
            body = rest if nxt == -1 else rest[:nxt]
            if 'project_root / "build"' in body:
                offenders.append(path.name)

        assert offenders == [], (
            f"{offenders} call resolve_build_dir() and still hardcode "
            'project_root / "build" inside build() — that is the esp_idf '
            "defect: the redirect moved and the read did not"
        )
