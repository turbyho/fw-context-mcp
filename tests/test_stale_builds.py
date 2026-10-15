"""The staleness of each (variant, image) build, not only of the newest one.

Each query names one build.  An edit to a file that only another image
compiles left that build stale, and the status tool and the start check of
the daemon read only the newest build.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.db import (
    CURRENT_ROW_FORMAT,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.mcp.shared.stale import latest_builds


def _index(tmp_path: Path) -> tuple:
    """An index of two builds of one variant: app (current) and boot (a source changed since).

    Each build carries ``CURRENT_ROW_FORMAT`` and full header tracking,
    because each stands in for a COMPLETED index run, and the postprocess of
    a real run writes both.  Without the stamp each build reports an old row
    format.
    """
    root = tmp_path / "proj"
    root.mkdir()
    conn = open_db(tmp_path / "db" / "index.db")
    old = time.time() - 3600
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(root))
        for image in ("boot", "app"):
            cc = root / f"{image}.json"
            cc.write_text("[]", encoding="utf-8")
            os.utime(cc, (old, old))
            upsert_build_config(
                conn, f"h-{image}", "pid", str(cc), variant="v", image=image,
                row_format=CURRENT_ROW_FORMAT,
            )
            source = root / f"{image}.c"
            source.write_text("int x;\n", encoding="utf-8")
            upsert_file(conn, f"h-{image}", f"{image}.c", "c", mtime=source.stat().st_mtime)
        # Full header tracking, as a completed run gives: a build without it
        # is not up to date, and that is another test.
        conn.execute("UPDATE build_configs SET manifest_verification = 'full'")
    # The source of the bootloader changes after the index.
    later = time.time() + 5
    os.utime(root / "boot.c", (later, later))
    return conn, root


def _health(conn, root: Path, tmp_path: Path, image: str, run_state: str | None = None) -> dict:
    """The health of the build *image*, as get_project_status computes it for each build.

    *run_state* is the state of the index run of the project
    (``read_reindex_state``): None, "pending" or "reindexing".
    """
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.mcp.handlers.maintenance import AutobuildState, _build_health, _IndexTarget, _ProjectFacts

    target = _IndexTarget(root, Config(), "pid", tmp_path / "db" / "index.db", run_state)
    build = next(r for r in latest_builds(conn, "pid") if r["image"] == image)
    facts = _ProjectFacts([], AutobuildState.WILL_BUILD, "", False, "")
    return _build_health(conn, target, build, fast=False, hash_cache={}, facts=facts)


def test_latest_builds_gives_one_row_for_each_pair(tmp_path):
    conn, _ = _index(tmp_path)
    with transaction(conn):
        upsert_build_config(conn, "h-app-2", "pid", "/x.json", variant="v", image="app")
    rows = latest_builds(conn, "pid")

    assert sorted((r["image"], r["config_hash"]) for r in rows) == [("app", "h-app-2"), ("boot", "h-boot")]


def test_a_modified_source_makes_only_its_build_behind(tmp_path):
    conn, root = _index(tmp_path)

    boot = _health(conn, root, tmp_path, "boot")
    app = _health(conn, root, tmp_path, "app")

    assert boot["modified_files_count"] == 1
    assert boot["reindex_needed"] is False, "modified files make a build stale, the next run reindexes it"
    assert app["modified_files_count"] == 0


def test_a_missing_database_needs_a_reindex(tmp_path):
    conn, root = _index(tmp_path)
    (root / "boot.json").unlink()

    health = _health(conn, root, tmp_path, "boot")

    assert health["reindex_needed"] is True
    # A sentence with the action that makes the file again, and not the
    # code of the check.  The project has no build system that fw-context
    # can run, thus the action is a build outside of fw-context.
    assert health["reindex_reasons"] == [
        f"compile_commands.json {root / 'boot.json'} does not exist. Build the project outside of "
        "fw-context (fw-context cannot run its build, for example the build of an IDE project), "
        "then run `fw-context index`."
    ]
    assert health["index_message"].startswith(health["reindex_reasons"][0])


def _make_newer(path: Path) -> None:
    later = time.time() + 3600
    os.utime(path, (later, later))


def test_a_newer_database_names_a_plain_run(tmp_path):
    """A plain run reads the new file, thus no build is necessary."""
    conn, root = _index(tmp_path)
    _make_newer(root / "boot.json")

    health = _health(conn, root, tmp_path, "boot")

    assert health["reindex_needed"] is True
    assert health["reindex_reasons"] == [
        "compile_commands.json changed after the last index run. Run `fw-context index`."
    ]
    assert health["index_message"].startswith(health["reindex_reasons"][0])


@pytest.mark.parametrize("run_state", ["pending", "reindexing"])
def test_a_reason_of_the_database_asks_to_wait_for_a_run(tmp_path, run_state):
    """A run with `--build` writes the database again, thus it is newer during the run.

    The command of the reason is then often the command that runs.
    """
    conn, root = _index(tmp_path)
    _make_newer(root / "boot.json")

    health = _health(conn, root, tmp_path, "boot", run_state)

    assert health["reindex_reasons"] == [
        "compile_commands.json changed after the last index run. An index run is in progress or "
        "scheduled; wait for it, and when this reason stays after the run, run `fw-context index`."
    ]


def _set_row_format(conn, config_hash: str, row_format: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE build_configs SET row_format=? WHERE config_hash=?",
            (row_format, config_hash),
        )


def test_an_older_row_format_needs_a_reindex(tmp_path):
    """The builds of one project are not always indexed together.

    One build can thus carry the text of an older version while another
    build is current.
    """
    conn, root = _index(tmp_path)
    _set_row_format(conn, "h-app", "fw-context-rows/0")

    health = _health(conn, root, tmp_path, "app")

    assert health["reindex_needed"] is True, "only an index run writes the rows again"
    assert health["reindex_reasons"][0].startswith(f"row_format_mismatch: fw-context-rows/0 != {CURRENT_ROW_FORMAT}")


def test_a_newer_row_format_is_no_reason(tmp_path):
    """A newer format means that this process is the old reader.

    A reindex writes the same newer format again, thus it cannot repair this.
    """
    conn, root = _index(tmp_path)
    _set_row_format(conn, "h-app", "fw-context-rows/999")

    health = _health(conn, root, tmp_path, "app")

    assert health["reindex_needed"] is False
    assert health["reindex_reasons"] == []
    assert health["_restart_reason"], "the client must restart, and no command repairs it"


def test_the_project_names_the_build_of_each_reason(tmp_path):
    """A reason of one build gets its label, a reason of every build comes one time."""
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    _set_row_format(conn, "h-boot", "fw-context-rows/0")
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert project["status"] == "reindex_needed"
    assert len(project["reindex_reasons"]) == 1
    assert project["reindex_reasons"][0].startswith("build v/boot: row_format_mismatch"), project
    assert "v/boot: not_scheduled" in project["index_message"], project["index_message"]
    assert "v/app" not in project["index_message"], "a current build is not behind"


def test_a_reason_of_every_build_comes_one_time(tmp_path):
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    for image in ("app", "boot"):
        _make_newer(root / f"{image}.json")
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert project["reindex_reasons"] == [
        "compile_commands.json changed after the last index run. Run `fw-context index`."
    ], project["reindex_reasons"]


def test_a_missing_database_of_each_build_is_a_reason_of_each_build(tmp_path):
    """The reason names the file, and each build has a file of its own."""
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    for image in ("app", "boot"):
        (root / f"{image}.json").unlink()
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert [reason.split(" does not exist")[0] for reason in project["reindex_reasons"]] == [
        f"build v/app: compile_commands.json {root / 'app.json'}",
        f"build v/boot: compile_commands.json {root / 'boot.json'}",
    ], project["reindex_reasons"]


def test_a_missing_database_and_a_degraded_index_name_the_action_of_the_reason(tmp_path):
    """A plain run reads the same missing file, thus the warning must not name it."""
    from fw_context_mcp.mcp.handlers.maintenance import _REASON_COMMAND

    conn, root = _index(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE build_configs SET manifest_verification = 'none'")
    (root / "boot.json").unlink()

    health = _health(conn, root, tmp_path, "boot")

    assert f"Tell the user to run {_REASON_COMMAND}" in health["_warning"], health["_warning"]
    assert "'fw-context index'" not in health["_warning"]


def test_an_old_schema_names_its_command(tmp_path, monkeypatch):
    """Each reason names its action, thus a caller that reads only the reasons finds it."""
    from fw_context_mcp.mcp.handlers import maintenance
    from fw_context_mcp.indexer.db import CURRENT_SCHEMA_VERSION

    conn, root = _index(tmp_path)
    monkeypatch.setattr(maintenance, "get_db_schema_version", lambda conn: 0)

    health = _health(conn, root, tmp_path, "boot")

    assert health["reindex_reasons"] == [f"schema_mismatch: 0 < {CURRENT_SCHEMA_VERSION}. Run `fw-context index`."]


@pytest.mark.parametrize("run_state", ["pending", "reindexing"])
def test_an_old_schema_and_row_format_ask_to_wait_for_a_run(tmp_path, monkeypatch, run_state):
    """Each index run writes the rows again, thus a second run asks for the same work."""
    from fw_context_mcp.mcp.handlers import maintenance
    from fw_context_mcp.indexer.db import CURRENT_SCHEMA_VERSION

    conn, root = _index(tmp_path)
    monkeypatch.setattr(maintenance, "get_db_schema_version", lambda conn: 0)
    _set_row_format(conn, "h-app", "fw-context-rows/0")

    health = _health(conn, root, tmp_path, "app", run_state=run_state)

    wait = (
        ". An index run is in progress or scheduled; wait for it, and when this "
        "reason stays after the run, run `fw-context index`."
    )
    schema, row_format = health["reindex_reasons"]
    assert schema == f"schema_mismatch: 0 < {CURRENT_SCHEMA_VERSION}{wait}"
    assert row_format.startswith(f"row_format_mismatch: fw-context-rows/0 != {CURRENT_ROW_FORMAT} — ")
    assert row_format.endswith(wait), row_format


def test_a_missing_database_comes_before_an_old_schema_in_the_message(tmp_path, monkeypatch):
    """The plain run that an old schema needs reads the same missing file."""
    from fw_context_mcp.mcp.handlers import maintenance

    conn, root = _index(tmp_path)
    (root / "boot.json").unlink()
    monkeypatch.setattr(maintenance, "get_db_schema_version", lambda conn: 0)

    health = _health(conn, root, tmp_path, "boot")

    assert health["reindex_reasons"][0].startswith("schema_mismatch: 0 < ")
    assert health["index_message"].startswith(
        f"compile_commands.json {root / 'boot.json'} does not exist. Build the project outside of fw-context"
    ), health["index_message"]


def test_the_project_message_names_a_build_with_modified_files(tmp_path):
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert project["status"] == "ready"
    assert "v/boot: not_scheduled (1 modified file(s))" in project["index_message"], project["index_message"]
    assert "_warning" not in project["index_message"], "no build has a warning to read"


def test_the_start_check_of_the_daemon_reads_every_build(tmp_path, monkeypatch):
    """The newest build is app, and the stale one is boot."""
    from fw_context_mcp.mcp import daemon
    from fw_context_mcp.mcp.shared import context

    conn, root = _index(tmp_path)
    conn.close()
    monkeypatch.setattr(context, "_db_path", lambda project_root: tmp_path / "db" / "index.db")
    monkeypatch.setattr(daemon, "derive_project_id", lambda project_root: "pid")
    monkeypatch.setattr(
        "fw_context_mcp.mcp.shared.stale.check_structural_staleness",
        lambda *a, **k: [],
    )

    needs, reasons = daemon._staleness_check(root)

    assert needs is True
    assert reasons == ["v/boot: 1 modified files"]
