"""The staleness of each (variant, image) build, not only of the newest one.

Each query names one build.  An edit to a file that only another image
compiles left that build stale, and get_active_build and the start check of
the daemon read only the newest build.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.db import (
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.mcp.shared.stale import build_staleness, latest_builds, other_stale_builds


def _index(tmp_path: Path) -> tuple:
    """An index of two builds of one variant: app (current) and boot (a source changed since)."""
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
            upsert_build_config(conn, f"h-{image}", "pid", str(cc), variant="v", image=image)
            source = root / f"{image}.c"
            source.write_text("int x;\n", encoding="utf-8")
            upsert_file(conn, f"h-{image}", f"{image}.c", "c", mtime=source.stat().st_mtime)
    # The source of the bootloader changes after the index.
    later = time.time() + 5
    os.utime(root / "boot.c", (later, later))
    return conn, root


def test_latest_builds_gives_one_row_for_each_pair(tmp_path):
    conn, _ = _index(tmp_path)
    with transaction(conn):
        upsert_build_config(conn, "h-app-2", "pid", "/x.json", variant="v", image="app")
    rows = latest_builds(conn, "pid")

    assert sorted((r["image"], r["config_hash"]) for r in rows) == [("app", "h-app-2"), ("boot", "h-boot")]


def test_a_modified_source_makes_its_build_stale(tmp_path):
    conn, root = _index(tmp_path)
    boot = next(r for r in latest_builds(conn, "pid") if r["image"] == "boot")

    needed, reasons = build_staleness(conn, boot, root, use_cache=False)

    assert needed is False, "modified files make a build stale, the next run reindexes it"
    assert reasons == ["1 modified file(s)"]


def test_a_missing_database_needs_a_reindex(tmp_path):
    conn, root = _index(tmp_path)
    (root / "boot.json").unlink()
    boot = next(r for r in latest_builds(conn, "pid") if r["image"] == "boot")

    needed, reasons = build_staleness(conn, boot, root, use_cache=False)

    assert needed is True
    assert reasons[0] == "compile_commands_missing"


def test_the_other_stale_builds_leave_out_the_active_one(tmp_path):
    conn, root = _index(tmp_path)

    stale = other_stale_builds(conn, "pid", root, "h-app", use_cache=False)

    assert stale == [{"variant": "v", "image": "boot", "reindex_needed": False, "reasons": ["1 modified file(s)"]}]
    assert other_stale_builds(conn, "pid", root, "h-boot", use_cache=False) == []


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
