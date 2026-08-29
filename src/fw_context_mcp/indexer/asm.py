"""Assembly translation units — preprocess, and map the output back.

WHY this module exists at all: libclang reads a translation unit as C or
C++.  Handed a `.S` it parses it as C and yields no cursor and one error
diagnostic — measured on a Zephyr startup.S: 0 cursors, "error: expected
identifier or '('".  So assembly reaches the index through the preprocessor
instead, and `runner.run()` keeps skipping it for libclang.

WHY the preprocessor and not a regex over the file: `.S` with a capital
letter means, in the words of the gcc suffix table, "assembly that must be
preprocessed".  Running it is what the build does, not a trick this module
plays, and three things depend on it:

* ``#if`` resolves, so an inactive branch does not reach the index.
* Macros expand.  Zephyr writes ``GTEXT(sym)``, not ``.globl sym``; a regex
  over the raw text finds nothing at all.
* An include can carry the whole file.  Measured on FM,
  ``startup_stm32yyxx.S`` is five lines of ``#include CMSIS_STARTUP_FILE``
  and expands to 946 lines holding a 194-entry vector table.

The output is split per source file through the ``# <line> "<file>"``
linemarkers, so every line keeps the file and line it came from — which is
often an included file rather than the unit itself, as in that FM case.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# How long the preprocessor may take on one unit.  It reads a handful of
# files and expands macros; a minute is far more than any real assembly
# unit needs, and the run must not hang on a pathological include loop.
PREPROCESS_TIMEOUT_S: float = 60.0

# `# <line> "<file>" <flags>` — the linemarker cpp emits whenever the
# output switches file or jumps within one.  The flags (1 = entering a
# file, 2 = returning) are not needed: the file and line alone place every
# following line.
_LINEMARKER_RE = re.compile(r'^# (\d+) "([^"]*)"')

# Linemarkers cpp emits for things that are not files on disk.  Lines
# attributed to them belong to no source and are dropped.
_SYNTHETIC_FILES = frozenset({"<built-in>", "<command line>", "<stdin>", ""})


@dataclass
class AsmSource:
    """The preprocessed form of one assembly translation unit.

    Attributes:
        text: The whole preprocessed output, linemarkers included.  Steps
            that read directives (symbols, the vector table) walk this with
            *line_map* beside it.
        line_map: One entry per line of *text*, holding ``(file, line)`` of
            the source it came from, or None for a linemarker and for a
            line attributed to a synthetic file.  Indexed by output line,
            zero-based.
        active: ``{resolved file path: set of source line numbers}`` — the
            lines that survived the preprocessor.  A file appears here only
            if it contributed at least one line, which is what makes an
            ``#if`` branch that was not taken absent rather than blank.
    """

    text: str
    line_map: list[tuple[str, int] | None]
    active: dict[str, set[int]] = field(default_factory=dict)


def preprocess(unit) -> AsmSource | None:
    """Run the preprocessor on *unit* and map its output back to sources.

    Returns None when the preprocessor cannot answer — it is missing, it
    fails, or it runs out of time.  A unit that cannot be preprocessed is
    skipped and counted by the caller, never guessed at: half of an
    assembly file read without its macros would be worse than none of it.

    The compiler comes from the unit's own flags, so a cross build gets its
    own preprocessor rather than the host one.
    """
    args = list(unit.clang_args)
    try:
        result = subprocess.run(
            # -C keeps comments.  Without it cpp strips them, and every
            # comment line then reads as "the preprocessor dropped this"
            # and gets blanked out of files.content — measured on
            # irq_cm4f.S, 63 of 228 lines, including the @brief block that
            # search_content is supposed to find.
            ["clang", "-E", "-C", *args, str(unit.file)],
            capture_output=True,
            text=True,
            timeout=PREPROCESS_TIMEOUT_S,
            cwd=str(unit.directory) if unit.directory else None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("preprocess %s: %s", unit.file.name, exc)
        return None
    if result.returncode != 0:
        first = (result.stderr or "").strip().splitlines()[:1]
        log.debug("preprocess %s failed: %s", unit.file.name, first)
        return None

    base = unit.directory if unit.directory else unit.file.parent
    return _map_output(result.stdout, Path(base))


def _map_output(text: str, base: Path) -> AsmSource:
    """Attribute every output line to the source file and line it came from.

    cpp announces a position with a linemarker and then counts up from it,
    so the state carried between markers is (file, next line number).  A
    marker itself belongs to no source and maps to None.

    Paths are resolved against *base*, the directory of the compilation.
    cpp echoes them as the command line gave them, so the same file arrives
    both as `./mbed-os/…/except.S` and as an absolute path — measured, that
    put one file into the map twice, once unreadable.  One spelling per
    file is the invariant every path-keyed lookup here relies on.
    """
    line_map: list[tuple[str, int] | None] = []
    active: dict[str, set[int]] = {}
    cur_file: str | None = None
    cur_line = 0

    for raw in text.splitlines():
        marker = _LINEMARKER_RE.match(raw)
        if marker is not None:
            cur_line = int(marker.group(1))
            name = marker.group(2)
            if name in _SYNTHETIC_FILES:
                cur_file = None
            else:
                candidate = Path(name)
                cur_file = str(
                    candidate.resolve()
                    if candidate.is_absolute()
                    else (base / candidate).resolve()
                )
            line_map.append(None)
            continue
        if cur_file is None:
            line_map.append(None)
            cur_line += 1
            continue
        line_map.append((cur_file, cur_line))
        active.setdefault(cur_file, set()).add(cur_line)
        cur_line += 1

    return AsmSource(text=text, line_map=line_map, active=active)


def filtered_content(path: str, active_lines: set[int]) -> str | None:
    """Return the text of *path* with every line the preprocessor dropped blanked.

    The same convention the C path uses: line numbers stay put and an
    inactive line becomes a bare newline, so a caller can quote a line
    number from the index and find it in the editor.  Here "inactive" means
    the preprocessor did not emit it — an ``#if`` branch that was not taken.

    Returns None when the file cannot be read, which the caller treats as
    "no content" rather than as an empty file.
    """
    try:
        original = Path(path).read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
    except OSError:
        return None
    if not original:
        return None

    last = max(active_lines) if active_lines else 0
    return "".join(
        original[i - 1] if i in active_lines else "\n"
        for i in range(1, min(len(original), last) + 1)
    )


def store_units(conn, config_hash: str, units: list, project_root: Path,
                build_dir_patterns: list[str] | None = None) -> tuple[int, int]:
    """Give every assembly unit and its includes a files row.  Returns (files, failed).

    Runs AFTER every C and C++ unit, and that ordering is load-bearing for
    the steps built on top of this one: classifying a vector slot as
    unhandled means asking whether a strong definition exists anywhere, and
    that question has no answer until the rest of the index is in place.

    The rows carry text and nothing else.  libclang never saw these units —
    it cannot read assembly — so they contribute no symbol, and a caller
    that finds a files row here must not conclude the file was parsed.
    """
    from fw_context_mcp.indexer.db import upsert_file
    from fw_context_mcp.indexer.manifest import _is_generated_header
    from fw_context_mcp.indexer.ops import _normalize_file_path
    from fw_context_mcp.utils import compute_source_hash

    stored = 0
    failed = 0
    for unit in units:
        source = preprocess(unit)
        if source is None:
            failed += 1
            continue
        for path, active_lines in source.active.items():
            content = filtered_content(path, active_lines)
            if content is None:
                continue
            db_path = _normalize_file_path(path, project_root)
            resolved = Path(path)
            try:
                mtime = resolved.stat().st_mtime
            except OSError:
                mtime = 0.0
            upsert_file(
                conn, config_hash, db_path,
                "c",
                generated=_is_generated_header(db_path, build_dir_patterns),
                mtime=mtime,
                source_hash=compute_source_hash(resolved),
            )
            conn.execute(
                "UPDATE files SET content=? WHERE config_hash=? AND path=?",
                (content, config_hash, db_path),
            )
            stored += 1
    return stored, failed
