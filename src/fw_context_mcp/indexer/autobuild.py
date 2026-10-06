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

* ``build_problem.json`` — why the last index run stopped without an index:
  the build is not there, or the run failed.  Each query tool answer of
  the project carries this text as a warning, and ``get_active_build``
  names it as a reason.  A file and not a check per query: the check reads
  compile_commands.json, and the index run already did it.  The next run
  that ends well with the build there removes the file.
* ``excluded_sources.json`` — files that a build ran for and still did not
  cover.  The build system does not want them, thus reporting them again
  only tells the caller to run a command that changes nothing.

A failed automatic build is not blocked for a time.  The daemon starts a
run only on a change of a C/C++ file, and only one run at a time, thus a
build that fails again costs one build per edit.  A pause hid the error
from the user and kept a repaired build off for no reason.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

PROBLEM_MARKER = "build_problem.json"
EXCLUDED_MARKER = "excluded_sources.json"


@dataclass(frozen=True)
class BuildProblem:
    """The content of the problem marker."""

    text: str
    """The sentence for the caller; it names the command that repairs it."""

    build_missing: bool
    """True when the run stopped because the build is not there.  A reader
    that finds the build there again knows that the text is out of date;
    the text of a failed run stays true until a run ends well."""


class AutobuildState(Enum):
    """What will happen to a source file that compile_commands.json misses."""

    WILL_BUILD = "will_build"
    """fw-context builds it on its own; the caller does nothing."""

    UNSUPPORTED = "unsupported"
    """The backend cannot build in the background; the caller must act."""


def record_problem(db_dir: Path, text: str, *, build_missing: bool = False) -> None:
    """Remember why the last index run stopped, for each MCP answer.

    A marker that cannot be written costs the warning, never the run, thus
    the error goes to the debug log only.
    """
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / PROBLEM_MARKER).write_text(
            json.dumps({"text": text, "build_missing": build_missing}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        log.debug("could not write the build problem marker", exc_info=True)


def clear_problem(db_dir: Path) -> None:
    """Drop the marker after an index run that ended well."""
    try:
        (db_dir / PROBLEM_MARKER).unlink(missing_ok=True)
    except OSError:
        log.debug("could not remove the build problem marker", exc_info=True)


def read_problem(db_dir: Path) -> BuildProblem | None:
    """Give the content of the marker, or None when there is none."""
    try:
        data = json.loads((db_dir / PROBLEM_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError and json.JSONDecodeError.  A
        # damaged marker reads as no marker; the next index run writes or
        # removes it again.
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
        return None
    return BuildProblem(text=data["text"].strip(), build_missing=data.get("build_missing") is True)


def state(builder_cls, build_cfg) -> AutobuildState:
    """Say what will happen to a source file that compile_commands.json misses.

    *builder_cls* is the class from the registry, or None when no build
    system was detected.  The question goes to the backend with the config
    the build would really use — ``protocol.py`` states that the answer may
    depend on ``isolated_build_dir``.
    """
    from .builders import background_build_safe

    if builder_cls is None or not background_build_safe(builder_cls(), build_cfg):
        return AutobuildState.UNSUPPORTED
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


def build_missing_reason(compile_commands: Path) -> str:
    """Say why the build of *compile_commands* is not there, or give "".

    The build is not there when compile_commands.json does not exist, or
    when a ``directory`` of its entries does not exist (``rm -rf build``,
    ``idf.py fullclean``, while a builder kept a copy of the file in
    ``.fw-context/``).  WHY it matters: the parse asks the compiler of each
    unit in that directory, as the build runs it (``_driver_query``), and the
    build makes its generated headers there.  Without the directory, each
    unit gets no answer of its compiler.

    A file that does not parse gives "": the run that reads it reports the
    real error.  A ``directory`` is read as compile_commands.json gives it,
    as ``compile_commands.parse`` reads it.
    """
    if not compile_commands.exists():
        return f"compile_commands.json {compile_commands} does not exist"
    try:
        entries = json.loads(compile_commands.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    if not isinstance(entries, list):
        return ""
    directories = {
        str(entry["directory"]) for entry in entries
        if isinstance(entry, dict) and entry.get("directory")
    }
    missing = sorted(d for d in directories if not Path(d).is_dir())
    if not missing:
        return ""
    more = f" and {len(missing) - 1} more" if len(missing) > 1 else ""
    return f"the build directory {missing[0]}{more} of {compile_commands} does not exist"
