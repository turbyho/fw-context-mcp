"""``fw-context db`` — index database management (list, stats, delete, cleanup).

Provides commands to inspect and manage the SQLite index database without
needing to run raw SQL queries.  Operations: listing all builds with stats,
detailed per-build statistics, safe deletion of builds, and cleanup of
orphaned artifacts.

WHY a separate ``db`` command group: the index database holds the builds
of a project, one ``(variant, image)`` pair each, and also older rows of a
pair that an interrupted run left, and rows that an index run writes now.
Users need visibility into what exists and a safe way to free disk space
by deleting obsolete builds without corrupting the database.

A query reads one row of each pair: the newest completed row
(``latest_builds``).  These commands mark that row, in place of the newest
build of the project, which is not one build that the queries read.
"""

from __future__ import annotations

import argparse
import os

#: The marker of a row that the queries of its build read.
QUERIED_FOOTER = "* = the row that the queries of its build read"


def _row_label(row) -> str:
    """Return the label of a build row: ``variant/image``, or ``(no name)``."""
    from ..mcp.shared.stale import build_label

    return build_label(row) or "(no name)"


def _row_state(row, queried: set[str]) -> str:
    """Return what a row is: read by the queries, a row of an index run, or an older row.

    ``indexing`` is a row that an index run writes, or that a stopped run
    left: the run sets ``manifest_verification`` when it ends.
    """
    if row["config_hash"] in queried:
        return "queried"
    if row["manifest_verification"] == "indexing":
        return "indexing"
    return "older row"


def _resolve_config_hash(conn, prefix: str) -> str | None:
    """Resolve a short hash prefix to a full 64-char ``config_hash``.

    Returns the full hash on exact match, or when *prefix* uniquely matches
    one row via LIKE.  Returns ``None`` on ambiguous prefix or no match.

    WHY prefix resolution: full 64-char hashes are unreadable for humans;
    prefix matching lets users type just the first 8-12 characters.
    """
    # Exact match first (full 64-char hash)
    row = conn.execute(
        "SELECT config_hash FROM build_configs WHERE config_hash = ?", (prefix,)
    ).fetchone()
    if row is not None:
        return row["config_hash"]

    # Prefix fallback via LIKE
    rows = conn.execute(
        "SELECT config_hash FROM build_configs WHERE config_hash LIKE ?",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["config_hash"]
    if len(rows) > 1:
        return None  # ambiguous
    return None


def cmd_db(args: argparse.Namespace) -> int:
    """Dispatcher for ``fw-context db`` sub-subcommands."""
    if getattr(args, "db_command", None) is None:
        print("Usage: fw-context db <list|stats|delete|cleanup>")
        return 1
    return args.func(args)


def cmd_db_list(args: argparse.Namespace) -> int:
    """List all builds for a project with per-build statistics.

    Each build is shown with hash, label (``variant/image``), its state,
    description (git branch+tag), timestamps, and symbol/file/reference
    counts.  The row that the queries of its build read is marked with
    ``*``.

    WHY multi-line block format: build descriptions can be long (branch +
    commit message), so a simple table would truncate them.  Block format
    gives each build its own visual section with full description.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_all_builds_for_project, open_db
    from ..mcp.shared.stale import latest_builds
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    conn = open_db(db_path)
    try:
        queried = {build["config_hash"] for build in latest_builds(conn, project_id)}
        builds = get_all_builds_for_project(conn, project_id)
    finally:
        conn.close()

    if not builds:
        print("No builds found.")
        return 0

    print(f"Project: {project_root.name}  path={project_root}")
    print()

    hash_width = 64 if args.verbose else 12
    # Multi-line block format — each build gets its own block of lines
    # so the description is fully readable without truncation.
    for b in builds:
        ch = b["config_hash"] if args.verbose else b["config_hash"][:hash_width]
        desc = b["description"] or "-"
        first_at = b["first_indexed_at"] or "-"
        last_at = b["created_at"] or "-"
        marker = "*" if b["config_hash"] in queried else " "

        # Header line: *HASH  LABEL  STATE  DESCRIPTION
        print(f"{marker}{ch}  {_row_label(b)}  {_row_state(b, queried)}  {desc}")

        # Info line: timestamps
        print(f"  {'First indexed:':<16} {first_at:<22}  {'Last indexed:':<15} {last_at}")

        # Stats line: symbol / file / reference counts
        print(f"  {'Symbols:':<16} {b['symbol_count']:<22,}  {'Files:':<15} {b['file_count']:<,}  "
              f"{'Refs:':<8} {b['reference_count']:,}")

        if len(builds) > 1:
            print()  # blank line between builds

    if queried:
        print(f"\n{QUERIED_FOOTER}")
    return 0


def cmd_db_stats(args: argparse.Namespace) -> int:
    """Show detailed statistics for a specific build."""
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_build_stats, open_db
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    conn = open_db(db_path)
    try:
        config_hash = _resolve_config_hash(conn, args.config_hash)
        if config_hash is None:
            rows = conn.execute(
                "SELECT config_hash FROM build_configs WHERE config_hash LIKE ?",
                (args.config_hash + "%",),
            ).fetchall()
            if len(rows) > 1:
                print(f"error: ambiguous prefix '{args.config_hash}' matches {len(rows)} builds.")
                print("Use a longer prefix or the full 64-char hash. Run 'fw-context db list' to see available builds.")
            else:
                print(f"error: config_hash '{args.config_hash[:12]}...' not found.")
                print("Run 'fw-context db list' to see available builds.")
            return 1
        stats = get_build_stats(conn, config_hash)
    finally:
        conn.close()

    if "error" in stats:
        print(f"error: {stats['error']}")
        return 1

    print(f"Build:          {stats['config_hash']}")
    print(f"  Label:        {_row_label(stats)}")
    print(f"  Board:        {stats.get('board') or '-'}")
    desc = stats.get("description") or "(no git context)"
    print(f"  Description:  {desc}")
    print(f"  First indexed: {stats.get('first_indexed_at') or '-'}")
    print(f"  Last indexed: {stats['created_at']}")
    print(f"  Compile cmds: {stats.get('compile_commands_path', '')}")
    print(f"  Embedding dim:{stats.get('embedding_dim') or 'N/A'}")
    print(f"  Manifest:     {stats.get('manifest_verification') or 'N/A'}")
    print()

    print("Symbols by kind:")
    for kind, count in sorted(stats["by_kind"].items(), key=lambda x: -x[1]):
        print(f"  {kind:<22s} {count:>8}")
    print(f"  {'---':<22s} {'---':>8}")
    print(f"  {'TOTAL':<22s} {stats['symbol_total']:>8}")
    print(f"  Definitions (is_definition=1): {stats['symbol_definitions']}")
    print()

    analysis = stats.get("analysis") or {}
    proj = analysis.get("project", {"analyzed": 0, "total": 0})
    vend = analysis.get("vendor", {"analyzed": 0, "total": 0})
    print(f"LLM analysis (project): {proj['analyzed']}/{proj['total']} analyzed")
    print(f"LLM analysis (vendor):  {vend['analyzed']}/{vend['total']} analyzed")
    print(f"  Skipped:        {proj.get('skipped', 0)} project, {vend.get('skipped', 0)} vendor")
    print(f"  analyze_vendor: {analysis.get('analyze_vendor', False)}")
    print(f"  Complete:       {analysis.get('complete', False)}")
    if analysis.get("model"):
        print(f"  Model:          {analysis['model']}")
    print(f"Embeddings:     {stats['embedding_count']} symbols with embeddings")
    print(f"Files:          {stats['file_count']}")
    print(f"References:     {stats['reference_count']}")
    print(f"Macros:         {stats['macro_count']}")
    print(f"Indirect calls: {stats['indirect_call_sites']}")
    print(f"FP assignments: {stats['fp_assignments']}")
    print(f"Overrides:      {stats['override_count']}")
    print(f"Hotspot cache:  {stats['hotspot_cache_entries']}")
    print(f"PageRank cov:   {stats['pagerank_coverage']} / {stats['symbol_total']} symbols")
    return 0


def cmd_db_delete(args: argparse.Namespace) -> int:
    """Delete a specific build (by config_hash) or all builds (``--all``).

    Single-build delete refuses to delete a row that the queries of its
    build read without ``--force``, and refuses to delete the only build
    entirely — both guards prevent accidental data loss.  WHY each queried
    row and not the newest build of the project: each build of a project
    of several builds has its own queries, and the guard protected only the
    build that the last index run wrote.

    WHY pause file during --all delete: the background reindex daemon may
    try to write to the database while we delete it.  Writing a
    ``reindex.pause`` file signals the daemon to pause, preventing race
    conditions between delete and auto-reindex.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import (
        delete_build_data,
        get_all_builds_for_project,
        open_db,
        transaction,
    )
    from ..mcp.shared.stale import latest_builds
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    # ── --all: delete entire index ──
    if args.all:
        conn = open_db(db_path)
        try:
            builds = get_all_builds_for_project(conn, project_id)
            queried = {build["config_hash"] for build in latest_builds(conn, project_id)}
        finally:
            conn.close()

        if not builds:
            print("No builds to delete.")
            return 0

        print(f"Project: {project_root.name}  path={project_root}")
        print(f"  {len(builds)} build(s) found:")
        for b in builds:
            desc = b["description"] or "-"
            print(f"  {b['config_hash'][:12]}...  {_row_label(b)}  {_row_state(b, queried)}  {desc}")

        if not args.yes:
            answer = input(f"\nThis will delete the ENTIRE index including all {len(builds)} builds.\n"
                           "Delete all builds? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        # Pause background reindex so it doesn't try to write while we delete
        pause_file = db_path.parent / "reindex.pause"
        try:
            pause_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        try:
            # Delete DB files
            db_path.unlink(missing_ok=True)
            (db_path.parent / (db_path.name + "-wal")).unlink(missing_ok=True)
            (db_path.parent / (db_path.name + "-shm")).unlink(missing_ok=True)
            # Clean up compile_commands artifacts
            from ..indexer.runner import _cleanup_orphaned_cc_artifacts

            _cleanup_orphaned_cc_artifacts(db_path, project_id)
        finally:
            try:
                if pause_file.exists():
                    content = pause_file.read_text(encoding="utf-8").strip()
                    if content == str(os.getpid()):
                        pause_file.unlink(missing_ok=True)
            except OSError:
                pass

        print("Index deleted. Run 'fw-context index' to rebuild.")
        return 0

    # ── Single build delete ──
    if not args.config_hash:
        print("error: Specify a config_hash to delete, or use --all to delete all builds.")
        print("Usage: fw-context db delete <config_hash>")
        print("       fw-context db delete --all")
        return 1

    conn = open_db(db_path)
    try:
        config_hash = _resolve_config_hash(conn, args.config_hash)
        if config_hash is None:
            rows = conn.execute(
                "SELECT config_hash FROM build_configs WHERE config_hash LIKE ?",
                (args.config_hash + "%",),
            ).fetchall()
            if len(rows) > 1:
                print(f"error: ambiguous prefix '{args.config_hash}' matches {len(rows)} builds.")
                print("Use a longer prefix or the full 64-char hash. Run 'fw-context db list' to see available builds.")
            else:
                print(f"error: config_hash '{args.config_hash[:12]}...' not found.")
                print("Run 'fw-context db list' to see available builds.")
            return 1

        queried = {build["config_hash"] for build in latest_builds(conn, project_id)}
        target = conn.execute(
            "SELECT config_hash, variant, image, manifest_verification, description, created_at, first_indexed_at "
            "FROM build_configs WHERE config_hash = ?",
            (config_hash,),
        ).fetchone()
        if target is None:
            print(f"error: config_hash '{config_hash[:12]}...' not found.")
            return 1

        # A row that the queries of its build read
        if config_hash in queried:
            if not args.force:
                print(f"error: The queries of build {_row_label(target)} read this row. "
                      "Cannot delete it without --force.")
                print(f"  Use --force to proceed. The queries of {_row_label(target)} then read "
                      "an older row of that build, or no row.")
                return 1
            # Check that at least one other build exists
            other = conn.execute(
                "SELECT COUNT(*) FROM build_configs WHERE project_id = ? AND config_hash != ?",
                (project_id, config_hash),
            ).fetchone()[0]
            if other == 0:
                print("error: Cannot delete the only build.")
                print("  Use 'fw-context db delete --all' to delete the entire index.")
                return 1

        # A row of an index run.  WHY a guard: the delete of one build
        # writes no reindex.pause, and a run that still writes the row
        # writes it again when it ends, as complete, without the data that
        # the delete removed.
        if _row_state(target, queried) == "indexing" and not args.force:
            print(f"error: An index run writes this row of build {_row_label(target)}, or a stopped run "
                  "left it. Cannot delete it without --force.")
            print("  Wait for the run to end ('fw-context watch status'), or use --force for a row "
                  "that a stopped run left.")
            return 1

        # Show what will be deleted
        row = target

        sym_count = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash = ?", (config_hash,)
        ).fetchone()[0]
        file_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE config_hash = ?", (config_hash,)
        ).fetchone()[0]
        ref_count = conn.execute(
            "SELECT COUNT(*) FROM refs WHERE config_hash = ?", (config_hash,)
        ).fetchone()[0]

        desc = row["description"] or "-"

        print(f"Build:          {row['config_hash']}")
        print(f"  Label:        {_row_label(target)}")
        print(f"  Description:  {desc}")
        print(f"  First indexed: {row['first_indexed_at'] or '-'}")
        print(f"  Last indexed: {row['created_at']}")
        print(f"  Symbols:      {sym_count}")
        print(f"  Files:        {file_count}")
        print(f"  References:   {ref_count}")
        print(f"  State:        {_row_state(target, queried)}")

        if not args.yes:
            answer = input("\nDelete this build? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        with transaction(conn):
            delete_build_data(conn, config_hash)
    finally:
        conn.close()

    # Clean up compile_commands.<hash>.json artifact (outside transaction)
    cc_path = cfg.index.db_dir / project_id / f"compile_commands.{config_hash}.json"
    if cc_path.exists():
        try:
            cc_path.unlink()
        except OSError:
            pass

    print(f"Build {config_hash[:12]}... deleted.")
    return 0


def cmd_db_cleanup(args: argparse.Namespace) -> int:
    """Remove orphaned per-build artifacts: compile_commands and linker pass state files."""
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.runner import _cleanup_orphaned_cc_artifacts
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    deleted = _cleanup_orphaned_cc_artifacts(db_path, project_id)
    if deleted:
        print(f"Cleaned up {deleted} orphaned per-build artifact(s).")
    else:
        print("No orphaned artifacts found.")
    return 0
