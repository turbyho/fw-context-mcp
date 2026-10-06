"""Every backend builds into the output directory of fw-context, and reads from it.

The output directory is ``.fw-context/build/<variant>/out`` (see
``indexer/build_layout.py``).  Each backend sends its build there with its
own flag — `-B`, `--build`, `--build-path`, `-d`, ``PLATFORMIO_BUILD_DIR`` —
and each has a second place that must follow: the read of
``compile_commands.json``.  esp_idf once moved the first and not the second,
and the backend then failed in every configuration.

The build of the user (``build/``, ``.pio/build``, ``BUILD/``) must stay
untouched, because the index must never read a file that the next build of
the user replaces.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.build_layout import BuildLayout


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


def _out(root: Path, variant: str = "") -> Path:
    return BuildLayout(root).out_dir(variant)


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
            bdir = Path(cmd[cmd.index("-B") + 1])
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "compile_commands.json").write_text("[]", encoding="utf-8")
        return _Result()

    monkeypatch.setattr(mod, "run_build_command", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(mod, "resolve_real_binary", lambda n: Path(f"/usr/bin/{n}"))
    return root, calls


class TestESPIDFOutputDirectory:
    def test_the_database_is_read_from_the_output_directory(self, esp_idf_project):
        """The regression: `-B` moved and the read stayed on build/."""
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project

        result = ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert result == _out(root) / "compile_commands.json"
        assert result.exists(), "build() must return a compilation database that exists"
        assert not (root / "build").exists(), "the build must not create the directory of the user"

    def test_the_build_targets_the_output_directory(self, esp_idf_project):
        """A configured build/ of the user says nothing about the output directory.

        The gate that decides whether configuration is needed must look at
        the directory this build writes to.  It used to look at
        project_root/"build", so a build/ the user had left behind made the
        other directory appear ready.
        """
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project
        (root / "build").mkdir()  # the user has built here before

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        build_cmd = next(c for c in calls if "build" in c)
        assert build_cmd[build_cmd.index("-B") + 1] == str(_out(root))

    def test_the_error_names_the_directory_it_looked_in(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        root.mkdir()
        (root / "CMakeLists.txt").write_text("x\n", encoding="utf-8")
        (root / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32"\n', encoding="utf-8")

        monkeypatch.setattr(mod, "run_build_command", lambda *a, **kw: _Result())  # writes nothing
        monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "resolve_real_binary", lambda n: Path(f"/usr/bin/{n}"))

        with pytest.raises(RuntimeError, match=r"default/out"):
            ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))


class TestESPIDFLeavesSdkconfigAlone:
    """`idf.py set-target` renames <project>/sdkconfig to sdkconfig.old.

    `-B` does not move that file: tools/cmake/project.cmake takes
    ${CMAKE_SOURCE_DIR}/sdkconfig, which is the project directory.  A build
    that ran set-target therefore deleted a file the user usually has
    committed — and the target it passed came from a guess read out of that
    same file, with esp32 as the fallback, so it could also regenerate the
    configuration for another chip.

    Nothing is lost by skipping it: ensure_build_directory() runs cmake on
    its own and project.cmake takes the target from the sdkconfig already
    there.  set-target is needed only when that file is absent — and then it
    renames nothing.
    """

    def test_an_existing_sdkconfig_is_never_renamed(self, esp_idf_project):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, calls = esp_idf_project  # the fixture writes an sdkconfig

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert not any("set-target" in c for c in calls), (
            "the project has an sdkconfig, thus cmake can configure from it "
            "and set-target would only rename it away"
        )
        assert 'CONFIG_IDF_TARGET="esp32s3"' in (root / "sdkconfig").read_text(
            encoding="utf-8"
        ), "the configuration of the user must come through untouched"

    def test_a_project_without_sdkconfig_still_gets_a_target(self, tmp_path: Path, monkeypatch):
        """A fresh clone has nothing to rename, thus set-target is safe there."""
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        root.mkdir()
        (root / "CMakeLists.txt").write_text("x\n", encoding="utf-8")
        # no sdkconfig on purpose

        calls: list[list[str]] = []

        def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
            calls.append([str(c) for c in cmd])
            if "build" in cmd and "set-target" not in cmd:
                bdir = Path(cmd[cmd.index("-B") + 1])
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / "compile_commands.json").write_text("[]", encoding="utf-8")
            return _Result()

        monkeypatch.setattr(mod, "run_build_command", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "resolve_real_binary", lambda n: Path(f"/usr/bin/{n}"))

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert any("set-target" in c for c in calls), (
            "with no sdkconfig cmake has no target to read, thus set-target "
            "is the only way to configure — and it renames nothing"
        )


class TestESPIDFValidation:
    """The database of an ESP-IDF build sits beside its build.ninja."""

    def test_a_database_beside_build_ninja_passes(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        out = _out(tmp_path)
        out.mkdir(parents=True)
        (out / "build.ninja").write_text("", encoding="utf-8")
        cc = out / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        assert ESPIDFBuildSystem().validate_artifacts(cc, tmp_path) == []

    def test_a_database_without_a_build_directory_gets_a_warning(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        issues = ESPIDFBuildSystem().validate_artifacts(cc, tmp_path)
        assert [(i.category, i.severity) for i in issues] == [("missing_build_dir", "warning")]


class TestEveryBackendBuildsIntoTheOutputDirectory:
    """One test per backend that compiles: the flag and the read both name ``out/``."""

    @staticmethod
    def _record(monkeypatch, mod, on_call) -> list[tuple[list[str], dict]]:
        calls: list[tuple[list[str], dict]] = []

        def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None, **kw):
            command = [str(c) for c in cmd]
            calls.append((command, dict(env or {})))
            on_call(command, dict(env or {}), Path(cwd) if cwd else None)
            return _Result()

        monkeypatch.setattr(mod, "run_build_command", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
        return calls

    @staticmethod
    def _write_cc(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "compile_commands.json").write_text("[]", encoding="utf-8")

    def test_cmake(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.generic_cmake as mod

        def on_call(cmd, env, cwd):
            if "-B" in cmd:
                self._write_cc(Path(cmd[cmd.index("-B") + 1]))

        self._record(monkeypatch, mod, on_call)
        (tmp_path / "build").mkdir()

        result = mod.GenericCMakeBuildSystem().build(tmp_path, BuildConfig(variant_name="dev"))

        assert result == _out(tmp_path, "dev") / "compile_commands.json"
        assert list((tmp_path / "build").iterdir()) == []

    def test_arduino(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.arduino as mod

        def on_call(cmd, env, cwd):
            if "--only-compilation-database" in cmd:
                self._write_cc(Path(cmd[cmd.index("--build-path") + 1]))

        self._record(monkeypatch, mod, on_call)

        result = mod.ArduinoBuildSystem().build(tmp_path, BuildConfig(fqbn="arduino:avr:uno"))

        assert result == _out(tmp_path) / "compile_commands.json"

    def test_zephyr(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.zephyr as mod

        def on_call(cmd, env, cwd):
            self._write_cc(Path(cmd[cmd.index("-d") + 1]))

        self._record(monkeypatch, mod, on_call)
        monkeypatch.setattr(mod.ZephyrBuildSystem, "_prepare_ninja_wrapper", lambda self, root: ("", {}))

        result = mod.ZephyrBuildSystem().build(tmp_path, BuildConfig(board="nrf52840dk/nrf52840"))

        assert result == _out(tmp_path) / "compile_commands.json"

    def test_mbed(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.mbed_os as mod

        def on_call(cmd, env, cwd):
            Path(cmd[cmd.index("--output") + 1]).write_text("[]", encoding="utf-8")

        calls = self._record(monkeypatch, mod, on_call)

        result = mod.MbedOSBuildSystem().build(tmp_path, BuildConfig(target="NRF52840_DK", clean=False))

        [(cmd, _env)] = calls
        assert cmd[cmd.index("--build") + 1] == str(_out(tmp_path))
        assert result == _out(tmp_path) / "compile_commands.json"

    def test_platformio(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.platformio as mod

        def on_call(cmd, env, cwd):
            if "compiledb" in cmd:
                (tmp_path / "compile_commands.json").write_text("[]", encoding="utf-8")

        calls = self._record(monkeypatch, mod, on_call)

        result = mod.PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False))

        assert {env.get("PLATFORMIO_BUILD_DIR") for cmd, env in calls if "run" in cmd} == {str(_out(tmp_path))}
        assert result == _out(tmp_path) / "compile_commands.json"

    def test_manual_writes_its_dependency_files_there(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.manual as mod

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        calls = self._record(monkeypatch, mod, lambda cmd, env, cwd: None)

        result = mod.ManualBuildSystem().build(tmp_path, BuildConfig(source_dirs=["src"]))

        [(cmd, _env)] = calls
        assert cmd[cmd.index("-MF") + 1] == str(_out(tmp_path) / "deps" / "src" / "main.d")
        assert result == _out(tmp_path) / "compile_commands.json"
