"""The marker of rows of a build that no post-processing saw.

An index run writes the rows of each translation unit in its own
transaction, and the post-processing over the whole build comes after the
last unit.  A run that stops between the two (a watchdog, a superseding
manual operation, a crash) leaves rows that no post-processing saw.  The
manifest checkpoint (``runner._checkpoint_manifest``) then lets the next
run keep those units as unchanged, thus that run can parse nothing.  The
values of the macros are then lost: the step runs only after a parse
(``_postprocess._needs_macros``).  The marker makes it run.

The marker has two states:

- *Rows unseen* (an empty file, or any other text): rows exist that no
  post-processing saw.  An index run repairs it.
- *No answer* (``{"no_answer": [[unit, reason], ...]}``): the last macros
  step ran, and the preprocessor gave no answer for these units (a
  missing header, no clang).  An index run alone repairs nothing: the
  cause must be repaired first.  The next run asks these units again.

The writers:

- The runner, before the first unit that it parses (rows unseen).  After
  the post-processing it removes the marker, keeps it as rows unseen when
  the macros step failed, or writes the units with no answer.
- ``reindex_file``, before the first row that it stores (rows unseen): the
  values of the macros need each unit of the build, thus it writes none.

The readers: the runner (``pending_run``, each state: the macros step
runs again), and the state of the index.  Rows unseen is a reason of
``mcp.shared.stale.quick_structural_reasons``: each query answer, and the
start check of the daemon, which then starts the run.  No answer is a
reason of the state of each query answer and of ``get_project_status``,
with the units and the reasons, and not of the start check of the
daemon: a run at each start of the daemon repaired nothing.

The other steps read the database and run in every run.  A marker of an
older version holds a list of units to parse again, for the edges of a
TEMP table that is gone; the list has no use now, and only the file
counts.

One file for each ``config_hash``, next to the manifest of the build, so
that the builds of a project with variants and images do not share it.
The orphan cleanup at the start of a run removes the marker of a build
that is gone (``_embedding._cleanup_orphaned_cc_artifacts``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
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
    """Say if the next run must run the macros step: the marker is there, in either state."""
    return marker_path(db_dir, config_hash).is_file()


@dataclass(frozen=True)
class MarkerState:
    """The state of the marker of a build, from one read of the file.

    *no_answer* holds ``(unit, reason)`` of each unit to which the
    preprocessor of the last macros step gave no answer.  Empty: the state
    rows unseen.
    """

    no_answer: tuple[tuple[str, str], ...]

    @property
    def rows_unseen(self) -> bool:
        """True in the state rows unseen: an index run repairs it."""
        return not self.no_answer


def read_state(db_dir: Path, config_hash: str) -> MarkerState | None:
    """Return the state of the marker of the build *config_hash*, or None when there is no marker.

    One read of the file.  WHY one: a check of the file and a second read
    of its text could see two states (a run that removes or writes the
    marker in between).  A marker that cannot be read, or that holds no
    list of units (an empty file, a text of an older version), is the state
    rows unseen: the next run runs the step, which is the safe direction.
    """
    try:
        text = marker_path(db_dir, config_hash).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # ValueError: a text that is not UTF-8 (UnicodeDecodeError).
        return MarkerState(())
    try:
        data = json.loads(text)
    except ValueError:
        return MarkerState(())
    units = data.get("no_answer") if isinstance(data, dict) else None
    if not isinstance(units, list):
        return MarkerState(())
    return MarkerState(tuple(
        (str(item[0]), str(item[1])) for item in units if isinstance(item, list) and len(item) == 2
    ))


def rows_unseen(db_dir: Path, config_hash: str) -> bool:
    """Say if rows of the build exist that no post-processing saw (one read, :func:`read_state`)."""
    state = read_state(db_dir, config_hash)
    return state is not None and state.rows_unseen


def no_answer_units(db_dir: Path, config_hash: str) -> list[tuple[str, str]]:
    """Return ``(unit, reason)`` of each unit to which the last macros step had no answer (one read, :func:`read_state`)."""
    state = read_state(db_dir, config_hash)
    return [] if state is None else list(state.no_answer)


def mark_pending(db_dir: Path, config_hash: str) -> None:
    """Write the marker, before the first row of the run.

    The write is atomic.  An error goes up: a run that cannot write its
    marker cannot promise that a stop leaves a trace, and the index
    directory is writable for any run that can write the database.
    """
    write_text_atomic(marker_path(db_dir, config_hash), "")


def mark_no_answer(db_dir: Path, config_hash: str, units: Sequence[tuple[str, str]]) -> None:
    """Write the marker in the state no answer: the units and the reasons of the preprocessor (see the module)."""
    write_text_atomic(
        marker_path(db_dir, config_hash),
        json.dumps({"no_answer": [[unit, reason] for unit, reason in units]}, ensure_ascii=False),
    )


def clear_pending(db_dir: Path, config_hash: str) -> None:
    """Remove the marker, after the post-processing of the run."""
    marker_path(db_dir, config_hash).unlink(missing_ok=True)
