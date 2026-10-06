"""The build tree of fw-context: where each build writes, and git does not see it.

Every build that fw-context runs writes into ``.fw-context/build/<variant>/out``
(see ``indexer/build_layout.py``).  The variant name is a directory name, thus
it has the rules of a directory name on Linux, macOS and Windows, and two
names of one directory are an error.

The output is a tool's build artifact, and it must not show up as untracked
in the user's repository.  Two things cover that, and both are tested here:
the rule written inside the build tree, which needs no action from the user
and works for a project that will never run ``init`` again, and the entry
``init`` adds for new projects.
"""
import subprocess
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.build_layout import (
    BUILD_ROOT_REL,
    DEFAULT_VARIANT,
    BuildLayout,
    InvalidVariantName,
    check_variant_names,
    validate_variant_name,
)


class TestPaths:
    def test_the_build_without_variants_has_a_name(self, tmp_path: Path):
        layout = BuildLayout(tmp_path)

        assert layout.variant_dir("") == tmp_path / BUILD_ROOT_REL / DEFAULT_VARIANT
        assert layout.out_dir("") == tmp_path / ".fw-context" / "build" / "default" / "out"

    def test_each_variant_gets_its_own_output_directory(self, tmp_path: Path):
        """One shared directory would make the variants overwrite each other."""
        layout = BuildLayout(tmp_path)

        assert layout.out_dir("nrf52840-dev") != layout.out_dir("nrf54lm20a-dev")
        assert layout.out_dir("nrf52840-dev") == tmp_path / BUILD_ROOT_REL / "nrf52840-dev" / "out"

    def test_a_path_creates_nothing(self, tmp_path: Path):
        """A reader asks for the paths too, and must not make a directory that says "a build was here"."""
        BuildLayout(tmp_path).out_dir("v")

        assert not (tmp_path / ".fw-context").exists()

    def test_a_bad_name_is_refused_before_it_becomes_a_path(self, tmp_path: Path):
        with pytest.raises(InvalidVariantName):
            BuildLayout(tmp_path).out_dir("../escape")


class TestVariantNames:
    @pytest.mark.parametrize("name", ["default", "nrf52840-dev", "esp32dev", "v1.2_rc", "Release", "console", "com10"])
    def test_a_directory_name_is_accepted(self, name: str):
        validate_variant_name(name)

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", ".hidden", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\tb",
         "dev.", "dev ", "nul", "CON", "com1", "lpt9.dev"],
    )
    def test_a_name_that_cannot_be_a_directory_is_refused(self, name: str):
        with pytest.raises(InvalidVariantName):
            validate_variant_name(name)

    def test_two_names_of_one_directory_are_refused(self):
        """macOS and Windows file systems do not tell case apart, thus the two share one directory there."""
        with pytest.raises(InvalidVariantName, match="differ only in case"):
            check_variant_names(["Dev", "dev"])

    def test_a_repeated_name_is_not_a_case_collision(self):
        check_variant_names(["dev", "dev", "prod"])


class TestIgnoreRule:
    def test_the_rule_is_written_inside_the_build_tree(self, tmp_path: Path):
        """The project's own .gitignore is not touched.

        That file belongs to the user, and a tool appending to it on every
        build would be its own kind of noise.
        """
        project_gitignore = tmp_path / ".gitignore"
        project_gitignore.write_text("build/\n", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()

        written = tmp_path / BUILD_ROOT_REL / ".gitignore"
        assert written.read_text(encoding="utf-8").endswith("*\n")
        assert project_gitignore.read_text(encoding="utf-8") == "build/\n"

    def test_it_creates_the_directory_when_missing(self, tmp_path: Path):
        """The rule has to be in place BEFORE a builder writes anything there."""
        assert not (tmp_path / BUILD_ROOT_REL).exists()

        BuildLayout(tmp_path).ensure_ignored()

        assert (tmp_path / BUILD_ROOT_REL / ".gitignore").is_file()

    def test_it_does_not_overwrite_an_existing_rule(self, tmp_path: Path):
        """A user who edited the file keeps their version.

        It runs before every build, so overwriting would undo an edit
        silently and repeatedly.
        """
        marker = tmp_path / BUILD_ROOT_REL / ".gitignore"
        marker.parent.mkdir(parents=True)
        marker.write_text("*\n!keep-this\n", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()

        assert marker.read_text(encoding="utf-8") == "*\n!keep-this\n"

    def test_an_unwritable_project_does_not_stop_a_build(self, tmp_path: Path, caplog):
        """Untidy git status beats a refused build, and the log says so."""
        (tmp_path / ".fw-context").write_text("not a directory", encoding="utf-8")

        BuildLayout(tmp_path).ensure_ignored()  # must not raise

        assert "untracked" in caplog.text


_GIT_MISSING = subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0


@pytest.mark.skipif(_GIT_MISSING, reason="git is not available")
def test_git_really_stops_reporting_the_build_tree(tmp_path: Path):
    """The point of the whole thing, checked against git itself.

    A rule that looks right but that git does not honour would be no fix, so
    this asserts on `git status` and not on the file we wrote.
    """
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, check=True,
        ).stdout

    git("init", "-q")
    # Hermetic: the machine's identity and signing settings must not decide
    # whether this passes.
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")

    # The shape of a real project: config.toml is committed, so git knows
    # about .fw-context/ and reports paths inside it individually.  Without
    # a tracked file in there, `git status` collapses the whole directory to
    # one `?? .fw-context/` line and the test would prove nothing.  The
    # project .gitignore names no fw-context path, as in a project that was
    # initialised by hand.
    config = tmp_path / ".fw-context" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[build]\n", encoding="utf-8")
    git("add", ".fw-context/config.toml")
    git("commit", "-q", "-m", "initial")

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    # A build output as a builder would leave it.
    output = BuildLayout(tmp_path).out_dir("")
    output.mkdir(parents=True)
    (output / "app.elf").write_bytes(b"\x7fELF")

    before = git("status", "--porcelain")
    assert ".fw-context/build/" in before, before

    BuildLayout(tmp_path).ensure_ignored()

    after = git("status", "--porcelain")
    assert ".fw-context" not in after, after
    assert "main.c" in after, "an unrelated file must stay visible"
    assert git("ls-files", ".fw-context/").strip() == ".fw-context/config.toml", (
        "the committed config must stay tracked"
    )


@pytest.mark.skipif(_GIT_MISSING, reason="git is not available")
def test_init_covers_the_build_tree_for_a_new_project(tmp_path: Path):
    """A new project ignores the build output through its own .gitignore.

    The rule inside the build tree covers every project; the entry that
    ``init`` writes is what covers a project whose build tree does not exist
    yet.

    ``init`` writes ``.fw-context/*`` and not one line for each directory,
    thus this asks git what the entries do and does not read their text.
    The shared ``config.toml`` must stay visible — ``test_gitignore_rules``
    holds that whole rule.
    """
    from fw_context_mcp.cli._init import _ensure_gitignore

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _ensure_gitignore(tmp_path, fix=True)

    def ignored(relative: str) -> bool:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "-q", relative], check=False
        ).returncode == 0

    assert ignored(f"{BUILD_ROOT_REL}/default/out/app.elf")
    assert ignored(f"{BUILD_ROOT_REL}/default/out/compile_commands.json")
    assert not ignored(".fw-context/config.toml"), "the shared config must stay committable"



class TestRemoveLegacyOutput:
    """The output of the layout before .fw-context/build/<variant>/out goes, with a log line each."""

    @staticmethod
    def _legacy(root: Path) -> dict[str, Path]:
        """Write each kind of old output, and one new build beside it."""
        fw = root / ".fw-context"
        paths = {
            "autobuild": fw / "autobuild" / "default" / "compile_commands.json",
            "flat": fw / "build" / "compile_commands.json",
            "per_image": fw / "build" / "compile_commands.dev.app.json",
            "sidecar": fw / "build" / "platformio_link.json",
            "deps": fw / "build" / "deps" / "main.d",
            "new": fw / "build" / "default" / "out" / "compile_commands.json",
            "ignore": fw / "build" / ".gitignore",
            "config": fw / "config.toml",
        }
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        return paths

    def test_the_old_output_goes_and_the_new_stays(self, tmp_path, caplog):
        import logging

        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        paths = self._legacy(tmp_path)
        with caplog.at_level(logging.INFO, logger="fw_context_mcp.indexer.build_layout"):
            removed = remove_legacy_output(tmp_path)

        fw = tmp_path / ".fw-context"
        assert set(removed) == {
            fw / "autobuild", paths["flat"], paths["per_image"], paths["sidecar"], fw / "build" / "deps",
        }
        for name in ("new", "ignore", "config"):
            assert paths[name].exists(), name
        assert len([r for r in caplog.records if "older fw-context" in r.getMessage()]) == len(removed)

    def test_a_second_run_removes_nothing(self, tmp_path):
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        self._legacy(tmp_path)
        remove_legacy_output(tmp_path)

        assert remove_legacy_output(tmp_path) == []

    @pytest.mark.parametrize("name", ["deps", "platformio_link.json", "compile_commands.x.json"])
    def test_a_variant_directory_stays_whatever_its_name(self, tmp_path, name):
        """A directory of the build root that holds out/ is a variant, also when its name is an old name."""
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        database = tmp_path / BUILD_ROOT_REL / name / "out" / "compile_commands.json"
        database.parent.mkdir(parents=True)
        database.write_text("[]", encoding="utf-8")

        assert remove_legacy_output(tmp_path) == []
        assert database.exists()

    def test_a_database_that_the_index_reads_stays(self, tmp_path):
        """The old index reads the old copies until a run indexes the build again."""
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        paths = self._legacy(tmp_path)
        remove_legacy_output(tmp_path, keep=[paths["flat"], paths["autobuild"]])

        assert paths["flat"].exists()
        assert paths["autobuild"].exists(), "a directory that holds a kept file stays"
        assert not paths["sidecar"].exists()

    def test_a_kept_file_through_another_path_stays(self, tmp_path):
        """The file system decides if two paths are one file: a link, or another case on macOS and Windows."""
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        paths = self._legacy(tmp_path)
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path / BUILD_ROOT_REL, target_is_directory=True)

        remove_legacy_output(tmp_path, keep=[alias / "compile_commands.json"])

        assert paths["flat"].exists()

    def test_a_symbolic_link_goes_and_not_its_target(self, tmp_path):
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        target = tmp_path / "elsewhere"
        (target / "default").mkdir(parents=True)
        (tmp_path / ".fw-context").mkdir()
        (tmp_path / ".fw-context" / "autobuild").symlink_to(target, target_is_directory=True)

        remove_legacy_output(tmp_path)

        assert not (tmp_path / ".fw-context" / "autobuild").is_symlink()
        assert (target / "default").is_dir()

    def test_a_path_that_cannot_go_gives_a_warning_and_the_rest_goes(self, tmp_path, monkeypatch, caplog):
        import logging

        from fw_context_mcp.indexer import build_layout

        paths = self._legacy(tmp_path)

        def refuse(path, *a, **k):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(build_layout.shutil, "rmtree", refuse)
        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.indexer.build_layout"):
            removed = build_layout.remove_legacy_output(tmp_path)

        assert paths["autobuild"].exists() and paths["deps"].exists()
        assert not paths["flat"].exists() and paths["flat"] in removed
        assert any("Cannot remove" in r.getMessage() for r in caplog.records)

    def test_a_project_without_old_output_is_untouched(self, tmp_path):
        from fw_context_mcp.indexer.build_layout import remove_legacy_output

        assert remove_legacy_output(tmp_path) == []


class TestIndexRemovesLegacyOutput:
    """`fw-context index` removes the old output after a run that ends well."""

    def _run(
        self, tmp_path: Path, monkeypatch, *, compile_commands: str | None = None,
        indexed: Path | None = None, exit_code: int = 0,
    ) -> Path:
        """Run ``cmd_index`` with the build and the index stubbed; return the old flat database.

        *indexed* is a database that a build of the index reads.
        """
        from types import SimpleNamespace

        import fw_context_mcp.config.settings as settings
        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project

        project_id = "0123456789abcdef0123456789abcdef"
        global_cfg = tmp_path / "global.toml"
        global_cfg.write_text("", encoding="utf-8")
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        root = tmp_path / "proj"
        (root / ".fw-context").mkdir(parents=True)
        (root / "CMakeLists.txt").write_text("project(p)\n", encoding="utf-8")
        (root / ".fw-context" / "config.toml").write_text(
            f'[project]\nid = "{project_id}"\n[build]\nsystem = "cmake"\n', encoding="utf-8")
        (root / ".fw-context" / "local.toml").write_text(
            f'[index]\ndb_dir = "{tmp_path / "index"}"\n', encoding="utf-8")
        flat = root / BUILD_ROOT_REL / "compile_commands.json"
        flat.parent.mkdir(parents=True)
        flat.write_text("[]", encoding="utf-8")
        if indexed is not None:
            db_path = tmp_path / "index" / project_id / "index.db"
            db_path.parent.mkdir(parents=True)
            conn = open_db(db_path)
            with transaction(conn):
                upsert_project(conn, project_id, "p", str(root))
                upsert_build_config(conn, "h-old", project_id, str(indexed))
            conn.close()

        monkeypatch.setattr(index_mod, "_run_single", lambda *a, **k: exit_code)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
        monkeypatch.setattr(index_mod, "_refuse_without_build", lambda *a, **k: "")
        args = SimpleNamespace(
            verbose=False, project=str(root), background=False, build=True,
            compile_commands=compile_commands, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )
        assert index_mod.cmd_index(args) == exit_code
        return flat

    @staticmethod
    def _db(tmp_path: Path) -> Path:
        return tmp_path / "index" / "0123456789abcdef0123456789abcdef" / "index.db"

    def test_a_run_that_ends_well_removes_the_old_copy(self, tmp_path, monkeypatch):
        assert not self._run(tmp_path, monkeypatch).exists()

    @staticmethod
    def _direct(tmp_path: Path, db_path: Path) -> Path:
        """Call ``_remove_legacy_output`` on a project with an old flat copy; return the copy."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _remove_legacy_output

        flat = tmp_path / BUILD_ROOT_REL / "compile_commands.json"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_text("[]", encoding="utf-8")
        _remove_legacy_output(SimpleNamespace(compile_commands=None), tmp_path, "proj", db_path)
        return flat

    def test_a_project_without_an_index_gets_no_index_file(self, tmp_path):
        """A run with --no-index leaves a project without an index as it was: get_active_build says no_index."""
        db_path = tmp_path / "db" / "index.db"

        assert not self._direct(tmp_path, db_path).exists()
        assert not db_path.exists()

    def test_a_project_without_old_output_does_not_open_the_index(self, tmp_path, monkeypatch):
        """An open of the index costs an integrity check, and the removal runs after each index run."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _remove_legacy_output
        from fw_context_mcp.indexer import db as db_module

        db_path = tmp_path / "index.db"
        db_path.write_bytes(b"")
        monkeypatch.setattr(db_module, "open_db", lambda *a, **k: pytest.fail("the index was opened"))

        _remove_legacy_output(SimpleNamespace(compile_commands=None), tmp_path, "proj", db_path)

    def test_an_index_that_cannot_be_read_keeps_the_old_output(self, tmp_path, monkeypatch):
        """Without the list of the databases that the index reads, nothing can go."""
        import sqlite3

        from fw_context_mcp.indexer import db as db_module

        db_path = tmp_path / "db" / "index.db"
        db_module.open_db(db_path).close()

        def fail(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db_module, "get_builds_for_scope", fail)

        assert self._direct(tmp_path, db_path).exists()

    def test_a_relative_path_in_the_index_is_relative_to_the_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        flat = self._run(tmp_path, monkeypatch, indexed=BUILD_ROOT_REL / "compile_commands.json")
        assert flat.exists()

    def test_a_failed_run_keeps_the_old_output(self, tmp_path, monkeypatch):
        """The old index stays, and it reads the old copy."""
        assert self._run(tmp_path, monkeypatch, exit_code=1).exists()

    def test_a_copy_that_a_build_of_the_index_reads_stays(self, tmp_path, monkeypatch):
        """A run that --variant narrowed leaves the other variants on their old copies."""
        flat = self._run(tmp_path, monkeypatch, indexed=tmp_path / "proj" / BUILD_ROOT_REL / "compile_commands.json")
        assert flat.exists()

    def test_the_file_on_the_command_line_stays(self, tmp_path, monkeypatch):
        flat = self._run(tmp_path, monkeypatch, compile_commands=str(BUILD_ROOT_REL / "compile_commands.json"))
        assert flat.exists()
