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
        _store_symbols(conn, config_hash, source, project_root)
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


# Directives that name a symbol.  GNU as accepts both spellings of the
# global one, and the projects measured use `.global`; a regex that knows
# only `.globl` returns nothing on any of them.
_GLOBAL_RE = re.compile(r"^\s*\.(?:globl|global)\s+([A-Za-z_.$][\w.$]*)")
_WEAK_RE = re.compile(r"^\s*\.weak\s+([A-Za-z_.$][\w.$]*)")
_TYPE_RE = re.compile(r"^\s*\.type\s+([A-Za-z_.$][\w.$]*)\s*,\s*[%@#]?(\w+)")
_LABEL_RE = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*:")
# `.thumb_set alias, target` is how CMSIS points an unimplemented interrupt
# at the default handler.  `.set` and `.equ` do the same for other targets.
_ALIAS_RE = re.compile(
    r"^\s*\.(?:thumb_set|set|equ)\s+([A-Za-z_.$][\w.$]*)\s*,\s*([A-Za-z_.$][\w.$]*)\s*$"
)
# A label directive can carry a `.section` before it on the same line, so
# every pattern here matches a STATEMENT, not a whole line.


@dataclass
class AsmSymbol:
    """A symbol an assembly unit defines.

    Attributes:
        name: The symbol as written.
        file: Resolved path of the file holding the label.
        line: Line of the label in that file.
        kind: ``"function"`` or ``"variable"``, from ``.type``.  GNU as does
            not require the directive, so a label without one is called a
            function: in assembly an exported label is overwhelmingly code,
            and calling it a variable would put it in the wrong place in
            every answer.
        is_global: The unit exported it with ``.global``.
        is_weak: Declared ``.weak``, thus a definition another object may
            override.  The vector-table step needs this to tell an
            interrupt that is handled from one that is not.
        alias_target: For ``.thumb_set``/``.set``/``.equ``, the name this
            one points at.  None otherwise.
    """

    name: str
    file: str
    line: int
    kind: str = "function"
    is_global: bool = False
    is_weak: bool = False
    alias_target: str | None = None


def extract_symbols(source: AsmSource) -> list[AsmSymbol]:
    """Return the symbols *source* defines, located in their own files.

    A label is what defines a symbol; ``.global`` and ``.weak`` only say
    how it is exported, and ``.type`` says what it is.  The directives may
    come before or after the label — measured, irq_cm4f.S writes `.type`
    then `.global` then the label — so the whole unit is read before
    anything is decided.

    Each line is split on `;` first, because the assembler reads it that
    way and a macro expansion arrives as one line of several statements.
    Zephyr's SECTION_FUNC expands to
    `.section .text.sym, "ax"; .thumb; .balign 4; sym :` — anchoring on the
    start of the line finds `.section` there and never sees the label, which
    is why this returned nothing at all on Zephyr assembly.

    An alias (``.thumb_set``) has no label of its own and is reported at the
    line of the directive: it IS the definition.
    """
    labels: dict[str, tuple[str, int]] = {}
    kinds: dict[str, str] = {}
    globals_: set[str] = set()
    weaks: set[str] = set()
    aliases: dict[str, tuple[str, str, int]] = {}

    # splitlines() once, not once per line: the output of a Zephyr unit is
    # over a thousand lines and re-splitting it each time made this
    # quadratic for no reason.
    text_lines = source.text.splitlines()

    for out_line, mapped in enumerate(source.line_map):
        if mapped is None:
            continue
        file, line = mapped

        for stmt in text_lines[out_line].split(";"):
            m = _LABEL_RE.match(stmt)
            if m is not None:
                labels.setdefault(m.group(1), (file, line))
                continue
            m = _GLOBAL_RE.match(stmt)
            if m is not None:
                globals_.add(m.group(1))
                continue
            m = _WEAK_RE.match(stmt)
            if m is not None:
                weaks.add(m.group(1))
                continue
            m = _TYPE_RE.match(stmt)
            if m is not None:
                kinds[m.group(1)] = "variable" if m.group(2) == "object" else "function"
                continue
            m = _ALIAS_RE.match(stmt)
            if m is not None:
                aliases.setdefault(m.group(1), (m.group(2), file, line))

    found: list[AsmSymbol] = []
    for name, (file, line) in labels.items():
        found.append(AsmSymbol(
            name=name, file=file, line=line,
            kind=kinds.get(name, "function"),
            is_global=name in globals_, is_weak=name in weaks,
        ))
    for name, (target, file, line) in aliases.items():
        if name in labels:
            continue  # a real label wins over an alias of the same name
        found.append(AsmSymbol(
            name=name, file=file, line=line,
            kind=kinds.get(name, "function"),
            is_global=name in globals_, is_weak=name in weaks,
            alias_target=target,
        ))
    return found


# NOT here: header edges for assembly units.  They were planned and
# measured out.  An assembly unit is reprocessed on every index run — 25 to
# 53 ms each, 374 ms for the largest project — so nothing gates on its
# staleness and an edge would record a dependency no reader consults.  The
# headers themselves are already in the manifest anyway: measured, assembly
# adds one to three that C did not already pull in, on four of the five
# projects that have assembly.
#
# The edge becomes worth recording the day an unchanged assembly unit is
# skipped.  At 374 ms that day is not close.


# USR namespace for a symbol that libclang never saw.  libclang mints
# `c:@F@name` and friends; assembly needs its own prefix or a handler
# defined both in C and in a `.S` would collide on the primary key and one
# definition would silently replace the other.
_ASM_USR_PREFIX = "asm:"


def _store_symbols(conn, config_hash: str, source: AsmSource, project_root: Path) -> int:
    """Write the symbols of one preprocessed unit.  Returns how many.

    A weak definition is stored like any other and marked through its USR
    namespace, not merged with the C one of the same name: `except.S`
    defines HardFault_Handler weakly and a project defines it strongly, and
    both are true — the linker picks, the index reports.
    """
    from fw_context_mcp.indexer.db import insert_symbols_batch, upsert_file
    from fw_context_mcp.indexer.ops import _normalize_file_path

    symbols = extract_symbols(source)
    if not symbols:
        return 0

    file_ids: dict[str, int] = {}
    rows = []
    for sym in symbols:
        db_path = _normalize_file_path(sym.file, project_root)
        if db_path not in file_ids:
            file_ids[db_path] = upsert_file(conn, config_hash, db_path, "c")
        rows.append((
            config_hash, file_ids[db_path], db_path, sym.name,
            f"{_ASM_USR_PREFIX}{db_path}@{sym.name}", sym.name, sym.name,
            sym.kind, sym.line, 1, sym.line, 1,
            "", "", None, 0, 0, "", 0, "", 1, 0.0, "",
        ))
    insert_symbols_batch(conn, rows)
    return len(rows)
