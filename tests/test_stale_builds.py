"""The staleness of each (variant, image) build, not only of the newest one.

Each query names one build.  An edit to a file that only another image
compiles left that build stale, and the status tool and the start check of
the daemon read only the newest build.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

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


def _health(conn, root: Path, tmp_path: Path, image: str) -> dict:
    """The health of the build *image*, as get_project_status computes it for each build."""
    from fw_context_mcp.config.settings import Config
    from fw_context_mcp.mcp.handlers.maintenance import AutobuildState, _build_health, _IndexTarget, _ProjectFacts

    target = _IndexTarget(root, Config(), "pid", tmp_path / "db" / "index.db", False)
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
    assert health["reindex_reasons"][0] == "compile_commands_missing"


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
    assert "v/boot: reindex_needed" in project["index_message"], project["index_message"]
    assert "v/app" not in project["index_message"], "a current build is not behind"


def test_a_reason_of_every_build_comes_one_time(tmp_path):
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    for image in ("app", "boot"):
        (root / f"{image}.json").unlink()
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert project["reindex_reasons"].count("compile_commands_missing") == 1, project["reindex_reasons"]


def test_the_project_message_names_a_build_with_modified_files(tmp_path):
    from fw_context_mcp.mcp.handlers.maintenance import _project_health

    conn, root = _index(tmp_path)
    builds = sorted(latest_builds(conn, "pid"), key=lambda r: r["image"])
    healths = [_health(conn, root, tmp_path, build["image"]) for build in builds]

    project = _project_health(builds, healths)

    assert project["status"] == "ready"
    assert "v/boot: 1 modified file(s)" in project["index_message"], project["index_message"]
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
