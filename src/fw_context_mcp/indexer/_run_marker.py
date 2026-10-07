"""The marker of an index run that stored rows and did not reach its end.

An index run writes the rows of each translation unit in its own
transaction, and the post-processing over the whole build comes after the
last unit.  A run that stops between the two (a watchdog, a superseding
manual operation, a crash) leaves rows that no post-processing saw.  The
manifest checkpoint (``runner._checkpoint_manifest``) then lets the next
run keep those units as unchanged, thus that run can parse nothing.  Two
results of the post-processing are then lost:

* The expanded values of the macros: the step runs only after a parse
  (``_postprocess._needs_macros``).  The marker makes it run.
* The dispatch edges (a callback that an event loop, a thread start or a
  timer calls).  A parse writes them to a TEMP table of its connection,
  and the post-processing resolves them; a stop loses the table.  Only a
  new parse of the unit gives them again, thus the marker holds the units
  whose edges were not resolved, and the next run parses them.

The other steps read the database and run in every run.

The runner writes the marker before the first unit that it parses, and
removes it after the post-processing.  One file for each ``config_hash``,
next to the manifest of the build, so that the builds of a project with
variants and images do not share it.  The orphan cleanup at the start of a
run removes the marker of a build that is gone
(``_embedding._cleanup_orphaned_cc_artifacts``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from ..utils import write_text_atomic

log = logging.getLogger(__name__)

PREFIX = "index_run."
SUFFIX = ".pending"


def marker_path(db_dir: Path, config_hash: str) -> Path:
    """Return the path of the marker of the build *config_hash*."""
    return db_dir / f"{PREFIX}{config_hash}{SUFFIX}"


def config_hash_of(name: str) -> str | None:
    """Return the config_hash in the file name *name* of a marker, or None for another file."""
    if name.startswith(PREFIX) and name.endswith(SUFFIX):
        return name[len(PREFIX): -len(SUFFIX)]
    return None


def is_pending(db_dir: Path, config_hash: str) -> bool:
    """Say if an earlier run of the build stored rows and stopped before its end."""
    return marker_path(db_dir, config_hash).is_file()


def units_to_parse(db_dir: Path, config_hash: str) -> frozenset[str]:
    """Return the normalized paths of the units whose dispatch edges a stopped run lost.

    An empty set when there is no marker, or no such unit.  A marker that
    cannot be read gives an error: the next run must not keep the units as
    unchanged without the list.
    """
    path = marker_path(db_dir, config_hash)
    if not path.is_file():
        return frozenset()
    text = path.read_text(encoding="utf-8")
    return frozenset(json.loads(text)["parse"]) if text else frozenset()


def mark_pending(db_dir: Path, config_hash: str, parse: Iterable[str] = ()) -> None:
    """Write the marker with the units that the next run must parse.

    The runner calls it before the first row of the run, at each manifest
    checkpoint, and before the post-processing, each time with every unit
    whose dispatch edges are not resolved yet.  The write is atomic: a cut
    marker would stop the next run.  An error goes up: a run that
    cannot write its marker cannot promise that a stop leaves a trace, and
    the index directory is writable for any run that can write the database.
    """
    write_text_atomic(marker_path(db_dir, config_hash), json.dumps({"parse": sorted(parse)}))


def clear_pending(db_dir: Path, config_hash: str) -> None:
    """Remove the marker, after the post-processing of the run."""
    marker_path(db_dir, config_hash).unlink(missing_ok=True)
