"""A branch switch needs `index --build`, not a plain reindex.

`compile_commands.json` is a build artifact of the branch it was generated
on: it carries that branch's file list AND its compiler flags.  A reindex
reads the same file again.

Measured on the Mbed project: switching from 4.15.3 to 4.15.1 left two generated
zcbor sources listed in that file and absent from the tree.  Two files out of
881 then ended the whole index run — fixed separately — and even with the
skip in place the index describes the wrong tree until a build regenerates
the file.

The detection compares the branch recorded in `build_configs.description`
with the branch checked out now.  For that comparison to mean anything, the
recorded value has to describe the CONTENT of the index, which is what the
`description=None` path of `upsert_build_config` protects.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    open_db,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.indexer.git_context import (
    branch_of_description,
    get_git_description,
    head_label,
    head_moved,
    head_path,
    read_head,
)


class TestReadingTheStoredBranch:
    """The format `get_git_description` writes."""

    def test_a_branch_and_a_tag(self):
        assert branch_of_description("branch: main, tag: v2.1.0") == "main"

    def test_a_branch_alone(self):
        assert branch_of_description("branch: main") == "main"

    def test_a_tag_alone(self):
        # A description written while HEAD was detached carries no branch.
        assert branch_of_description("tag: v2.1.0") == ""

    def test_nothing(self):
        assert branch_of_description("") == ""

    def test_a_branch_name_that_holds_a_slash(self):
        # The measured shape on the Mbed project.
        found = branch_of_description(
            "branch: v4.15.1/devel_work, tag: 4.15.1-PRE.12"
        )
        assert found == "v4.15.1/devel_work"

    def test_a_branch_name_that_holds_a_comma(self):
        # The format cannot express it, and the reader must not invent a
        # name.  Splitting on the comma gives the part before it.
        assert branch_of_description("branch: odd,name") == "odd"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit on `main`."""
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True,
            capture_output=True, text=True,
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    # Hermetic against the developer's global config.  A machine with
    # `tag.gpgsign = true` makes `git tag NAME` an annotated tag, which then
    # fails with "no tag message?" — measured here.
    run("config", "tag.gpgsign", "false")
    run("config", "commit.gpgsign", "false")
    (tmp_path / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    run("add", "main.c")
    run("commit", "-q", "-m", "first")
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


class TestTheHeadOfTheTree:
    """`read_head` gives the text of the file HEAD, for each shape of a checkout."""

    def test_a_branch(self, repo):
        assert read_head(repo) == "ref: refs/heads/main"

    def test_a_detached_head(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", sha)
        assert read_head(repo) == sha

    def test_a_subdirectory_of_the_repository(self, repo):
        # A project root below the top of the repository reads the same HEAD.
        sub = repo / "fw"
        sub.mkdir()
        assert read_head(sub) == "ref: refs/heads/main"

    def test_a_worktree_reads_its_own_head(self, repo, tmp_path_factory):
        # In a worktree `.git` is a file, and HEAD lives in the main repository.
        other = tmp_path_factory.mktemp("wt") / "tree"
        _git(repo, "worktree", "add", "-q", "-b", "feature", str(other))
        assert (other / ".git").is_file()
        assert read_head(other) == "ref: refs/heads/feature"
        assert read_head(repo) == "ref: refs/heads/main"

    def test_a_directory_that_is_not_a_repository(self, tmp_path):
        nowhere = tmp_path / "nowhere"
        nowhere.mkdir()
        assert head_path(nowhere) is None
        assert read_head(nowhere) == ""

    def test_the_labels(self):
        assert head_label("ref: refs/heads/release/4.15.1") == "release/4.15.1"
        assert head_label("0123456789abcdef0123") == "commit 0123456789ab"
        assert head_label("") == ""


class TestTheDetection:
    """`head_moved`: the recorded HEAD first, the branch of an older index second."""

    def test_the_same_head(self):
        assert head_moved("ref: refs/heads/main", "", "ref: refs/heads/main") == ("", "")

    def test_another_branch(self):
        assert head_moved("ref: refs/heads/main", "", "ref: refs/heads/release/4.15.1") == (
            "main", "release/4.15.1")

    def test_a_checkout_of_a_tag_is_a_move(self):
        # The blind spot of the branch name: a tag or a commit detaches HEAD.
        assert head_moved("ref: refs/heads/main", "branch: main", "a" * 40) == (
            "main", "commit aaaaaaaaaaaa")

    def test_from_one_commit_to_another(self):
        assert head_moved("a" * 40, "", "b" * 40) == ("commit aaaaaaaaaaaa", "commit bbbbbbbbbbbb")

    def test_a_tree_without_git_reports_nothing(self):
        assert head_moved("ref: refs/heads/main", "branch: main", "") == ("", "")

    def test_an_older_index_compares_the_branch(self):
        # No recorded HEAD: the branch of the description, as before.
        assert head_moved("", "branch: main, tag: v1", "ref: refs/heads/main") == ("", "")
        assert head_moved("", "branch: main", "ref: refs/heads/dev") == ("main", "dev")

    def test_an_older_index_does_not_see_a_detached_head(self):
        assert head_moved("", "branch: main", "a" * 40) == ("", "")
        assert head_moved("", "tag: v1", "ref: refs/heads/main") == ("", "")

    def test_a_new_tag_on_the_same_branch_is_not_a_move(self, repo):
        # The description also carries the last tag, and a release tag moves
        # no file, and no HEAD.
        before = read_head(repo)
        _git(repo, "tag", "v1.0.0")
        assert "tag: v1.0.0" in get_git_description(repo), "the fixture did not tag"
        assert head_moved(before, "branch: main", read_head(repo)) == ("", "")


class TestTheDescriptionDescribesTheContent:
    """`upsert_build_config(description=None)` keeps the recorded value.

    A run stamps `build_configs` twice — once before reading any translation
    unit and once after.  The first write used to overwrite the description
    with the CURRENT git context, so a run that failed left the new branch
    recorded over the old content, and the detection above then saw
    agreement that was not there.
    """

    def _db(self, tmp_path: Path):
        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
        return conn

    def _description(self, conn) -> str:
        row = conn.execute(
            "SELECT description FROM build_configs WHERE config_hash='ch'"
        ).fetchone()
        return str(row["description"])

    def test_none_keeps_the_stored_value(self, tmp_path):
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json", description="branch: old"
                )
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json", description=None)
            assert self._description(conn) == "branch: old"
        finally:
            conn.close()

    def test_none_on_a_first_insert_stores_nothing(self, tmp_path):
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json", description=None)
            assert self._description(conn) == ""
        finally:
            conn.close()

    def test_a_value_still_replaces_the_stored_one(self, tmp_path):
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json", description="branch: old"
                )
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json", description="branch: new"
                )
            assert self._description(conn) == "branch: new"
        finally:
            conn.close()

    def test_an_empty_string_clears_it(self, tmp_path):
        # Distinct from None: a caller that means "no git context" says so.
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json", description="branch: old"
                )
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json", description="")
            assert self._description(conn) == ""
        finally:
            conn.close()

    def test_the_first_write_of_a_run_does_not_stamp_the_branch(self, tmp_path):
        """What `runner.run` does, without running an index.

        The early write must leave the recorded branch alone so a failed run
        cannot claim the index came from the new tree.
        """
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json",
                    description="branch: v4.15.3/devel_work",
                )
            # The shape of the early call in runner.run: everything else is
            # refreshed, the description is not.
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", "cc.json",
                    description=None,
                    manifest_verification="indexing",
                )
            assert self._description(conn) == "branch: v4.15.3/devel_work"
            row = conn.execute(
                "SELECT manifest_verification FROM build_configs "
                "WHERE config_hash='ch'"
            ).fetchone()
            assert row["manifest_verification"] == "indexing"
        finally:
            conn.close()


# ── The reason reaches the tool ────────────────────────────────────────────


@pytest.fixture
def indexed_repo(repo: Path, tmp_path_factory):
    """A git project with an index of one file, ready for a branch switch.

    Writes the config directly and calls `runner.run` rather than driving the
    CLI: two subprocess launches would put this test out of reach of
    `make test`, and the wiring it covers is exactly what a fast suite has to
    catch.
    """
    import json

    from fw_context_mcp.indexer.runner import run

    (repo / "compile_commands.json").write_text(
        json.dumps([{
            "directory": str(repo),
            "file": "main.c",
            "arguments": ["cc", "-c", "main.c", "-o", "main.o"],
        }]),
        encoding="utf-8",
    )
    index_dir = tmp_path_factory.mktemp("index_dir")
    config_dir = repo / ".fw-context"
    config_dir.mkdir(parents=True, exist_ok=True)
    project_id = "0123456789abcdef0123456789abcdef"
    (config_dir / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n', encoding="utf-8"
    )
    # Keep the index out of the real database directory.
    (config_dir / "local.toml").write_text(
        f'[index]\ndb_dir = "{index_dir}"\n', encoding="utf-8"
    )

    run(
        compile_commands=repo / "compile_commands.json",
        db_path=index_dir / project_id / "index.db",
        project_root=repo,
        project_id=project_id,
        index_refs=False,
        index_embeddings=False,
        analyze_symbols=False,
        analyze_overrides=False,
    )
    return repo


def _switch_to(repo: Path, branch: str) -> None:
    subprocess.run(
        ["git", "checkout", "-q", "-b", branch], cwd=repo,
        check=True, capture_output=True, text=True,
    )


class TestTheToolAsksForABuild:
    """`get_project_status` is the mandatory first call, so the reason has to
    reach it — twice in this series a wiring mistake survived unit tests."""

    def _state(self, root: Path) -> dict:
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        return get_project_status(project_root=str(root))

    def test_the_recorded_branch_is_the_one_the_content_came_from(
        self, indexed_repo
    ):
        # The comparison is only meaningful because a successful index stamps
        # the branch it actually read.
        state = self._state(indexed_repo)
        assert "branch: main" in state["description"]

    def test_the_same_branch_reports_no_move(self, indexed_repo):
        state = self._state(indexed_repo)
        assert not [
            reason for reason in state.get("reindex_reasons", [])
            if "checkout changed" in reason
        ], state.get("reindex_reasons")

    def test_a_switch_asks_for_index_build(self, indexed_repo):
        # `--build` needs a build that fw-context can run: the fixture names
        # no build system, and for an unknown one the advice names the build
        # of the user (`autobuild.build_advice`).
        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)

        assert state["reindex_needed"] is True
        reasons = state["reindex_reasons"]
        moved = [r for r in reasons if "checkout changed" in r]
        assert moved, f"no reason names the checkout: {reasons}"
        reason = moved[0]
        # The command matters: a plain reindex reuses compile_commands.json,
        # which belongs to the branch the index was built on.
        assert "--build" in reason, reason
        assert "main" in reason, reason
        assert "release/4.15.1" in reason, reason

    def test_the_status_says_a_reindex_is_needed(self, indexed_repo):
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)
        assert state["status"] == "reindex_needed"
        assert state["stale"] is True

    def test_the_advice_names_the_automatic_path_too(self, indexed_repo):
        """Both halves, and the reason why.

        Measured on a real checkout of the Mbed project: this reason read as a
        command while the new-source reason beside it read "no command is
        needed", and the two contradicted each other.  A project the daemon
        will handle must not read like one that needs a command typed — and
        the command still has to be there, because a checkout on a project
        nobody watches produces no burst for the daemon to see.

        `cmake` is a backend that allows a background build.  Named
        explicitly: the fixture leaves `[build] system` out, and marker
        detection on a bare directory answers with something else.
        """
        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)
        reason = next(
            r for r in state["reindex_reasons"] if "checkout changed" in r
        )
        assert "background reindex" in reason, reason
        assert "--build" in reason, reason

    def test_a_backend_that_cannot_build_gets_the_command_alone(
        self, indexed_repo
    ):
        # A `[build] command` builds, but never in the background, thus
        # nothing will happen on its own and the command is the whole answer.
        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\ncommand = "make compiledb"\n',
            encoding="utf-8",
        )
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)
        reason = next(
            r for r in state["reindex_reasons"] if "checkout changed" in r
        )
        assert "background reindex" not in reason, reason
        assert "run `fw-context index --build`" in reason, reason


# ── The automatic build takes the branch as a second trigger ──────────────


class TestThePlanTakesTheBranch:
    """`cli._index._plan_auto_build` is where fw-context decides to build.

    It turns `--build` on for a `--background` run, and only for a backend
    that may build on its own.  The daemon does not pass the flag itself:
    measured on a real checkout of the Mbed project, that produced "error:
    --build and --background are mutually exclusive" and the run died in
    under a second.
    """

    def _project(self, repo: Path, tmp_path: Path, description: str,
                 system: str = "cmake"):
        """A project with an index that records *description*.

        compile_commands.json is where a run reads it
        (``build.checked_compile_commands``); a file elsewhere reads as a
        missing build, which outranks the branch.
        """
        import json

        cc = repo / ".fw-context" / "build" / "default" / "out" / "compile_commands.json"
        cc.parent.mkdir(parents=True, exist_ok=True)
        cc.write_text(
            json.dumps([{
                "directory": str(repo), "file": "main.c",
                "arguments": ["cc", "-c", "main.c"],
            }]),
            encoding="utf-8",
        )
        db_dir = tmp_path / "index" / "0123456789abcdef0123456789abcdef"
        db_dir.mkdir(parents=True)
        conn = open_db(db_dir / "index.db")
        try:
            with transaction(conn):
                upsert_project(
                    conn, "0123456789abcdef0123456789abcdef", "p", str(repo)
                )
                upsert_build_config(
                    conn, "ch", "0123456789abcdef0123456789abcdef",
                    str(cc),
                    description=description,
                    manifest_verification="full",
                )
        finally:
            conn.close()
        config_dir = repo / ".fw-context"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.toml").write_text(
            f'[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            f'[build]\nsystem = "{system}"\n',
            encoding="utf-8",
        )
        (config_dir / "local.toml").write_text(
            f'[index]\ndb_dir = "{tmp_path / "index"}"\n', encoding="utf-8"
        )
        return db_dir / "index.db"

    def _plan(self, repo: Path, db_path: Path):
        from fw_context_mcp.cli._index import _plan_auto_build
        from fw_context_mcp.config import load as load_config

        cfg = load_config(project_root=repo)
        return _plan_auto_build(repo, db_path, cfg, None, background=True)

    def test_the_same_branch_plans_no_build(self, repo, tmp_path):
        db_path = self._project(repo, tmp_path, "branch: main")
        keys, config, reason = self._plan(repo, db_path)
        assert keys == []
        assert config is None
        assert reason == ""

    def test_a_switch_plans_a_build(self, repo, tmp_path):
        db_path = self._project(repo, tmp_path, "branch: main")
        _switch_to(repo, "release/4.15.1")
        keys, config, reason = self._plan(repo, db_path)
        assert keys == ["checkout:release/4.15.1"]
        assert config is not None
        assert "checkout changed from main to release/4.15.1" in reason
        assert "compile_commands.json belongs to the old checkout" in reason

    def test_no_index_plans_nothing(self, repo, tmp_path):
        # The project and its config exist; the database does not.  Without
        # one there is nothing to compare the branch against, thus no plan —
        # the first condition of the docstring.
        self._project(repo, tmp_path, "branch: main")
        _switch_to(repo, "release/4.15.1")
        keys, config, reason = self._plan(repo, tmp_path / "absent" / "index.db")
        assert (keys, config, reason) == ([], None, "")

    def test_a_backend_that_cannot_isolate_plans_nothing(self, repo, tmp_path):
        # No builder knows the system, thus nothing may build in the
        # background — that refusal outranks the branch.
        db_path = self._project(repo, tmp_path, "branch: main", system="no-such-system")
        _switch_to(repo, "release/4.15.1")
        keys, config, reason = self._plan(repo, db_path)
        assert (keys, config, reason) == ([], None, "")

    def test_an_earlier_failure_does_not_block_the_build(self, repo, tmp_path):
        # The failed run told the caller through each MCP answer.  The next
        # run builds again: a repaired branch must not wait for a timer.
        from fw_context_mcp.indexer.autobuild import record_problem

        db_path = self._project(repo, tmp_path, "branch: main")
        _switch_to(repo, "release/4.15.1")
        record_problem(db_path.parent, "The last index run failed (exit code 1).")
        keys, _config, _reason = self._plan(repo, db_path)
        assert keys == ["checkout:release/4.15.1"]


class TestTheDaemonPassesNoBuildFlag:
    """The daemon decides nothing about building, and must not.

    `--build --background` is legal only with an isolated output directory,
    which only `_plan_auto_build` sets.  A flag from the daemon reached the
    CLI without it and every branch-triggered reindex failed instantly.
    """

    def test_the_spawn_carries_no_build_flag(self, tmp_path):
        import asyncio as aio

        from fw_context_mcp.mcp import daemon

        captured: list[str] = []

        class _FakeProc:
            pid = 4242
            returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured.extend(str(arg) for arg in args)
            return _FakeProc()

        async def _drive() -> None:
            original = aio.create_subprocess_exec
            aio.create_subprocess_exec = _fake_exec  # type: ignore[assignment]
            try:
                await daemon._run_index_async(tmp_path, tmp_path)
            finally:
                aio.create_subprocess_exec = original  # type: ignore[assignment]

        aio.run(_drive())
        assert "index" in captured
        assert "--background" in captured
        assert "--build" not in captured, (
            "the daemon must leave the build decision to the index run"
        )

    def test_the_helper_takes_no_build_keyword(self):
        import inspect

        from fw_context_mcp.mcp import daemon

        signature = inspect.signature(daemon._run_index_async)
        assert "with_build" not in signature.parameters

    def test_the_daemon_holds_no_branch_decision(self):
        from fw_context_mcp.mcp import daemon

        assert not hasattr(daemon, "_branch_needs_build")


# ── The recorded checkout, and the builds that the checks reach ───────────


class TestTheRecordedHead:
    def _db(self, tmp_path: Path):
        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
        return conn

    def test_the_column_is_outside_the_fingerprint_of_the_schema(self, tmp_path):
        """An index of an older version must not read as an older schema."""
        from fw_context_mcp.indexer.db._schema import CURRENT_SCHEMA_VERSION

        conn = self._db(tmp_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(build_configs)")}
            assert "git_head" not in columns
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json", git_head="ref: refs/heads/main")
            assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()
        conn = open_db(tmp_path / "index.db")
        try:
            row = conn.execute("SELECT git_head FROM build_configs WHERE config_hash='ch'").fetchone()
            assert row["git_head"] == "ref: refs/heads/main"
        finally:
            conn.close()

    def test_none_keeps_the_stored_value(self, tmp_path):
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json", git_head="ref: refs/heads/main")
            with transaction(conn):
                upsert_build_config(conn, "ch", "pid", "cc.json")
            row = conn.execute("SELECT git_head FROM build_configs WHERE config_hash='ch'").fetchone()
            assert row["git_head"] == "ref: refs/heads/main"
        finally:
            conn.close()

    def test_an_index_run_records_the_head(self, indexed_repo):
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        state = get_project_status(project_root=str(indexed_repo))
        assert "branch: main" in state["description"]
        assert not [r for r in state.get("reindex_reasons", []) if "checkout changed" in r]

    def test_a_checkout_of_a_tag_asks_for_a_build(self, indexed_repo):
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        # `--build` needs a build that fw-context can run: the fixture names
        # no build system, and for an unknown one the advice names the build
        # of the user (`autobuild.build_advice`).
        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _git(indexed_repo, "tag", "v1.0.0")
        _git(indexed_repo, "checkout", "-q", "v1.0.0")
        state = get_project_status(project_root=str(indexed_repo))
        moved = [r for r in state["reindex_reasons"] if "checkout changed" in r]
        assert moved and "commit " in moved[0] and "--build" in moved[0], state["reindex_reasons"]
        assert state["status"] == "reindex_needed"

    def test_a_run_that_does_not_build_keeps_the_old_checkout(self, indexed_repo):
        """`record_git=False`: the reason stays until a build.

        Before, a plain run recorded the new branch, and the reason went away
        while the index still read the database of the old branch.
        """
        from fw_context_mcp.indexer.runner import run
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        _switch_to(indexed_repo, "release/4.15.1")
        index_dir = Path(
            (indexed_repo / ".fw-context" / "local.toml").read_text(encoding="utf-8").split('"')[1]
        )
        run(
            compile_commands=indexed_repo / "compile_commands.json",
            db_path=index_dir / "0123456789abcdef0123456789abcdef" / "index.db",
            project_root=indexed_repo,
            project_id="0123456789abcdef0123456789abcdef",
            index_refs=False, index_embeddings=False, analyze_symbols=False,
            analyze_overrides=False, record_git=False,
        )
        state = get_project_status(project_root=str(indexed_repo))
        assert "branch: main" in state["description"]
        assert [r for r in state["reindex_reasons"] if "checkout changed" in r]


class TestTheRunDecidesWhatItRecords:
    """`cli._index._records_checkout`."""

    def _decide(self, repo: Path, db_path: Path, *, build: bool = False, explicit: bool = False,
                system: str = "cmake", variants: bool = False, configured: str = "", **narrow) -> bool:
        import argparse

        from fw_context_mcp.cli._index import _records_checkout
        from fw_context_mcp.config.settings import BuildVariant, Config

        cfg = Config()
        cfg.build.system = system
        if variants:
            cfg.build.variants = [BuildVariant(name="one")]
        if configured:
            cfg.index.compile_commands = configured
        args = argparse.Namespace(build=build, **narrow)
        return _records_checkout(args, cfg, None, repo, "0123456789abcdef0123456789abcdef", db_path, explicit)

    def _db(self, repo: Path, tmp_path: Path, head: str) -> Path:
        db_path = tmp_path / "index" / "index.db"
        db_path.parent.mkdir(parents=True)
        conn = open_db(db_path)
        try:
            with transaction(conn):
                upsert_project(conn, "0123456789abcdef0123456789abcdef", "p", str(repo))
                upsert_build_config(conn, "ch", "0123456789abcdef0123456789abcdef", "cc.json",
                                    manifest_verification="full", git_head=head)
        finally:
            conn.close()
        return db_path

    def test_the_same_checkout_records(self, repo, tmp_path):
        assert self._decide(repo, self._db(repo, tmp_path, read_head(repo)))

    def test_another_checkout_without_a_build_keeps_the_old_one(self, repo, tmp_path):
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        assert not self._decide(repo, db_path)

    def test_a_build_records(self, repo, tmp_path):
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        assert self._decide(repo, db_path, build=True)

    @staticmethod
    def _database(path: Path, repo: Path, *, after_checkout: bool) -> Path:
        """Write a database that is newer or older than the last checkout of *repo*."""
        import os

        from fw_context_mcp.indexer.git_context import head_path

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")
        head = head_path(repo)
        assert head is not None
        when = head.stat().st_mtime + (100 if after_checkout else -100)
        os.utime(path, (when, when))
        return path

    def test_a_run_of_the_user_with_a_database_of_the_user_records(self, repo, tmp_path):
        """fw-context builds nothing then: the user made the database for this tree.

        Also an older one: a database from CI or from git keeps the time of
        its build, thus the time of the file cannot decide for the user.
        """
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        cc = self._database(tmp_path / "ci" / "cc.json", repo, after_checkout=False)
        assert self._decide(repo, db_path, explicit=True, compile_commands=str(cc), background=False)

    @pytest.mark.parametrize("after_checkout", [True, False])
    def test_a_run_in_the_background_records_a_newer_database(self, repo, tmp_path, after_checkout):
        """The daemon runs after each change, also before the user makes the database again."""
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        cc = self._database(tmp_path / "ci" / "cc.json", repo, after_checkout=after_checkout)
        decided = self._decide(repo, db_path, explicit=True, compile_commands=str(cc), background=True)
        assert decided is after_checkout

    def test_a_relative_database_is_read_from_the_project(self, repo, tmp_path):
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        self._database(repo / "ci" / "cc.json", repo, after_checkout=True)
        assert self._decide(repo, db_path, explicit=True, compile_commands="ci/cc.json", background=True)

    @pytest.mark.parametrize("after_checkout", [True, False])
    def test_the_database_that_the_config_names(self, repo, tmp_path, after_checkout):
        """`[index] compile_commands`: no file on the command line, and the run reads that file."""
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        self._database(repo / "ci" / "cc.json", repo, after_checkout=after_checkout)
        decided = self._decide(
            repo, db_path, explicit=True, configured="ci/cc.json", compile_commands=None, background=True,
        )
        assert decided is after_checkout

    @pytest.mark.parametrize("background", [False, True])
    def test_a_project_of_variants_records_only_its_builds(self, repo, tmp_path, background):
        """Each variant reads its own database, and a file of the user is not one of them."""
        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        self._database(repo / "compile_commands.json", repo, after_checkout=True)
        assert not self._decide(repo, db_path, system="stm32cubeide", variants=True, background=background)

    def test_a_database_of_the_same_time_as_the_checkout_is_old(self, repo, tmp_path):
        """A file system with a coarse time gives the two the same time: the database can be older."""
        import os

        from fw_context_mcp.indexer.git_context import head_path

        db_path = self._db(repo, tmp_path, read_head(repo))
        _switch_to(repo, "release/4.15.1")
        cc = self._database(tmp_path / "ci" / "cc.json", repo, after_checkout=True)
        head = head_path(repo)
        assert head is not None
        same = head.stat().st_mtime
        os.utime(cc, (same, same))
        assert not self._decide(repo, db_path, explicit=True, compile_commands=str(cc), background=True)

    def test_the_time_of_a_linked_head_is_the_time_of_the_link(self, repo, tmp_path):
        """With `core.preferSymlinkRefs` HEAD links to the ref, and each commit changes the time of the ref."""
        import os

        from fw_context_mcp.cli._index import _database_after_checkout
        from fw_context_mcp.indexer.git_context import head_path

        head = head_path(repo)
        assert head is not None
        target = tmp_path / "ref"
        target.write_text("0" * 40 + "\n", encoding="utf-8")
        head.unlink()
        head.symlink_to(target)
        cc = tmp_path / "cc.json"
        cc.write_text("[]", encoding="utf-8")
        link_time = head.lstat().st_mtime
        os.utime(cc, (link_time + 100, link_time + 100))
        # A commit after the build of the user: the ref is newer than the database.
        os.utime(target, (link_time + 200, link_time + 200))
        assert _database_after_checkout(cc, repo)

    def test_no_index_records(self, repo, tmp_path):
        assert self._decide(repo, tmp_path / "absent" / "index.db")


class TestEachBuildIsChecked:
    """A project of several builds: the build that moved can be any one."""

    def test_the_plan_sees_a_build_that_is_not_the_newest(self, repo, tmp_path):
        import json

        from fw_context_mcp.cli._index import _plan_auto_build
        from fw_context_mcp.config import load as load_config

        pid = "0123456789abcdef0123456789abcdef"
        cc = repo / ".fw-context" / "build" / "default" / "out" / "compile_commands.json"
        cc.parent.mkdir(parents=True, exist_ok=True)
        cc.write_text(json.dumps([{"directory": str(repo), "file": "main.c",
                                   "arguments": ["cc", "-c", "main.c"]}]), encoding="utf-8")
        db_dir = tmp_path / "index" / pid
        db_dir.mkdir(parents=True)
        conn = open_db(db_dir / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, pid, "p", str(repo))
                # The bootloader recorded another commit; the application, the
                # newest build, is on the tree of now.
                upsert_build_config(conn, "boot", pid, str(cc), manifest_verification="full",
                                    image="bootloader", git_head="a" * 40)
                upsert_build_config(conn, "app", pid, str(cc), manifest_verification="full",
                                    git_head=read_head(repo))
        finally:
            conn.close()
        config_dir = repo / ".fw-context"
        (config_dir / "config.toml").write_text(
            f'[project]\nid = "{pid}"\n[build]\nsystem = "cmake"\n', encoding="utf-8")
        (config_dir / "local.toml").write_text(
            f'[index]\ndb_dir = "{tmp_path / "index"}"\n', encoding="utf-8")

        keys, config, reason = _plan_auto_build(
            repo, db_dir / "index.db", load_config(project_root=repo), None, background=True)

        assert keys == ["checkout:main"], (keys, reason)
        assert config is not None and "commit aaaaaaaaaaaa" in reason

    def test_the_daemon_start_sees_a_checkout(self, indexed_repo):
        from fw_context_mcp.mcp.daemon import _staleness_check

        _switch_to(indexed_repo, "release/4.15.1")
        needs, reasons = _staleness_check(indexed_repo)
        assert needs and any("checkout changed from main to release/4.15.1" in r for r in reasons), reasons


# ── The fixes of the review: stubs, narrowed runs, rebases, detached commits ──


class TestABuildOutsideOfFwContext:
    """A stub such as STM32CubeIDE: the user builds in the IDE, and `--build` fails."""

    def _db(self, repo: Path, tmp_path: Path) -> Path:
        return TestTheRunDecidesWhatItRecords()._db(repo, tmp_path, read_head(repo))

    @staticmethod
    def _default_database(repo: Path, system: str, *, after_checkout: bool) -> None:
        """Write the database that a plain run of a project of *system* reads."""
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.indexer.build import default_compile_commands

        cfg = Config()
        cfg.build.system = system
        TestTheRunDecidesWhatItRecords._database(
            default_compile_commands(repo, cfg), repo, after_checkout=after_checkout,
        )

    @pytest.mark.parametrize("system", ["stm32cubeide", "no-such-system"])
    def test_a_run_of_the_user_records_the_checkout(self, repo, tmp_path, system):
        """After the build of the user.  For an unknown system no run recorded it, and the reason stayed."""
        db_path = self._db(repo, tmp_path)
        _switch_to(repo, "release/4.15.1")
        self._default_database(repo, system, after_checkout=False)
        decide = TestTheRunDecidesWhatItRecords()._decide
        assert decide(repo, db_path, system=system, background=False)
        assert not decide(repo, db_path, system="cmake", background=False), (
            "only a build of fw-context writes that database"
        )

    @pytest.mark.parametrize("system", ["stm32cubeide", "no-such-system"])
    @pytest.mark.parametrize("after_checkout", [True, False])
    def test_a_run_in_the_background_records_after_the_build_of_the_user(
        self, repo, tmp_path, system, after_checkout,
    ):
        """The run of the daemon after a checkout recorded it, and the index read the old database."""
        db_path = self._db(repo, tmp_path)
        _switch_to(repo, "release/4.15.1")
        self._default_database(repo, system, after_checkout=after_checkout)
        decided = TestTheRunDecidesWhatItRecords()._decide(repo, db_path, system=system, background=True)
        assert decided is after_checkout

    def test_the_advice_names_the_ide_and_not_build(self, indexed_repo):
        from fw_context_mcp.mcp.handlers.maintenance import get_project_status

        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n[build]\nsystem = "stm32cubeide"\n',
            encoding="utf-8",
        )
        _switch_to(indexed_repo, "release/4.15.1")
        reason = next(r for r in get_project_status(project_root=str(indexed_repo))["reindex_reasons"]
                      if "checkout changed" in r)
        assert "outside of fw-context" in reason and "--build" not in reason, reason


class TestANarrowedRun:
    """`--image app` builds and records the application alone."""

    def _two_images(self, repo: Path, tmp_path: Path) -> Path:
        db_path = tmp_path / "index" / "index.db"
        db_path.parent.mkdir(parents=True)
        pid = "0123456789abcdef0123456789abcdef"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                upsert_project(conn, pid, "p", str(repo))
                upsert_build_config(conn, "boot", pid, "b.json", manifest_verification="full",
                                    image="mcuboot", git_head="a" * 40)
                upsert_build_config(conn, "app", pid, "a.json", manifest_verification="full",
                                    image="app", git_head=read_head(repo))
        finally:
            conn.close()
        return db_path

    def test_a_build_that_the_run_leaves_out_does_not_count(self, repo, tmp_path):
        import argparse

        from fw_context_mcp.cli._index import _checkout_moves

        db_path = self._two_images(repo, tmp_path)
        conn = open_db(db_path)
        try:
            pid = "0123456789abcdef0123456789abcdef"
            narrowed = argparse.Namespace(variant=None, variants=None, image="app", exclude_image=[])
            excluded = argparse.Namespace(variant=None, variants=None, image=None, exclude_image=["mcuboot"])
            assert _checkout_moves(conn, pid, repo, narrowed) == []
            assert _checkout_moves(conn, pid, repo, excluded) == []
            assert [m[0] for m in _checkout_moves(conn, pid, repo)] == ["mcuboot"]
        finally:
            conn.close()

    def test_the_variant_filters_follow_the_run(self, repo, tmp_path):
        """`--variant` wins over `--variants`, and a project without variants ignores both."""
        import argparse

        from fw_context_mcp.cli._index import _checkout_moves

        db_path = self._two_images(repo, tmp_path)
        conn = open_db(db_path)
        pid = "0123456789abcdef0123456789abcdef"
        try:
            # No build has a variant name: `--variant foo` indexes the build of "".
            ignored = argparse.Namespace(variant="foo", variants=None, image=None, exclude_image=[])
            assert [m[0] for m in _checkout_moves(conn, pid, repo, ignored)] == ["mcuboot"]
            with transaction(conn):
                conn.execute("UPDATE build_configs SET variant = 'A'")
            both = argparse.Namespace(variant="A", variants="B", image=None, exclude_image=[])
            assert [m[0] for m in _checkout_moves(conn, pid, repo, both)] == ["A/mcuboot"]
        finally:
            conn.close()

    def test_the_narrowed_run_records(self, repo, tmp_path):
        db_path = self._two_images(repo, tmp_path)
        decide = TestTheRunDecidesWhatItRecords()._decide
        assert decide(repo, db_path, variant=None, variants=None, image="app", exclude_image=[])
        assert not decide(repo, db_path, variant=None, variants=None, image=None, exclude_image=[])


class TestHeadMovesWithoutACheckout:
    def test_a_rebase_reads_as_the_branch_that_it_updates(self, repo):
        # While a rebase runs, HEAD is detached and moves with each commit.
        before = read_head(repo)
        _git(repo, "checkout", "-q", "--detach")
        state = repo / ".git" / "rebase-merge"
        state.mkdir()
        (state / "head-name").write_text("refs/heads/main\n", encoding="utf-8")
        assert read_head(repo) == before
        assert head_moved(before, "", read_head(repo), repo) == ("", "")

    def test_a_rebase_of_a_detached_head_names_no_branch(self, repo):
        _git(repo, "checkout", "-q", "--detach")
        sha = _git(repo, "rev-parse", "HEAD")
        state = repo / ".git" / "rebase-apply"
        state.mkdir()
        (state / "head-name").write_text("detached HEAD\n", encoding="utf-8")
        assert read_head(repo) == sha

    def test_a_commit_on_a_detached_head_is_no_checkout(self, repo):
        _git(repo, "checkout", "-q", "--detach")
        indexed = read_head(repo)
        _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "third")
        live = read_head(repo)
        assert live != indexed
        assert head_moved(indexed, "", live, repo) == ("", "")
        # Without the tree the reflog cannot be read, thus it is a move.
        assert head_moved(indexed, "", live) != ("", "")

    def test_a_checkout_of_another_commit_is_a_checkout(self, repo):
        first = _git(repo, "rev-parse", "HEAD")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
        _git(repo, "checkout", "-q", "--detach")
        indexed = read_head(repo)
        _git(repo, "checkout", "-q", first)
        assert head_moved(indexed, "", read_head(repo), repo) == (
            f"commit {indexed[:12]}", f"commit {first[:12]}")


class TestTheCacheOfTheHeadPath:
    def test_a_later_git_init_is_seen(self, tmp_path, monkeypatch):
        from fw_context_mcp.indexer import git_context

        tree = tmp_path / "tree"
        tree.mkdir()
        assert head_path(tree) is None
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tree, check=True, capture_output=True)
        assert head_path(tree) is None, "inside the recheck time the answer is the cached one"
        monkeypatch.setattr(git_context, "_NO_GIT_RECHECK_S", 0.0)
        assert read_head(tree) == "ref: refs/heads/main"

    def test_a_relative_root_gives_an_absolute_path(self, repo, monkeypatch):
        monkeypatch.chdir(repo.parent)
        found = head_path(Path(repo.name))
        assert found is not None and found.is_absolute()
        monkeypatch.chdir("/")
        assert read_head(Path(repo)) == "ref: refs/heads/main"

    def test_a_git_directory_that_went_away_is_asked_again(self, repo):
        import shutil

        assert head_path(repo) is not None
        shutil.rmtree(repo / ".git")
        assert head_path(repo) is None, "the cache kept the path of a removed git directory"


class TestARunThatBuiltOnAnotherPath:
    """A database that the run built belongs to the tree of now, also without `--build`."""

    def test_the_single_build(self, tmp_path, monkeypatch):
        import argparse

        from fw_context_mcp.cli import _index
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.indexer import runner

        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")
        seen: list[bool] = []
        monkeypatch.setattr(_index, "_resolve_compile_commands", lambda *a, **k: (cc, False))
        monkeypatch.setattr(_index, "_validate_and_fix_artifacts", lambda *a, **k: (cc, [], True))
        monkeypatch.setattr(_index, "_post_index_optimize", lambda *a, **k: None)
        monkeypatch.setattr(runner, "run", lambda **k: seen.append(k["record_git"]) or "h" * 16)
        cfg = Config()
        cfg.build.system = "cmake"
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()

        for built in (False, True):
            args = argparse.Namespace(image=None, exclude_image=[], variant=None, variants=None)
            if built:
                _index._note_build(args)
            _index._run_single(args, cfg, tmp_path, "proj", db_path, "cmake", False, {"record_git": False})

        assert seen == [False, True]

    def test_a_variant_that_the_run_built(self, tmp_path, monkeypatch):
        import argparse

        from fw_context_mcp.cli import _index
        from fw_context_mcp.config.settings import Config
        from fw_context_mcp.indexer import runner
        from fw_context_mcp.indexer.build import BuildVariant

        found = [("a", "", tmp_path / "a.json", "")]
        built = [("b", "", tmp_path / "b.json", "")]
        monkeypatch.setattr(_index, "_discover_existing_cc", lambda *a, **k: list(found))
        monkeypatch.setattr(_index, "_build_variants", lambda *a, **k: list(built))
        monkeypatch.setattr(_index, "_post_index_optimize", lambda *a, **k: None)
        seen: dict[str, bool] = {}
        monkeypatch.setattr(runner, "run", lambda *, variant="", **k: seen.__setitem__(variant, k["record_git"]) or "h" * 16)
        cfg = Config()
        cfg.build.system = "cmake"
        cfg.build.variants = [BuildVariant(name="a"), BuildVariant(name="b")]
        args = argparse.Namespace(build=False, background=False, image=None, exclude_image=[],
                                  variant=None, variants=None)
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()

        _index._run_multi(args, cfg, tmp_path, "proj", db_path, "cmake",
                          {"vendor_paths": [], "project_paths": [], "record_git": False})

        assert seen == {"a": False, "b": True}
