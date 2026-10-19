"""The marker of a ``fw-context index --force-llm`` command that did not reach each symbol.

A new LLM analysis of each symbol takes hours, and a run that a watchdog
or a person stops is not rare, as is an Ollama server that stops during
the run.  The marker keeps the start time of the first command and its
chat model, in the index directory.  The next ``--force-llm`` with the same
model continues: an analysis from after that time comes from the local
cache, and only the other symbols go to the model.  Without the marker,
each new command began again from the first symbol.

* The CLI writes the marker under the index lock (:func:`since_for`).
* The analysis step of a build sets ``unfinished`` when it did not reach
  each symbol (:func:`mark_unfinished`): Ollama did not answer, or some
  symbols got no analysis.  A command can end with exit code 0 in that
  case, thus the exit code alone does not tell it.
* At the end of a command that ended well, the CLI removes the marker, or
  keeps it when a build set ``unfinished`` (:func:`finish`).
* ``reset_index`` removes it with the index (:func:`remove`).

The time is UTC in the format of ``analyzed_at`` of the local cache
(``YYYY-MM-DD HH:MM:SS``).  WHY the model is in it: the content hash of
an analysis does not hold the model, thus a command for another model
that continued would take the analyses of the old model from the cache.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from ..utils import write_text_atomic

log = logging.getLogger(__name__)

MARKER = "force_llm.json"


def _read(db_dir: Path) -> dict | None:
    try:
        data = json.loads((db_dir / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def since_for(db_dir: Path, model: str) -> tuple[str, bool]:
    """Return the start time of the command for *model*, and True when it continues a stopped one.

    A marker of another model does not apply: the command begins again,
    and the marker gets the new time and model.
    """
    data = _read(db_dir)
    if data is not None and data.get("model") == model and isinstance(data.get("since"), str) and data["since"]:
        write_text_atomic(db_dir / MARKER, json.dumps({"since": data["since"], "model": model}))
        return data["since"], True
    since = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    write_text_atomic(db_dir / MARKER, json.dumps({"since": since, "model": model}))
    return since, False


def mark_unfinished(db_dir: Path) -> None:
    """Record that an analysis step of the command did not reach each symbol."""
    data = _read(db_dir)
    if data is None:
        return
    data["unfinished"] = True
    write_text_atomic(db_dir / MARKER, json.dumps(data))


def finish(db_dir: Path) -> bool:
    """End the command: remove the marker and return True, or keep it and return False when unfinished."""
    data = _read(db_dir)
    if data is not None and data.get("unfinished"):
        return False
    remove(db_dir)
    return True


def remove(db_dir: Path) -> None:
    """Remove the marker (``reset_index``, and a command that reached each symbol)."""
    (db_dir / MARKER).unlink(missing_ok=True)
