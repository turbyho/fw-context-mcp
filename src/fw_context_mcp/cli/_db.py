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
import sys
from collections.abc import Callable
from pathlib import Path

from ._builds import OLDER, open_for_reading, reading_problem

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
    from ..indexer.db import get_all_builds_for_project
    from ..mcp.shared.stale import latest_builds
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    conn = open_for_reading(db_path)
    if conn is None:
        return 1
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
    from ..indexer.db import get_build_stats
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    conn = open_for_reading(db_path)
    if conn is None:
        return 1
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

    WHY the index lock (``_under_index_lock``): an index run of the
    project must not write the database that the command deletes.  Before,
    ``--all`` wrote only ``reindex.pause``, and a running index run went on
    writing into the removed file.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_all_builds_for_project, open_db_readonly
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
        # Read only, before the lock: an index of an older schema cannot
        # list its builds, and the command deletes it as a whole.
        conn = open_db_readonly(db_path)
        try:
            problem = reading_problem(conn)
            older = problem is not None
            builds = [] if older else get_all_builds_for_project(conn, project_id)
            queried = set() if older else {build["config_hash"] for build in latest_builds(conn, project_id)}
        finally:
            conn.close()

        if not builds and not older:
            print("No builds to delete.")
            return 0

        print(f"Project: {project_root.name}  path={project_root}")
        if problem is not None:
            print(f"  The builds are not listed: {problem[1]}")
        else:
            print(f"  {len(builds)} build(s) found:")
        for b in builds:
            desc = b["description"] or "-"
            print(f"  {b['config_hash'][:12]}...  {_row_label(b)}  {_row_state(b, queried)}  {desc}")

        if not args.yes:
            count = "all its builds" if older else f"all {len(builds)} builds"
            answer = input(f"\nThis will delete the ENTIRE index including {count}.\n"
                           "Delete all builds? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        def _delete_all() -> int:
            from ..indexer import _force_llm_marker
            from ..indexer.autobuild import clear_excluded, clear_problem
            from ..indexer.runner import _cleanup_orphaned_cc_artifacts

            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)
            _cleanup_orphaned_cc_artifacts(db_path, project_id)
            # The markers of the index that went away, as reset_index removes them.
            clear_problem(db_path.parent)
            clear_excluded(db_path.parent)
            _force_llm_marker.remove(db_path.parent)
            print("Index deleted. Run 'fw-context index' to rebuild.")
            return 0

        # The index lock after the question: a person who reads it must not
        # hold the index.  Before, a pause marker let a running index run go
        # on writing into the removed database.
        return _under_index_lock(db_path.parent, args, _delete_all)

    # ── Single build delete ──
    if not args.config_hash:
        print("error: Specify a config_hash to delete, or use --all to delete all builds.")
        print("Usage: fw-context db delete <config_hash>")
        print("       fw-context db delete --all")
        return 1

    decision = _delete_one_build(args, cfg, project_id, db_path, ask=True)
    if decision is not None:
        return decision

    def _delete() -> int:
        code = _delete_one_build(args, cfg, project_id, db_path, ask=False)
        return 0 if code is None else code

    # The checks again and the delete under the index lock: no run writes
    # the build while the command checks and deletes it.
    return _under_index_lock(db_path.parent, args, _delete)


def _delete_one_build(args: argparse.Namespace, cfg, project_id: str, db_path: Path, *, ask: bool) -> int | None:
    """Check the build of ``args.config_hash``, and show it and ask (*ask*) or delete it.

    With *ask* the function runs before the index lock: it shows the build,
    asks unless ``--yes``, and returns None when the delete goes on, or the
    exit code.  Without it the caller holds the index lock: the same checks
    run again, and the build goes.
    """
    from ..indexer.db import delete_build_data, open_db, transaction
    from ..mcp.shared.stale import latest_builds

    # Under the lock: another command can have deleted the whole index while
    # the person read the question, and open_db makes a new empty one.
    if not ask and not db_path.exists():
        print("error: the index was deleted meanwhile. Nothing to delete.")
        return 1
    if ask:
        # Read only, before the lock.  An index of an older schema cannot
        # show the build: the person confirms the hash, and the checks run
        # under the lock, where open_db migrates the index.
        from ..indexer.db import open_db_readonly

        probe = open_db_readonly(db_path)
        try:
            problem = reading_problem(probe)
        finally:
            probe.close()
        if problem is not None and problem[0] != OLDER:
            print(f"error: {problem[1]} Use 'fw-context db delete --all' to delete the whole index.")
            return 1
        if problem is not None:
            print(f"Build {args.config_hash}: {problem[1]}")
            if not args.yes:
                answer = input("\nDelete this build after the migration? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted.")
                    return 0
            return None
    conn = open_for_reading(db_path) if ask else open_db(db_path)
    if conn is None:
        return 1
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

        # A row of an index run: a background run writes it (before the
        # lock), or a run that stopped left it (under the lock, the command
        # took the index).  The next index run of the build continues it,
        # thus the delete asks for --force.
        if _row_state(target, queried) == "indexing" and not args.force:
            print(f"error: An index run writes this row of build {_row_label(target)}, or a run that "
                  "stopped left it. The next index run of the build continues it. "
                  "Cannot delete it without --force.")
            return 1

        # The person reads and answers before the command takes the index:
        # a person who reads must not hold it.  The checks above run again
        # under the lock, then the delete.
        if ask:
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
            # Phase 2 deletes the build that the person confirmed, not what
            # the prefix names then.
            args.config_hash = config_hash
            return None

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

    def _cleanup() -> int:
        deleted = _cleanup_orphaned_cc_artifacts(db_path, project_id)
        if deleted:
            print(f"Cleaned up {deleted} orphaned per-build artifact(s).")
        else:
            print("No orphaned artifacts found.")
        return 0

    # Under the index lock: a run writes compile_commands.<hash>.json before
    # the row of its build, and the cleanup took such a file for an orphan.
    if not db_path.parent.is_dir():
        return _cleanup()
    return _under_index_lock(db_path.parent, args, _cleanup)


def _under_index_lock(db_dir: Path, args: argparse.Namespace, work: Callable[[], int]) -> int:
    """Run *work* while the command holds the index lock, as a manual `fw-context index` does.

    Within one project every writer holds the lock: the command takes the
    index over from a background run, and a manual run (``fw-context
    index``, ``fw-context analyze``) refuses it.
    """
    from ..exit_codes import EXIT_ALREADY_RUNNING
    from ..indexer.db._locking import IndexRunLocked
    from ..indexer.runner import IndexStopped
    from ._index import _owned_index

    try:
        with _owned_index(db_dir, args):
            return work()
    except IndexRunLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except IndexStopped as exc:
        print(f"{exc.label}: {exc}", file=sys.stderr)
        return exc.exit_code
