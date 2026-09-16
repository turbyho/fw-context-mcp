"""The ``project`` selector must reach the named project, or fail loudly.

Before this selector existed, the only way to name a project was
``project_root`` — an absolute path.  A caller that wrote
``project="<name>"`` got no error: pydantic ignores an unknown argument,
thus the tool answered about the project of the current directory.  The
answer looked correct but came from other source code.

These tests hold the four parts that together close that hole:

- ``get_projects_by_name`` — the registry lookup
- ``resolve_project_root`` — one field that takes a path, a name, or an id
- ``_with_project_selector`` — the ``project`` parameter of each tool
- ``_forbid_unknown_tool_arguments`` — an unknown argument is an error
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from fw_context_mcp.config import global_db
from fw_context_mcp.mcp import server
from fw_context_mcp.utils import AmbiguousProjectError, resolve_project_root

# sqlite3 through the module that the code under test uses.  A separate
# import in this file would bind the standard library module when the
# pysqlite3 redirect has not run yet, and the two exception hierarchies
# do not match.
sqlite3 = global_db.sqlite3


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Give the global project registry its own database for one test.

    ``_global_db_path()`` reads the module attribute at call time, thus a
    monkeypatch of that attribute reaches ``open_global_db()``.  The
    cached connection must be cleared as well, otherwise the test reuses
    the connection to the real registry of the user.
    """
    monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", tmp_path / "projects.db")
    monkeypatch.setattr(global_db, "_global_conn", None)
    conn = global_db.open_global_db()
    yield conn
    conn.close()


def _register(conn, project_id: str, name: str, root_path) -> None:
    """Write one project into the test registry."""
    global_db.upsert_project_registry(conn, project_id, name, "mbed-os", str(root_path))


def _project_dir(root, project_id: str):
    """Make a directory that answers for *project_id*, as ``init`` leaves it."""
    marker = root / ".fw-context"
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n', encoding="utf-8"
    )
    return root


# ── The registry can be redirected, and the suite redirects it ─────────


class TestTheRegistryPathIsRedirectable:
    """A test must not write the registry of the operator.

    ``conftest.py`` pointed ``FW_CONTEXT_INDEX_DIR`` at a temp directory
    from the start, and the registry had no such door.  Every run of this
    suite therefore registered its throwaway projects for real: measured
    on one machine, 28989 rows of which 6 named a project that exists.
    """

    def test_the_environment_moves_the_registry(self, tmp_path, monkeypatch):
        """A subprocess cannot inherit a monkeypatched attribute.

        This suite spawns the CLI in one, thus the redirect has to travel
        through the environment to reach it.
        """
        monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", global_db._DEFAULT_GLOBAL_DB_PATH)
        monkeypatch.setenv("FW_CONTEXT_PROJECTS_DB", str(tmp_path / "moved.db"))
        assert global_db._global_db_path() == tmp_path / "moved.db"

    def test_an_explicit_path_wins_over_the_environment(self, tmp_path, monkeypatch):
        """The in-process test knows which registry it wants."""
        monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", tmp_path / "explicit.db")
        monkeypatch.setenv("FW_CONTEXT_PROJECTS_DB", str(tmp_path / "from-env.db"))
        assert global_db._global_db_path() == tmp_path / "explicit.db"

    def test_the_default_answers_when_nothing_redirects(self, monkeypatch):
        monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", global_db._DEFAULT_GLOBAL_DB_PATH)
        monkeypatch.delenv("FW_CONTEXT_PROJECTS_DB", raising=False)
        assert global_db._global_db_path() == global_db._DEFAULT_GLOBAL_DB_PATH

    def test_a_moved_path_reopens_the_cached_connection(self, tmp_path, monkeypatch):
        """The cache is keyed on the path, thus a redirect reaches it.

        A connection opened before the redirect would read the registry
        that the caller just moved away from — and a test would then write
        the real one.
        """
        monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", global_db._DEFAULT_GLOBAL_DB_PATH)
        monkeypatch.setattr(global_db, "_global_conn", None)
        monkeypatch.setattr(global_db, "_global_conn_path", None)

        monkeypatch.setenv("FW_CONTEXT_PROJECTS_DB", str(tmp_path / "first.db"))
        first = global_db.open_global_db()
        global_db.upsert_project_registry(
            first, "a" * 32, "only-in-first", "mbed-os", str(tmp_path / "p")
        )
        assert global_db.get_projects_by_name("only-in-first")

        monkeypatch.setenv("FW_CONTEXT_PROJECTS_DB", str(tmp_path / "second.db"))
        second = global_db.open_global_db()
        assert second is not first, "the connection did not follow the redirect"
        assert global_db.get_projects_by_name("only-in-first") == []
        second.close()


# ── The registry keeps only the rows that disk answers for ─────────────


class TestPruneStaleProjects:
    """A registry row is worth keeping while something still confirms it.

    Nothing removed a row until this existed: one was written at ``init``
    and at ``index``, and a project that moved, that went away, or that
    took a new id left its old row for ever.  Measured on one machine,
    28989 rows of which 6 named a project that exists.

    The cost of a leftover is narrow and real: the row carries a NAME, and
    two rows of one name make ``project="<name>"`` fail with an ambiguity
    error.  The id selector and ``project_root`` still answer, and no
    result of any tool is wrong.
    """

    def test_a_row_whose_config_declares_it_survives(self, registry, tmp_path):
        pid = "a" * 32
        _register(registry, pid, "keeper", _project_dir(tmp_path / "keeper", pid))
        assert global_db.prune_stale_projects(registry) == 0
        assert len(global_db.get_projects_by_name("keeper")) == 1

    def test_a_row_whose_root_is_gone_is_dropped(self, registry, tmp_path):
        _register(registry, "b" * 32, "vanished", tmp_path / "vanished")
        assert global_db.prune_stale_projects(registry) == 1
        assert global_db.get_projects_by_name("vanished") == []

    def test_a_row_that_a_new_id_replaced_is_dropped(self, registry, tmp_path):
        """The case that a reader meets: one path, two ids, one name.

        The project regenerated its id, and the row of the old one stayed.
        The config at the root names the new id, thus the old row is the
        one that nothing answers for.
        """
        root = tmp_path / "FM"
        new_id, old_id = "c" * 32, "d" * 32
        _register(registry, old_id, "FM", root)
        _register(registry, new_id, "FM", _project_dir(root, new_id))
        assert len(global_db.get_projects_by_name("FM")) == 2

        assert global_db.prune_stale_projects(registry) == 1
        rows = global_db.get_projects_by_name("FM")
        assert [r["project_id"] for r in rows] == [new_id]

    def test_a_config_with_no_id_confirms_nothing(self, registry, tmp_path):
        """A rule written as a list of deaths missed this shape.

        Measured on one machine: 107 rows of a directory that still
        existed and held a config that declared no id at all.
        """
        root = tmp_path / "half"
        (root / ".fw-context").mkdir(parents=True)
        (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
        _register(registry, "e" * 32, "half", root)
        assert global_db.prune_stale_projects(registry) == 1

    def test_an_id_of_another_table_does_not_answer(self, registry, tmp_path):
        """Only the ``[project]`` table names the project.

        A scan that took the first ``id`` of the file would let another
        table decide whether a registry row lives.
        """
        root = tmp_path / "elsewhere"
        (root / ".fw-context").mkdir(parents=True)
        (root / ".fw-context" / "config.toml").write_text(
            '[llm]\nid = "9" * 32\n\n[project]\nid = "7777777777777777777777777777777a"\n',
            encoding="utf-8",
        )
        _register(registry, "7" * 31 + "a", "elsewhere", root)
        assert global_db.prune_stale_projects(registry) == 0

    def test_a_comment_after_the_id_is_not_part_of_it(self, registry, tmp_path):
        root = tmp_path / "commented"
        pid = "8" * 32
        (root / ".fw-context").mkdir(parents=True)
        (root / ".fw-context" / "config.toml").write_text(
            f'[project]\nid = "{pid}"   # generated at init\n', encoding="utf-8"
        )
        _register(registry, pid, "commented", root)
        assert global_db.prune_stale_projects(registry) == 0

    def test_a_root_without_the_marker_is_dropped(self, registry, tmp_path):
        root = tmp_path / "plain"
        root.mkdir()
        _register(registry, "f" * 32, "plain", root)
        assert global_db.prune_stale_projects(registry) == 1

    def test_an_index_of_that_id_keeps_the_row(self, registry, tmp_path, monkeypatch):
        """The guard against a false positive.

        A project on a mount that is not attached cannot confirm itself,
        and its index still can.
        """
        index_root = tmp_path / "index"
        pid = "0" * 32
        (index_root / pid).mkdir(parents=True)
        (index_root / pid / "index.db").write_bytes(b"")
        monkeypatch.setenv("FW_CONTEXT_INDEX_DIR", str(index_root))

        _register(registry, pid, "unmounted", tmp_path / "not-here")
        assert global_db.prune_stale_projects(registry) == 0
        assert len(global_db.get_projects_by_name("unmounted")) == 1

    def test_the_prune_is_idempotent(self, registry, tmp_path):
        _register(registry, "1" * 32, "gone-a", tmp_path / "gone-a")
        _register(registry, "2" * 32, "gone-b", tmp_path / "gone-b")
        assert global_db.prune_stale_projects(registry) == 2
        assert global_db.prune_stale_projects(registry) == 0

    def test_an_empty_root_path_confirms_nothing(self, registry):
        _register(registry, "3" * 32, "rootless", "")
        assert global_db.prune_stale_projects(registry) == 1


class TestTheOperatorCanRefuseThePrune:
    """A DELETE over the data of the operator has to be refusable.

    Nothing asks before this runs, and the judgement can be wrong: a
    project on a filesystem that is not mounted right now, whose index
    lives somewhere other than the default directory, confirms itself
    through neither test and is dropped.  The cost is one row that the
    next ``init`` or ``index`` writes again — small, but the operator
    must be able to say no, and must be able to see what went.
    """

    def test_a_delete_that_fails_does_not_report_a_removal(self, registry, tmp_path):
        """"removed 0 rows: gone-a" told the operator the opposite of the truth.

        ``_delete_project_rows`` answers 0 for a registry it cannot write,
        and the report used to paste that 0 into a sentence that went on
        to name the rows as if they had gone.
        """
        import sqlite3 as _sqlite3

        _register(registry, "4" * 32, "gone-a", tmp_path / "gone-a")
        _register(registry, "5" * 32, "gone-b", tmp_path / "gone-b")

        # A real read-only handle on the same file: the SELECT answers and
        # the DELETE raises, which is the shape a locked or read-only
        # ``~/.fw-context`` gives.
        readonly = _sqlite3.connect(
            f"file:{tmp_path / 'projects.db'}?mode=ro", uri=True
        )
        readonly.row_factory = _sqlite3.Row
        try:
            assert len(global_db.stale_project_rows(readonly)) == 2
            message = global_db.prune_registry_report(readonly)
        finally:
            readonly.close()

        assert "could NOT remove" in message, message
        assert "removed 2" not in message, message
        assert len(global_db.get_projects_by_name("gone-a")) == 1, (
            "the rows must still be there"
        )

    def test_the_report_scans_the_disk_once(self, registry, tmp_path, monkeypatch):
        """Two scans cost twice, and they can disagree with each other.

        The names come from the scan and the count from the delete.  When
        the report ran its own scan and the delete ran another, a row that
        gained a config between them was named as removed and stayed.
        """
        _register(registry, "6" * 32, "gone-c", tmp_path / "gone-c")
        calls = {"n": 0}
        real = global_db.stale_project_rows

        def _counted(conn):
            calls["n"] += 1
            return real(conn)

        monkeypatch.setattr(global_db, "stale_project_rows", _counted)
        global_db.prune_registry_report(registry)

        assert calls["n"] == 1, f"the disk was scanned {calls['n']} times"

    def test_the_rows_can_be_read_before_they_go(self, registry, tmp_path):
        _register(registry, "6" * 32, "gone-c", tmp_path / "gone-c")
        pid = "7" * 32
        _register(registry, pid, "keeper", _project_dir(tmp_path / "keeper", pid))

        stale = global_db.stale_project_rows(registry)

        assert [r["name"] for r in stale] == ["gone-c"], stale
        assert len(global_db.get_projects_by_name("gone-c")) == 1, "reading deleted"

    def test_the_report_names_the_projects_it_removed(self, registry, tmp_path):
        _register(registry, "8" * 32, "gone-d", tmp_path / "gone-d")

        message = global_db.prune_registry_report(registry)

        assert "gone-d" in message, message
        assert "--no-prune" in message, f"the way out must be in the text: {message}"
        assert global_db.get_projects_by_name("gone-d") == []

    def test_keep_names_the_projects_and_leaves_them(self, registry, tmp_path):
        _register(registry, "9" * 32, "gone-e", tmp_path / "gone-e")

        message = global_db.prune_registry_report(registry, keep=True)

        assert "gone-e" in message, message
        assert "kept" in message, message
        assert len(global_db.get_projects_by_name("gone-e")) == 1, (
            "--no-prune removed a row anyway"
        )

    def test_a_tidy_registry_reports_nothing(self, registry, tmp_path):
        pid = "a" * 32
        _register(registry, pid, "keeper", _project_dir(tmp_path / "keeper", pid))

        assert global_db.prune_registry_report(registry) == ""

    def test_a_background_run_prunes_nothing(self, registry, tmp_path, capsys):
        """The daemon spawns ``index --background`` with a fixed argv.

        No flag reaches that run and its output goes to reindex.log, thus a
        delete there is one the operator can neither refuse nor see.  The
        next run that the operator starts does the work instead.
        """
        import argparse

        from fw_context_mcp.cli._index import _post_index_optimize

        _register(registry, "b" * 32, "gone-f", tmp_path / "gone-f")
        db_path = tmp_path / "index.db"
        db_path.touch()
        root = _project_dir(tmp_path / "live", "c" * 32)

        _post_index_optimize(
            db_path, root, "c" * 32, "bare",
            argparse.Namespace(background=True, no_prune=False),
        )

        assert len(global_db.get_projects_by_name("gone-f")) == 1, (
            "a background run removed a registry row"
        )
        assert "registry:" not in capsys.readouterr().out

    def test_a_foreground_run_still_prunes(self, registry, tmp_path, capsys):
        import argparse

        from fw_context_mcp.cli._index import _post_index_optimize

        _register(registry, "d" * 32, "gone-g", tmp_path / "gone-g")
        db_path = tmp_path / "index.db"
        db_path.touch()
        root = _project_dir(tmp_path / "live2", "e" * 32)

        _post_index_optimize(
            db_path, root, "e" * 32, "bare",
            argparse.Namespace(background=False, no_prune=False),
        )

        assert global_db.get_projects_by_name("gone-g") == []
        assert "gone-g" in capsys.readouterr().out

    def test_a_write_does_not_prune(self, registry, tmp_path):
        """``upsert_project_registry`` writes and nothing else.

        A write that also deleted would judge every OTHER row at a moment
        that says nothing about them, and a caller that wants one row
        written could not ask for that alone.
        """
        _register(registry, "4" * 32, "ghost", tmp_path / "ghost")
        _register(registry, "5" * 32, "second", tmp_path / "second")
        assert len(global_db.get_projects_by_name("ghost")) == 1


# ── Registry lookup ────────────────────────────────────────────────────


def test_a_name_with_no_match_gives_an_empty_list(registry):
    assert global_db.get_projects_by_name("no-such-project") == []


def test_a_name_with_one_match_gives_that_project(registry, tmp_path):
    _register(registry, "a" * 32, "boot-loader", tmp_path / "boot-loader")
    rows = global_db.get_projects_by_name("boot-loader")
    assert [r["project_id"] for r in rows] == ["a" * 32]


def test_a_duplicate_name_gives_every_match(registry, tmp_path):
    """The name column has no UNIQUE constraint, thus duplicates are real."""
    _register(registry, "a" * 32, "boot-loader", tmp_path / "work" / "boot-loader")
    _register(registry, "b" * 32, "boot-loader", tmp_path / "archive" / "boot-loader")
    rows = global_db.get_projects_by_name("boot-loader")
    assert len(rows) == 2
    assert {r["project_id"] for r in rows} == {"a" * 32, "b" * 32}


def test_the_match_is_exact(registry, tmp_path):
    """A prefix match would let one name select a different project."""
    _register(registry, "a" * 32, "boot-loader", tmp_path / "boot-loader")
    assert global_db.get_projects_by_name("boot") == []
    assert global_db.get_projects_by_name("boot-loader-v2") == []


# ── resolve_project_root ───────────────────────────────────────────────


def test_an_existing_directory_wins_over_a_registry_name(registry, tmp_path, monkeypatch):
    """A directory that the caller can see must not be shadowed by a name."""
    visible = tmp_path / "boot-loader"
    visible.mkdir()
    _register(registry, "a" * 32, "boot-loader", tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == visible.resolve()


def test_a_name_resolves_to_the_registered_root(registry, tmp_path, monkeypatch):
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == root.resolve()


def test_a_project_id_resolves_to_the_registered_root(registry, tmp_path, monkeypatch):
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("a" * 32) == root.resolve()


def test_an_ambiguous_name_names_every_candidate(registry, tmp_path, monkeypatch):
    """fw-context must not choose — the wrong choice looks correct."""
    first = tmp_path / "work" / "boot-loader"
    second = tmp_path / "archive" / "boot-loader"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", first)
    _register(registry, "b" * 32, "boot-loader", second)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AmbiguousProjectError) as exc:
        resolve_project_root("boot-loader")
    message = str(exc.value)
    assert "a" * 32 in message
    assert "b" * 32 in message
    assert str(first) in message
    assert str(second) in message


def test_an_unknown_selector_stays_a_path(registry, tmp_path, monkeypatch):
    """The behaviour for a path that does not exist yet does not change."""
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("not-indexed") == (tmp_path / "not-indexed").resolve()


def test_a_broken_registry_does_not_stop_path_resolution(tmp_path, monkeypatch):
    """A registry that cannot be read must not break every tool call."""

    def _raise(_selector):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(global_db, "get_project_by_id", _raise)
    monkeypatch.setattr(global_db, "get_projects_by_name", _raise)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == (tmp_path / "boot-loader").resolve()


def test_no_selector_keeps_the_git_root_search(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "drivers"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_project_root(None) == repo


# ── _merge_project_selector ────────────────────────────────────────────


def test_project_becomes_project_root():
    kwargs = server._merge_project_selector("lookup_symbol", {"name": "main", "project": "boot"})
    assert kwargs == {"name": "main", "project_root": "boot"}


def test_an_empty_project_keeps_the_default():
    kwargs = server._merge_project_selector("lookup_symbol", {"name": "main", "project": "  "})
    assert kwargs == {"name": "main"}


def test_the_same_value_in_both_fields_is_accepted():
    kwargs = server._merge_project_selector(
        "lookup_symbol", {"project": "/tmp/boot", "project_root": "/tmp/boot"}
    )
    assert kwargs == {"project_root": "/tmp/boot"}


def test_two_different_projects_in_one_call_is_an_error():
    """Choosing one of them silently would answer from the wrong project."""
    with pytest.raises(ValueError, match="different projects"):
        server._merge_project_selector(
            "lookup_symbol", {"project": "boot-loader", "project_root": "/tmp/other"}
        )


def test_a_name_and_the_root_of_that_same_project_are_accepted(
    registry, tmp_path, monkeypatch
):
    """One row of ``list_projects`` carries both spellings of one project.

    A caller that reads ``name`` and ``root_path`` from one row and sends
    both is repeating itself, not contradicting itself.  The comparison
    used to be string against string, thus the call was refused with a
    sentence that states a fact nobody checked: "select different
    projects".
    """
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)

    kwargs = server._merge_project_selector(
        "lookup_symbol", {"project": "boot-loader", "project_root": str(root)}
    )
    assert kwargs == {"project_root": "boot-loader"}


def test_a_project_id_and_the_root_of_that_project_are_accepted(
    registry, tmp_path, monkeypatch
):
    """The id is the third spelling of the same project."""
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "b" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)

    kwargs = server._merge_project_selector(
        "lookup_symbol", {"project": "b" * 32, "project_root": str(root)}
    )
    assert kwargs == {"project_root": "b" * 32}


def test_a_name_and_the_root_of_another_project_is_still_an_error(
    registry, tmp_path, monkeypatch
):
    """Two spellings that resolve apart keep the refusal — and now it is true."""
    mine = tmp_path / "vendor" / "boot-loader"
    other = tmp_path / "vendor" / "application"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    _register(registry, "c" * 32, "boot-loader", mine)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="different projects"):
        server._merge_project_selector(
            "lookup_symbol", {"project": "boot-loader", "project_root": str(other)}
        )


def test_two_spellings_of_one_path_are_accepted(tmp_path, monkeypatch):
    """A relative and an absolute spelling of one directory are one project."""
    root = tmp_path / "work" / "boot-loader"
    root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "work")

    kwargs = server._merge_project_selector(
        "lookup_symbol", {"project": "boot-loader", "project_root": str(root)}
    )
    assert kwargs == {"project_root": "boot-loader"}


def test_an_ambiguous_name_beside_a_root_is_refused(registry, tmp_path, monkeypatch):
    """A name that reaches two projects cannot confirm the root beside it."""
    first = tmp_path / "work" / "boot-loader"
    second = tmp_path / "archive" / "boot-loader"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _register(registry, "d" * 32, "boot-loader", first)
    _register(registry, "e" * 32, "boot-loader", second)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="different projects"):
        server._merge_project_selector(
            "lookup_symbol", {"project": "boot-loader", "project_root": str(first)}
        )


# ── The registered tools ───────────────────────────────────────────────


def _tools():
    """Return every registered MCP tool."""
    return asyncio.run(server.mcp.list_tools())


TOOLS = _tools()
TOOL_IDS = [t.name for t in TOOLS]


def test_the_server_registers_tools():
    assert len(TOOLS) >= 38, TOOL_IDS


def test_the_module_docstring_counts_what_the_server_serves():
    """A docstring that drifts describes a server that no longer exists.

    It said "37 MCP tools" while 39 were registered.  The number is the
    first thing a reader of the module sees, thus it is pinned here.
    """
    doc = server.__doc__ or ""
    resources = asyncio.run(server.mcp.list_resource_templates())
    static = asyncio.run(server.mcp.list_resources())
    assert f"Serves {len(TOOLS)} MCP tools" in doc, doc.splitlines()[:4]
    assert f"and {len(resources) + len(static)} MCP resources" in doc, (
        doc.splitlines()[:4]
    )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_a_tool_that_takes_a_project_root_also_takes_a_project(tool):
    """One tool without the parameter recreates the original failure."""
    properties = tool.inputSchema.get("properties", {})
    if "project_root" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    assert "project" in properties, f"{tool.name} has project_root but no project"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_project_parameter_is_described(tool):
    """The description is what the LLM reads before it calls the tool."""
    properties = tool.inputSchema.get("properties", {})
    if "project" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    assert properties["project"].get("description")
    assert "list_projects" in properties["project"]["description"], (
        f"{tool.name}: the description does not say where the names come from"
    )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_project_entry_sits_in_the_args_section(tool):
    """A parameter documented after Returns: is not in the parameter list.

    The model reads ``Args:`` to learn what it can pass.  An entry that
    hangs at the end of the docstring is a different kind of text, and
    every other parameter of the tool is in ``Args:``.
    """
    properties = tool.inputSchema.get("properties", {})
    if "project" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    doc = tool.description or ""
    entry = re.search(r"^[ \t]*project:", doc, re.MULTILINE)
    assert entry, f"{tool.name}: the docstring has no `project:` entry"
    assert "Args:" in doc, f"{tool.name}: the docstring has no Args: section"
    assert doc.index("Args:") < entry.start(), f"{tool.name}: `project:` is before Args:"
    if "Returns:" in doc:
        assert entry.start() < doc.index("Returns:"), (
            f"{tool.name}: `project:` sits after Returns:, thus outside the parameter list"
        )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_project_root_says_that_it_takes_a_name_too(tool):
    """resolve_project_root resolves a name and an id — the text must say so."""
    properties = tool.inputSchema.get("properties", {})
    if "project_root" not in properties:
        pytest.skip(f"{tool.name} takes no project_root")
    description = properties["project_root"].get("description", "")
    assert "project name or a project_id" in description, (
        f"{tool.name}: project_root still describes a path only"
    )


def test_the_original_project_root_text_is_kept():
    """The handlers say different useful things — none may be lost."""
    tools = {t.name: t for t in TOOLS}
    listing = tools["list_projects"].inputSchema["properties"]["project_root"]["description"]
    assert "distinguish multiple indexed projects" in listing


def test_a_docstring_without_an_args_section_gets_a_separate_block():
    """The fallback covers a handler that someone adds later."""
    result = server._document_project_parameter("Only a summary.\n")
    assert "Project selection:" in result
    assert "project:" in result


def test_the_entry_takes_the_indentation_of_its_neighbour():
    """Handler docstrings are not indented the same way."""
    doc = "Summary.\n\nArgs:\n  project_root: A path.\n  limit: A number.\n"
    result = server._document_project_parameter(doc)
    lines = result.splitlines()
    entry = next(i for i, line in enumerate(lines) if line.lstrip().startswith("project:"))
    assert lines[entry].startswith("  project:")
    assert lines[entry - 1].strip().startswith("project_root:")
    assert lines[-1].strip().startswith("limit:")


def test_a_multi_line_project_root_entry_is_not_split():
    """The new entry must go after the continuation lines, not into them."""
    doc = "Summary.\n\nArgs:\n    project_root: A path.\n        More about it.\n    limit: A number.\n"
    result = server._document_project_parameter(doc)
    lines = result.splitlines()
    entry = next(i for i, line in enumerate(lines) if line.lstrip().startswith("project:"))
    assert lines[entry - 1].strip() == "More about it."


def test_describe_project_root_keeps_the_other_field_settings():
    """Only the description changes — a constraint must survive."""
    param = inspect.Parameter(
        "project_root",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[str | None, Field(description="A path.", max_length=42)],
    )
    result = server._describe_project_root(param)
    merged = TypeAdapter(result.annotation).json_schema()
    assert "A path." in merged["description"]
    assert "project name or a project_id" in merged["description"]
    assert merged["anyOf"][0]["maxLength"] == 42


def test_describe_project_root_leaves_other_parameters_alone():
    param = inspect.Parameter(
        "limit",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[int, Field(description="A number.")],
    )
    assert server._describe_project_root(param) is param


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_wrapper_adds_the_project_parameter_and_nothing_else(tool):
    """The wrapper must not lose or rename an existing parameter."""
    handler = inspect.unwrap(server.mcp._tool_manager.get_tool(tool.name).fn)
    expected = set(inspect.signature(handler).parameters) - {"ctx"}
    got = set(tool.inputSchema.get("properties", {})) - {"project"}
    assert got == expected


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_an_unknown_argument_is_rejected(tool):
    """A misspelled argument must fail, and the failure must name it."""
    assert tool.inputSchema.get("additionalProperties") is False
    arg_model = server.mcp._tool_manager.get_tool(tool.name).fn_metadata.arg_model
    with pytest.raises(ValidationError) as exc:
        arg_model.model_validate({"projekt": "boot-loader"})
    assert "projekt" in str(exc.value)


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_context_parameter_stays_out_of_the_schema(tool):
    """ctx is injected by FastMCP — the LLM must never see it."""
    assert "ctx" not in tool.inputSchema.get("properties", {})


def test_a_wrapped_tool_still_gets_its_context():
    """The __signature__ override must not break progress notifications.

    FastMCP finds ctx with typing.get_type_hints(), which reads
    __annotations__, and not with inspect.signature().  A regression here
    stops the 5 s progress notifications and long queries then hit a
    client-side idle timeout.
    """
    assert server.mcp._tool_manager.get_tool("lookup_symbol").context_kwarg == "ctx"


def test_a_tool_without_a_project_root_is_left_alone():
    """get_project_info takes a project_id, thus a project selector is wrong."""
    info = next(t for t in TOOLS if t.name == "get_project_info")
    assert "project" not in info.inputSchema.get("properties", {})


def test_the_selector_reaches_the_handler():
    """End to end: project=<name> must arrive as project_root=<name>."""
    seen: dict = {}

    def handler(
        name: str,
        project_root: str | None = None,
    ) -> list[dict]:
        """Doc.

        Args:
            name: Symbol name.
            project_root: Project root.

        Returns:
            list of dicts.
        """
        seen["project_root"] = project_root
        return [{"name": name}]

    wrapped = server._with_project_selector(handler)
    assert wrapped(name="main", project="boot-loader") == [{"name": "main"}]
    assert seen["project_root"] == "boot-loader"


# ── The instructions ───────────────────────────────────────────────────


def _instruction_copies() -> tuple[tuple[str, str], ...]:
    """The copies of the instruction text that must agree.

    Two remain, and they reach the reader by different routes:

    * ``server.mcp.instructions`` — sent over the MCP protocol.  It is a
      literal in ``server.py`` because FastMCP reads it while it builds the
      object, thus before any file read could run.
    * ``BASE_INSTRUCTIONS`` — what ``fw-context init`` writes into
      ``CLAUDE.md`` and ``AGENTS.md``.

    A third copy lived in ``data/instructions.md`` until 2026-09-04.  No
    code ever read it: a claim corrected in the other two stayed wrong
    there, and the file was deleted rather than kept in step.
    """
    from fw_context_mcp.config.tools import BASE_INSTRUCTIONS

    return (
        ("FastMCP instructions", server.mcp.instructions or ""),
        ("BASE_INSTRUCTIONS", BASE_INSTRUCTIONS),
    )


def test_every_copy_of_the_instructions_teaches_the_selector():
    for label, text in _instruction_copies():
        assert "list_projects" in text, label
        assert "DIFFERENT project" in text, label


def test_every_copy_of_the_instructions_says_one_query_one_build():
    """A reader that misses this asks about a bootloader and an application.

    Both selectors fail closed, thus a reader who does not know the rule
    meets an error and has to guess what to do.  The text must name the
    two axes, say that the answer covers one build, and give the way
    forward: ask twice.
    """
    for label, text in _instruction_copies():
        # One copy shouts the rule and the other writes it in prose, thus
        # the check reads the meaning and not the casing.
        assert "one build" in text.lower(), label
        assert "image" in text, label
        assert "variant" in text, label
        assert "get_active_build" in text, label
        assert "twice" in text, f"{label}: no way given to compare two builds"


def test_every_copy_of_the_instructions_teaches_paging():
    """A reader that misses this stops at the first page and calls it all.

    A full page is indistinguishable from a complete answer without the
    count, thus the notice is always present and the text must say so.
    """
    for label, text in _instruction_copies():
        assert "offset" in text, label
        assert "total" in text, label
        assert "more" in text, label


def test_no_copy_of_the_instructions_holds_a_dead_file():
    """``data/instructions.md`` is gone.  Nothing may point a reader at it."""
    from pathlib import Path

    import fw_context_mcp

    assert not (Path(fw_context_mcp.__file__).parent / "data" / "instructions.md").exists()
    for label, text in _instruction_copies():
        assert "instructions.md" not in text, label


def test_every_copy_of_the_instructions_states_the_scope_of_search_bodies():
    """The scope of ``search_bodies`` must not drift back to a false claim.

    ``symbols.source`` holds the text of EVERY definition — measured on one
    index: 1,844 struct, 1,142 class, 878 enum, 498 varglobal.  Both copies
    used to say "function BODIES … inside {}" and name a type declaration in
    a header as unreachable, and a reader then avoided the one tool that
    answers such a question.
    """
    for label, text in _instruction_copies():
        assert "TEXT OF EVERY DEFINITION" in text.upper(), label
        # The false claim, in the two shapes both copies carried.
        assert "inside {}" not in text, label
        assert "type definitions in headers" not in text, label


def test_every_copy_of_the_instructions_says_where_a_line_number_comes_from():
    """Without this the reader leaves fw-context for a text search."""
    for label, text in _instruction_copies():
        assert "match_lines" in text, label
        # The field carries no leading underscore: measured on one session,
        # the reader skipped `_match_lines` as internal and then spent three
        # calls deriving a line number it already held.
        assert "_match_lines" not in text, label
        assert "end_line" in text, label


def test_every_copy_of_the_instructions_warns_about_a_same_name_symbol():
    """A reader that misses this reports the callers of the wrong symbol.

    Two classes can each hold a method of one name.  The tools answer in two
    shapes, and a reader needs both: a list tool labels each row with
    ``target_qualified_name``, and a tool that returns one body adds
    ``ambiguous_warning``.  Without the first, a caller of one class reads
    as a caller of the other — the report that led to the fix.  Without the
    second, one body reads as the only body of that name.
    """
    for label, text in _instruction_copies():
        assert "target_qualified_name" in text, label
        assert "ambiguous_warning" in text, label
        assert "qualified name" in text, label
        # The structured offer, and the way to reach past the shown part.
        assert "candidates" in text, label
        assert "candidates_total" in text, label
        assert "offset" in text, label
        assert "`class`" in text, label
        # Both shapes must name their tools, or a reader cannot tell which
        # answer to expect from which tool.
        assert "find_callers" in text, label
        assert "get_symbol_context" in text, label


def test_every_copy_of_the_instructions_gives_the_query_rule_per_tool():
    """One rule for every tool was false and cost recall silently.

    ``search_bodies`` sends the query to FTS5 as written — a space is an AND
    and no wildcard is added — while ``search_code`` and ``search_content``
    expand each term to ``term*`` and OR-join them.  Both copies used to
    carry the OR rule alone, thus a reader wrote ``SELF_TEST`` for a body
    search and never saw the definition that holds ``Self tester``.
    """
    for label, text in _instruction_copies():
        assert "OR-joined" in text, label
        assert "AND" in text, label
        assert "SELF_TEST*" in text, label


def test_every_copy_of_the_instructions_marks_the_llm_analysis_untrusted():
    """A model wrote it.  A reader must not quote it as a fact."""
    for label, text in _instruction_copies():
        assert "llm_analysis" in text, label


def test_every_copy_of_the_instructions_names_the_symbol_parameter():
    """A guessed argument name costs a whole call — the tools reject it."""
    for label, text in _instruction_copies():
        assert "symbol_name" in text, label  # named as the WRONG spelling
        assert "PARAMETER NAMES" in text.upper(), label
