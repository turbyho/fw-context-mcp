"""State of the automatic build, shared by the CLI and the MCP handlers.

A source file that the build system never saw has no translation unit: it
is absent from compile_commands.json, and only a build puts it there.  A
plain reindex skips it without a word, thus fw-context runs the build
itself where it safely can.

WHY this lives in ``indexer/`` rather than in ``cli/``: two layers need the
same answers.  ``cli/_index.py`` needs them to decide whether to build, and
``mcp/handlers/maintenance.py`` needs them to tell the caller what will
happen.  An import from ``cli/`` into ``mcp/`` would cross the layer
boundary that CLAUDE.md draws; ``indexer/`` sits below both.

Two markers live next to the index database:

* ``autobuild.failed`` — a build that fw-context started and that failed.
  Without it the same build would run on every daemon cycle: a failure
  leaves compile_commands.json untouched, thus the file stays "newer" than
  it and the trigger stays armed.
* ``excluded_sources.json`` — files that a build ran for and still did not
  cover.  The build system does not want them, thus reporting them again
  only tells the caller to run a command that changes nothing.
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

FAILED_MARKER = "autobuild.failed"
EXCLUDED_MARKER = "excluded_sources.json"

# How long a failure keeps the automatic build off.  Long enough that a
# broken tree does not burn the CPU, short enough that a repair takes effect
# within one working session.  An explicit `fw-context index --build`
# ignores the marker.
BACKOFF_S = 1800.0


class AutobuildState(Enum):
    """What will happen to a source file that compile_commands.json misses."""

    WILL_BUILD = "will_build"
    """fw-context builds it on its own; the caller does nothing."""

    UNSUPPORTED = "unsupported"
    """The backend cannot build in the background; the caller must act."""

    BACKOFF = "backoff"
    """A build was tried for these files and failed; the caller must act."""


def blocked(db_dir: Path, new_sources: list[str]) -> bool:
    """Tell whether a previous automatic build failed for the same files.

    The marker holds the file list of the attempt.  A different list means
    that the tree moved on, thus the next attempt is worth one more try; the
    same list within the backoff window means that nothing changed and the
    build would fail again.
    """
    marker = db_dir / FAILED_MARKER
    try:
        stamp, _, recorded = marker.read_text(encoding="utf-8").partition("\n")
        age = time.time() - float(stamp)
    except (OSError, ValueError):
        return False
    if age > BACKOFF_S:
        return False
    return recorded.splitlines() == new_sources


def record_failure(db_dir: Path, new_sources: list[str]) -> None:
    """Remember a failed automatic build, so the next run does not repeat it."""
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / FAILED_MARKER).write_text(
            f"{time.time()}\n" + "\n".join(new_sources), encoding="utf-8"
        )
    except OSError:
        log.debug("could not write the autobuild failure marker", exc_info=True)


def clear_failure(db_dir: Path) -> None:
    """Drop the marker after a build that worked."""
    try:
        (db_dir / FAILED_MARKER).unlink(missing_ok=True)
    except OSError:
        log.debug("could not remove the autobuild failure marker", exc_info=True)


def state(builder_cls, build_cfg, db_dir: Path, new_sources: list[str]) -> AutobuildState:
    """Say what will happen to *new_sources* on this project.

    *builder_cls* is the class from the registry, or None when no build
    system was detected.  The question goes to the backend with the config
    the build would really use — ``protocol.py`` states that the answer may
    depend on ``isolated_build_dir``.
    """
    from .builders import background_build_safe

    if builder_cls is None or not background_build_safe(builder_cls(), build_cfg):
        return AutobuildState.UNSUPPORTED
    if blocked(db_dir, new_sources):
        return AutobuildState.BACKOFF
    return AutobuildState.WILL_BUILD


def load_excluded(db_dir: Path) -> dict[str, str]:
    """Return ``{path: source_hash}`` of files the build system does not want.

    An unreadable or damaged marker reads as empty.  Erring towards one
    extra report is right: the alternative is to hide a file that the index
    really does not cover.
    """
    try:
        data = json.loads((db_dir / EXCLUDED_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def record_excluded(db_dir: Path, paths_with_hashes: dict[str, str]) -> None:
    """Store the files a build ran for and still did not cover.

    The value is the hash of the content, not a timestamp.  An edit changes
    it and the file is reported again — which is right, because the user may
    have just added it to the build.  A timestamp would either expire while
    nothing changed or never expire at all.

    An empty mapping removes the marker: every file it named is now covered.
    """
    if not paths_with_hashes:
        clear_excluded(db_dir)
        return
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / EXCLUDED_MARKER).write_text(
            json.dumps(paths_with_hashes, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        log.debug("could not write the excluded-sources marker", exc_info=True)


def clear_excluded(db_dir: Path) -> None:
    """Drop the excluded-sources marker."""
    try:
        (db_dir / EXCLUDED_MARKER).unlink(missing_ok=True)
    except OSError:
        log.debug("could not remove the excluded-sources marker", exc_info=True)
