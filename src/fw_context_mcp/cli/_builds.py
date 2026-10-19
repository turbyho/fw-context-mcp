"""The build of a CLI command, by the rules of the MCP tools.

A project can hold several builds, each a ``(variant, image)`` pair.  A
command that reads the code of the index reads ONE build, and the person
at the shell selects it with ``--variant`` and ``--image``.  A project of
one build needs no option.  The rule and its reasons are in
``mcp/shared/variants.py``; this module only gives it the words of a
command line.

WHY not the newest build when the options are absent: the CLI did that,
and an answer from a build that nobody asked for looks correct.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.settings import Config

NO_BUILD = "No build config indexed."


def add_build_selector(parser: argparse.ArgumentParser, what: str, *, each_build: bool = False) -> None:
    """Add ``--variant`` and ``--image`` to *parser*; *what* says what the build is for.

    *each_build* is for a command that does its work in each build when
    the options are absent (`selected_builds`).
    """
    without = (
        "Without --variant and --image, the command does its work in each build."
        if each_build else "A project of one build needs no option."
    )
    parser.add_argument(
        "--variant", metavar="NAME", default="",
        help=f"Variant of the build {what}. {without} 'fw-context status' lists the builds.",
    )
    parser.add_argument(
        "--image", metavar="NAME", default="",
        help=f"Image of the build {what}, when that build has an image name.",
    )


def selected_build(
    conn: sqlite3.Connection, project_id: str, cfg: Config, args: argparse.Namespace,
) -> tuple[sqlite3.Row | None, str | None]:
    """Return the build row that the options of *args* select, or the error text.

    The text is the refusal of the MCP tools in the words of a command line,
    or `NO_BUILD` for an index without a completed build.
    """
    from ..mcp.shared.stale import latest_builds
    from ..mcp.shared.variants import resolve_build

    config_hash, refusal = resolve_build(
        conn, project_id, cfg, getattr(args, "variant", "") or "", getattr(args, "image", "") or "", cli=True,
    )
    if refusal:
        return None, refusal
    if config_hash is None:
        return None, NO_BUILD
    for build in latest_builds(conn, project_id):
        if build["config_hash"] == config_hash:
            return build, None
    return None, NO_BUILD


def selected_builds(
    conn: sqlite3.Connection, project_id: str, cfg: Config, args: argparse.Namespace,
) -> tuple[list[sqlite3.Row], str | None]:
    """Return each build of the project without options, or the one build that they select.

    For a command that WRITES each build, as ``reindex_file`` does: work
    that a change of the tree needs in every build.  Ordered by label, as
    ``fw-context status`` lists them.
    """
    from ..mcp.shared.stale import build_label, latest_builds

    if getattr(args, "variant", "") or getattr(args, "image", ""):
        build, error = selected_build(conn, project_id, cfg, args)
        return ([build] if build is not None else []), error
    builds = sorted(latest_builds(conn, project_id), key=build_label)
    return builds, (None if builds else NO_BUILD)


def label(build: sqlite3.Row) -> str:
    """Return the label of *build* for the output: ``variant/image``, or ``(no name)``."""
    from ..mcp.shared.stale import build_label

    return build_label(build) or "(no name)"


# The kinds of an index that a command cannot read as it is (reading_problem).
OLDER, EMPTY, UNREADABLE = "older", "empty", "unreadable"


def reading_problem(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Return ``(kind, text)`` when a read-only *conn* cannot read the index as this version writes it, else None.

    *kind* is :data:`OLDER` (an older fw-context wrote it: only an index
    run migrates it), :data:`EMPTY` (the file holds no tables) or
    :data:`UNREADABLE` (SQLite cannot read the file).  ``open_db`` made the
    tables and migrated the schema for each command; a read-only open does
    neither, because within one project only a writer that holds the index
    lock writes.
    """
    from ..indexer.db import missing_columns

    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'build_configs'"
        ).fetchone():
            return EMPTY, "the index holds no tables. Run 'fw-context index' to make it."
        if missing_columns(conn):
            return OLDER, ("an older fw-context wrote this index. Run 'fw-context index' to migrate it, "
                           "then run this command again.")
    except sqlite3.DatabaseError as exc:
        return UNREADABLE, f"the index cannot be read ({exc}). Run 'fw-context doctor' to check it."
    return None


def open_for_reading(db_path: Path) -> sqlite3.Connection | None:
    """Open the index read-only for a command that only reads it, or print the error and return None.

    See :func:`reading_problem` for an index that the command cannot read.
    """
    from ..indexer.db import open_db_readonly

    try:
        conn = open_db_readonly(db_path)
    except sqlite3.DatabaseError as exc:
        print(f"error: the index cannot be read ({exc}). Run 'fw-context doctor' to check it.", file=sys.stderr)
        return None
    problem = reading_problem(conn)
    if problem is not None:
        conn.close()
        print(f"error: {problem[1]}", file=sys.stderr)
        return None
    return conn
