"""The images of a Zephyr build come from ``domains.yaml``, which sysbuild writes.

NCS builds with sysbuild also without ``--sysbuild``.  Measured with NCS
v3.4.0 on ``hello_world``: ``west build`` put the database of the
application in ``out/hello_world/``, with MCUboot also ``out/mcuboot/``, and
``--no-sysbuild`` put it in ``out/``.  The application image has the name of
the application directory, and ``domains.yaml`` names it as the default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem


def _domains(out_dir: Path, default: str, names: list[str], *, top: Path | None = None) -> None:
    """Write ``domains.yaml`` as sysbuild writes it, with absolute paths under *top*."""
    top = top or out_dir
    lines = [f"default: {default}", f"build_dir: {top}", "domains:"]
    for name in names:
        lines += [f"  - name: {name}", f"    build_dir: {top / name}"]
    lines += ["flash_order:"] + [f"  - {name}" for name in names]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "domains.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _database(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "compile_commands.json"
    database.write_text("[]", encoding="utf-8")
    return database


class TestOutputCompileCommands:
    def test_each_domain_is_an_image(self, tmp_path):
        _domains(tmp_path, "hello_world", ["hello_world", "mcuboot"])
        app = _database(tmp_path / "hello_world")
        boot = _database(tmp_path / "mcuboot")

        found = ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig())

        assert found == {"hello_world": app, "mcuboot": boot}

    def test_a_directory_that_no_domain_names_is_no_image(self, tmp_path):
        """A pristine build is necessary to remove the directory of an image that the build no longer makes.

        Measured on one project: domains.yaml named four images, and five
        subdirectories held a database.
        """
        _domains(tmp_path, "app", ["app"])
        app = _database(tmp_path / "app")
        _database(tmp_path / "app_retired")

        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {"app": app}

    def test_without_domains_the_build_made_one_program(self, tmp_path):
        """Upstream Zephyr, or west build --no-sysbuild."""
        database = _database(tmp_path)

        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {"": database}

    def test_no_build_gives_nothing(self, tmp_path):
        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {}

    def test_the_paths_are_relative_to_the_build_directory_of_the_file(self, tmp_path):
        """A project that moved keeps its images: the absolute paths in the file name the old place."""
        _domains(tmp_path / "new", "app", ["app"], top=tmp_path / "old")
        app = _database(tmp_path / "new" / "app")

        assert ZephyrBuildSystem().output_compile_commands(tmp_path / "new", BuildConfig()) == {"app": app}

    @pytest.mark.parametrize(
        "text",
        ["{", "- a list\n", "default: app\nbuild_dir: /x\n", "default: other\nbuild_dir: /x\ndomains:\n  - name: app\n    build_dir: /x/app\n"],
        ids=["yaml", "not-a-mapping", "no-domains", "default-is-no-domain"],
    )
    def test_an_invalid_file_gives_no_build(self, tmp_path, text):
        """The caller then builds again; an invalid file is not a build of one program."""
        (tmp_path / "domains.yaml").write_text(text, encoding="utf-8")
        _database(tmp_path)

        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {}
        assert ZephyrBuildSystem().application_database(tmp_path) is None


class TestApplicationDatabase:
    def test_the_default_domain_is_the_application(self, tmp_path):
        _domains(tmp_path, "myapp", ["mcuboot", "myapp"])

        assert ZephyrBuildSystem().application_database(tmp_path) == tmp_path / "myapp" / "compile_commands.json"

    def test_without_domains_no_application_is_named(self, tmp_path):
        _database(tmp_path)

        assert ZephyrBuildSystem().application_database(tmp_path) is None


class TestBuild:
    def _build(self, tmp_path, monkeypatch, cfg: BuildConfig, make_output) -> tuple[Path, list[str]]:
        """Run ``build()`` with a fake west that calls *make_output(out_dir)*."""
        from fw_context_mcp.indexer.build_layout import BuildLayout
        from fw_context_mcp.indexer.builders import zephyr

        out_dir = BuildLayout(tmp_path).out_dir("")
        commands: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            make_output(out_dir)

        monkeypatch.setattr(zephyr.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(zephyr, "run_build_command", fake_run)
        monkeypatch.setattr(ZephyrBuildSystem, "_prepare_ninja_wrapper", lambda self, root: ("", {}))
        return ZephyrBuildSystem().build(tmp_path, cfg), commands[0]

    def test_a_sysbuild_returns_the_database_of_the_application(self, tmp_path, monkeypatch):
        def output(out_dir: Path) -> None:
            _domains(out_dir, "myapp", ["myapp", "mcuboot"])
            _database(out_dir / "myapp")
            _database(out_dir / "mcuboot")

        database, _ = self._build(tmp_path, monkeypatch, BuildConfig(board="b"), output)

        assert database.parent.name == "myapp"

    def test_a_build_of_one_program_returns_its_database(self, tmp_path, monkeypatch):
        database, command = self._build(tmp_path, monkeypatch, BuildConfig(board="b"), _database)

        assert database.name == "compile_commands.json" and database.parent.name == "out"
        assert "--sysbuild" not in command

    def test_the_sysbuild_key_adds_the_flag(self, tmp_path, monkeypatch):
        """Upstream Zephyr builds with sysbuild only with the flag."""
        _, command = self._build(tmp_path, monkeypatch, BuildConfig(board="b", sysbuild=True), _database)

        assert "--sysbuild" in command

    def test_a_missing_database_is_an_error(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="compile_commands.json not found"):
            self._build(tmp_path, monkeypatch, BuildConfig(board="b"), lambda out_dir: None)


class TestScalars:
    def test_an_application_directory_named_like_a_number_is_a_name(self, tmp_path):
        """safe_load made `2024` a number and `yes` a bool, and the file read as invalid."""
        _domains(tmp_path, "2024", ["2024", "yes"])
        app = _database(tmp_path / "2024")
        other = _database(tmp_path / "yes")

        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {"2024": app, "yes": other}
        assert ZephyrBuildSystem().application_database(tmp_path) == app

    def test_an_unreadable_file_gives_no_build(self, tmp_path):
        (tmp_path / "domains.yaml").mkdir()

        assert ZephyrBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {}


class TestSourceDir:
    def test_the_build_gives_the_application_directory_to_west(self, tmp_path, monkeypatch):
        _, command = TestBuild()._build(
            tmp_path, monkeypatch, BuildConfig(board="b", source_dir="app"), _database,
        )

        assert "app" in command and command.index("app") < command.index("--")
