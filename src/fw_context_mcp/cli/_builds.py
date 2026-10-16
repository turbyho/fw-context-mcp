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
