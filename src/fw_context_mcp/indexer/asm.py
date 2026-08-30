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
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ._asm_macro import Report as MacroReport
from ._asm_macro import expand_stream as expand_macros

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


def _is_project_file(path: str, project_root: Path,
                     vendor_patterns: list[str], project_patterns: list[str]) -> int:
    """Say whether *path* is code of the team or of the SDK.

    The same rule the C path uses, and it has to be: a startup file lives in
    the framework package, outside the project entirely — FM's is under
    ~/.platformio/packages — and calling it project code puts ninety weak
    interrupt aliases into `find_dead_code(project_only=True)`.  Measured
    before this: 91 handlers reported, against 2 real ones before assembly
    was indexed at all.
    """
    from .sdk_detect import _path_matches

    resolved = Path(path).resolve()
    try:
        rel = str(resolved.relative_to(project_root.resolve()))
    except ValueError:
        # Outside the project: vendor unless a project pattern claims it.
        return 1 if any(_path_matches(str(resolved), p) for p in project_patterns) else 0
    if any(_path_matches(rel, p) for p in project_patterns):
        return 1
    return 0 if any(_path_matches(rel, p) for p in vendor_patterns) else 1


def _clear_previous_pass(conn, config_hash: str) -> None:
    """Delete everything a previous run of this pass wrote.

    Without it a re-index cannot correct anything this pass stored.  Two
    separate mechanisms hide the stale rows:

    * ``insert_symbols_batch`` merges on ``ON CONFLICT(config_hash, usr)``
      but its guard is ``WHERE excluded.is_definition = 1 AND
      symbols.is_definition = 0``.  Every assembly symbol is a definition,
      so an existing row matches neither side and the UPDATE never runs —
      the row from the earlier index survives untouched, line number,
      is_project and all.  Measured: an is_project fix looked like it had
      no effect at all until this function existed.
    * ``refs`` has no unique constraint, so a vector edge is appended
      again on every run.  Measured on FM: one edge reported, two rows.

    The C path solves the same problem with ``replace_file_data``, keyed by
    file id.  That is the wrong key here: an assembly unit can include a
    header that C also includes, and clearing by file would delete the C
    symbols stored earlier in this same run.  The ``asm:`` USR namespace is
    the right key — it exists precisely so the two cannot collide.

    Clearing the whole namespace is safe because the pass has no
    incremental path: ``runner.run`` hands it every assembly unit of the
    build on every run, thus everything deleted here is about to be
    written again.
    """
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM symbols WHERE config_hash=? AND usr LIKE 'asm:%'",
            (config_hash,),
        )
    ]
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        # Ordered like _delete_rows_owned_by: what selects by symbol_id has
        # to go before the symbols do.
        conn.execute(f"DELETE FROM embeddings WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        conn.execute(f"DELETE FROM llm_analysis WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        try:
            conn.execute(f"DELETE FROM vec_symbols WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        except sqlite3.OperationalError:
            pass  # sqlite-vec not loaded — the virtual table does not exist
    conn.execute(
        "DELETE FROM symbols WHERE config_hash=? AND usr LIKE 'asm:%'", (config_hash,)
    )
    conn.execute(
        "DELETE FROM refs WHERE config_hash=? AND ref_kind=?",
        (config_hash, VECTOR_REF_KIND),
    )


def store_units(conn, config_hash: str, units: list, project_root: Path,
                build_dir_patterns: list[str] | None = None,
                vendor_patterns: list[str] | None = None,
                project_patterns: list[str] | None = None) -> AsmResult:
    """Index every assembly unit: text, symbols, and vector table edges.

    Runs AFTER every C and C++ unit, and that ordering is load-bearing for
    the steps built on top of this one: pointing a vector slot at the
    handler it names means asking which definition of that name the linker
    keeps, and that question has no answer until the rest of the index is
    in place.

    libclang never saw these units — it cannot read assembly — so nothing
    here comes from an AST.  The text is the preprocessed source, the
    symbols are the directives it holds, and the vector edges are the slots
    that name a symbol the rest of the index already knows.
    """
    from fw_context_mcp.indexer.db import upsert_file
    from fw_context_mcp.indexer.manifest import _is_generated_header
    from fw_context_mcp.indexer.ops import _normalize_file_path
    from fw_context_mcp.utils import compute_source_hash

    result = AsmResult()
    cleared = False
    for unit in units:
        source = preprocess(unit)
        if source is None:
            result.failed += 1
            continue
        # Deferred to the first unit that preprocessed, so that a run where
        # nothing preprocesses at all — no clang on PATH is the way that
        # happens — leaves the previous index alone instead of emptying it.
        # The C path has the same property for free: a translation unit
        # that fails to parse never reaches its own `replace_file_data`.
        if not cleared:
            _clear_previous_pass(conn, config_hash)
            cleared = True
        symbols = extract_symbols(source, result.macros)
        _store_symbols(
            conn, config_hash, source, project_root, symbols,
            vendor_patterns or [], project_patterns or [],
        )
        linked, unresolved = _store_vectors(
            conn, config_hash, source, project_root,
        )
        result.symbols += len(symbols)
        result.vectors += linked
        result.unresolved += unresolved
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
            # is_project rides along with the content, and it must: the
            # column defaults to 0, `upsert_file` cannot set it, and
            # search_content(project_only=True) filters on it.  Without this
            # a project that writes its own assembly could not find its own
            # file.  MAX keeps the rule the C path uses — a file that any
            # project symbol claimed is never downgraded to vendor.
            conn.execute(
                "UPDATE files SET content=?, is_project=MAX(is_project, ?) "
                "WHERE config_hash=? AND path=?",
                (
                    content,
                    _is_project_file(
                        path, project_root, vendor_patterns or [],
                        project_patterns or [],
                    ),
                    config_hash,
                    db_path,
                ),
            )
            result.files += 1
            result.paths.add(db_path)
    return result


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
class AsmResult:
    """What one pass over the assembly units produced.

    Attributes:
        files: files rows written, unit and includes together.
        symbols: symbol definitions found.
        vectors: vector slots linked to a handler.
        unresolved: vector slots naming something the index does not hold —
            a linker symbol such as `_estack`, an ambiguous name, or a
            handler an assembler macro defines, which the C preprocessor
            does not expand.
        failed: units the preprocessor could not read.
        paths: the files rows written, in the spelling files.path uses.
            The coverage purge in _postprocess deletes any row that does
            not belong to the build, and it builds that set from the
            libclang units and the manifest — neither of which knows about
            assembly.  Without this the rows were written and then removed
            in the same run.
        macros: what the assembler-macro expansion did, over every unit.
            Carried out so the run can log it: a refusal nobody can see
            is indistinguishable from a file that simply held nothing.
    """

    files: int = 0
    symbols: int = 0
    vectors: int = 0
    unresolved: int = 0
    failed: int = 0
    paths: set[str] = field(default_factory=set)
    macros: MacroReport = field(default_factory=MacroReport)


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
            override.
        alias_target: For ``.thumb_set``/``.set``/``.equ``, the name this
            one points at.  None otherwise.

    The pair ``is_weak`` + ``alias_target == "Default_Handler"`` is how
    CMSIS says "assembly does not service this interrupt, C may".  The
    vector-table step deliberately does NOT act on it: the slot gets its
    edge either way, and where that edge lands — the startup file rather
    than C — is what tells a reader the interrupt traps.  Acting on it
    once meant asking about the alias before asking the index, and every
    C override was thrown away: measured on FM, one edge instead of 27.
    """

    name: str
    file: str
    line: int
    kind: str = "function"
    is_global: bool = False
    is_weak: bool = False
    alias_target: str | None = None


# GNU as treats a label with this prefix as local to the file and keeps it
# out of the object symbol table.  A compiler-written branch target is
# spelled that way — startup_NRF52840.S has `.LC0` and `.LC1` around its
# copy loop — and indexing them puts names in `lookup_symbol` that no
# linker, debugger or reader will ever ask for.
_LOCAL_LABEL_PREFIX = ".L"


def _strip_block_comments(text: str, in_comment: bool) -> tuple[str, bool]:
    """Remove ``/* */`` regions from one line.  Returns (text, still open).

    The state has to cross lines because the comments do.  A string holding
    the two characters — `.ascii "/*"` — would open one falsely; no
    assembly measured does that, and the alternative is a full tokenizer
    for a case that has not appeared.
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        if in_comment:
            end = text.find("*/", index)
            if end < 0:
                break
            in_comment = False
            index = end + 2
            continue
        start = text.find("/*", index)
        if start < 0:
            out.append(text[index:])
            break
        out.append(text[index:start])
        in_comment = True
        index = start + 2
    return "".join(out), in_comment

# The repetition directives, whose bodies are still skipped rather than
# expanded.  `.macro` is no longer here: `_asm_macro` expands it, and a
# body reached through an invocation is real code.  A `.rept 0` body is
# not, so reading one would invent — see the corpus in
# tests/fixtures/asm, where it did.
_REPEAT_OPEN = re.compile(r"^\s*\.(rept|irp|irpc)\b", re.IGNORECASE)
_REPEAT_CLOSE = re.compile(r"^\s*\.endr\b", re.IGNORECASE)


def _raw_statements(source: AsmSource) -> Iterator[tuple[str, int, str]]:
    """Yield ``(file, line, statement)`` before macros are expanded.

    Two transformations, and each one was needed to read real assembly:

    * Block comments go first.  The preprocessor keeps them — ``-C`` is
      deliberate, because ``files.content`` must match what a reader sees
      in the file — and prose reads like assembly often enough to matter.
      startup_NRF52840.S opens with ``NOTE: Template files ...`` inside
      ``/* */`` and the label pattern took ``NOTE`` for a definition.
    * The line is then split on ``;``, because the assembler reads it that
      way and a macro expansion arrives as one line of several statements.
      Zephyr's ``SECTION_FUNC`` expands to
      ``.section .text.sym, "ax"; .thumb; .balign 4; sym :`` — anchoring on
      the start of the line finds ``.section`` and never the label, which
      is why the symbol pass returned nothing at all on Zephyr assembly.

    The body of ``.rept``, ``.irp`` and ``.irpc`` is skipped here, because
    reading it as ordinary assembly INVENTS symbols: ``.rept 0`` assembles
    its body zero times, so a label inside is defined nowhere.  Measured
    against `arm-none-eabi-as` on the corpus in ``tests/fixtures/asm``,
    which is where that one was found.

    ``.macro`` is NOT skipped here — :func:`_statements` expands it, and
    a statement reached through an invocation is code that really exists.

    A line the map cannot place still advances the comment state: a
    comment opened in it closes lines later, in a line that IS placed.
    """
    # splitlines() once, not once per line: the output of a Zephyr unit is
    # over a thousand lines and re-splitting it each time made this
    # quadratic for no reason.
    text_lines = source.text.splitlines()
    in_comment = False
    repeat_depth = 0
    for out_line, mapped in enumerate(source.line_map):
        clean, in_comment = _strip_block_comments(text_lines[out_line], in_comment)
        if mapped is None:
            continue
        file, line = mapped
        for stmt in clean.split(";"):
            if _REPEAT_OPEN.match(stmt):
                repeat_depth += 1
                continue
            if repeat_depth:
                if _REPEAT_CLOSE.match(stmt):
                    repeat_depth -= 1
                continue
            yield file, line, stmt


def _statements(
    source: AsmSource, report: MacroReport | None = None
) -> Iterator[tuple[str, int, str]]:
    """Yield every statement of the unit, macros expanded.

    The single funnel both the symbol pass and the vector pass read
    through, so a construct handled here is handled by both.

    *report* collects what the expansion did.  Pass one to log it; the
    default discards it, which is what the callers that only want the
    statements do.
    """
    return expand_macros(_raw_statements(source), report or MacroReport())


def extract_symbols(
    source: AsmSource, report: MacroReport | None = None
) -> list[AsmSymbol]:
    """Return the symbols *source* defines, located in their own files.

    A label is what defines a symbol; ``.global`` and ``.weak`` only say
    how it is exported, and ``.type`` says what it is.  The directives may
    come before or after the label — measured, irq_cm4f.S writes `.type`
    then `.global` then the label — so the whole unit is read before
    anything is decided.

    ``_statements`` supplies the lines: it removes block comments, splits
    on `;` and expands assembler macros.  All three matter here — a
    comment line reading ``NOTE: Template files ...`` looked exactly like
    a label definition, a Zephyr macro arrives as several statements on
    one line, and an nRF startup defines every handler through `.macro`.

    *report* collects what the macro expansion did, so the caller can log
    it.  The vector pass reads the same stream and expands the same
    macros again rather than sharing this one, which would double-count
    the report; the cost is one extra pass over a file measured in tens
    of milliseconds.

    An alias (``.thumb_set``) has no label of its own and is reported at the
    line of the directive: it IS the definition.
    """
    labels: dict[str, tuple[str, int]] = {}
    kinds: dict[str, str] = {}
    globals_: set[str] = set()
    weaks: set[str] = set()
    aliases: dict[str, tuple[str, str, int]] = {}

    for file, line, stmt in _statements(source, report):
        m = _LABEL_RE.match(stmt)
        if m is not None:
            if not m.group(1).startswith(_LOCAL_LABEL_PREFIX):
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


def _store_symbols(conn, config_hash: str, source: AsmSource, project_root: Path,
                   symbols: list[AsmSymbol], vendor_patterns: list[str],
                   project_patterns: list[str]) -> int:
    """Write the symbols of one preprocessed unit.  Returns how many.

    A weak definition is stored like any other and marked through its USR
    namespace, not merged with the C one of the same name: `except.S`
    defines HardFault_Handler weakly and a project defines it strongly, and
    both are true — the linker picks, the index reports.
    """
    from fw_context_mcp.indexer.db import insert_symbols_batch, upsert_file
    from fw_context_mcp.indexer.ops import _normalize_file_path

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
            "", "", None, 0, 0, "", 0, "",
            _is_project_file(sym.file, project_root, vendor_patterns, project_patterns),
            0.0, "",
        ))
    insert_symbols_batch(conn, rows)
    return len(rows)


# A vector table slot: a data directive naming the symbol the hardware
# jumps to.  `.word` and `.long` are GNU as, `DCD` is the ARM assembler,
# and both spellings appear in the startup files measured.
_VECTOR_RE = re.compile(
    r"^\s*(?:\.word|\.long|\.quad|DCD|DCQ)\s+([A-Za-z_.$][\w.$]*)\s*$", re.IGNORECASE
)


@dataclass
class VectorEntry:
    """One slot of a vector table, and where it is written."""

    name: str
    file: str
    line: int


def extract_vectors(source: AsmSource) -> list[VectorEntry]:
    """Return the vector table slots that name a symbol.

    A slot holding a number is not one: a reserved slot is written `.word 0`
    and a startup file has dozens of them — measured on FM, 12 of 206.  The
    pattern therefore takes an identifier and nothing else, which also drops
    the IAR form `sfe(CSTACK)`.

    A linker symbol IS returned, `__StackTop` and `_estack` alike.  Nothing
    in the index defines it, so it resolves to no handler and becomes an
    unresolved slot rather than an edge — the right answer, since a slot the
    linker fills is not an interrupt anybody services.

    Reads through ``_statements`` for the same reason the symbol pass does:
    a `.word` inside a block comment is not a slot of the table.  That
    stripping widened what this accepts, because the pattern requires the
    statement to end after the identifier and a trailing `/* Top of Stack */`
    used to prevent the match.  Measured on zbox-ecb-fw the slot count went
    from 53 to 54 and the linked count stayed at 11.
    """
    found: dict[tuple[str, str, int], VectorEntry] = {}
    for file, line, stmt in _statements(source):
        m = _VECTOR_RE.match(stmt)
        if m is not None:
            # Keyed, not appended: cpp can emit the same source line
            # more than once — measured, a Zephyr unit appears twice in
            # its own output — and one slot must not become two edges.
            # The symbol side dedupes the same way, through setdefault.
            key = (m.group(1), file, line)
            found.setdefault(key, VectorEntry(name=m.group(1), file=file, line=line))
    return list(found.values())


# refs.ref_kind for a vector table slot.  NOT "call": nothing calls a
# handler by name, the hardware jumps to an address in a table, and an
# answer that says "called from" would misdescribe how it runs.
VECTOR_REF_KIND = "vector"


def _resolve_handler(conn, config_hash: str, name: str) -> str | None:
    """Return the USR a vector slot points at, or None when it is not clear.

    A handler often has two definitions — a weak one in the SDK's assembly
    and a strong one in the project's C — and the linker takes the strong
    one.  So does this: a definition outside assembly wins, and only when
    there is none does an assembly definition answer.

    None when nothing matches, and None when several equally good ones do.
    A wrong edge here would claim the vector table reaches something it does
    not, and on an interrupt that is the only edge a reader has.
    """
    rows = conn.execute(
        "SELECT usr, is_definition FROM symbols WHERE config_hash=? AND name=?",
        (config_hash, name),
    ).fetchall()
    if not rows:
        return None

    strong = [r for r in rows if r["is_definition"] and not r["usr"].startswith(_ASM_USR_PREFIX)]
    if len(strong) == 1:
        return strong[0]["usr"]
    if len(strong) > 1:
        return None

    from_asm = [r for r in rows if r["usr"].startswith(_ASM_USR_PREFIX)]
    return from_asm[0]["usr"] if len(from_asm) == 1 else None


def _store_vectors(
    conn, config_hash: str, source: AsmSource,
    project_root: Path,
) -> tuple[int, int]:
    """Write a reference for every vector slot.  Returns (linked, unresolved).

    Every slot that names something the index knows gets an edge, and a
    slot pointing at a weak alias of the default handler is no exception.
    That slot exists, the hardware reaches it, and the symbol it names is
    already in the index — withholding the one reference it has would say
    the alias is unreferenced, which is false.

    Whether the interrupt is serviced is then visible from where the edge
    LANDS, and that is a better answer than a count: an edge into C means a
    handler runs, an edge into the startup file means the slot reaches
    `Default_Handler` and the device traps.  Measured on FM, 26 of 97 slots
    land in C, 64 in the startup file, 6 name a linker symbol.

    ``.weak X`` plus ``.thumb_set X, Default_Handler`` is how CMSIS lets C
    override a handler, so the alias never decides anything on its own.
    Asking about it before asking the index threw every override away: the
    same FM table carried one edge instead of 27.

    *unresolved* counts slots naming something the index does not hold —
    a linker symbol such as `_estack`, an ambiguous name, or a handler an
    assembler macro defines, which the C preprocessor does not expand.
    """
    from fw_context_mcp.indexer.db import insert_refs_batch
    from fw_context_mcp.indexer.ops import _normalize_file_path

    rows = []
    unresolved = 0

    for entry in extract_vectors(source):
        usr = _resolve_handler(conn, config_hash, entry.name)
        if usr is None:
            unresolved += 1
            continue
        rows.append((
            config_hash, usr,
            _normalize_file_path(entry.file, project_root), entry.line,
            None, VECTOR_REF_KIND,
        ))

    if rows:
        insert_refs_batch(conn, rows)
    return len(rows), unresolved
