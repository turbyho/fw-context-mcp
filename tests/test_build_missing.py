"""An index run builds first when the build of compile_commands.json is gone.

The parse asks the compiler of each unit in the ``directory`` of its entry,
as the build runs it (``_driver_query``).  Without the build each unit gets
no answer of its compiler: wrong system headers and macros.

A build of fw-context keeps its database in its output directory,
``.fw-context/build/<variant>/out`` (see ``build_layout``), thus a removed
build takes the database with it.  A database whose ``directory`` is gone
while the file stays is the other form of a missing build.  Thus:

- A run of the user turns ``--build`` on, as the user would.
- A background run plans a build (``_plan_auto_build``), as it does for a
  branch switch.
- A run that builds nothing does not index: it stops with a clear error, and
  each MCP answer carries the same text.  A build that fw-context cannot run
  (a stub such as STM32CubeIDE) must run outside of fw-context.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.autobuild import build_missing_reason
from fw_context_mcp.indexer.build_layout import BuildLayout
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project

PROJECT_ID = "0123456789abcdef0123456789abcdef"


def _write_cc(path: Path, directory: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"directory": str(directory), "file": "main.c",
                                 "arguments": ["cc", "-c", "main.c"]}]), encoding="utf-8")
    return path


def _out(root: Path) -> Path:
    """The output directory of the build without variants."""
    return BuildLayout(root).out_dir("")


def _default_cc(root: Path) -> Path:
    """The database that a CMake build of fw-context writes, and a run without a file reads."""
    return _out(root) / "compile_commands.json"


def _built(root: Path) -> Path:
    """A build of fw-context that is there: the database in out/, its units compiled in out/."""
    return _write_cc(_default_cc(root), _out(root))


def _gone(root: Path) -> Path:
    """A build that the user removed: no output directory, thus no database either."""
    return _default_cc(root)


def _half_gone(root: Path) -> Path:
    """A database that stays while the directory that its units name is gone."""
    return _write_cc(_default_cc(root), _out(root) / "app")


@pytest.fixture
def global_config(tmp_path: Path, monkeypatch) -> None:
    """Keep load() away from the global config of the operator (fixed at import)."""
    import fw_context_mcp.config.settings as settings

    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)


class TestTheReason:
    def test_a_present_build_gives_no_reason(self, tmp_path: Path) -> None:
        assert build_missing_reason(_write_cc(tmp_path / "cc.json", tmp_path)) == ""

    def test_a_missing_file_is_a_reason(self, tmp_path: Path) -> None:
        assert "does not exist" in build_missing_reason(tmp_path / "cc.json")

    def test_a_missing_build_directory_is_a_reason(self, tmp_path: Path) -> None:
        reason = build_missing_reason(_write_cc(tmp_path / "cc.json", tmp_path / "build"))
        assert f"the build directory {tmp_path / 'build'}" in reason

    def test_a_file_that_does_not_parse_is_left_to_the_run(self, tmp_path: Path) -> None:
        """The run that reads it reports the real error; this check must not hide it."""
        broken = tmp_path / "cc.json"
        broken.write_text("{not json", encoding="utf-8")
        assert build_missing_reason(broken) == ""


class TestCanRunTheBuild:
    def test_a_registered_builder_can(self) -> None:
        from fw_context_mcp.indexer.build import BuildConfig, can_run_build

        assert can_run_build(BuildConfig(), "cmake")

    @pytest.mark.parametrize("system", ["stm32cubeide", "ti-ccs"])
    def test_a_stub_cannot(self, system: str) -> None:
        """Its build() only raises with instructions; the build runs in the IDE."""
        from fw_context_mcp.indexer.build import BuildConfig, can_run_build

        assert not can_run_build(BuildConfig(), system)

    def test_a_command_can_without_a_system(self) -> None:
        """``[build] command`` runs whatever the system is."""
        from fw_context_mcp.indexer.build import BuildConfig, can_run_build

        assert can_run_build(BuildConfig(command="bear -- make"), None)
        assert can_run_build(BuildConfig(command="bear -- make"), "stm32cubeide")

    def test_an_unknown_system_cannot(self) -> None:
        from fw_context_mcp.indexer.build import BuildConfig, can_run_build

        assert not can_run_build(BuildConfig(), "no-such-system")
        assert not can_run_build(BuildConfig(), None)


def _project(root: Path, index_dir: Path, cc: Path, system: str = "cmake") -> Path:
    """A project whose index names *cc* as its compile_commands.json."""
    db_dir = index_dir / PROJECT_ID
    db_dir.mkdir(parents=True)
    conn = open_db(db_dir / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "p", str(root))
            upsert_build_config(conn, "ch", PROJECT_ID, str(cc), description="", manifest_verification="full")
    finally:
        conn.close()
    config_dir = root / ".fw-context"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n[build]\nsystem = "{system}"\n', encoding="utf-8")
    (config_dir / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir}"\n', encoding="utf-8")
    return db_dir / "index.db"


@pytest.mark.usefixtures("global_config")
class TestTheCheckedFile:
    """The CLI and get_active_build ask one function, thus they cannot disagree."""

    @staticmethod
    def _cfg(root: Path, build_table: str = 'system = "cmake"'):
        from fw_context_mcp.config import load as load_config

        (root / ".fw-context").mkdir(parents=True, exist_ok=True)
        (root / ".fw-context" / "config.toml").write_text(f"[build]\n{build_table}\n", encoding="utf-8")
        return load_config(project_root=root)

    def test_without_an_index_the_default_file_is_checked(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.build import checked_compile_commands

        cfg = self._cfg(tmp_path)
        assert checked_compile_commands(tmp_path, cfg, None) == _default_cc(tmp_path)

    def test_an_index_of_the_default_file_is_checked(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.build import checked_compile_commands

        cfg = self._cfg(tmp_path)
        default = _default_cc(tmp_path)
        assert checked_compile_commands(tmp_path, cfg, default) == default

    def test_a_removed_build_with_a_deeper_database_is_checked(self, tmp_path: Path) -> None:
        """Zephyr without sysbuild writes out/zephyr/compile_commands.json.

        After `rm -rf .fw-context/build`, the build cannot say where its
        database was.  The path of the index can, and it is in out/.
        """
        from fw_context_mcp.indexer.build import checked_compile_commands

        cfg = self._cfg(tmp_path, 'system = "zephyr"\nboard = "b"')
        indexed = _out(tmp_path) / "zephyr" / "compile_commands.json"
        assert checked_compile_commands(tmp_path, cfg, indexed) == indexed
        assert "does not exist" in build_missing_reason(indexed)

    def test_an_index_of_another_file_is_not_checked(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.build import checked_compile_commands

        cfg = self._cfg(tmp_path)
        assert checked_compile_commands(tmp_path, cfg, tmp_path / "ci" / "cc.json") is None

    def test_build_variants_are_not_checked(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.build import checked_compile_commands

        cfg = self._cfg(tmp_path, 'system = "zephyr"\n[[build.variants]]\nname = "a"\nboard = "b"')
        assert checked_compile_commands(tmp_path, cfg, None) is None


@pytest.mark.usefixtures("global_config")
class TestTheBackgroundPlan:
    def _plan(self, root: Path, db_path: Path, *, background: bool = True):
        from fw_context_mcp.cli._index import _plan_auto_build
        from fw_context_mcp.config import load as load_config

        return _plan_auto_build(
            root, db_path, load_config(project_root=root), None, background=background
        )

    def test_a_run_of_the_user_plans_no_build_for_a_missing_build(self, tmp_path: Path) -> None:
        """A run of the user builds as --build does (`_build_if_missing`, `_resolve_compile_commands`)."""
        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _gone(root))
        assert self._plan(root, db_path, background=False) == ([], None, "")

    def test_an_index_of_another_file_plans_no_build(self, tmp_path: Path) -> None:
        """An index of an explicit file: a build would replace that file with its own.

        The build of the default file is gone, but the index did not come
        from it.
        """
        root = tmp_path / "proj"
        root.mkdir()
        explicit = _write_cc(tmp_path / "ci" / "compile_commands.json", tmp_path / "ci")
        db_path = _project(root, tmp_path / "index", explicit)
        assert self._plan(root, db_path) == ([], None, "")

    def test_a_removed_build_plans_a_build(self, tmp_path: Path) -> None:
        """`rm -rf .fw-context/build/default/out` removes the database too."""
        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _gone(root))
        keys, config, reason = self._plan(root, db_path)
        assert keys == ["build-missing"]
        assert config is not None
        assert "does not exist" in reason
        assert str(_out(root)) in reason, "the line names where the build goes"

    def test_a_missing_build_directory_plans_a_build(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _half_gone(root))
        keys, _config, reason = self._plan(root, db_path)
        assert keys == ["build-missing"]
        assert "the build directory" in reason

    def test_a_present_build_plans_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _built(root))
        assert self._plan(root, db_path) == ([], None, "")

    def test_an_earlier_failure_does_not_block_the_next_build(self, tmp_path: Path) -> None:
        """The failed run told the caller; a repaired build must not wait for a timer."""
        from fw_context_mcp.indexer.autobuild import record_problem

        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _gone(root))
        record_problem(db_path.parent, "The last index run failed (exit code 1).")
        keys, _config, _reason = self._plan(root, db_path)
        assert keys == ["build-missing"]


@pytest.mark.usefixtures("global_config")
class TestTheRunOfTheUser:
    def _cfg(self, root: Path, build_table: str):
        from fw_context_mcp.config import load as load_config

        config_dir = root / ".fw-context"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(f"[build]\n{build_table}\n", encoding="utf-8")
        return load_config(project_root=root)

    def test_a_missing_build_turns_build_on(self, tmp_path: Path, capsys) -> None:
        from fw_context_mcp.cli._index import _build_if_missing

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "cmake"')
        _half_gone(root)
        args = argparse.Namespace(build=False)
        _build_if_missing(args, root, cfg, None, None)
        assert args.build is True
        assert "running the build (--build)" in capsys.readouterr().err

    def test_a_present_build_leaves_build_off(self, tmp_path: Path) -> None:
        from fw_context_mcp.cli._index import _build_if_missing, _refuse_without_build

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "cmake"')
        _built(root)
        args = argparse.Namespace(build=False)
        _build_if_missing(args, root, cfg, None, None)
        assert args.build is False
        assert _refuse_without_build(root, cfg, None, None) == ""

    def test_a_stub_turns_nothing_on_and_the_run_stops(self, tmp_path: Path) -> None:
        """fw-context cannot run the build of an IDE project; it must not index wrong data."""
        from fw_context_mcp.cli._index import _build_if_missing, _refuse_without_build

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "stm32cubeide"')
        _write_cc(root / cfg.index.compile_commands, root / "Debug")
        args = argparse.Namespace(build=False)
        _build_if_missing(args, root, cfg, None, None)
        assert args.build is False
        refusal = _refuse_without_build(root, cfg, None, None)
        assert "cannot run the build" in refusal
        assert "outside of fw-context" in refusal

    def test_a_command_turns_build_on_without_a_system(self, tmp_path: Path) -> None:
        from fw_context_mcp.cli._index import _build_if_missing

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'command = "bear -- make"')
        _half_gone(root)
        args = argparse.Namespace(build=False)
        _build_if_missing(args, root, cfg, None, None)
        assert args.build is True

    def test_a_run_that_builds_nothing_names_the_build_command(self, tmp_path: Path) -> None:
        """A buildable project whose run builds nothing: the text names ``--build``.

        Such a run is a background run of a backend that cannot build in
        isolation; the refusal text is the same for each caller.
        """
        from fw_context_mcp.cli._index import _refuse_without_build

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "cmake"')
        _half_gone(root)
        refusal = _refuse_without_build(root, cfg, None, None)
        assert "does not index without the build" in refusal
        assert "fw-context index --build" in refusal

    @pytest.mark.parametrize("background", [False, True])
    def test_a_refused_run_leaves_its_text_for_the_mcp_answers(
        self, tmp_path: Path, background: bool
    ) -> None:
        """The marker is how the LLM learns of it; a background run has no other way."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import cmd_index
        from fw_context_mcp.config import load as load_config
        from fw_context_mcp.indexer.autobuild import read_problem

        root = tmp_path / "proj"
        root.mkdir()
        cc = _write_cc(root / ".fw-context" / "build" / "compile_commands.json", root / "Debug")
        db_path = _project(root, tmp_path / "index", cc, system="stm32cubeide")
        _write_cc(root / load_config(project_root=root).index.compile_commands, root / "Debug")
        args = SimpleNamespace(
            verbose=False, project=str(root), background=background, build=False,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert cmd_index(args) == 1
        problem = read_problem(db_path.parent)
        assert problem is not None and problem.build_missing
        assert "cannot run the build" in problem.text

    def test_a_build_that_goes_away_during_the_run_is_kept_as_a_problem(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A run that ends well must not clear the marker of a build that is gone now.

        Another run can refuse and write its marker while this run runs,
        and the build can go away meanwhile.
        """
        import shutil
        from types import SimpleNamespace

        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.indexer.autobuild import read_problem

        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _built(root))

        def _run_single(*a, **kw) -> int:
            shutil.rmtree(_out(root))  # `rm -rf .fw-context/build` while the run indexes
            return 0

        monkeypatch.setattr(index_mod, "_run_single", _run_single)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        monkeypatch.setattr(index_mod, "_record_still_uncovered", lambda *a, **kw: None)
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
        args = SimpleNamespace(
            verbose=False, project=str(root), background=False, build=False,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert index_mod.cmd_index(args) == 0
        problem = read_problem(db_path.parent)
        assert problem is not None and problem.build_missing
        assert f"{_default_cc(root)} does not exist" in problem.text

    def test_a_retired_build_dir_stops_the_run_before_any_build(self, tmp_path: Path, monkeypatch) -> None:
        """The config names a directory that the build does not use, thus the run refuses it."""
        from types import SimpleNamespace

        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.indexer.autobuild import read_problem

        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _built(root))
        config = root / ".fw-context" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + '[[build.variants]]\nname = "dev"\nboard = "b"\nbuild_dir = "build/dev"\n',
            encoding="utf-8",
        )
        built: list[str] = []
        monkeypatch.setattr(index_mod, "_run_multi", lambda *a, **kw: built.append("multi") or 0)
        args = SimpleNamespace(
            verbose=False, project=str(root), background=False, build=True,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert index_mod.cmd_index(args) == 1
        assert built == []
        problem = read_problem(db_path.parent)
        assert problem is not None and "[[build.variants]] 'dev' build_dir" in problem.text

    def test_a_missing_database_of_the_user_is_an_error_and_no_build(self, tmp_path: Path, capsys) -> None:
        """A build of fw-context writes another file; it cannot give the file that the config names."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _resolve_compile_commands

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "cmake"\n[index]\ncompile_commands = "ci/cc.json"')
        args = SimpleNamespace(build=False, compile_commands=None, no_clean=False)

        assert _resolve_compile_commands(args, root, cfg, "cmake", False) == (None, False)
        assert "[index] compile_commands names it" in capsys.readouterr().err

    def test_a_database_of_the_user_is_indexed_and_never_rebuilt(self, tmp_path: Path, monkeypatch) -> None:
        """Its build directory is gone, and still no build replaces the file in the index."""
        from types import SimpleNamespace

        import fw_context_mcp.indexer.build as build_mod
        import fw_context_mcp.indexer.runner as runner_mod
        from fw_context_mcp.cli import _index as index_mod

        root = tmp_path / "proj"
        root.mkdir()
        user_cc = _write_cc(root / "ci" / "cc.json", tmp_path / "ci-machine" / "build")
        _project(root, tmp_path / "index", user_cc)
        config = root / ".fw-context" / "config.toml"
        config.write_text(config.read_text(encoding="utf-8") + '[index]\ncompile_commands = "ci/cc.json"\n',
                          encoding="utf-8")
        indexed: list[Path] = []

        def _no_build(*a, **kw):
            raise AssertionError("a build replaced the database of the user")

        monkeypatch.setattr(build_mod, "generate_compile_commands", _no_build)
        monkeypatch.setattr(runner_mod, "run", lambda **kw: indexed.append(Path(kw["compile_commands"])) or "0" * 64)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        monkeypatch.setattr(index_mod, "_post_index_optimize", lambda *a, **kw: None)
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
        args = SimpleNamespace(
            verbose=False, project=str(root), background=False, build=False,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert index_mod.cmd_index(args) == 0
        assert indexed == [user_cc.resolve()]

    def test_a_missing_file_is_left_to_the_default_path(self, tmp_path: Path) -> None:
        """Before the run, ``_resolve_compile_commands`` builds then, or gives its own error."""
        from fw_context_mcp.cli._index import _build_if_missing, _refuse_without_build

        root = tmp_path / "proj"
        cfg = self._cfg(root, 'system = "cmake"')
        args = argparse.Namespace(build=False)
        _build_if_missing(args, root, cfg, None, None)
        assert args.build is False
        assert _refuse_without_build(root, cfg, None, None) == ""


@pytest.mark.usefixtures("global_config")
class TestTheMcpAnswers:
    """Each answer carries the text of the last run that stopped without an index."""

    @staticmethod
    def _indexed(tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "proj"
        root.mkdir()
        return root, _project(root, tmp_path / "index", _built(root))

    def test_no_marker_leaves_the_answer_as_it_is(self, tmp_path: Path) -> None:
        from fw_context_mcp.mcp.shared.stale import annotate_build_problem

        root, _ = self._indexed(tmp_path)
        assert annotate_build_problem([{"name": "main"}], str(root)) == [{"name": "main"}]
        assert annotate_build_problem({"name": "main"}, str(root)) == {"name": "main"}

    def test_a_list_gets_a_leading_warning_and_a_dict_a_key(self, tmp_path: Path) -> None:
        from fw_context_mcp.indexer.autobuild import record_problem
        from fw_context_mcp.mcp.shared.stale import annotate_build_problem

        root, db_path = self._indexed(tmp_path)
        record_problem(db_path.parent, "The last index run failed (exit code 1).")

        assert annotate_build_problem([{"name": "main"}], str(root)) == [
            {"warning": "The last index run failed (exit code 1)."}, {"name": "main"},
        ]
        assert annotate_build_problem({"name": "main"}, str(root)) == {
            "name": "main", "build_warning": "The last index run failed (exit code 1).",
        }

    def test_a_project_that_does_not_resolve_gets_no_warning(self, tmp_path: Path) -> None:
        """The handler already gave the error; a warning must never fail an answer."""
        from fw_context_mcp.mcp.shared.stale import annotate_build_problem

        answer = [{"error": "No index found"}]
        assert annotate_build_problem(answer, str(tmp_path / "nowhere")) == answer

    def test_the_tool_wrapper_adds_it(self, tmp_path: Path) -> None:
        """Every query tool passes through _wrap_tool, thus none can miss it."""
        import asyncio

        from fw_context_mcp.indexer.autobuild import record_problem
        from fw_context_mcp.mcp.server import _wrap_tool

        root, db_path = self._indexed(tmp_path)
        record_problem(db_path.parent, "The last index run failed (exit code 1).")

        def handler(project_root: str | None = None) -> list[dict]:
            return [{"name": "main"}]

        answer = asyncio.run(_wrap_tool(handler)(project_root=str(root)))
        assert answer[0] == {"warning": "The last index run failed (exit code 1)."}

    def test_get_active_build_finds_a_build_that_went_away(self, tmp_path: Path) -> None:
        """No run has seen it yet, thus there is no marker; the tool checks itself."""
        import shutil

        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root, _ = self._indexed(tmp_path)
        shutil.rmtree(_out(root))

        result = get_active_build(project_root=str(root))
        assert result["status"] == "reindex_needed"
        assert result["index_message"].startswith(f"compile_commands.json {_default_cc(root)} does not exist")
        assert "fw-context index --build" in result["index_message"]
        assert "compile_commands_missing" not in result["reindex_reasons"], "one reason names it once"

    def test_get_active_build_finds_a_missing_build_directory(self, tmp_path: Path) -> None:
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root = tmp_path / "proj"
        root.mkdir()
        _project(root, tmp_path / "index", _half_gone(root))

        result = get_active_build(project_root=str(root))
        assert result["status"] == "reindex_needed"
        assert result["index_message"].startswith(f"the build directory {_out(root) / 'app'}")

    def test_a_reason_of_a_build_that_is_back_goes_away(self, tmp_path: Path) -> None:
        """The user built outside of fw-context: the old text must not stay in each answer."""
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root, db_path = self._indexed(tmp_path)
        record_problem(db_path.parent, "the build directory /x does not exist.", build_missing=True)

        result = get_active_build(project_root=str(root))
        assert not any("does not exist" in r for r in result["reindex_reasons"])
        assert read_problem(db_path.parent) is None

    def test_a_failed_run_stays_while_the_build_is_there(self, tmp_path: Path) -> None:
        """Only a run that ends well removes the text of a failed run."""
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root, db_path = self._indexed(tmp_path)
        record_problem(db_path.parent, "The last index run failed (exit code 1).")

        result = get_active_build(project_root=str(root))
        assert result["status"] == "reindex_needed"
        assert "The last index run failed (exit code 1)." in result["reindex_reasons"]
        assert read_problem(db_path.parent) is not None

    def test_a_missing_build_is_named_once(self, tmp_path: Path) -> None:
        """The marker of the refused run and the check of the tool say the same."""
        import shutil

        from fw_context_mcp.indexer.autobuild import record_problem
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root, db_path = self._indexed(tmp_path)
        shutil.rmtree(_out(root))
        record_problem(db_path.parent, "refused: the build is gone.", build_missing=True)

        result = get_active_build(project_root=str(root))
        assert [r for r in result["reindex_reasons"] if "build" in r] == ["refused: the build is gone."]

    def test_an_index_of_an_explicit_file_gets_no_check(self, tmp_path: Path) -> None:
        """`fw-context index cc.json` does no check, and `--build` would replace the file.

        The build of the default file is gone too: only the comparison with
        the file of the index keeps the check away.
        """
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root = tmp_path / "proj"
        root.mkdir()
        explicit = _write_cc(tmp_path / "ci" / "compile_commands.json", tmp_path / "ci-machine" / "build")
        _project(root, tmp_path / "index", explicit)

        result = get_active_build(project_root=str(root))
        assert not any("the build directory" in r for r in result["reindex_reasons"])
        assert not any("does not exist" in r for r in result["reindex_reasons"])

    def test_a_marker_stays_when_the_tool_cannot_check(self, tmp_path: Path) -> None:
        """No check is no evidence that the build is back."""
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root = tmp_path / "proj"
        root.mkdir()
        explicit = _write_cc(tmp_path / "ci" / "compile_commands.json", tmp_path / "ci")
        db_path = _project(root, tmp_path / "index", explicit)
        record_problem(db_path.parent, "refused: the build is gone.", build_missing=True)

        result = get_active_build(project_root=str(root))
        assert "refused: the build is gone." in result["reindex_reasons"]
        assert read_problem(db_path.parent) is not None

    def test_a_build_of_the_run_clears_the_marker_at_the_end_of_the_run(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The single path: the build writes the default file, and the end check finds it."""
        from types import SimpleNamespace

        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.indexer.autobuild import read_problem, record_problem

        root = tmp_path / "proj"
        root.mkdir()
        db_path = _project(root, tmp_path / "index", _gone(root))
        record_problem(db_path.parent, "refused: the build is gone.", build_missing=True)

        def _run_single(*a, **kw) -> int:
            _built(root)  # the build writes the default file into its output directory
            return 0

        monkeypatch.setattr(index_mod, "_run_single", _run_single)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        monkeypatch.setattr(index_mod, "_record_still_uncovered", lambda *a, **kw: None)
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
        args = SimpleNamespace(
            verbose=False, project=str(root), background=True, build=False,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert index_mod.cmd_index(args) == 0
        assert read_problem(db_path.parent) is None


@pytest.mark.usefixtures("global_config")
class TestTheDatabaseOfTheUser:
    """[index] compile_commands names a database of the user: the CLI and MCP give one answer.

    fw-context runs no build for it (``build.user_database``), thus no
    check of its build, no advice ``--build``, and no promise of a build in
    the background.
    """

    @staticmethod
    def _project_with_user_database(tmp_path: Path, extra: str = "") -> tuple[Path, Path]:
        root = tmp_path / "proj"
        root.mkdir()
        user_cc = _write_cc(root / "ci" / "cc.json", tmp_path / "ci-machine" / "build")
        db_path = _project(root, tmp_path / "index", user_cc)
        config = root / ".fw-context" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8") + extra + '[index]\ncompile_commands = "ci/cc.json"\n',
            encoding="utf-8",
        )
        return root, db_path

    def test_no_build_of_it_is_checked(self, tmp_path: Path) -> None:
        from fw_context_mcp.config import load as load_config
        from fw_context_mcp.indexer.build import checked_compile_commands

        root, _ = self._project_with_user_database(tmp_path)
        cfg = load_config(project_root=root)

        assert checked_compile_commands(root, cfg, (root / "ci" / "cc.json").resolve()) is None

    def test_get_active_build_reports_no_missing_build(self, tmp_path: Path) -> None:
        """The directory of a CI database is on the CI machine; that is no missing build."""
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        root, _ = self._project_with_user_database(tmp_path)

        result = get_active_build(project_root=str(root))
        assert not any("build directory" in r or "--build" in r for r in result.get("reindex_reasons", []))

    def test_new_sources_get_the_advice_of_a_database_of_the_user(self, tmp_path: Path) -> None:
        from fw_context_mcp.config import load as load_config
        from fw_context_mcp.indexer.autobuild import AutobuildState
        from fw_context_mcp.mcp.handlers.maintenance import _autobuild_state

        root, _ = self._project_with_user_database(tmp_path)

        state = _autobuild_state(root, load_config(project_root=root), ["src/new.c"])
        assert state is AutobuildState.USER_DATABASE

    def test_build_with_it_is_refused(self, tmp_path: Path, capsys) -> None:
        """--build writes another file; the index would change from one database to the other."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _resolve_compile_commands
        from fw_context_mcp.config import load as load_config

        root, _ = self._project_with_user_database(tmp_path)
        args = SimpleNamespace(build=True, compile_commands=None, no_clean=False)

        assert _resolve_compile_commands(args, root, load_config(project_root=root), "cmake", False) == (None, False)
        assert "--build would index the build of fw-context" in capsys.readouterr().err

    def test_variants_with_it_are_refused(self, tmp_path: Path, capsys) -> None:
        from types import SimpleNamespace

        from fw_context_mcp.cli import _index as index_mod

        root, _ = self._project_with_user_database(
            tmp_path, extra='[[build.variants]]\nname = "dev"\nboard = "b"\n',
        )
        args = SimpleNamespace(
            verbose=False, project=str(root), background=False, build=False,
            compile_commands=None, no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )

        assert index_mod.cmd_index(args) == 1
        assert "[[build.variants]] build their own databases" in capsys.readouterr().err

    def test_init_does_not_build_a_missing_one(self, tmp_path: Path, monkeypatch) -> None:
        import fw_context_mcp.indexer.build as build_mod
        from fw_context_mcp.cli._init_build import _auto_build_if_possible
        from fw_context_mcp.config import load as load_config

        root, _ = self._project_with_user_database(tmp_path)
        (root / "ci" / "cc.json").unlink()

        def _no_build(*a, **kw):
            raise AssertionError("init built a database that the index does not read")

        monkeypatch.setattr(build_mod, "generate_compile_commands", _no_build)
        ok, path, error = _auto_build_if_possible(root, "cmake", load_config(project_root=root))

        assert (ok, path) == (False, None)
        assert "[index] compile_commands names it" in (error or "")
