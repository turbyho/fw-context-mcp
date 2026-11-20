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

import os
import re
import shutil
import sys
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
    monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
    monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
    # The lookup of the builders (require_build_tool): Windows looks with
    # _find_program, and a stub of shutil.which does not reach it.
    monkeypatch.setattr(
        mod, "require_build_tool",
        lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
    )
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

    def test_the_build_does_not_give_back_bash_env(self, esp_idf_project, monkeypatch):
        """The overrides of the build must not carry BASH_ENV of the process.

        build_env() removes BASH_ENV and then applies the overrides.  The
        ESP-IDF build gave a full copy of os.environ as overrides, thus
        BASH_ENV came back, and `bash -c` of activate ran the hook of a harness.
        """
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, _ = esp_idf_project
        monkeypatch.setenv("BASH_ENV", "/harness/hook.sh")
        overrides: list[dict] = []
        recorded = mod.run_build_command

        def keep_env(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
            overrides.append(dict(env or {}))
            return recorded(cmd, cwd=cwd, description=description, env=env, build_cfg=build_cfg, timeout=timeout)

        monkeypatch.setattr(mod, "run_build_command", keep_env)

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert overrides and all("BASH_ENV" not in env for env in overrides)

    def test_the_build_asks_for_no_dependency_files(self, esp_idf_project, monkeypatch):
        """No ninja wrapper, no -MMD and no CCACHE_DEPEND: no part of fw-context reads a .d file."""
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root, _ = esp_idf_project
        envs: list[dict] = []
        recorded = mod.run_build_command

        def keep_env(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
            envs.append(dict(env or {}))
            return recorded(cmd, cwd=cwd, description=description, env=env, build_cfg=build_cfg, timeout=timeout)

        monkeypatch.setattr(mod, "run_build_command", keep_env)

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert envs and all("-MMD" not in env.get("EXTRA_CFLAGS", "") for env in envs)
        assert all("CCACHE_DEPEND" not in env for env in envs)
        assert not (root / ".fw-context" / "ninja").exists()

    def test_the_error_names_the_directory_it_looked_in(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.esp_idf as mod
        from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem

        root = tmp_path / "proj"
        root.mkdir()
        (root / "CMakeLists.txt").write_text("x\n", encoding="utf-8")
        (root / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32"\n', encoding="utf-8")

        monkeypatch.setattr(mod, "run_build_command", lambda *a, **kw: _Result())  # writes nothing
        monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
        # The lookup of the builders (require_build_tool): Windows looks with
        # _find_program, and a stub of shutil.which does not reach it.
        monkeypatch.setattr(
            mod, "require_build_tool",
            lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
        )

        # The message names the directory in the form of the system.
        with pytest.raises(RuntimeError, match=re.escape(str(Path("default", "out")))):
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

    def test_a_project_without_sdkconfig_gets_no_set_target(self, tmp_path: Path, monkeypatch):
        """ESP-IDF selects the target itself: IDF_TARGET, sdkconfig.defaults, else esp32 (targets.cmake).

        Measured with ESP-IDF 6.1: a project with only sdkconfig.defaults for
        esp32s3 got esp32s3.  The set-target esp32 of an older version took
        esp32 for each chip.
        """
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
        monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
        # The lookup of the builders (require_build_tool): Windows looks with
        # _find_program, and a stub of shutil.which does not reach it.
        monkeypatch.setattr(
            mod, "require_build_tool",
            lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
        )

        ESPIDFBuildSystem().build(root, replace(BuildConfig(), clean=False))

        assert not [c for c in calls if "set-target" in c], calls
        assert calls[-1][-1] == "build"


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
        monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
        # The lookup of the builders (require_build_tool): Windows looks with
        # _find_program, and a stub of shutil.which does not reach it.
        monkeypatch.setattr(
            mod, "require_build_tool",
            lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
        )
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

        calls = self._record(monkeypatch, mod, on_call)

        result = mod.ArduinoBuildSystem().build(tmp_path, BuildConfig(fqbn="arduino:avr:uno"))

        assert result == _out(tmp_path) / "compile_commands.json"
        # The port of sketch.yaml stays out: a board that is not attached
        # stopped the compile (default_port with default_protocol).
        assert all(cmd[cmd.index("--port") + 1] == "fw-context-no-upload" for cmd, _ in calls), calls

    def test_zephyr(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.zephyr as mod

        def on_call(cmd, env, cwd):
            self._write_cc(Path(cmd[cmd.index("-d") + 1]))

        calls = self._record(monkeypatch, mod, on_call)

        result = mod.ZephyrBuildSystem().build(tmp_path, BuildConfig(board="nrf52840dk/nrf52840"))

        assert result == _out(tmp_path) / "compile_commands.json"
        # No .d files: no part of fw-context reads one (the manifest records
        # the headers), and the ninja wrapper looked for ninja outside the
        # environment of the project.
        command, env = calls[0]
        assert not any("-MMD" in arg for arg in command)
        assert "CCACHE_DEPEND" not in env
        assert not (tmp_path / ".fw-context" / "ninja").exists()

    def _zephyr_with_old_build(self, tmp_path: Path, monkeypatch, variant: str, cache_dir: Path | None = None):
        """Return (module, calls, seen) for a Zephyr build over an output directory of an earlier build.

        The earlier build left ``CMakeCache.txt`` in ``out/``.  The cache
        names *cache_dir*, or ``out/`` itself when it is None.  After a move
        or a copy of the project, the cache names the old directory, and
        only a removal of ``out/`` repairs it: ``west build --pristine``
        cleans the directory that the cache names.  *seen* records, for each
        west call, if the old cache was still there when west started.
        """
        import fw_context_mcp.indexer.builders.zephyr as mod

        old_cache = _out(tmp_path, variant) / "CMakeCache.txt"
        old_cache.parent.mkdir(parents=True)
        named = cache_dir or old_cache.parent
        old_cache.write_text(
            f"CMAKE_CACHEFILE_DIR:INTERNAL={named}\nAPPLICATION_BINARY_DIR:PATH={named}\n", encoding="utf-8",
        )
        seen: list[bool] = []

        def on_call(cmd, env, cwd):
            seen.append(old_cache.exists())
            self._write_cc(Path(cmd[cmd.index("-d") + 1]))

        calls = self._record(monkeypatch, mod, on_call)
        return mod, calls, seen

    @pytest.mark.parametrize("clean", [True, False])
    def test_zephyr_clean_build_removes_the_old_output(self, tmp_path: Path, monkeypatch, clean: bool):
        mod, calls, seen = self._zephyr_with_old_build(tmp_path, monkeypatch, "")

        mod.ZephyrBuildSystem().build(tmp_path, BuildConfig(board="nrf52840dk/nrf52840", clean=clean))

        assert seen == [not clean]
        # The removal replaces --pristine: west cleans the directory of the
        # cache, which is the old path after a move.  --pristine=auto stays,
        # because an incremental build after a change of the board fails
        # without it (west build.pristine = never).
        assert [arg for arg in calls[0][0] if arg.startswith("--pristine")] == ["--pristine=auto"]

    @pytest.mark.parametrize("clean", [True, False])
    def test_zephyr_variant_obeys_clean(self, tmp_path: Path, monkeypatch, clean: bool):
        from fw_context_mcp.indexer.build import BuildVariant

        mod, calls, seen = self._zephyr_with_old_build(tmp_path, monkeypatch, "dev")
        other = _out(tmp_path, "other") / "CMakeCache.txt"
        other.parent.mkdir(parents=True)
        other.write_text("x\n", encoding="utf-8")
        cfg = BuildConfig(
            clean=clean,
            variants=[BuildVariant(name="dev", board="nrf52840dk/nrf52840"), BuildVariant(name="other", board="b")],
        )

        built = mod.ZephyrBuildSystem().build_multi(tmp_path, cfg, [cfg.variants[0]])

        assert seen == [not clean]
        assert built == [("dev", "", _out(tmp_path, "dev") / "compile_commands.json")]
        # The clean build of one variant leaves the build of each other variant.
        assert other.exists()
        assert [arg for arg in calls[0][0] if arg.startswith("--pristine")] == ["--pristine=auto"]

    def test_zephyr_variant_whose_output_cannot_go_fails_without_west(self, tmp_path: Path, monkeypatch):
        """A failed removal is a RuntimeError, the error of one failed variant build (``_build_variants``)."""
        import fw_context_mcp.indexer.build_layout as layout_mod
        from fw_context_mcp.indexer.build import BuildVariant

        mod, calls, _seen = self._zephyr_with_old_build(tmp_path, monkeypatch, "dev")

        def refuse(path, *args, **kwargs):
            raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(layout_mod.shutil, "rmtree", refuse)
        cfg = BuildConfig(variants=[BuildVariant(name="dev", board="nrf52840dk/nrf52840")])

        with pytest.raises(RuntimeError, match="cannot remove the build output"):
            mod.ZephyrBuildSystem().build_multi(tmp_path, cfg)
        assert calls == []

    def test_mbed(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.mbed_os as mod

        def on_call(cmd, env, cwd):
            Path(cmd[cmd.index("record") + 1]).write_text("[]", encoding="utf-8")

        calls = self._record(monkeypatch, mod, on_call)
        python = "/opt/mbed/bin/python"

        result = mod.MbedOSBuildSystem().build(
            tmp_path, BuildConfig(target="NRF52840_DK", clean=False, python=python)
        )

        [(cmd, _env)] = calls
        # The Python of mbed-cli runs the script, and the script runs `mbed compile`.
        assert cmd[:3] == [python, str(mod._COMPILE_DB_SCRIPT), "record"]
        assert cmd[4:7] == ["--", "compile", "-t"]
        assert cmd[cmd.index("--build") + 1] == str(_out(tmp_path))
        assert result == _out(tmp_path) / "compile_commands.json"

    def test_mbed_without_its_python_stops(self, tmp_path: Path, monkeypatch):
        """The `mbed` command on PATH does not tell which Python has mbed-cli."""
        import fw_context_mcp.indexer.builders.mbed_os as mod

        calls = self._record(monkeypatch, mod, lambda cmd, env, cwd: None)

        with pytest.raises(RuntimeError, match=r"\[build\] python"):
            mod.MbedOSBuildSystem().build(tmp_path, BuildConfig(target="NRF52840_DK", clean=False))
        assert calls == []

    def test_manual_writes_its_dependency_files_there(self, tmp_path: Path, monkeypatch):
        import fw_context_mcp.indexer.builders.manual as mod

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        calls = self._record(monkeypatch, mod, lambda cmd, env, cwd: None)

        result = mod.ManualBuildSystem().build(tmp_path, BuildConfig(source_dirs=["src"]))

        [(cmd, _env)] = calls
        assert cmd[cmd.index("-MF") + 1] == str(_out(tmp_path) / "deps" / "src" / "main.d")
        assert result == _out(tmp_path) / "compile_commands.json"


class TestPlatformIO:
    """One environment for each build, and the database in its build directory.

    The project root keeps the compile_commands.json of the user: the script
    that PLATFORMIO_EXTRA_SCRIPTS adds moves the database of the build of
    fw-context into ``out/<env>/``.  Measured with PlatformIO 6 on two
    projects before this backend used it.
    """

    @staticmethod
    def _fake_pio(monkeypatch, root: Path, environments: list[str]) -> list[tuple[list[str], dict]]:
        import json

        import fw_context_mcp.indexer.builders.platformio as mod

        calls: list[tuple[list[str], dict]] = []

        def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None, **kw):
            command = [str(c) for c in cmd]
            calls.append((command, dict(env or {})))
            result = _Result()
            if "config" in command:
                result.stdout = json.dumps([[f"env:{name}", []] for name in environments])
            elif "compiledb" in command:
                env_name = command[command.index("--environment") + 1]
                out = Path(env["PLATFORMIO_BUILD_DIR"]) / env_name
                out.mkdir(parents=True, exist_ok=True)
                (out / "compile_commands.json").write_text("[]", encoding="utf-8")
            return result

        monkeypatch.setattr(mod, "run_build_command", fake_run)
        monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
        # The lookup of the builders (require_build_tool): Windows looks with
        # _find_program, and a stub of shutil.which does not reach it.
        monkeypatch.setattr(
            mod, "require_build_tool",
            lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
        )
        return calls

    def test_the_only_environment_builds_into_out(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        calls = self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])

        result = PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False))

        assert result == _out(tmp_path) / "esp32dev" / "compile_commands.json"
        assert not (tmp_path / "compile_commands.json").exists(), "the file of the user stays as it was"
        runs = [(cmd, env) for cmd, env in calls if "run" in cmd]
        assert runs and all(cmd[cmd.index("--environment") + 1] == "esp32dev" for cmd, _ in runs)
        assert {env["PLATFORMIO_BUILD_DIR"] for _, env in runs} == {str(_out(tmp_path))}

    def test_the_script_is_written_beside_out_and_named_with_a_newline(self, tmp_path: Path, monkeypatch):
        """A value without a newline splits at ", ", thus a path with a comma would break."""
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        calls = self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])

        PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False))

        script = BuildLayout(tmp_path).variant_dir("") / "fw_context_compiledb.py"
        assert 'COMPILATIONDB_PATH="$BUILD_DIR/compile_commands.json"' in script.read_text(encoding="utf-8")
        assert {env["PLATFORMIO_EXTRA_SCRIPTS"] for cmd, env in calls if "run" in cmd} == {f"pre:{script}\n"}

    def test_a_variant_builds_its_environment(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        calls = self._fake_pio(monkeypatch, tmp_path, ["a", "b"])

        result = PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False, variant_name="b"))

        assert result == _out(tmp_path, "b") / "b" / "compile_commands.json"
        assert not any("config" in cmd for cmd, _ in calls), "the variant names its environment"

    def test_two_environments_without_variants_are_refused(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        self._fake_pio(monkeypatch, tmp_path, ["a", "b"])

        with pytest.raises(RuntimeError, match="2 environments"):
            PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False))

    def test_the_build_of_another_environment_goes(self, tmp_path: Path, monkeypatch):
        """platformio.ini renamed the environment: two databases would be no answer."""
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        old = _out(tmp_path) / "old_name"
        old.mkdir(parents=True)
        (old / "compile_commands.json").write_text("[]", encoding="utf-8")
        self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])

        PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False))

        assert not old.exists()
        assert PlatformIOBuildSystem().output_compile_commands(_out(tmp_path), BuildConfig()) == {
            "": _out(tmp_path) / "esp32dev" / "compile_commands.json"
        }

    def test_one_environment_is_no_variant(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])
        cfg = BuildConfig()

        assert PlatformIOBuildSystem().implicit_variants(tmp_path, cfg) == []
        assert cfg.environment == "esp32dev"

    def test_each_of_two_environments_is_a_variant(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.build import build_variant_config
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        self._fake_pio(monkeypatch, tmp_path, ["nucleo", "native"])
        cfg = BuildConfig()

        variants = PlatformIOBuildSystem().implicit_variants(tmp_path, cfg)

        assert [v.name for v in variants] == ["nucleo", "native"]
        assert build_variant_config(cfg, variants[1]).environment == "native"

    def test_the_scripts_of_the_user_stay(self, tmp_path: Path, monkeypatch):
        """A script that the user adds through the variable runs too, before ours."""
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        calls = self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])
        monkeypatch.setenv("PLATFORMIO_EXTRA_SCRIPTS", "pre:ci.py")
        cfg = BuildConfig(clean=False, extra_env={"PLATFORMIO_EXTRA_SCRIPTS": "post:local.py"})

        PlatformIOBuildSystem().build(tmp_path, cfg)

        script = BuildLayout(tmp_path).variant_dir("") / "fw_context_compiledb.py"
        values = {env["PLATFORMIO_EXTRA_SCRIPTS"] for cmd, env in calls if "run" in cmd}
        assert values == {f"post:local.py\npre:{script}\n"}, "the config wins over the process, and ours is added"

    def test_a_build_dir_of_the_user_is_refused(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        self._fake_pio(monkeypatch, tmp_path, ["esp32dev"])

        with pytest.raises(RuntimeError, match="PLATFORMIO_BUILD_DIR"):
            PlatformIOBuildSystem().build(tmp_path, BuildConfig(env={"PLATFORMIO_BUILD_DIR": "x"}))

    @pytest.mark.parametrize("name", ["semi;colon", "dollar$sign"])
    def test_a_path_that_platformio_cannot_read_is_refused(self, tmp_path: Path, monkeypatch, name: str):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        root = tmp_path / name
        root.mkdir()
        self._fake_pio(monkeypatch, root, ["esp32dev"])

        with pytest.raises(RuntimeError, match="Move the project"):
            PlatformIOBuildSystem().build(root, BuildConfig(clean=False))

    def test_a_database_of_an_earlier_build_is_not_taken(self, tmp_path: Path, monkeypatch):
        """A compiledb that writes elsewhere must not leave the old file to read as new."""
        import fw_context_mcp.indexer.builders.platformio as mod

        old = _out(tmp_path) / "esp32dev" / "compile_commands.json"
        old.parent.mkdir(parents=True)
        old.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(mod, "run_build_command", lambda *a, **kw: _Result())
        monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
        monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
        # The lookup of the builders (require_build_tool): Windows looks with
        # _find_program, and a stub of shutil.which does not reach it.
        monkeypatch.setattr(
            mod, "require_build_tool",
            lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
        )

        with pytest.raises(RuntimeError, match="COMPILATIONDB_PATH"):
            mod.PlatformIOBuildSystem().build(tmp_path, BuildConfig(clean=False, environment="esp32dev"))

    def test_two_environment_builds_are_no_answer(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        for env_name in ("a", "b"):
            (_out(tmp_path) / env_name).mkdir(parents=True)
            (_out(tmp_path) / env_name / "compile_commands.json").write_text("[]", encoding="utf-8")

        assert PlatformIOBuildSystem().output_compile_commands(_out(tmp_path), BuildConfig()) == {}


def _fake_backend(monkeypatch, backend: str, root: Path, marker: Path):
    """Return (builder, calls) for *backend*, with a fake build tool.

    Each entry of *calls* is ``(command, marker_was_there)``: the command
    and if *marker*, a file of an earlier build in ``out/``, was still
    there when the command started.  The fake writes the database where
    the real tool writes it.
    """
    import importlib
    import json

    module_name, class_name = {
        "cmake": ("generic_cmake", "GenericCMakeBuildSystem"),
        "arduino": ("arduino", "ArduinoBuildSystem"),
        "esp_idf": ("esp_idf", "ESPIDFBuildSystem"),
        "platformio": ("platformio", "PlatformIOBuildSystem"),
    }[backend]
    mod = importlib.import_module(f"fw_context_mcp.indexer.builders.{module_name}")
    calls: list[tuple[list[str], bool]] = []

    def write_cc(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "compile_commands.json").write_text("[]", encoding="utf-8")

    def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None, **kw):
        command = [str(c) for c in cmd]
        calls.append((command, marker.exists()))
        result = _Result()
        if backend == "cmake" and "-B" in command:
            write_cc(Path(command[command.index("-B") + 1]))
        elif backend == "arduino" and "--only-compilation-database" in command:
            write_cc(Path(command[command.index("--build-path") + 1]))
        elif backend == "esp_idf" and "build" in command:
            write_cc(Path(command[command.index("-B") + 1]))
        elif backend == "platformio" and "config" in command:
            result.stdout = json.dumps([["env:dev", []]])
        elif backend == "platformio" and "compiledb" in command:
            write_cc(Path(env["PLATFORMIO_BUILD_DIR"]) / command[command.index("--environment") + 1])
        return result

    monkeypatch.setattr(mod, "run_build_command", fake_run)
    monkeypatch.setattr(shutil, "which", lambda n, path=None: f"/usr/bin/{n}")
    monkeypatch.setattr(mod, "build_tool_available", lambda *a, **kw: True, raising=False)
    # The lookup of the builders (require_build_tool): Windows looks with
    # _find_program, and a stub of shutil.which does not reach it.
    monkeypatch.setattr(
        mod, "require_build_tool",
        lambda names, cwd, cfg, hint: names if isinstance(names, str) else names[0], raising=False,
    )
    return getattr(mod, class_name)(), calls


_CLEAN_BACKENDS = ["cmake", "arduino", "esp_idf", "platformio"]


class TestCleanBuildRemovesTheOutput:
    """A clean build removes ``out/`` of the variant before the first build command.

    The build tools have their own clean step (``idf.py fullclean``,
    ``pio run --target clean``), but a failure of that step was ignored,
    and the build then ran over the old ``out/``.  A removal that fails is
    a RuntimeError: each caller of a build catches only that error, and it
    then reports the failed build of one variant (``cli._index``).
    """

    @staticmethod
    def _old_build(root: Path) -> Path:
        marker = _out(root, "dev") / "old-build-file"
        marker.parent.mkdir(parents=True)
        marker.write_text("x\n", encoding="utf-8")
        return marker

    @pytest.mark.parametrize("backend", _CLEAN_BACKENDS)
    @pytest.mark.parametrize("clean", [True, False])
    def test_the_old_output_goes_only_for_a_clean_build(self, tmp_path: Path, monkeypatch, backend: str, clean: bool):
        marker = self._old_build(tmp_path)
        builder, calls = _fake_backend(monkeypatch, backend, tmp_path, marker)

        builder.build(tmp_path, BuildConfig(variant_name="dev", fqbn="arduino:avr:uno", clean=clean))

        assert calls, "the build ran no command"
        assert {was_there for _, was_there in calls} == {not clean}
        # The clean step of the build tool is gone: the removal replaces it.
        assert not any("fullclean" in cmd or "clean" in cmd for cmd, _ in calls), calls

    @pytest.mark.parametrize("backend", _CLEAN_BACKENDS)
    def test_an_output_that_cannot_go_fails_before_the_build(self, tmp_path: Path, monkeypatch, backend: str):
        import fw_context_mcp.indexer.build_layout as layout_mod

        marker = self._old_build(tmp_path)
        builder, calls = _fake_backend(monkeypatch, backend, tmp_path, marker)

        def refuse(path, *args, **kwargs):
            raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(layout_mod.shutil, "rmtree", refuse)

        with pytest.raises(RuntimeError, match="cannot remove the build output"):
            builder.build(tmp_path, BuildConfig(variant_name="dev", fqbn="arduino:avr:uno", clean=True))
        assert calls == []
        assert marker.exists()

    def test_a_platformio_environment_that_cannot_go_is_a_runtime_error(self, tmp_path: Path, monkeypatch):
        """The build of an other environment in ``out/`` goes also in an incremental build."""
        import fw_context_mcp.indexer.builders.platformio as mod

        old = _out(tmp_path, "dev") / "old_name"
        old.mkdir(parents=True)
        (old / "compile_commands.json").write_text("[]", encoding="utf-8")
        builder, calls = _fake_backend(monkeypatch, "platformio", tmp_path, old)

        def refuse(path, *args, **kwargs):
            raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(mod.shutil, "rmtree", refuse)

        with pytest.raises(RuntimeError, match="cannot remove the build of environment old_name"):
            builder.build(tmp_path, BuildConfig(variant_name="dev", clean=False))
        assert calls == []


class TestZephyrBuildOfOtherDirectory:
    """An ``out/`` whose CMake cache names an other directory goes, also with ``--no-clean``.

    A moved project, or a copy that took ``.fw-context/build`` with it,
    leaves such a cache.  CMake refuses it, and the pristine step of
    ``west build --pristine=auto`` removes the content of the directory
    that the cache names: in a copy, the output of the original project.
    """

    _helper = TestEveryBackendBuildsIntoTheOutputDirectory()

    @staticmethod
    def _original_output(tmp_path: Path) -> Path:
        """Return the ``out/`` of the original project, with a file in it."""
        original = tmp_path / "original" / "out"
        original.mkdir(parents=True)
        (original / "build.ninja").write_text("x\n", encoding="utf-8")
        return original

    @pytest.mark.parametrize("moved", [True, False], ids=["moved", "copied"])
    def test_build_removes_the_output_of_other_directory(self, tmp_path: Path, monkeypatch, moved: bool):
        root = tmp_path / "copy"
        root.mkdir()
        original = tmp_path / "gone" / "out" if moved else self._original_output(tmp_path)
        mod, calls, seen = self._helper._zephyr_with_old_build(root, monkeypatch, "", original)

        mod.ZephyrBuildSystem().build(root, BuildConfig(board="nrf52840dk/nrf52840", clean=False))

        assert seen == [False]
        assert [arg for arg in calls[0][0] if arg.startswith("--pristine")] == ["--pristine=auto"]
        if not moved:
            assert (original / "build.ninja").exists(), "the output of the original project stays"

    def test_variant_removes_the_output_of_other_directory(self, tmp_path: Path, monkeypatch):
        from fw_context_mcp.indexer.build import BuildVariant

        root = tmp_path / "copy"
        root.mkdir()
        original = self._original_output(tmp_path)
        mod, _calls, seen = self._helper._zephyr_with_old_build(root, monkeypatch, "dev", original)
        cfg = BuildConfig(clean=False, variants=[BuildVariant(name="dev", board="nrf52840dk/nrf52840")])

        mod.ZephyrBuildSystem().build_multi(root, cfg)

        assert seen == [False]
        assert (original / "build.ninja").exists()

    def test_a_cache_without_the_keys_stays(self, tmp_path: Path, monkeypatch):
        """No directory in the cache proves nothing, thus ``out/`` stays for west to judge."""
        import fw_context_mcp.indexer.builders.zephyr as mod

        cache = _out(tmp_path) / "CMakeCache.txt"
        cache.parent.mkdir(parents=True)
        cache.write_text("CMAKE_PROJECT_NAME:STATIC=app\n", encoding="utf-8")
        seen: list[bool] = []

        def on_call(cmd, env, cwd):
            seen.append(cache.exists())
            self._helper._write_cc(Path(cmd[cmd.index("-d") + 1]))

        self._helper._record(monkeypatch, mod, on_call)

        mod.ZephyrBuildSystem().build(tmp_path, BuildConfig(board="nrf52840dk/nrf52840", clean=False))

        assert seen == [True]

    def test_the_cache_parser_reads_crlf_and_windows_paths(self, tmp_path: Path):
        from fw_context_mcp.indexer.builders.zephyr import _cache_directories

        cache = tmp_path / "CMakeCache.txt"
        cache.write_bytes(
            b"CMAKE_PROJECT_NAME:STATIC=app\r\n"
            b"CMAKE_CACHEFILE_DIR:INTERNAL=C:/work/proj/out\r\n"
            b"APPLICATION_BINARY_DIR:PATH=C:/work/proj/out\r\n"
        )

        assert _cache_directories(cache) == ["C:/work/proj/out", "C:/work/proj/out"]

    @pytest.mark.skipif(sys.platform == "win32", reason="a symbolic link on Windows needs a privilege of the user")
    def test_own_output_through_a_linked_root_stays(self, tmp_path: Path, monkeypatch):
        """The cache can name the same ``out/`` through a symbolic link: that is no other directory."""
        root = tmp_path / "proj"
        root.mkdir()
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        mod, _calls, seen = self._helper._zephyr_with_old_build(root, monkeypatch, "", _out(link))

        mod.ZephyrBuildSystem().build(root, BuildConfig(board="nrf52840dk/nrf52840", clean=False))

        assert seen == [True]

    @pytest.mark.skipif(sys.platform != "linux", reason="only Linux accepts a file name that is not UTF-8")
    def test_own_output_with_a_name_that_is_not_utf8_stays(self, tmp_path: Path, monkeypatch):
        """CMake writes the bytes of the path: a Latin-1 name must come back as the same path."""
        root = tmp_path / os.fsdecode(b"proj\xe9")
        root.mkdir()
        mod, _calls, seen = self._helper._zephyr_with_old_build(root, monkeypatch, "", Path("/placeholder"))
        out = os.fsencode(_out(root))
        (_out(root) / "CMakeCache.txt").write_bytes(
            b"CMAKE_CACHEFILE_DIR:INTERNAL=" + out + b"\nAPPLICATION_BINARY_DIR:PATH=" + out + b"\n"
        )

        mod.ZephyrBuildSystem().build(root, BuildConfig(board="nrf52840dk/nrf52840", clean=False))

        assert seen == [True]
