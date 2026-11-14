"""The definitions of the macros at the end of each unit (``clang -dM -E``), and their values in the index.

``clang -dM -E <flags> <file>`` prints each macro that is defined at the
end of the unit, as ``#define NAME definition``: the predefined macros of
the target, the ``-D`` macros of the flags, and the last ``#define`` of
each name in the files of the unit.  The preprocessor does not expand the
macros in a definition: ``#define B 42`` and ``#define A B`` give
``#define A B`` (measured with clang 23).

A row of the ``macros`` table is one ``#define`` line of one file.  Many
units of a build can read that file, and the definition of the name at
the end of each of them can differ: a later ``#undef`` and ``#define``, a
``-D`` of the flags of one unit, a header that only some units include.
Thus the value of a row is the definition of its name at the end of EACH
unit that reads its file (:func:`store_values_of_build`), and not of the
last unit that ran.  The flags are the flags of the parse of each unit
(``compile_commands.json``), thus ``#ifdef`` resolves as for the index.

The same run of the preprocessor writes a dependency file (``-MD``): the
files that the unit reads.  WHY not the header lists of the manifest: they
come from the parse of libclang, thus a unit whose parse failed has none,
and a file that a build lists two times with other flags had one list for
the two listings.  Measured on an ESP32 project (116 units): the files of
the dependency file are a superset of the lists of the manifest, and the
two agree on each file that holds a macro row.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..path_key import canonical_key, canonical_path, index_key
from ..utils import remove_file
from ._clang_cli import clang_binary as _clang_binary
from ._clang_cli import run_clang
from ._driver_options import DRIVER_TAKES_NEXT
from ._parsed_text import decode_lines

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroDefinition:
    """One definition of a macro at the end of a unit, in the form of the ``macros`` table.

    *value* is the replacement text alone, and *params* the parameter list
    without its parentheses, written ``a, b`` (``symbols._split_macro_definition``).
    *params* is empty for an object-like macro and for ``NAME()``: only
    *is_function_like* tells the two apart.
    """

    value: str
    params: str
    is_function_like: bool


class NoMacroAnswer(Exception):
    """``clang -dM -E`` gave no answer for a unit: the reason is the message."""


@dataclass(frozen=True)
class UnitMacros:
    """The answer of the preprocessor for one unit: the definitions at its end, and the files that it read.

    *files* holds the canonical path of each file of the dependency file:
    the source file and each header, also each file of ``-include`` and
    ``-imacros``.
    """

    definitions: dict[str, MacroDefinition]
    files: frozenset[Path]


def parse_dependency_file(text: str) -> list[str]:
    """Return the prerequisites of the one rule in the dependency file *text* that clang writes (``-MD``).

    The form is Make: ``target: file file \\`` with a backslash and a newline
    to continue a line.  The escapes are the ones of clang
    (``DependencyFileGenerator``, the Make form): before a space, clang
    writes each backslash of the name two times and then a backslash; before
    ``#`` one backslash; ``$`` as ``$$``.  Thus a run of *n* backslashes
    before a space is *n* // 2 backslashes of the name and a space of the
    name, and each other backslash is part of the name, as in a path of
    Windows (``C:\\x\\y.h``).  The caller gives the target (``-MT``), thus
    the first ``": "`` ends it, also before a drive letter of Windows.

    A limit of clang, not of this parser: on Linux clang writes a backslash
    of a POSIX name as ``/``, thus such a name names no file, and the rows of
    that file get no state from the unit.
    """
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    _target, separator, body = text.partition(": ")
    if not separator:
        raise ValueError("the dependency file has no rule")
    names: list[str] = []
    current: list[str] = []
    position = 0
    while position < len(body):
        char = body[position]
        if char == "\\":
            run_end = position
            while run_end < len(body) and body[run_end] == "\\":
                run_end += 1
            count = run_end - position
            following = body[run_end] if run_end < len(body) else ""
            if following == " ":
                current.append("\\" * (count // 2) + " ")
                position = run_end + 1
            elif following == "#":
                current.append("\\" * (count - 1) + "#")
                position = run_end + 1
            else:
                current.append("\\" * count)
                position = run_end
            continue
        if char == "$" and body[position:position + 2] == "$$":
            current.append("$")
            position += 2
            continue
        if char in " \t\r\n":
            if current:
                names.append("".join(current))
                current = []
        else:
            current.append(char)
        position += 1
    if current:
        names.append("".join(current))
    return names


def parse_definitions(lines: Iterable[str]) -> dict[str, MacroDefinition]:
    """Return the definitions in the output of ``clang -dM -E``, by name.

    Each line is ``#define NAME definition`` or ``#define NAME(params)
    definition``, with one space before the definition, which can be empty
    (``#define EMPTY `` and ``#define F(x) ``).  The parameters have no
    space after a comma (``F(a,b)``, ``H(x,...)``), measured with clang 22.
    A ``(`` that touches the name opens the parameter list, as in the
    preprocessor: ``#define OBJ (1)`` is object-like.
    """
    definitions: dict[str, MacroDefinition] = {}
    for line in lines:
        if not line.startswith("#define "):
            continue
        rest = line[len("#define "):]
        name_end = next((i for i, char in enumerate(rest) if char in " ("), len(rest))
        name = rest[:name_end]
        if not name:
            continue
        tail = rest[name_end:]
        if tail.startswith("("):
            close = tail.find(")")
            if close < 0:
                continue
            params = ", ".join(part.strip() for part in tail[1:close].split(",") if part.strip())
            definitions[name] = MacroDefinition(_body(tail[close + 1:]), params, True)
        else:
            definitions[name] = MacroDefinition(_body(tail), "", False)
    return definitions


def _body(text: str) -> str:
    """Return the definition after the one space that ``clang -dM -E`` writes before it."""
    return text[1:] if text.startswith(" ") else text


def definitions_at_end_of_unit(
    flags: Sequence[str],
    file_path: Path,
    *,
    clang_binary: str | None = None,
    timeout: float = 30.0,
    cwd: str | Path | None = None,
) -> UnitMacros:
    """Run ``clang -dM -E`` on the unit *file_path* with its *flags*, and return the definitions at its end and the files that it read.

    WHY ``clang -dM -E`` and not only the AST: libclang gives the text of
    each ``#define`` directive in a file.  The preprocessor of the unit
    tells which definition of a name is in force at its end (after
    ``#undef`` and a new ``#define``), and it adds the ``-D`` macros of the
    flags and the predefined macros of the target.  *cwd* is the directory
    of the compilation: a relative path in the flags is relative to it,
    and so is a relative name in the dependency file.

    Raise :class:`NoMacroAnswer` when clang cannot run (each OSError of the
    start), runs out of time, or fails, and when the source file does not
    exist.  WHY no empty
    answer: an empty answer reads as "no macro is defined at the end of
    the unit", which is a wrong value for each row.  Before, a missing
    source file was replaced with an empty file, with that result.
    """
    if not file_path.exists():
        raise NoMacroAnswer(f"the source file {file_path} does not exist")
    cmd = [clang_binary or _clang_binary(), "-dM", "-E", *_sanitize_flags(flags), str(file_path)]
    # A directory of its own, and clang makes the file in it.  WHY not a
    # file of mkstemp: a scanner of Windows can hold a new file, and clang
    # then could not open it for its write.  WHY not a TemporaryDirectory:
    # its cleanup raised the error of a held file out of this function, and
    # the macros step then lost the values of each unit.
    directory = Path(tempfile.mkdtemp(prefix="fwctx-deps-"))
    dependency_file = directory / "unit.d"
    try:
        # -MT: a target of our choice, thus the first ": " ends it
        # (parse_dependency_file).
        arguments = [*cmd[1:], "-MD", "-MF", str(dependency_file), "-MT", "unit"]
        result, dependency_text = _run_with_dependency_file(cmd[0], arguments, dependency_file, timeout, cwd)
    finally:
        _remove_dependency_directory(directory, dependency_file)
    # WHY bytes and decode_lines: a value is text of the sources, decoded as
    # the content fill decodes them (UTF-8 with replacement).  ``text=True``
    # decoded it in the locale: on Windows the ANSI code page, and a byte
    # that is no UTF-8 stopped the run with UnicodeDecodeError on Linux.
    definitions = parse_definitions(line.rstrip("\r\n") for line in decode_lines(result.stdout))
    try:
        names = parse_dependency_file(dependency_text)
    except ValueError as exc:
        raise NoMacroAnswer(f"the dependency file of clang -dM -E: {exc}") from exc
    base = Path(cwd) if cwd is not None else Path.cwd()
    return UnitMacros(definitions, frozenset(canonical_path(base / name) for name in names))


def _remove_dependency_directory(directory: Path, dependency_file: Path) -> None:
    """Remove the dependency file and its directory; a part that stays is a warning.

    ``remove_file``: on Windows a scanner can still hold the new file open,
    and the function waits for a short read.  A file or a directory that
    stays is a leftover in the temporary directory, and the answer of the
    unit stays.  clang removes the file itself when a header is missing.
    """
    try:
        remove_file(dependency_file)
        directory.rmdir()
    except OSError as exc:
        log.warning("The dependency file %s stays in the temporary directory: %s", dependency_file, exc)


def _run_with_dependency_file(
    binary: str, arguments: list[str], dependency_file: Path, timeout: float, cwd: str | Path | None,
) -> tuple[subprocess.CompletedProcess[bytes], str]:
    """Run clang, and return its result and the text of its dependency file; raise :class:`NoMacroAnswer` for each failure.

    The text is decoded as UTF-8, with the bytes that are no UTF-8 kept
    (``surrogateescape``): a name of a POSIX file is bytes, and
    ``canonical_path`` takes the same string back to the same bytes.
    """
    try:
        result = run_clang(binary, arguments, timeout=timeout, cwd=cwd)
    except OSError as exc:
        # The text of the error: clang can be missing, the temporary
        # directory of the response file can be missing too, and the
        # directory of the compilation can be gone or not readable.  It is
        # an answer for this unit, and the other units go on.
        raise NoMacroAnswer(f"clang cannot run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NoMacroAnswer(f"clang -dM -E ran longer than {timeout:.0f} s") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NoMacroAnswer(f"clang -dM -E failed (exit {result.returncode}): {stderr[0] if stderr else ''}")
    try:
        return result, dependency_file.read_bytes().decode("utf-8", "surrogateescape")
    except OSError as exc:
        raise NoMacroAnswer(f"clang -dM -E wrote no dependency file: {exc}") from exc


# The options that select an other output than the preprocessed text, or
# write a file next to it.  `_OUTPUT_TAKES_NEXT` go with their value.
_OUTPUT_TAKES_NEXT = frozenset({
    "-o", "--output", "-MF", "-MT", "-MQ", "-MJ", "--serialize-diagnostics",
    "-dumpdir", "--dumpdir", "-dumpbase", "--dumpbase", "-dumpbase-ext", "--dumpbase-ext", "--dump",
})
_OUTPUT_FLAGS = frozenset({
    "-c", "-S", "-E", "-fsyntax-only", "-emit-llvm", "-emit-ast",
    "-M", "-MM", "-MD", "-MMD", "-MG", "-MP",
})

# The options that load code into the compiler.  fw-context does not run
# such code from compile_commands.json (supply-chain defense-in-depth).
_PLUGIN_PREFIXES = ("-fplugin", "-fpass-plugin")
_PLUGIN_CC1_PREFIXES = ("-load", "-plugin", "-add-plugin")


def _sanitize_flags(flags: Sequence[str]) -> list[str]:
    """Return the flags of the unit for ``clang -dM -E``: each flag of *flags*, without the output options, and ``-w``.

    *flags* are the libclang arguments of the unit (``clang_args``): the
    parse of the unit reads them, and ``normalize_args`` removed the flags
    that clang refuses.  WHY each flag: a flag can change the predefined
    macros or the search of the headers (``-O2``, ``-ffreestanding``,
    ``-fno-exceptions``, ``-iprefix``, ``-Xclang -include``).  The values
    of the macros must be the values of the parse.  An earlier list of the
    flags to keep lacked such flags: the macros disagreed with the parse,
    and ``-iwithprefixbefore`` lost its directory, which clang then read as
    a second source file (each unit of the ESP32 Arduino framework).

    The output options go (``_OUTPUT_*``): the caller gives ``-dM -E`` and
    the dependency file.  ``DRIVER_TAKES_NEXT`` tells which argument is the
    value of an option: a value goes with its option, and a value that
    starts with ``-`` is no flag of its own.  ``-w``: a warning of clang
    that GCC does not give, under the ``-Werror`` of the unit, stopped the
    preprocessor, and the preprocessor gives values, not warnings.

    Raise :class:`NoMacroAnswer` for an option that loads a compiler
    plugin: fw-context does not run code from compile_commands.json.
    """
    kept: list[str] = []
    index = 0
    while index < len(flags):
        arg = flags[index]
        index += 1
        takes_next = arg in DRIVER_TAKES_NEXT and index < len(flags)
        value = flags[index] if takes_next else None
        if arg.startswith(_PLUGIN_PREFIXES) or (arg == "-Xclang" and value is not None
                                                 and value.startswith(_PLUGIN_CC1_PREFIXES)):
            raise NoMacroAnswer(f"the unit loads a compiler plugin ({arg} {value or ''}), and fw-context does not run it")
        if takes_next:
            index += 1
        if arg in _OUTPUT_TAKES_NEXT or arg in _OUTPUT_FLAGS or arg.startswith("-save-temps"):
            continue
        kept.append(arg)
        if value is not None:
            kept.append(value)
    kept.append("-w")
    return kept


#: A row of the ``macros`` table: a ``sqlite3.Row`` of a query, or a dict.
MacroRow = sqlite3.Row | Mapping[str, Any]


def _column(row: MacroRow, name: str) -> str:
    """Return the text of the column *name* of *row*, or "" when the row has no such column.

    WHY a row can lack it: an index of an earlier schema has no
    ``expanded_values`` until the next index run migrates it, and the MCP
    server reads such an index.
    """
    return str(row[name] or "") if name in row.keys() else ""


#: The state of a name at the end of a unit that reads the file of a row:
#: a definition, no definition (an ``#undef``), or no answer (the
#: preprocessor failed, or the run does not know which files the unit reads).
State = Literal["defined", "undefined", "no_answer"]


@dataclass
class MacroValuesReport:
    """What :func:`store_values_of_build` did: the units, and each unit with no answer and why."""

    units: int = 0
    rows: int = 0
    no_answer: list[tuple[str, str]] = field(default_factory=list)


def store_values_of_build(
    conn: sqlite3.Connection,
    config_hash: str,
    units: Sequence[Any],
    project_root: Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    flags_hashes: Sequence[str] | None = None,
    answer_of: Callable[[Any], UnitMacros] | None = None,
) -> MacroValuesReport:
    """Store the values of each macro row of *config_hash*: the definition of its name at the end of each unit that reads its file.

    The files that a unit reads come from the dependency file of the same
    run of the preprocessor (:func:`definitions_at_end_of_unit`).  Each row
    gets ``expanded_values``: a JSON list of the distinct states, each with
    the count of units, the most frequent first.  ``expanded_value`` holds
    the value only when each unit that reads the file has the same
    definition: the full-text index reads that column.

    A unit with no answer (:class:`NoMacroAnswer`) counts as ``no_answer``
    for each row of the files that the parse of libclang read for it: the
    headers of its entry in *manifest* (the entry of its file with its flags
    hash, *flags_hashes* in the order of *units*), and its source file.
    WHY those: the preprocessor gave no list of files, and the parse is the
    one other record of them.  Before, each row of the build got the
    ``no_answer``, thus one unit with a missing header removed the value of
    each macro.  A unit with no such entry (a new build, a failed parse)
    counts for each row of the build: nothing tells its files.  The report
    names each such unit, and the runner keeps it in the marker of the run
    (``_run_marker``): the next run asks again, and the state of the index
    names the units.

    *units* are the C and C++ units of the build (``ctx["units"]`` of the
    post-processing): an assembly unit that reads a header gives no value
    to its rows.  *answer_of* gives the answer for one unit; the default
    runs :func:`definitions_at_end_of_unit` with the flags and the
    directory of the unit.  A test gives a fake.
    """
    answer_of = answer_of or _answer_of_unit
    key_to_id = {row[1]: row[0] for row in conn.execute("SELECT id, path FROM files WHERE config_hash = ?", (config_hash,))}
    rows_of_file: dict[int, list[tuple[int, str]]] = {}
    current: dict[int, tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT id, file_id, name, expanded_value, expanded_values FROM macros WHERE config_hash = ?", (config_hash,),
    ):
        rows_of_file.setdefault(row[1], []).append((row[0], row[2]))
        current[row[0]] = (row[3], row[4])

    entries_of: dict[str, list[Mapping[str, Any]]] = {}
    for entry in (manifest or {}).get("entries") or ():
        entries_of.setdefault(entry.get("file", ""), []).append(entry)

    report = MacroValuesReport(units=len(units), rows=len(current))
    counts: dict[int, Counter[tuple[State, str, str, bool]]] = {macro_id: Counter() for macro_id in current}
    for position, unit in enumerate(units):
        key = index_key(unit.file, project_root)
        try:
            answer = answer_of(unit)
        except NoMacroAnswer as exc:
            report.no_answer.append((key, str(exc)))
            entry = _entry_of_listing(entries_of.get(key, []), None if flags_hashes is None else flags_hashes[position])
            if entry is None:
                no_answer_rows = list(counts)
            else:
                no_answer_rows = [
                    macro_id
                    for file_key in {key, *(entry.get("headers") or ())}
                    for macro_id, _name in rows_of_file.get(key_to_id.get(file_key, -1), ())
                ]
            for macro_id in no_answer_rows:
                counts[macro_id][("no_answer", "", "", False)] += 1
            continue
        # The names of the dependency file are canonical paths already
        # (definitions_at_end_of_unit), thus the key needs no second resolve.
        rows = [
            row
            for file_key in {canonical_key(path, project_root) for path in answer.files}
            for row in rows_of_file.get(key_to_id.get(file_key, -1), ())
        ]
        for macro_id, name in rows:
            found = answer.definitions.get(name)
            state: tuple[State, str, str, bool] = ("undefined", "", "", False) if found is None else (
                "defined", found.value, found.params, found.is_function_like,
            )
            counts[macro_id][state] += 1

    updates: list[tuple[str, str, int]] = []
    for macro_id, counter in counts.items():
        value, values = _column_texts(counter)
        if (value, values) != current[macro_id]:
            updates.append((value, values, macro_id))
    conn.executemany("UPDATE macros SET expanded_value = ?, expanded_values = ? WHERE id = ?", updates)
    return report


def _entry_of_listing(entries: Sequence[Mapping[str, Any]], flags_hash: str | None) -> Mapping[str, Any] | None:
    """Return the manifest entry of one listing of a file: the entry with *flags_hash*, else None.

    *flags_hash* None (no hashes given): the one entry of the file, else
    None.  WHY None and not the first entry: two entries of one file with
    other flags read other headers, and a wrong list gives wrong counts.
    """
    if flags_hash is None:
        return entries[0] if len(entries) == 1 else None
    return next((entry for entry in entries if entry.get("flags_hash") == flags_hash), None)


def _answer_of_unit(unit: Any) -> UnitMacros:
    """Run :func:`definitions_at_end_of_unit` with the flags and the directory of the compilation of *unit*.

    The directory is the ``directory`` of its entry in
    ``compile_commands.json``: a relative path in the flags is relative to
    it, as for the compiler.
    """
    return definitions_at_end_of_unit(unit.clang_args, unit.file, cwd=unit.directory)


def _column_texts(counter: Counter[tuple[State, str, str, bool]]) -> tuple[str, str]:
    """Return ``(expanded_value, expanded_values)`` of a row from the count of each state.

    No unit reads the file of the row: the two are empty, which means "no
    value known".
    """
    if not counter:
        return "", ""
    entries = []
    for (state, value, params, is_function_like), units in sorted(
        counter.items(), key=lambda item: (-item[1], item[0]),
    ):
        entry: dict[str, Any] = {"state": state, "units": units}
        if state == "defined":
            entry.update(value=value, params=params, is_function_like=is_function_like)
        entries.append(entry)
    single = entries[0] if len(entries) == 1 and entries[0]["state"] == "defined" else None
    return (single["value"] if single else ""), json.dumps(entries, ensure_ascii=False)


def expanded_fields(row: MacroRow) -> dict[str, Any]:
    """Return the fields of an answer about the macro *row* that say its value in the build.

    ``expanded_value``: each unit that reads the file of the row has one
    definition of the name, with the parameters of the row.  Else
    ``expanded_values``: each distinct state (``defined`` with its value
    and parameters, ``undefined``, ``no_answer``) with its count of units.
    No field: the index has no value for the row (no unit reads its file,
    or ``reindex_file`` parsed the file after the last index run).

    An index of an earlier row format has no ``expanded_values``: its
    ``expanded_value`` goes out as it is (the status of the index says that
    the index needs a run).
    """
    text = _column(row, "expanded_values")
    if not text:
        old = _column(row, "expanded_value")
        return {"expanded_value": old} if old else {}
    entries = json.loads(text)
    if len(entries) == 1 and entries[0]["state"] == "defined":
        entry = entries[0]
        same_shape = entry["is_function_like"] == bool(row["is_function_like"]) and entry["params"] == row["params"]
        if same_shape:
            return {"expanded_value": entry["value"]}
    return {"expanded_values": entries}


def agreed_value(rows: Iterable[MacroRow]) -> str | None:
    """Return the one object-like value that each unit of the build has for the macro *rows*, else None.

    For a caller that needs a number of the build (``_callgraph._user_irq_offset``).
    None when a unit has no definition or no answer, when two values or a
    function-like definition exist, or when no row has values: a guess
    would place an interrupt handler in a slot that no build confirmed.
    """
    seen: set[str] = set()
    for row in rows:
        text = _column(row, "expanded_values")
        for entry in json.loads(text) if text else ():
            if entry["state"] != "defined" or entry["is_function_like"]:
                return None
            seen.add(entry["value"])
    return seen.pop() if len(seen) == 1 else None
