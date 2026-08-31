"""A branch switch needs `index --build`, not a plain reindex.

`compile_commands.json` is a build artifact of the branch it was generated
on: it carries that branch's file list AND its compiler flags.  A reindex
reads the same file again.

Measured on zbox-ecb-fw: switching from 4.15.3 to 4.15.1 left two generated
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
    branch_moved_since,
    branch_of_description,
    current_branch,
    get_git_description,
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
        # The measured shape on zbox-ecb-fw.
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


class TestTheCurrentBranch:
    def test_a_repository_on_a_branch(self, repo):
        assert current_branch(repo) == "main"

    def test_a_detached_head(self, repo):
        # `git rev-parse --abbrev-ref HEAD` answers "HEAD" here, which is a
        # position and not a branch name.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "-q", head], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        assert current_branch(repo) == ""

    def test_a_directory_that_is_not_a_repository(self, tmp_path):
        assert current_branch(tmp_path / "nowhere") == ""


class TestTheDetection:
    def test_the_same_branch(self, repo):
        assert branch_moved_since("branch: main", repo) == ("", "")

    def test_another_branch(self, repo):
        subprocess.run(
            ["git", "checkout", "-q", "-b", "release/4.15.1"], cwd=repo,
            check=True, capture_output=True, text=True,
        )
        assert branch_moved_since("branch: main", repo) == ("main", "release/4.15.1")

    def test_a_new_tag_on_the_same_branch_is_not_a_move(self, repo):
        # The description also carries the last tag, and a release tag moves
        # no file.  Comparing whole descriptions would ask for a build after
        # every tag.
        subprocess.run(
            ["git", "tag", "v1.0.0"], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        before = "branch: main"
        after = get_git_description(repo)
        assert "tag: v1.0.0" in after, "the fixture did not tag"
        assert before != after
        assert branch_moved_since(before, repo) == ("", "")

    def test_an_index_with_no_recorded_branch(self, repo):
        # Nothing to compare against, thus nothing is reported.
        assert branch_moved_since("", repo) == ("", "")
        assert branch_moved_since("tag: v1.0.0", repo) == ("", "")

    def test_a_detached_head_reports_nothing(self, repo):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "-q", head], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        # A documented blind spot: checking out a tag or a commit leaves no
        # branch name on either side.  Reporting a move would be a guess,
        # and the ordinary staleness checks still see the changed files.
        assert branch_moved_since("branch: main", repo) == ("", "")

    def test_a_project_that_is_not_a_repository(self, tmp_path):
        assert branch_moved_since("branch: main", tmp_path) == ("", "")


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
    """`get_active_build` is the mandatory first call, so the reason has to
    reach it — twice in this series a wiring mistake survived unit tests."""

    def _state(self, root: Path) -> dict:
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        return get_active_build(project_root=str(root))

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
            if "branch changed" in reason
        ], state.get("reindex_reasons")

    def test_a_switch_asks_for_index_build(self, indexed_repo):
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)

        assert state["reindex_needed"] is True
        reasons = state["reindex_reasons"]
        moved = [r for r in reasons if "branch changed" in r]
        assert moved, f"no reason names the branch change: {reasons}"
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

        Measured on a real checkout of zbox-ecb-fw: this reason read as a
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
            r for r in state["reindex_reasons"] if "branch changed" in r
        )
        assert "background reindex" in reason, reason
        assert "--build" in reason, reason

    def test_a_backend_that_cannot_build_gets_the_command_alone(
        self, indexed_repo
    ):
        # Keil detects and builds nothing, thus nothing will happen on its
        # own and the command is the whole answer.
        (indexed_repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "keil"\n',
            encoding="utf-8",
        )
        _switch_to(indexed_repo, "release/4.15.1")
        state = self._state(indexed_repo)
        reason = next(
            r for r in state["reindex_reasons"] if "branch changed" in r
        )
        assert "background reindex" not in reason, reason
        assert "run `fw-context index --build`" in reason, reason


# ── The daemon reindexes with --build ──────────────────────────────────────


class TestTheDaemonDecision:
    """`daemon._branch_needs_build` answers with a branch or with nothing.

    A git checkout is one of the bursts the reindex loop already debounces —
    its own comment says so — and the only missing piece was that the run it
    starts reused `compile_commands.json` from the branch the tree left.

    Every guard here answers "no", which costs a plain reindex and a reported
    reason rather than an unwanted build.
    """

    def _indexed(self, repo: Path, db_dir: Path, description: str) -> None:
        """Put one build config with *description* in a database."""
        conn = open_db(db_dir / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(repo))
                upsert_build_config(
                    conn, "ch", "pid", str(repo / "compile_commands.json"),
                    description=description,
                )
        finally:
            conn.close()

    def test_the_same_branch_needs_no_build(self, repo, tmp_path):
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        assert _branch_needs_build(repo, db_dir) == ""

    def test_no_database_needs_no_build(self, repo, tmp_path):
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        assert _branch_needs_build(repo, tmp_path / "absent") == ""

    def test_no_recorded_branch_needs_no_build(self, repo, tmp_path):
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "")
        assert _branch_needs_build(repo, db_dir) == ""

    def test_a_switch_asks_for_the_new_branch(self, repo, tmp_path):
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        # A build system that may build in the background.  Without one the
        # answer is "no" regardless of the branch.
        (repo / ".fw-context").mkdir(exist_ok=True)
        (repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(repo, "release/4.15.1")
        assert _branch_needs_build(repo, db_dir) == "release/4.15.1"

    def test_a_build_system_that_refuses_the_background(self, repo, tmp_path):
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        # A detection-only backend: it builds nothing, thus it must not be
        # asked to build in the background.
        (repo / ".fw-context").mkdir(exist_ok=True)
        (repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "keil"\n',
            encoding="utf-8",
        )
        _switch_to(repo, "release/4.15.1")
        assert _branch_needs_build(repo, db_dir) == ""

    def test_a_recent_failure_for_the_same_branch_blocks(self, repo, tmp_path):
        # A branch that does not build would otherwise start a build on every
        # burst of file changes.
        from fw_context_mcp.indexer.autobuild import record_failure
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        (repo / ".fw-context").mkdir(exist_ok=True)
        (repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(repo, "release/4.15.1")
        record_failure(db_dir, ["branch:release/4.15.1"])
        assert _branch_needs_build(repo, db_dir) == ""

    def test_a_failure_for_another_branch_does_not_block(self, repo, tmp_path):
        # The marker holds the key of the attempt, so a different branch is
        # worth one try.
        from fw_context_mcp.indexer.autobuild import record_failure
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        (repo / ".fw-context").mkdir(exist_ok=True)
        (repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(repo, "release/4.15.1")
        record_failure(db_dir, ["branch:some/other"])
        assert _branch_needs_build(repo, db_dir) == "release/4.15.1"

    def test_a_source_failure_does_not_block_a_branch_build(self, repo, tmp_path):
        # One marker file serves two triggers, and the comparison is on the
        # list contents, thus they do not interfere.
        from fw_context_mcp.indexer.autobuild import record_failure
        from fw_context_mcp.mcp.daemon import _branch_needs_build

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        self._indexed(repo, db_dir, "branch: main")
        (repo / ".fw-context").mkdir(exist_ok=True)
        (repo / ".fw-context" / "config.toml").write_text(
            '[project]\nid = "0123456789abcdef0123456789abcdef"\n'
            '[build]\nsystem = "cmake"\n',
            encoding="utf-8",
        )
        _switch_to(repo, "release/4.15.1")
        record_failure(db_dir, ["src/new_file.c"])
        assert _branch_needs_build(repo, db_dir) == "release/4.15.1"


class TestTheFlagReachesTheSubprocess:
    """`--build` has to be in the REAL argv, not only in the decision.

    The spawn is intercepted rather than reconstructed: a test that rebuilds
    the argument list checks a literal the test author wrote, not the code.
    """

    async def _spawn_argv(self, tmp_path: Path, *, with_build: bool) -> list[str]:
        import asyncio as aio

        from fw_context_mcp.mcp import daemon

        captured: list[str] = []

        class _FakeProc:
            pid = 4242
            returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured.extend(str(arg) for arg in args)
            return _FakeProc()

        original = aio.create_subprocess_exec
        aio.create_subprocess_exec = _fake_exec  # type: ignore[assignment]
        try:
            await daemon._run_index_async(
                tmp_path, tmp_path, with_build=with_build
            )
        finally:
            aio.create_subprocess_exec = original  # type: ignore[assignment]
        return captured

    def test_without_the_flag(self, tmp_path):
        import asyncio as aio

        argv = aio.run(self._spawn_argv(tmp_path, with_build=False))
        assert "index" in argv
        assert "--background" in argv
        assert "--build" not in argv

    def test_with_the_flag(self, tmp_path):
        import asyncio as aio

        argv = aio.run(self._spawn_argv(tmp_path, with_build=True))
        assert "--background" in argv
        assert "--build" in argv
        # Order matters to argparse only in that both are options of `index`.
        assert argv.index("index") < argv.index("--build")

    def test_the_helper_accepts_the_keyword(self):
        import inspect

        from fw_context_mcp.mcp import daemon

        signature = inspect.signature(daemon._run_index_async)
        assert "with_build" in signature.parameters
        assert signature.parameters["with_build"].default is False

    def test_the_loop_passes_the_decision_through(self):
        # The wiring, read from the source: the loop must call the decision
        # and hand it to the spawn.  Twice in this series a wiring mistake
        # survived tests that checked the parts.
        import inspect

        from fw_context_mcp.mcp import daemon

        source = inspect.getsource(daemon._reindex_loop)
        assert "_branch_needs_build" in source
        assert "with_build=bool(build_for)" in source
        assert "record_failure" in source
