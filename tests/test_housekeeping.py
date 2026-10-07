"""The cleanup of what an older fw-context wrote: files, directories and config keys.

The cleanup changes files of the user, a ``config.toml`` in git among them.
Thus each test here says what goes, and also what stays: the comments and
the other keys of a config, a database that an index reads, a file that
fw-context did not write.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings
from fw_context_mcp import housekeeping
from fw_context_mcp.housekeeping import RETIRED_KEYS, clean, databases_in_use
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project

PROJECT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """An empty global directory with an empty global config, for each test."""
    directory = tmp_path / "home"
    directory.mkdir()
    (directory / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", directory / "config.toml")
    return directory


def _project(root: Path, config: str = "", *, marker: str = "CMakeLists.txt") -> Path:
    """Write a project with *config* in its config.toml; return the path of the config."""
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    if marker:
        (root / marker).write_text("", encoding="utf-8")
    path = root / ".fw-context" / "config.toml"
    path.write_text(config, encoding="utf-8")
    return path


def _index(root: Path, index_dir: Path, database: Path) -> None:
    """Write an index whose one build reads *database*, and name it in local.toml."""
    db_path = index_dir / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "p", str(root))
            upsert_build_config(conn, "h", PROJECT_ID, str(database))
    finally:
        conn.close()
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir}"\n', encoding="utf-8")


@pytest.mark.usefixtures("home")
class TestConfigKeys:
    """Each retired key goes, and the rest of the file stays byte for byte."""

    @pytest.mark.parametrize("retired", RETIRED_KEYS, ids=lambda r: f"{r.section}.{r.key}")
    def test_each_retired_key_goes_and_the_rest_stays(self, tmp_path: Path, retired) -> None:
        config = _project(
            tmp_path,
            "# the team config\n"
            f"[{retired.section}]\n"
            "keep = 1  # stays\n"
            f'{retired.key} = "old"  # goes\n'
            "other = 2\n",
        )

        report = clean(tmp_path)

        assert config.read_text(encoding="utf-8") == (
            f"# the team config\n[{retired.section}]\nkeep = 1  # stays\nother = 2\n"
        )
        assert report.removed_keys == [(config, f'[{retired.section}] {retired.key} = "old"')]

    @pytest.mark.parametrize(
        "retired", [r for r in RETIRED_KEYS if r.in_variants], ids=lambda r: f"variant.{r.key}",
    )
    def test_a_retired_key_goes_from_each_variant(self, tmp_path: Path, retired) -> None:
        config = _project(
            tmp_path,
            '[[build.variants]]\nname = "dev"\nboard = "b"\n'
            f'{retired.key} = "old"\n\n'
            '[[build.variants]]\nname = "rel"\n'
            f'{retired.key} = "old"\n',
        )

        report = clean(tmp_path)

        assert config.read_text(encoding="utf-8") == (
            '[[build.variants]]\nname = "dev"\nboard = "b"\n\n[[build.variants]]\nname = "rel"\n'
        )
        assert [label for _, label in report.removed_keys] == [
            f"[[build.variants]] 'dev' {retired.key} = \"old\"", f"[[build.variants]] 'rel' {retired.key} = \"old\"",
        ]

    def test_the_report_holds_the_line_with_its_value(self, tmp_path: Path) -> None:
        """The user can write the key back from the report: config.toml has git, the report is all else."""
        config = _project(tmp_path, '[index]\nquery_driver = [\n  "/opt/gcc/*",  # arm\n  "/usr/bin/*",\n]\n')

        report = clean(tmp_path)

        assert report.removed_keys == [(config, '[index] query_driver = ["/opt/gcc/*", "/usr/bin/*"]')]
        assert report.lines() == [f'[index] query_driver = ["/opt/gcc/*", "/usr/bin/*"] in {config}']

    @pytest.mark.parametrize(
        ("text", "line"),
        [
            ('[index]\nquery_driver = {a = 1, b = "x"}\n', 'query_driver = {a = 1, b = "x"}'),
            ("[[index.query_driver]]\na = 1\n[[index.query_driver]]\nb = 2\n", "query_driver = [{a = 1}, {b = 2}]"),
            ("[index.query_driver]\na = 1\n[index.query_driver.sub]\nc = [1, 2]\n",
             "query_driver = {a = 1, sub = {c = [1, 2]}}"),
            ("[index]\nquery_driver = 1979-05-27T07:32:00Z\n", "query_driver = 1979-05-27T07:32:00Z"),
        ],
        ids=["inline-table", "array-of-tables", "table", "datetime"],
    )
    def test_the_line_of_each_value_type_is_one_line_of_toml(self, tmp_path: Path, text: str, line: str) -> None:
        """A table in the line became a [table] on more lines, and lost the bounds of an array of tables."""
        import tomllib

        config = _project(tmp_path, text)

        [(path, label)] = clean(tmp_path).removed_keys

        assert (path, label) == (config, f"[index] {line}")
        assert tomllib.loads(line) == tomllib.loads(text)["index"]

    def test_a_dotted_key_goes(self, tmp_path: Path) -> None:
        """TOML lets a key name its table: ``index.query_driver = ...`` at the top."""
        config = _project(tmp_path, 'index.query_driver = ["/x/*"]\nindex.index_refs = true\n')

        clean(tmp_path)

        text = config.read_text(encoding="utf-8")
        assert "query_driver" not in text and "index_refs = true" in text

    def test_a_crlf_file_stays_crlf(self, tmp_path: Path) -> None:
        """A checkout on Windows has CRLF; a rewrite to LF gives a diff of each line."""
        config = _project(tmp_path)
        config.write_bytes(b'[llm]\r\nallow_external_llm = true\r\nmodel = "m"\r\n')

        clean(tmp_path)

        assert config.read_bytes() == b'[llm]\r\nmodel = "m"\r\n'

    def test_the_local_and_the_global_config_are_cleaned(self, tmp_path: Path, home: Path) -> None:
        _project(tmp_path)
        local = tmp_path / ".fw-context" / "local.toml"
        local.write_text('[index]\nquery_driver_auto = true\n', encoding="utf-8")
        (home / "config.toml").write_text('[llm]\nallow_external_llm = false\n', encoding="utf-8")

        report = clean(tmp_path)

        assert "query_driver_auto" not in local.read_text(encoding="utf-8")
        assert "allow_external_llm" not in (home / "config.toml").read_text(encoding="utf-8")
        assert {path for path, _ in report.removed_keys} == {local, home / "config.toml"}

    def test_a_file_without_a_retired_key_is_not_written(self, tmp_path: Path) -> None:
        config = _project(tmp_path, "[index]\nindex_refs = true\n")
        before = config.stat().st_mtime_ns

        assert clean(tmp_path).empty
        assert config.stat().st_mtime_ns == before

    def test_a_file_that_does_not_parse_is_a_failure_and_stays(self, tmp_path: Path) -> None:
        config = _project(tmp_path, "[index\nquery_driver = 1\n")

        report = clean(tmp_path)

        assert config.read_text(encoding="utf-8") == "[index\nquery_driver = 1\n"
        assert [path for path, _ in report.failures][0] == config

    def test_a_write_that_fails_is_a_failure_and_no_removed_key(self, tmp_path: Path, monkeypatch, caplog) -> None:
        config = _project(tmp_path, "[index]\nquery_driver = 1\n")

        def refuse(path, *a, **k):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(housekeeping, "write_text_atomic", refuse)
        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.housekeeping"):
            report = clean(tmp_path)

        assert report.removed_keys == []
        assert [path for path, _ in report.failures] == [config]
        assert "query_driver" in config.read_text(encoding="utf-8")
        assert any("Cannot clean" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("home")
class TestBackup:
    """local.toml and the global config are not in git: the old text goes to <name>.bak."""

    def test_local_toml_gets_a_copy_of_its_old_text(self, tmp_path: Path) -> None:
        _project(tmp_path)
        local = tmp_path / ".fw-context" / "local.toml"
        old = b"# mine\r\n[index]\r\nquery_driver_auto = true\r\nindex_refs = true\r\n"
        local.write_bytes(old)

        clean(tmp_path)

        assert (tmp_path / ".fw-context" / "local.toml.bak").read_bytes() == old
        assert local.read_bytes() == b"# mine\r\n[index]\r\nindex_refs = true\r\n"

    def test_the_global_config_gets_a_copy(self, tmp_path: Path, home: Path) -> None:
        old = "[llm]\nallow_external_llm = false\n"
        (home / "config.toml").write_text(old, encoding="utf-8")

        clean(None)

        assert (home / "config.toml.bak").read_text(encoding="utf-8") == old

    def test_config_toml_of_the_project_gets_no_copy(self, tmp_path: Path) -> None:
        """git holds its old text."""
        _project(tmp_path, "[index]\nquery_driver = 1\n")

        clean(tmp_path)

        assert not (tmp_path / ".fw-context" / "config.toml.bak").exists()

    def test_a_new_cleanup_replaces_the_copy(self, tmp_path: Path, home: Path) -> None:
        (home / "config.toml").write_text("[llm]\nallow_external_llm = false\n", encoding="utf-8")
        clean(None)
        second = "[index]\nquery_driver = 1\n"
        (home / "config.toml").write_text(second, encoding="utf-8")

        clean(None)

        assert (home / "config.toml.bak").read_text(encoding="utf-8") == second

    def test_a_file_without_a_retired_key_gets_no_copy(self, tmp_path: Path, home: Path) -> None:
        (home / "config.toml").write_text("[llm]\nmodel = \"m\"\n", encoding="utf-8")

        clean(None)

        assert not (home / "config.toml.bak").exists()

    def test_a_dry_run_writes_no_copy(self, tmp_path: Path, home: Path) -> None:
        (home / "config.toml").write_text("[llm]\nallow_external_llm = false\n", encoding="utf-8")

        clean(None, dry_run=True)

        assert not (home / "config.toml.bak").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits and symbolic links")
    def test_the_copy_has_the_mode_of_the_config(self, tmp_path: Path, home: Path) -> None:
        """The copy holds the same text, a token too."""
        config = home / "config.toml"
        config.write_text("[llm]\nallow_external_llm = false\n", encoding="utf-8")
        config.chmod(0o600)

        clean(None)

        assert stat.S_IMODE((home / "config.toml.bak").stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="symbolic links")
    def test_the_copy_of_a_link_is_beside_the_link(self, tmp_path: Path, home: Path) -> None:
        """A copy beside the target would go into the dotfiles repository, and into its git."""
        target = tmp_path / "dotfiles" / "config.toml"
        target.parent.mkdir()
        target.write_text("[llm]\nallow_external_llm = false\n", encoding="utf-8")
        (home / "config.toml").unlink()
        (home / "config.toml").symlink_to(target)

        clean(None)

        assert (home / "config.toml.bak").is_file() and not (home / "config.toml.bak").is_symlink()
        assert not (target.parent / "config.toml.bak").exists()

    def test_a_copy_that_cannot_be_written_keeps_the_config(self, tmp_path: Path, home: Path, monkeypatch) -> None:
        old = "[llm]\nallow_external_llm = false\n"
        (home / "config.toml").write_text(old, encoding="utf-8")
        real = housekeeping.write_text_atomic

        def refuse_the_copy(path, *a, **k):
            if path.name.endswith(".bak"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *a, **k)

        monkeypatch.setattr(housekeeping, "write_text_atomic", refuse_the_copy)

        report = clean(None)

        assert (home / "config.toml").read_text(encoding="utf-8") == old
        assert report.removed_keys == [] and [path for path, _ in report.failures] == [home / "config.toml"]


@pytest.mark.usefixtures("home")
class TestOldDefaultDatabase:
    """``[index] compile_commands`` with an old default goes only when it has no effect."""

    @pytest.mark.parametrize("value", ["compile_commands.json", ".fw-context/build/compile_commands.json"])
    def test_it_goes_for_a_build_that_fw_context_runs(self, tmp_path: Path, value: str) -> None:
        config = _project(tmp_path, f'[index]\ncompile_commands = "{value}"\nindex_refs = true\n')

        report = clean(tmp_path)

        assert config.read_text(encoding="utf-8") == "[index]\nindex_refs = true\n"
        assert report.removed_keys == [(config, f'[index] compile_commands = "{value}"')]

    def test_it_stays_for_a_stub_build(self, tmp_path: Path) -> None:
        """Without a build of fw-context, the value names the only database of the project."""
        text = '[index]\ncompile_commands = "compile_commands.json"\n'
        config = _project(tmp_path, text, marker=".cproject")

        assert clean(tmp_path).empty
        assert config.read_text(encoding="utf-8") == text

    def test_another_value_stays(self, tmp_path: Path) -> None:
        """A file of the user (a CI database) is a choice, not an old default."""
        text = '[index]\ncompile_commands = "ci/compile_commands.json"\n'
        config = _project(tmp_path, text)

        assert clean(tmp_path).empty
        assert config.read_text(encoding="utf-8") == text

    def test_the_value_in_the_global_config_stays(self, tmp_path: Path, home: Path) -> None:
        """The global value applies to each project, a stub project too: one CMake project cannot decide."""
        text = '[index]\ncompile_commands = "compile_commands.json"\n'
        (home / "config.toml").write_text(text, encoding="utf-8")
        _project(tmp_path)

        assert clean(tmp_path).empty
        assert (home / "config.toml").read_text(encoding="utf-8") == text

    def test_an_old_default_over_a_value_of_the_user_stays(self, tmp_path: Path) -> None:
        """Without the key in local.toml, the index read the CI database of config.toml."""
        _project(tmp_path, '[index]\ncompile_commands = "ci/compile_commands.json"\n')
        local = tmp_path / ".fw-context" / "local.toml"
        local.write_text('[index]\ncompile_commands = "compile_commands.json"\n', encoding="utf-8")

        assert clean(tmp_path).empty
        assert "compile_commands" in local.read_text(encoding="utf-8")

    def test_an_old_default_under_a_value_of_the_user_goes(self, tmp_path: Path) -> None:
        """local.toml decides; the key in config.toml has no effect, and the value of local.toml stays."""
        config = _project(tmp_path, '[index]\ncompile_commands = "compile_commands.json"\n')
        local = tmp_path / ".fw-context" / "local.toml"
        local.write_text('[index]\ncompile_commands = "ci/compile_commands.json"\n', encoding="utf-8")

        report = clean(tmp_path)

        assert report.removed_keys == [(config, '[index] compile_commands = "compile_commands.json"')]
        assert "ci/compile_commands.json" in local.read_text(encoding="utf-8")

    def test_old_defaults_in_the_two_project_files_go(self, tmp_path: Path) -> None:
        config = _project(tmp_path, '[index]\ncompile_commands = ".fw-context/build/compile_commands.json"\n')
        local = tmp_path / ".fw-context" / "local.toml"
        local.write_text('[index]\ncompile_commands = "compile_commands.json"\n', encoding="utf-8")

        report = clean(tmp_path)

        assert {path for path, _ in report.removed_keys} == {config, local}

    def test_an_old_default_over_a_global_config_that_cannot_be_read_stays(self, tmp_path: Path, home: Path) -> None:
        """The value of the global layer is not known, thus a removal can change the result."""
        (home / "config.toml").write_text("[index\n", encoding="utf-8")
        text = '[index]\ncompile_commands = "compile_commands.json"\n'
        config = _project(tmp_path, text)

        clean(tmp_path)

        assert config.read_text(encoding="utf-8") == text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits and symbolic links")
@pytest.mark.usefixtures("home")
class TestTheWriteOfAConfig:
    """A config can hold a token: the rewrite keeps its mode, its link, and a read-only file."""

    @pytest.mark.parametrize("mode", [0o600, 0o640])
    def test_the_mode_stays(self, tmp_path: Path, mode: int) -> None:
        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        config.chmod(mode)

        clean(tmp_path)

        assert "query_driver" not in config.read_text(encoding="utf-8")
        assert stat.S_IMODE(config.stat().st_mode) == mode

    def test_a_link_stays_a_link_and_its_target_changes(self, tmp_path: Path) -> None:
        target = tmp_path / "dotfiles" / "config.toml"
        target.parent.mkdir()
        target.write_text("[index]\nquery_driver = 1\nindex_refs = true\n", encoding="utf-8")
        config = _project(tmp_path / "proj")
        config.unlink()
        config.symlink_to(target)

        report = clean(tmp_path / "proj")

        assert config.is_symlink()
        assert target.read_text(encoding="utf-8") == "[index]\nindex_refs = true\n"
        assert report.removed_keys == [(config, "[index] query_driver = 1")]

    def test_a_read_only_file_stays_also_in_a_dry_run(self, tmp_path: Path) -> None:
        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        config.chmod(0o444)

        report = clean(tmp_path, dry_run=True)

        assert report.removed_keys == []
        assert [path for path, _ in report.failures] == [config]

    def test_a_temporary_file_of_a_dead_writer_with_the_same_pid(self, tmp_path: Path) -> None:
        """The PID of a dead writer can come back: its file has the name of this process."""
        from fw_context_mcp.utils import owner_token, write_text_atomic

        target = tmp_path / "config.toml"
        target.write_text("old\n", encoding="utf-8")
        stale = tmp_path / f".config.toml.{owner_token()}.tmp"
        stale.write_text("stale", encoding="utf-8")
        stale.chmod(0o644)

        write_text_atomic(target, "new\n", mode=0o600)

        assert target.read_text(encoding="utf-8") == "new\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert not stale.exists()

    def test_a_read_only_file_stays_and_is_a_failure(self, tmp_path: Path) -> None:
        text = "[index]\nquery_driver = 1\n"
        config = _project(tmp_path, text)
        config.chmod(0o444)

        report = clean(tmp_path)

        assert config.read_text(encoding="utf-8") == text
        assert report.removed_keys == []
        assert [path for path, _ in report.failures] == [config]


class TestTheHomeDirectoryAsAProject:
    """In the home directory (a dotfiles repository), the config of the project is the global config."""

    @staticmethod
    def _home_project(tmp_path: Path, monkeypatch, text: str) -> tuple[Path, Path]:
        root = tmp_path / "user"
        config = _project(root, text)
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", config)
        return root, config

    def test_the_global_value_of_compile_commands_stays(self, tmp_path: Path, monkeypatch) -> None:
        text = '[index]\ncompile_commands = "compile_commands.json"\n'
        root, config = self._home_project(tmp_path, monkeypatch, text)

        assert clean(root).empty
        assert config.read_text(encoding="utf-8") == text

    def test_a_retired_key_is_reported_once(self, tmp_path: Path, monkeypatch) -> None:
        root, config = self._home_project(tmp_path, monkeypatch, "[index]\nquery_driver = 1\n")

        assert clean(root, dry_run=True).removed_keys == [(config, "[index] query_driver = 1")]
        assert clean(root).removed_keys == [(config, "[index] query_driver = 1")]


@pytest.mark.usefixtures("home")
class TestErrorsStayInTheReport:
    """A cleanup that cannot read a path must not stop `fw-context index`."""

    def test_a_config_that_cannot_be_examined(self, tmp_path: Path, monkeypatch) -> None:
        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        is_file = Path.is_file

        def refuse(path: Path) -> bool:
            if path == config:
                raise PermissionError(13, "Permission denied", str(path))
            return is_file(path)

        monkeypatch.setattr(Path, "is_file", refuse)

        assert [path for path, _ in clean(tmp_path).failures] == [config]

    def test_a_directory_in_the_place_of_a_config(self, tmp_path: Path) -> None:
        """The value of that layer is not known, and the user must see why."""
        _project(tmp_path)
        local = tmp_path / ".fw-context" / "local.toml"
        local.mkdir()

        assert clean(tmp_path).failures == [(local, "this is not a file")]

    def test_a_config_whose_presence_cannot_be_found(self, tmp_path: Path, monkeypatch) -> None:
        """Python 3.11 and 3.12: exists() raises for a directory without search permission."""
        _project(tmp_path)
        local = tmp_path / ".fw-context" / "local.toml"
        is_file, exists = Path.is_file, Path.exists

        def no_file(path: Path) -> bool:
            return False if path == local else is_file(path)

        def refuse(path: Path, *a, **k) -> bool:
            if path == local:
                raise PermissionError(13, "Permission denied", str(path))
            return exists(path, *a, **k)

        monkeypatch.setattr(Path, "is_file", no_file)
        monkeypatch.setattr(Path, "exists", refuse)

        assert [path for path, _ in clean(tmp_path).failures] == [local]

    def test_a_path_that_cannot_be_examined(self, tmp_path: Path, home: Path, monkeypatch) -> None:
        retired = home / "platformio-shared.ini"
        exists = Path.exists

        def refuse(path: Path, *a, **k) -> bool:
            if path == retired:
                raise PermissionError(13, "Permission denied", str(path))
            return exists(path, *a, **k)

        monkeypatch.setattr(Path, "exists", refuse)

        assert [path for path, _ in clean(None).failures] == [retired]

    def test_a_build_system_that_cannot_be_found_keeps_the_key(self, tmp_path: Path, monkeypatch) -> None:
        from fw_context_mcp.indexer import build

        text = '[index]\ncompile_commands = "compile_commands.json"\n'
        config = _project(tmp_path, text)

        def fail(*a, **k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(build, "detect_build_system", fail)

        assert clean(tmp_path).empty
        assert config.read_text(encoding="utf-8") == text


class TestFiles:
    """The files of an older layout go; a database that is in use stays."""

    def test_an_old_toolchains_file_goes(self, tmp_path: Path, home: Path) -> None:
        _project(tmp_path)
        toolchains = tmp_path / ".fw-context" / "toolchains.toml"
        toolchains.write_text("[x]\n", encoding="utf-8")

        report = clean(tmp_path)

        assert not toolchains.exists()
        assert report.removed_paths == [toolchains]

    def test_the_toolchains_file_goes_also_when_the_index_cannot_be_read(self, tmp_path: Path, home, monkeypatch) -> None:
        """Only the build output can be a database that an index reads."""
        _project(tmp_path)
        toolchains = tmp_path / ".fw-context" / "toolchains.toml"
        toolchains.write_text("[x]\n", encoding="utf-8")
        flat = tmp_path / ".fw-context" / "build" / "compile_commands.json"
        flat.parent.mkdir(parents=True)
        flat.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(housekeeping, "databases_in_use", lambda root: None)

        report = clean(tmp_path)

        assert not toolchains.exists() and flat.exists()
        assert [path for path, _ in report.failures] == [tmp_path]

    def test_a_retired_global_file_goes_and_the_others_stay(self, tmp_path: Path, home: Path) -> None:
        (home / "platformio-shared.ini").write_text("[env]\n", encoding="utf-8")
        (home / "llm-debug.jsonl").write_text("{}\n", encoding="utf-8")

        report = clean(None)

        assert not (home / "platformio-shared.ini").exists()
        assert (home / "llm-debug.jsonl").exists() and (home / "config.toml").exists()
        assert report.removed_paths == [home / "platformio-shared.ini"]

    def test_a_dry_run_changes_nothing_and_gives_the_same_list(self, tmp_path: Path, home: Path) -> None:
        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        toolchains = tmp_path / ".fw-context" / "toolchains.toml"
        toolchains.write_text("[x]\n", encoding="utf-8")
        (home / "platformio-shared.ini").write_text("", encoding="utf-8")

        dry = clean(tmp_path, dry_run=True)

        assert "query_driver" in config.read_text(encoding="utf-8")
        assert toolchains.exists() and (home / "platformio-shared.ini").exists()
        assert dry.lines() == clean(tmp_path).lines()
        assert clean(tmp_path).empty


class TestDatabasesInUse:
    def test_the_builds_of_the_index_and_the_configured_file(self, tmp_path: Path, home) -> None:
        root = tmp_path / "proj"
        _project(root, f'[project]\nid = "{PROJECT_ID}"\n')
        _index(root, tmp_path / "index", root / "build" / "compile_commands.json")

        assert databases_in_use(root) == [root / "build" / "compile_commands.json"]

    def test_a_stub_build_keeps_its_configured_database(self, tmp_path: Path, home) -> None:
        """Before the first index of a stub project, the config is all that names its database."""
        root = tmp_path / "proj"
        _project(root, marker=".cproject")

        assert databases_in_use(root) == [(root / ".fw-context" / "build" / "compile_commands.json").resolve()]

    def test_a_project_without_an_id_has_no_index(self, tmp_path: Path, home) -> None:
        _project(tmp_path)

        assert databases_in_use(tmp_path) == []

    def test_an_index_that_cannot_be_read_gives_none(self, tmp_path: Path, home, monkeypatch) -> None:
        import sqlite3

        from fw_context_mcp.indexer import db as db_module

        root = tmp_path / "proj"
        _project(root, f'[project]\nid = "{PROJECT_ID}"\n')
        _index(root, tmp_path / "index", root / "cc.json")

        def fail(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db_module, "get_builds_for_scope", fail)

        assert databases_in_use(root) is None

    def test_the_index_is_read_without_a_migration(self, tmp_path: Path, home, monkeypatch) -> None:
        """doctor, `cleanup --dry-run` and get_environment_status read it: open_db would migrate it."""
        from fw_context_mcp.indexer import db as db_module
        from fw_context_mcp.indexer.db import _connection

        root = tmp_path / "proj"
        _project(root, f'[project]\nid = "{PROJECT_ID}"\n')
        _index(root, tmp_path / "index", root / "cc.json")
        for module in (db_module, _connection):
            monkeypatch.setattr(module, "open_db", lambda *a, **k: pytest.fail("open_db migrates the index"))
            monkeypatch.setattr(module, "ensure_schema", lambda *a, **k: pytest.fail("a migration"), raising=False)

        assert databases_in_use(root) == [root / "cc.json"]

    def test_an_index_of_an_older_schema_keeps_the_copies(self, tmp_path: Path, home) -> None:
        """The query needs a column that an old schema does not have: no database may go."""
        import sqlite3

        root = tmp_path / "proj"
        _project(root, f'[project]\nid = "{PROJECT_ID}"\n')
        db_path = tmp_path / "index" / PROJECT_ID / "index.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE build_configs (project_id TEXT, compile_commands_path TEXT)")
        conn.commit()
        conn.close()
        (root / ".fw-context" / "local.toml").write_text(
            f'[index]\ndb_dir = "{tmp_path / "index"}"\n', encoding="utf-8")

        assert databases_in_use(root) is None


@pytest.mark.usefixtures("home")
class TestIndexRun:
    def test_a_database_that_the_index_reads_stays(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        _project(root, f'[project]\nid = "{PROJECT_ID}"\n')
        flat = root / ".fw-context" / "build" / "compile_commands.json"
        flat.parent.mkdir(parents=True)
        flat.write_text("[]", encoding="utf-8")
        unused = root / ".fw-context" / "build" / "compile_commands.dev.app.json"
        unused.write_text("[]", encoding="utf-8")
        _index(root, tmp_path / "index", flat)

        report = clean(root)

        assert flat.exists()
        assert not unused.exists() and report.removed_paths == [unused]


class TestDoctor:
    def test_the_check_lists_and_removes_nothing(self, tmp_path: Path, home: Path) -> None:
        from fw_context_mcp.deps._checks import check_obsolete_files

        config = _project(tmp_path, "[index]\nquery_driver = 1\n")

        result = check_obsolete_files(tmp_path)

        assert result.status == "degraded" and not result.critical
        assert result.fix_cmd == "fw-context doctor --fix"
        assert "[index] query_driver" in result.message
        assert "query_driver" in config.read_text(encoding="utf-8")

    def test_the_fix_removes_and_the_check_is_then_ok(self, tmp_path: Path, home: Path) -> None:
        from fw_context_mcp.deps._checks import check_obsolete_files
        from fw_context_mcp.deps._fixes import FIXABLE, fix_obsolete_files

        _project(tmp_path, "[index]\nquery_driver = 1\n")

        assert FIXABLE["obsolete-files"] is fix_obsolete_files
        ok, message = fix_obsolete_files(None, project_root=tmp_path)

        assert ok, message
        assert check_obsolete_files(tmp_path).status == "ok"

    def test_a_failure_alone_gives_no_fix_command(self, tmp_path: Path, home: Path) -> None:
        """The fix cannot repair a config that does not parse: the user must."""
        from fw_context_mcp.deps._checks import check_obsolete_files

        config = _project(tmp_path, "[index\n")

        result = check_obsolete_files(tmp_path)

        assert result.status == "degraded" and result.fix_cmd is None
        assert result.message.startswith(f"cannot clean {config}: ")
        assert "item(s)" not in result.message

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_a_read_only_config_gives_no_fix_command(self, tmp_path: Path, home: Path) -> None:
        """The dry run asks if the file can be written: else doctor offered a fix that failed."""
        from fw_context_mcp.deps._checks import check_obsolete_files

        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        config.chmod(0o444)

        result = check_obsolete_files(tmp_path)

        assert result.fix_cmd is None
        assert f"cannot clean {config}: the owner has no write permission" in result.message

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_a_fix_that_fails_in_part_names_what_went(self, tmp_path: Path, home: Path) -> None:
        """A removed key in config.toml needs a commit, thus "fix failed" alone is not enough."""
        from fw_context_mcp.deps._fixes import fix_obsolete_files

        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        local = tmp_path / ".fw-context" / "local.toml"
        local.write_text("[index]\nquery_driver_auto = true\n", encoding="utf-8")
        local.chmod(0o444)

        ok, message = fix_obsolete_files(None, project_root=tmp_path)

        assert not ok
        assert f"removed 1 item(s) of an older fw-context: [index] query_driver = 1 in {config}" in message
        assert f"cannot clean {local}: the owner has no write permission" in message

    def test_doctor_fix_cleans_and_reports_ok(self, tmp_path: Path, home: Path) -> None:
        """The path of `doctor --fix` and `init`: the full check, the fix, and the check again."""
        from fw_context_mcp.deps import run_fixes, run_full_check

        config = _project(tmp_path, "[index]\nquery_driver = 1\n")
        before = run_full_check(project_root=tmp_path, subset={"obsolete-files"})

        after = run_fixes(before, project_root=tmp_path)

        assert [(r.name, r.status) for r in before] == [("obsolete-files", "degraded")]
        assert [(r.name, r.status) for r in after] == [("obsolete-files", "ok")], after[0].message
        assert "query_driver" not in config.read_text(encoding="utf-8")


class TestCleanupCommand:
    def test_a_dry_run_prints_the_list_and_changes_nothing(self, tmp_path: Path, home: Path, capsys) -> None:
        from fw_context_mcp.cli._cleanup import cmd_cleanup

        config = _project(tmp_path, "[index]\nquery_driver = 1\n")

        assert cmd_cleanup(Namespace(project=str(tmp_path), dry_run=True)) == 0

        assert f"would remove: [index] query_driver = 1 in {config}" in capsys.readouterr().out
        assert "query_driver" in config.read_text(encoding="utf-8")

    def test_a_run_removes_and_a_second_run_finds_nothing(self, tmp_path: Path, home: Path, capsys) -> None:
        from fw_context_mcp.cli._cleanup import cmd_cleanup

        _project(tmp_path, "[index]\nquery_driver = 1\n")

        assert cmd_cleanup(Namespace(project=str(tmp_path), dry_run=False)) == 0
        assert "removed: [index] query_driver = 1 in " in capsys.readouterr().out
        assert cmd_cleanup(Namespace(project=str(tmp_path), dry_run=False)) == 0
        assert capsys.readouterr().out == "nothing to clean\n"

    def test_a_failure_gives_exit_code_1_and_one_line(self, tmp_path: Path, home: Path, capsys, caplog) -> None:
        from fw_context_mcp.cli._cleanup import cmd_cleanup

        _project(tmp_path, "[index\n")

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.housekeeping"):
            assert cmd_cleanup(Namespace(project=str(tmp_path), dry_run=False)) == 1

        assert capsys.readouterr().out.count("cannot clean") == 1
        assert not caplog.records, "the command prints the failure; a log line on stderr showed it two times"


class TestHome:
    def test_the_variable_moves_the_global_directory(self, tmp_path: Path, monkeypatch) -> None:
        from fw_context_mcp.utils import fw_context_home

        monkeypatch.setenv("FW_CONTEXT_HOME", str(tmp_path / "h"))
        assert fw_context_home() == tmp_path / "h"

    def test_without_the_variable_it_is_in_the_home_directory(self, monkeypatch) -> None:
        from fw_context_mcp.utils import fw_context_home

        monkeypatch.delenv("FW_CONTEXT_HOME", raising=False)
        assert fw_context_home() == Path.home() / ".fw-context"

    def test_the_global_config_of_a_new_process_follows_the_variable(self, tmp_path: Path) -> None:
        """The path is fixed at import; a CLI process that a test starts must get the variable."""
        env = {**os.environ, "FW_CONTEXT_HOME": str(tmp_path / "h")}
        code = "import fw_context_mcp.config.settings as s; print(s._GLOBAL_CONFIG_PATH)"
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, timeout=60,
        )

        assert result.stdout.strip() == str(tmp_path / "h" / "config.toml")
