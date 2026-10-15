"""Each query answer and each build of the status tools says whether its index is current.

An LLM that read an answer of an old index went on with it.  The answers
said "Background reindex in progress" also when no run was there, and
"run fw-context index" also when the daemon was about to run it.  The
state of the index of the build (``index_state``) tells the caller to wait
for a run, to ask the user for a command, or nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.background import PENDING_MARKER
from fw_context_mcp.mcp.shared.index_state import (
    WAIT_ADVICE,
    annotate_index_state,
    index_state,
    index_state_advice,
)
from fw_context_mcp.mcp.shared.pid_file import PidFile

PROJECT_ID = "f1e2d3c4b5a69788f1e2d3c4b5a69788"

# Two builds: one answers each query below, the other is the build that
# the query does not name.
BUILDS = [("h-a", "a", ""), ("h-b", "b", "")]


@pytest.fixture(autouse=True)
def _reset_sentinel():
    from fw_context_mcp.mcp.shared import context as ctx

    ctx._server_init_error = None
    yield
    ctx._server_init_error = None


@pytest.fixture(autouse=True)
def _global_config(tmp_path: Path, monkeypatch) -> None:
    """Keep load() away from the global config of the operator (the path is fixed at import)."""
    import fw_context_mcp.config.settings as settings

    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)


def _project(
    tmp_path: Path, builds=BUILDS, *, git_head: str = "", build_system: str = "", index_table: str = "",
) -> tuple[Path, Path]:
    """Make a project of *builds* ``(config_hash, variant, image)``, each with one indexed file.

    Returns ``(root, db_dir)``.  Each build stands in for a completed index
    run: the current row format, a compile_commands.json older than the
    run, and the hash of its file.  *build_system* goes to ``[build]
    system``: without it fw-context cannot run the build, and the advice
    names no ``--build``.  *index_table* holds more lines of ``[index]``.
    """
    from fw_context_mcp.indexer.db import (
        CURRENT_ROW_FORMAT,
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.utils import compute_source_hash

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    index_dir = tmp_path / "index"
    build_table = f'[build]\nsystem = "{build_system}"\n' if build_system else ""
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n[index]\ndb_dir = "{index_dir.as_posix()}"\n{index_table}{build_table}',
        encoding="utf-8",
    )
    (root / ".fw-context" / "local.toml").write_text("", encoding="utf-8")
    db_dir = index_dir / PROJECT_ID
    conn = open_db(db_dir / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "proj", str(root))
            for config_hash, variant, image in builds:
                cc = root / f"{config_hash}.json"
                cc.write_text("[]", encoding="utf-8")
                # Older than the run, thus compile_commands.json did not change.
                os.utime(cc, (1_000_000_000, 1_000_000_000))
                upsert_build_config(
                    conn, config_hash, PROJECT_ID, str(cc), variant=variant, image=image,
                    row_format=CURRENT_ROW_FORMAT, git_head=git_head or None,
                )
                source = root / f"{variant}.c"
                source.write_text("int x;\n", encoding="utf-8")
                upsert_file(
                    conn, config_hash, source.name, "c",
                    mtime=source.stat().st_mtime, source_hash=compute_source_hash(source),
                )
            conn.execute("UPDATE build_configs SET manifest_verification = 'full'")
    finally:
        conn.close()
    return root, db_dir


def _change(root: Path, variant: str) -> None:
    """Change the file of the build *variant* after the index run."""
    source = root / f"{variant}.c"
    source.write_text("int y;\n", encoding="utf-8")
    now = source.stat().st_mtime
    os.utime(source, (now + 100, now + 100))


def _schedule(db_dir: Path) -> None:
    """Write the marker of a live daemon that has work."""
    PidFile(db_dir / PENDING_MARKER, pid=os.getppid()).write()


def _run(db_dir: Path) -> None:
    """Write the PID file of a live index run."""
    PidFile(db_dir / "reindex.pid", pid=os.getppid()).write()


class TestTheRule:
    @pytest.mark.parametrize(
        ("behind", "run_state", "failed", "expected"),
        [
            (False, None, False, None),
            (False, "reindexing", True, None),
            (True, None, False, "not_scheduled"),
            (True, "pending", False, "pending"),
            (True, "reindexing", False, "reindexing"),
            (True, None, True, "failed"),
            # The run that repairs a failed run: "failed" gave the advice
            # to start a second run of the same work.
            (True, "reindexing", True, "reindexing"),
            (True, "pending", True, "pending"),
        ],
    )
    def test_the_state(self, behind, run_state, failed, expected):
        assert index_state(behind, run_state=run_state, failed=failed) == expected


class TestTheAdvice:
    @pytest.mark.parametrize("state", ["reindexing", "pending"])
    def test_a_run_asks_to_wait(self, state):
        text = index_state_advice(state, ["compile_commands.json changed"])
        assert text.startswith("The index of this build is not current: compile_commands.json changed.")
        assert text.endswith(WAIT_ADVICE)
        assert "fw-context index" not in text, "a command starts a second run of the same work"

    def test_no_run_names_the_command(self):
        text = index_state_advice("not_scheduled", ["compile_commands.json changed"])
        assert "No index run is in progress or scheduled" in text
        assert "`fw-context index`" in text

    def test_the_action_goes_into_the_text(self):
        text = index_state_advice(
            "not_scheduled", ["checkout changed from a to b"], action="run `fw-context index --build`",
        )
        assert "Tell the user to run `fw-context index --build`." in text

    def test_an_unknown_action_sends_the_caller_to_the_status(self):
        text = index_state_advice("not_scheduled", ["checkout changed from a to b"], action="")
        assert "Call get_project_status" in text
        assert "fw-context index" not in text

    def test_a_failed_run_gives_its_text(self):
        text = index_state_advice("failed", [], problem_text="The build failed. Run `fw-context index --build`.")
        assert text.startswith("The build failed. Run `fw-context index --build`.")


class TestTheQueryAnswer:
    """The state of the index of the build that the query names."""

    def test_a_current_build_adds_nothing(self, tmp_path):
        root, _ = _project(tmp_path)
        kw = {"project_root": str(root), "variant": "a"}

        assert annotate_index_state([{"name": "f"}], kw) == [{"name": "f"}]
        assert annotate_index_state({"name": "f"}, kw) == {"name": "f"}

    def test_a_run_for_another_build_adds_nothing(self, tmp_path):
        """A run of the project leaves a build that it does not change current."""
        root, db_dir = _project(tmp_path)
        _change(root, "b")
        _schedule(db_dir)

        answer = annotate_index_state([{"name": "f"}], {"project_root": str(root), "variant": "a"})
        assert answer == [{"name": "f"}]

    def test_a_scheduled_run_for_this_build_asks_to_wait(self, tmp_path):
        """The file is not in the answer, and only the hash of the files of the build tells."""
        root, db_dir = _project(tmp_path)
        _change(root, "a")
        _schedule(db_dir)

        answer = annotate_index_state([{"name": "f"}], {"project_root": str(root), "variant": "a"})
        assert answer[0]["index_state"] == "pending"
        assert "1 file(s) changed after the last index run" in answer[0]["warning"]
        assert answer[1:] == [{"name": "f"}]

    def test_a_running_run_and_a_dict(self, tmp_path):
        root, db_dir = _project(tmp_path)
        _change(root, "a")
        _run(db_dir)

        answer = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "a"})
        assert answer["index_state"] == "reindexing"
        assert answer["index_warning"].endswith(WAIT_ADVICE)
        assert "build_warning" not in answer

    def test_a_stale_answer_without_a_run_names_the_command(self, tmp_path):
        """No run: the hash of the files is not needed, the answer already says that a file changed."""
        root, _ = _project(tmp_path)
        stale = [{"warning": "Results can be out of date — 1 file(s) changed after the last index run.",
                  "stale": True}, {"name": "f"}]

        answer = annotate_index_state(stale, {"project_root": str(root), "variant": "a"})
        assert answer[0]["index_state"] == "not_scheduled"
        assert "files changed after the last index run" in answer[0]["warning"]
        assert answer[1:] == stale

    def test_a_changed_compile_commands_of_this_build(self, tmp_path):
        root, _ = _project(tmp_path)
        cc = root / "h-a.json"
        later = cc.stat().st_mtime + 4_000_000_000  # newer than the run
        os.utime(cc, (later, later))

        answer = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "a"})
        assert answer["index_state"] == "not_scheduled"
        assert "compile_commands.json changed" in answer["index_warning"]
        other = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "b"})
        assert other == {"name": "f"}, "the other build is current"

    def test_a_changed_checkout_asks_for_a_build(self, tmp_path, monkeypatch):
        from fw_context_mcp.indexer import git_context

        root, _ = _project(tmp_path, git_head="ref: refs/heads/one", build_system="cmake")
        monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")

        answer = annotate_index_state([{"name": "f"}], {"project_root": str(root), "variant": "a"})
        assert answer[0]["index_state"] == "not_scheduled"
        assert "checkout changed from one to two" in answer[0]["warning"]
        assert "`fw-context index --build`" in answer[0]["warning"]

    def test_an_unknown_build_sends_the_caller_to_the_status(self, tmp_path, monkeypatch):
        """An error of the config costs the action, never the answer."""
        from fw_context_mcp.indexer import autobuild, git_context

        def _broken(project_root, cfg):
            raise ValueError("bad config")

        root, _ = _project(tmp_path, git_head="ref: refs/heads/one", build_system="cmake")
        monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")
        monkeypatch.setattr(autobuild, "build_advice", _broken)

        answer = annotate_index_state([{"name": "f"}], {"project_root": str(root), "variant": "a"})
        assert answer[0]["index_state"] == "not_scheduled"
        assert "Call get_project_status" in answer[0]["warning"]

    def test_a_run_wins_over_a_failed_run(self, tmp_path):
        from fw_context_mcp.indexer.autobuild import record_problem

        root, db_dir = _project(tmp_path)
        record_problem(db_dir, "The last index run failed (exit code 1). Run `fw-context index`.")
        _run(db_dir)

        answer = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "a"})
        assert answer["index_state"] == "reindexing"
        assert "the last index run failed" in answer["index_warning"]
        assert "build_warning" not in answer, "its command starts a second run of the same work"


# The action that each kind of build gets, in the status and in a query.
_ACTIONS = {
    "cmake": "run `fw-context index --build`",
    "user_database": "make the database again (the file that [index] compile_commands names",
    "stm32cubeide": "build the project outside of fw-context",
    "": "build the project outside of fw-context",
}


class TestTheQueryAndTheStatusAgree:
    """One function selects the build that writes the file list: the two places named two commands."""

    @staticmethod
    def _setup(tmp_path, backend: str) -> Path:
        """A project of *backend*; "user_database" is a cmake project with its own database."""
        if backend == "user_database":
            # The database of build "a": the config names a file of the user.
            root, _ = _project(
                tmp_path, git_head="ref: refs/heads/one", build_system="cmake",
                index_table='compile_commands = "h-a.json"\n',
            )
        else:
            root, _ = _project(tmp_path, git_head="ref: refs/heads/one", build_system=backend)
        return root

    @pytest.mark.parametrize("backend", list(_ACTIONS))
    def test_after_a_checkout(self, tmp_path, monkeypatch, backend):
        """`--build` fails for a database of the user, a build in an IDE, and an unknown build system."""
        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root = self._setup(tmp_path, backend)
        monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")

        query = annotate_index_state([{"name": "f"}], {"project_root": str(root), "variant": "a"})[0]
        status = get_build_info(project_root=str(root), variant="a")
        reason = next(r for r in status["reindex_reasons"] if r.startswith("checkout changed"))
        assert query["index_state"] == "not_scheduled"
        assert _ACTIONS[backend] in query["warning"]
        assert _ACTIONS[backend] in reason
        assert _ACTIONS[backend] in status["index_message"], "the message agrees with the reason"
        if backend != "cmake":
            assert "--build" not in query["warning"] + reason + status["index_message"]

    @pytest.mark.parametrize(
        ("backend", "in_background", "action"),
        [
            ("cmake", False, _ACTIONS["cmake"]),
            ("cmake", True, "run `fw-context index`"),
            ("user_database", False, _ACTIONS["user_database"]),
            ("stm32cubeide", False, _ACTIONS["stm32cubeide"]),
            ("", False, _ACTIONS[""]),
        ],
    )
    def test_for_a_new_source_file(self, tmp_path, monkeypatch, backend, in_background, action):
        """Only a build adds the file.  A backend that builds in the background needs only the next run."""
        from fw_context_mcp.indexer.builders import generic_cmake
        from fw_context_mcp.mcp.handlers import maintenance
        from fw_context_mcp.mcp.shared.stale import annotate_stale

        root = self._setup(tmp_path, backend)
        monkeypatch.setattr(maintenance, "find_unindexed_sources", lambda *a, **k: ["src/new.c"])
        monkeypatch.setattr(
            generic_cmake.GenericCMakeBuildSystem, "background_build_safe", lambda self, cfg: in_background,
        )

        query = annotate_index_state(
            annotate_stale([], [], empty_new_sources=["src/new.c"]), {"project_root": str(root), "variant": "a"},
        )[0]
        status = maintenance.get_build_info(project_root=str(root), variant="a")
        reason = next(r for r in status["reindex_reasons"] if "not in compile_commands.json" in r)
        assert f"Tell the user to {action}" in query["warning"]
        assert action in reason
        if action != _ACTIONS["cmake"]:
            assert "--build" not in query["warning"] + reason + status["index_message"]

    def test_a_checkout_next_to_a_new_source_that_builds_itself(self, tmp_path, monkeypatch):
        """The message said "A plain reindex cannot see them", then named the plain run, and hid the checkout."""
        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.indexer.builders import generic_cmake
        from fw_context_mcp.mcp.handlers import maintenance

        root = self._setup(tmp_path, "cmake")
        monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")
        monkeypatch.setattr(maintenance, "find_unindexed_sources", lambda *a, **k: ["src/new.c"])
        monkeypatch.setattr(
            generic_cmake.GenericCMakeBuildSystem, "background_build_safe", lambda self, cfg: True,
        )

        status = maintenance.get_build_info(project_root=str(root), variant="a")
        assert "A plain reindex cannot see them" not in status["index_message"]
        assert status["index_message"].startswith("The checkout changed")

    def test_a_new_source_during_a_run_asks_to_wait(self, tmp_path, monkeypatch):
        """The state of a query asks to wait, and the status named the command at once."""
        from fw_context_mcp.mcp.handlers import maintenance

        root, db_dir = _project(tmp_path, build_system="stm32cubeide")
        monkeypatch.setattr(maintenance, "find_unindexed_sources", lambda *a, **k: ["src/new.c"])
        _run(db_dir)

        status = maintenance.get_build_info(project_root=str(root), variant="a")
        reason = next(r for r in status["reindex_reasons"] if "not in compile_commands.json" in r)
        assert "wait for it, and when this reason stays after the run" in reason
        assert _ACTIONS["stm32cubeide"] in reason

    def test_a_variant_that_names_its_build_system_builds(self, tmp_path, monkeypatch):
        """`--build` builds the variants, also when only a variant names the build system."""
        from fw_context_mcp.config import load as load_config
        from fw_context_mcp.indexer.autobuild import build_advice

        root, _ = _project(tmp_path)
        config = root / ".fw-context" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8") + '[[build.variants]]\nname = "a"\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        assert build_advice(root, load_config(root)) == _ACTIONS["cmake"]


class TestTheStatusTools:
    """``reindex_state`` is a field of each build: one build can be current and another not."""

    def test_an_old_row_format_is_not_a_changed_database(self, tmp_path):
        """The message said "Compile commands changed" for each reason that it did not name."""
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        conn = open_db(db_dir / "index.db")
        try:
            conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/1' WHERE variant = 'a'")
            conn.commit()
        finally:
            conn.close()

        result = get_build_info(project_root=str(root), variant="a")
        assert result["status"] == "reindex_needed"
        assert "Compile commands changed" not in result["index_message"]
        assert "row_format_mismatch" in result["index_message"]

    def test_each_build_has_its_state(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        root, db_dir = _project(tmp_path)
        _change(root, "a")
        _schedule(db_dir)

        result = get_project_status(project_root=str(root))
        states = {row["variant"]: row["reindex_state"] for row in result["builds"]}
        assert states == {"a": "pending", "b": None}
        assert "reindex_state" not in result, "a project of several builds has no state of its own"
        assert result["index_message"].startswith("An index run is in progress or scheduled")
        assert WAIT_ADVICE in result["index_message"]

    def test_no_run_gives_not_scheduled(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, _ = _project(tmp_path)
        _change(root, "a")

        result = get_build_info(project_root=str(root), variant="a")
        assert result["reindex_state"] == "not_scheduled"
        assert not result["index_message"].startswith("An index run is in progress")

    def test_a_run_for_another_build_leaves_a_build_current(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        _change(root, "b")
        _run(db_dir)

        result = get_build_info(project_root=str(root), variant="a")
        assert result["reindex_state"] is None
        assert result["bg_reindex_running"] is True
        assert not result["index_message"].startswith("An index run is in progress")


class TestTheAnswerOfANewSource:
    """A source file that compile_commands.json does not cover did not change, and only a build adds it."""

    def test_the_warning_marks_the_files(self):
        from fw_context_mcp.mcp.shared.stale import annotate_stale

        answer = annotate_stale([], [], empty_new_sources=["src/new.c"])
        assert answer[0]["unindexed_sources"] == 1
        assert "When no index run is in progress or scheduled" in answer[0]["warning"]

    def test_the_state_names_the_files_and_a_build(self, tmp_path, monkeypatch):
        from fw_context_mcp.indexer.builders import generic_cmake
        from fw_context_mcp.mcp.shared.stale import annotate_stale

        root, _ = _project(tmp_path, build_system="cmake")
        # A backend that does not build in the background: only a build adds
        # the file (`TestTheQueryAndTheStatusAgree` has the other backends).
        monkeypatch.setattr(
            generic_cmake.GenericCMakeBuildSystem, "background_build_safe", lambda self, cfg: False,
        )
        answer = annotate_stale([], [], empty_new_sources=["src/new.c"])

        state = annotate_index_state(answer, {"project_root": str(root), "variant": "a"})[0]
        assert state["index_state"] == "not_scheduled"
        assert "1 source file(s) are not in compile_commands.json" in state["warning"]
        assert "files changed" not in state["warning"], "no file changed"
        assert "`fw-context index --build`" in state["warning"]

    def test_a_backend_that_builds_on_its_own_needs_the_next_run(self, tmp_path, monkeypatch):
        """The status says the same: "fw-context builds them in the next index run"."""
        from fw_context_mcp.indexer.builders import generic_cmake
        from fw_context_mcp.mcp.shared.stale import annotate_stale

        root, _ = _project(tmp_path, build_system="cmake")
        monkeypatch.setattr(
            generic_cmake.GenericCMakeBuildSystem, "background_build_safe", lambda self, cfg: True,
        )
        answer = annotate_stale([], [], empty_new_sources=["src/new.c"])

        state = annotate_index_state(answer, {"project_root": str(root), "variant": "a"})[0]
        assert "Tell the user to run `fw-context index`." in state["warning"]

    def test_it_starts_the_daemon(self, tmp_path, monkeypatch):
        """The check at the start of the daemon finds the file, and its run builds it."""
        from fw_context_mcp.mcp import background
        from fw_context_mcp.mcp.shared import stale

        root, db_dir = _project(tmp_path)
        started: list[Path] = []
        monkeypatch.setattr(background, "_ensure_daemon_running", started.append)
        monkeypatch.setattr(stale, "diagnose_empty_result", lambda conn, config_hash, root: (0, ["src/new.c"]))

        stale._with_stale_recovery(root, db_dir / "index.db", lambda conn, config_hash: [], config_hash="h-a")
        assert started == [root]


class TestTheCheckAtTheStartOfTheDaemon:
    """A new source file that came while no daemon ran gets a run, where the run builds it."""

    @pytest.mark.parametrize(("backend", "expected"), [("will_build", True), ("unsupported", False)])
    def test_a_new_source_file(self, tmp_path, monkeypatch, backend, expected):
        from fw_context_mcp.indexer import autobuild
        from fw_context_mcp.mcp.daemon import _staleness_check
        from fw_context_mcp.mcp.shared import stale

        root, _ = _project(tmp_path)
        monkeypatch.setattr(stale, "find_unindexed_sources", lambda *a, **k: ["src/new.c"])
        monkeypatch.setattr(autobuild, "project_state", lambda root, cfg: autobuild.AutobuildState(backend))

        _needs, reasons = _staleness_check(root)
        assert ("1 source file(s) not in compile_commands.json" in reasons) is expected


class TestAWarningNeverFailsTheAnswer:
    def test_an_error_of_the_database_keeps_the_answer(self, tmp_path, monkeypatch):
        """The state is a note on a complete answer: the answer must reach the caller."""
        import asyncio
        import sqlite3

        from fw_context_mcp.mcp.server import _wrap_tool
        from fw_context_mcp.mcp.shared import variants

        root, _ = _project(tmp_path)

        def _locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(variants, "resolve_build", _locked)

        def handler(project_root: str | None = None, variant: str | None = None) -> list[dict]:
            return [{"name": "f"}]

        answer = asyncio.run(_wrap_tool(handler)(project_root=str(root), variant="a"))
        assert answer == [{"name": "f"}]


class TestAnIndexWithoutFullHeaderTracking:
    """Such an index cannot see a changed header, thus it is not current."""

    def test_the_state_and_the_message_agree(self, tmp_path):
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        conn = open_db(db_dir / "index.db")
        try:
            conn.execute("UPDATE build_configs SET manifest_verification = 'none'")
            conn.commit()
        finally:
            conn.close()
        _run(db_dir)

        result = get_build_info(project_root=str(root), variant="a")
        assert result["reindex_state"] == "reindexing"
        assert "the index of this build is current" not in result["index_message"]
        assert result["index_message"].startswith("An index run is in progress or scheduled")

    @pytest.mark.parametrize("verification", ["none", "partial"])
    def test_a_run_gives_no_contrary_advice(self, tmp_path, verification):
        """``_warning`` said "continue" while ``reindex_state`` said "wait"."""
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info
        from fw_context_mcp.mcp.shared.index_state import manifest_reason

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, verification)
        _run(db_dir)

        result = get_build_info(project_root=str(root), variant="a")
        assert "continue the analysis" not in result["_warning"]
        assert manifest_reason(verification) in result["_warning"]
        assert result["_warning"].endswith(WAIT_ADVICE)

    @pytest.mark.parametrize(("schedule", "expected"), [(False, "not_scheduled"), (True, "pending")])
    def test_a_query_agrees_with_the_status(self, tmp_path, schedule, expected):
        """The status gave a state for this build, and the query gave none."""
        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "partial")
        if schedule:
            _schedule(db_dir)

        answer = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "a"})
        assert answer["index_state"] == expected
        assert "manifest verification: partial" in answer["index_warning"]
        other = annotate_index_state({"name": "f"}, {"project_root": str(root), "variant": "b"})
        assert other == {"name": "f"}, "the other build has full header tracking"

    def test_no_run_is_not_fully_up_to_date(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "none")

        result = get_build_info(project_root=str(root), variant="a")
        assert result["reindex_state"] == "not_scheduled"
        assert "fully up to date" not in result["index_message"]
        assert "run 'fw-context index' to make the index complete" in result["_warning"]

    def test_a_reason_names_its_own_command(self, tmp_path):
        """The reason of a missing build names `--build`, thus a plain 'fw-context index' is not sufficient."""
        from fw_context_mcp.indexer.autobuild import record_problem
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "none")
        record_problem(db_dir, "The build is not there. Run `fw-context index --build`.", build_missing=True)

        result = get_build_info(project_root=str(root), variant="a")
        assert result["reindex_state"] == "failed"
        assert "'fw-context index'" not in result["_warning"]
        assert "the command that reindex_reasons names" in result["_warning"]
        assert "'fw-context index'" not in result["index_message"]
        assert "`fw-context index --build`" in " ".join(result["reindex_reasons"])

    def test_a_reason_of_a_plain_run_names_its_command(self, tmp_path):
        """A changed compile_commands.json names a plain run, and the warning leads to that reason."""
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "none")
        later = root.stat().st_mtime + 3600
        os.utime(root / "h-a.json", (later, later))  # newer than the index run

        result = get_build_info(project_root=str(root), variant="a")
        assert result["status"] == "reindex_needed"
        assert "the command that reindex_reasons names" in result["_warning"]
        assert result["reindex_reasons"] == [
            "compile_commands.json changed after the last index run. Run `fw-context index`."
        ], "the pointer leads to a command"

    def test_the_project_names_the_command_of_a_reason(self, tmp_path):
        """One build needs a plain run, and the other has a reason with its own command."""
        from fw_context_mcp.indexer.db import open_db
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "none", "a")
        _set_manifest(db_dir, "none", "b")
        conn = open_db(db_dir / "index.db")
        try:
            conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/1' WHERE variant = 'b'")
            conn.commit()
        finally:
            conn.close()

        result = get_project_status(project_root=str(root))
        assert "the command that reindex_reasons names" in result["_warning"]
        assert any(
            "row_format_mismatch" in reason and "Run `fw-context index`" in reason
            for reason in result["reindex_reasons"]
        ), "the pointer leads to a command"


def _set_manifest(db_dir: Path, value: str, variant: str = "a") -> None:
    """Set the manifest verification of the build *variant*."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_dir / "index.db")
    try:
        conn.execute("UPDATE build_configs SET manifest_verification = ? WHERE variant = ?", (value, variant))
        conn.commit()
    finally:
        conn.close()


class TestStaleHeaders:
    def test_no_run_is_not_fully_up_to_date(self, tmp_path, monkeypatch):
        from fw_context_mcp.mcp.handlers import maintenance

        root, _ = _project(tmp_path)
        monkeypatch.setattr(maintenance, "_check_header_staleness", lambda *a, **k: (2, []))

        result = maintenance.get_build_info(project_root=str(root), variant="a")
        assert result["status"] == "ready"
        assert result["reindex_state"] == "not_scheduled"
        assert "fully up to date" not in result["index_message"]
        assert "2 source file(s) have stale header dependencies" in result["index_message"]
        assert "run `fw-context index` for a changed header" in result["index_message"], "no reason names it"

    @pytest.mark.parametrize("reason", ["failed", "checkout"])
    def test_a_reason_names_its_own_command(self, tmp_path, monkeypatch, reason):
        """A plain `fw-context index` fails again after a failed run, and a checkout needs `--build`."""
        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.indexer.autobuild import record_problem
        from fw_context_mcp.mcp.handlers import maintenance

        root, db_dir = _project(tmp_path, git_head="ref: refs/heads/one", build_system="cmake")
        monkeypatch.setattr(maintenance, "_check_header_staleness", lambda *a, **k: (2, []))
        if reason == "failed":
            record_problem(db_dir, "The last index run failed (exit code 1). Repair the build.")
            monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/one")
        else:
            monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")

        result = maintenance.get_build_info(project_root=str(root), variant="a")
        assert result["status"] == "reindex_needed"
        assert "2 source file(s) have stale header dependencies" in result["index_message"]
        assert "for a changed header" not in result["index_message"]
        if reason == "failed":
            assert result["reindex_state"] == "failed"
            assert "Repair the build." in result["index_message"]
        else:
            assert "`fw-context index --build`" in " ".join(result["reindex_reasons"])


class TestTheWordsOfEachBuild:
    """The message of a project of several builds names the state of each build, and not ``status``."""

    def test_a_new_source_without_a_run(self, tmp_path, monkeypatch):
        """``status`` is "reindexing" there, and no run comes."""
        from fw_context_mcp.indexer.autobuild import AutobuildState
        from fw_context_mcp.mcp.handlers import maintenance

        root, _ = _project(tmp_path)
        monkeypatch.setattr(maintenance, "find_unindexed_sources", lambda *a, **k: ["src/new.c"])
        monkeypatch.setattr(maintenance, "project_autobuild_state", lambda root, cfg: AutobuildState.WILL_BUILD)

        result = maintenance.get_project_status(project_root=str(root))
        assert [row["reindex_state"] for row in result["builds"]] == ["not_scheduled", "not_scheduled"]
        assert "a: not_scheduled; b: not_scheduled" in result["index_message"]
        assert "reindexing" not in result["index_message"]

    def test_the_counts_follow_the_state(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        root, _ = _project(tmp_path)
        _change(root, "a")

        result = get_project_status(project_root=str(root))
        assert "a: not_scheduled (1 modified file(s))" in result["index_message"]


class TestARefusalDuringARun:
    def test_it_gets_no_text_of_the_failed_run(self, tmp_path):
        """A run wins over "failed": the text names the command of the work that runs."""
        from fw_context_mcp.indexer.autobuild import record_problem

        root, db_dir = _project(tmp_path)
        record_problem(db_dir, "The last index run failed (exit code 1). Run `fw-context index`.")
        refusal = {"error": "name a build"}

        assert annotate_index_state(refusal, {"project_root": str(root)})["build_warning"].startswith(
            "The last index run failed"
        ), "without a run the refusal keeps the text"
        _run(db_dir)
        assert annotate_index_state(refusal, {"project_root": str(root)}) == refusal


class TestTheTextOfTheManifest:
    """``"partial"`` has a manifest.json: the full-text index or the embeddings failed."""

    def test_each_value_has_its_text(self):
        from fw_context_mcp.mcp.shared.index_state import manifest_reason

        assert manifest_reason("full") == ""
        assert "wrote no manifest.json" in manifest_reason("none")
        partial = manifest_reason("partial")
        assert "full-text index or the embeddings" in partial
        assert "manifest.json" not in partial

    def test_the_status_uses_it(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_build_info

        root, db_dir = _project(tmp_path)
        _set_manifest(db_dir, "partial")

        result = get_build_info(project_root=str(root), variant="a")
        assert "full-text index or the embeddings" in result["_warning"]
