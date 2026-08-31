"""GNU ld linker script reader — symbols, entry point, and memory map.

The linker script is the third source of firmware knowledge, after the
translation units and the assembly files.  It defines symbols that no
compiled file defines.  `_estack` names the initial stack pointer in a
Cortex-M vector table, and the index stored it as `kind="undefined"`,
because no unit of the build defined it.

The reader gets three things from the script:

* Symbol definitions — `NAME = expr;` and the `PROVIDE` forms.
* The entry point — `ENTRY(name)`.
* The memory map — the `MEMORY` command.

**The reader never invents a symbol.**  This is the rule that governs the
assembly reader too.  A name in `lookup_symbol` that is in no binary is
worse than a missing name, because a user cannot tell the two apart.  A
statement that the reader cannot parse increments a counter in `Report` and
goes to the log.

**The reader does not evaluate an expression that holds a symbol.**
`ORIGIN(RAM) + LENGTH(RAM)` stays raw text.  Constant arithmetic gets a
number as well, because a reader cannot use `((673792) - 0xe6)` and can use
`673562`.  See `evaluate_constant`.

Grammar reference — the GNU ld manual, sections "Simple Assignments",
"PROVIDE", "MEMORY", and "Linker Script Format".  Two rules come from
measurement instead, and each one says so at the code that applies it.
"""

from __future__ import annotations

import ast
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ── Data ───────────────────────────────────────────────────────────────────


@dataclass
class LinkerSymbol:
    """One symbol that the linker script defines.

    Attributes:
        name: The symbol name, as the script writes it.
        line: The 1-based line of the assignment.
        expression: The raw right side, with no evaluation.
        value: The value when the expression is constant arithmetic, else
            None.  See `evaluate_constant`.
        provide: True for a `PROVIDE` form.  The linker defines the symbol
            only when something references it, thus the symbol can be
            absent from the binary.  The index keeps the distinction,
            because "defined here" and "defined here when referenced" are
            different facts.
        hidden: True for `HIDDEN` or `PROVIDE_HIDDEN`.  The symbol exists
            but the linker does not export it.
    """

    name: str
    line: int
    expression: str
    value: int | None = None
    provide: bool = False
    hidden: bool = False


@dataclass
class MemoryRegion:
    """One region of the `MEMORY` command.

    Origin and length keep their raw text, and get a number only when the
    expression is constant arithmetic.  A Zephyr script writes
    `LENGTH = ((673792) - 0xe6)`, which mixes decimal and hexadecimal, and
    an mbed script writes `LENGTH = 0xefe00`.  Both are correct, so the
    reader reports the text it read and adds the number when it is sure.

    Attributes:
        name: The region name.
        attributes: The attribute characters in parentheses, such as
            `rx` or `rwx`.  An empty string when the script gives none.
        origin: Raw origin expression.
        length: Raw length expression.
        origin_value: Origin as a number, or None.
        length_value: Length as a number, or None.
        line: The 1-based line of the region.
    """

    name: str
    attributes: str
    origin: str
    length: str
    line: int
    origin_value: int | None = None
    length_value: int | None = None


@dataclass
class Report:
    """What the reader could not do, so that a gap is visible.

    A silent skip and a file that holds nothing look the same in the index.
    The counters separate them.

    Attributes:
        refused: Reason to count, for every statement the reader skipped.
    """

    refused: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        """Return a log line, or an empty string when nothing was refused."""
        if not self.refused:
            return ""
        return ", ".join(
            f"{count} {reason}" for reason, count in sorted(self.refused.items())
        )


@dataclass
class LinkerScript:
    """Everything the reader got from one script file."""

    path: Path
    entry: str | None = None
    entry_line: int = 0
    symbols: list[LinkerSymbol] = field(default_factory=list)
    regions: list[MemoryRegion] = field(default_factory=list)
    report: Report = field(default_factory=Report)


# ── Constant arithmetic ────────────────────────────────────────────────────

# ld accepts K, M, and G after a number.  The manual documents them for the
# length of a memory region, and scripts use them there.
#
# Case-insensitive, which the manual does not say.  Measured on
# birdie1-v2-fw-v3: `SRAM1 (xrw) : ORIGIN = 0x20000194, LENGTH = 160k - 0x194`
# — lowercase — and that script produced the binaries in its build
# directory, thus ld accepted it.  With an uppercase-only pattern the
# expression stayed text and the region reported no length at all.
_SUFFIX_RE = re.compile(r"\b(\d+)\s*([KMGkmg])\b")
_SUFFIX_SCALE = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}

# The operators of an address expression.  ast gives the precedence, thus
# the reader needs no parser of its own.
_BINARY_OPS: dict[type, str] = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Mod: "%",
    ast.LShift: "<<", ast.RShift: ">>",
    ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
}
_UNARY_OPS = (ast.UAdd, ast.USub, ast.Invert)


def evaluate_constant(expression: str) -> int | None:
    """Return the value of *expression*, or None when it is not constant.

    Constant means integers and arithmetic, and nothing else.  A name, a
    call such as `ORIGIN(RAM)`, or the location counter `.` makes the
    result None, and the caller keeps the raw text.

    The function never calls `eval`.  It parses the expression with `ast`
    and walks the tree, thus an unexpected construct returns None instead
    of running.

    ld divides integers.  Python `/` gives a float, so the walk maps
    division to floor division.  A division by zero returns None.
    """
    prepared = _SUFFIX_RE.sub(
        lambda m: str(int(m.group(1)) * _SUFFIX_SCALE[m.group(2).upper()]),
        expression,
    )
    try:
        tree = ast.parse(prepared.strip(), mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    try:
        return _walk_constant(tree.body)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError,
            RecursionError):
        return None


def _walk_constant(node: ast.expr) -> int | None:
    """Return the value of one node of a constant expression, or None."""
    if isinstance(node, ast.Constant):
        # A bool is an int in Python.  Reject it, because ld has no bool
        # literal and a True here would come from a construct the reader
        # does not model.
        if isinstance(node.value, bool) or not isinstance(node.value, int):
            return None
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY_OPS):
        operand = _walk_constant(node.operand)
        if operand is None:
            return None
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Invert):
            return ~operand
        return operand
    if isinstance(node, ast.BinOp):
        left = _walk_constant(node.left)
        right = _walk_constant(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Div | ast.FloorDiv):
            if right == 0:
                return None
            return left // right
        symbol = _BINARY_OPS.get(type(node.op))
        if symbol is None:
            return None
        if symbol in {"<<", ">>"} and (right < 0 or right > 4096):
            # A huge shift is not an address expression, and it can make
            # Python build a number that fills the memory.
            return None
        return _apply_binary(symbol, left, right)
    return None


def _apply_binary(symbol: str, left: int, right: int) -> int | None:
    """Apply one binary operator to two integers."""
    if symbol == "+":
        return left + right
    if symbol == "-":
        return left - right
    if symbol == "*":
        return left * right
    if symbol == "%":
        return left % right if right != 0 else None
    if symbol == "<<":
        return left << right
    if symbol == ">>":
        return left >> right
    if symbol == "&":
        return left & right
    if symbol == "|":
        return left | right
    if symbol == "^":
        return left ^ right
    return None


# ── Lexical layer ──────────────────────────────────────────────────────────


def strip_comments(text: str) -> str:
    """Return *text* with every comment replaced by spaces.

    The result keeps the length and every newline of the input, thus an
    offset in the result names the same line in the file.

    Two comment forms:

    * `/* ... */`, which the manual documents — "You may include comments
      in linker scripts just as in C, delimited by '/*' and '*/'".
    * `# ...` to the end of the line, which the manual does not document.
      Zephyr writes it: `build/.../app/zephyr/linker.cmd` holds nine such
      lines outside any `/* */`, and the link produced `zephyr.elf`, thus
      ld accepts the form.  A reader that treats `#` as code would read
      prose as a statement.

    A quoted string holds no comment.  `ASSERT(x, "a # b")` keeps its
    message.
    """
    out = list(text)
    index = 0
    end_of_text = len(text)
    while index < end_of_text:
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "/" and text.startswith("/*", index):
            close = text.find("*/", index + 2)
            stop = end_of_text if close < 0 else close + 2
            _blank(out, text, index, stop)
            index = stop
            continue
        if char == "#":
            newline = text.find("\n", index)
            stop = end_of_text if newline < 0 else newline
            _blank(out, text, index, stop)
            index = stop
            continue
        index += 1
    return "".join(out)


def _skip_string(text: str, start: int) -> int:
    """Return the offset after the string that starts at *start*.

    The manual says that a name goes in double quotes when it holds a
    separator character, and that "There is no way to use a double quote
    character in a file name".  Thus the string has no escape and it ends
    at the next quote.  An unterminated string ends at the line, so that a
    single stray quote cannot swallow the rest of the file.
    """
    index = start + 1
    while index < len(text) and text[index] not in {'"', "\n"}:
        index += 1
    return index + 1 if index < len(text) and text[index] == '"' else index


def _blank(out: list[str], text: str, start: int, stop: int) -> None:
    """Replace the characters of a comment with spaces, and keep newlines."""
    for offset in range(start, stop):
        if text[offset] != "\n":
            out[offset] = " "


def find_memory_blocks(text: str) -> list[tuple[int, int]]:
    """Return the offsets of the body of every `MEMORY` block.

    Each pair is the offset after the opening brace and the offset of the
    matching closing brace.  The caller uses the pairs twice: to read the
    regions, and to keep the assignment scan out of them, because
    `ORIGIN = 0x100` inside a region is not a symbol.
    """
    blocks: list[tuple[int, int]] = []
    for match in re.finditer(r"\bMEMORY\b\s*\{", text):
        start = match.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            char = text[index]
            if char == '"':
                index = _skip_string(text, index)
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if not depth:
                    break
            index += 1
        if depth:
            # An unbalanced block: report the rest of the file so that the
            # regions are still read, and so that no assignment inside it
            # becomes a symbol.
            blocks.append((start, len(text)))
        else:
            blocks.append((start, index))
    return blocks


# ── Statements ─────────────────────────────────────────────────────────────

# A statement ends at one of these.  The manual requires the semicolon
# after an assignment — "The semicolon after expression is required" — and
# a brace ends an output section description, which carries no semicolon.
_TERMINATORS = frozenset({";", "{", "}"})


def statements(
    text: str, skip: list[tuple[int, int]] | None = None
) -> Iterator[tuple[str, int]]:
    """Yield each statement of *text* with the line where its text starts.

    *skip* holds offset ranges the scan steps over, which is how the caller
    keeps the `MEMORY` regions out of the assignment scan.

    A quoted string never terminates a statement, thus
    `ASSERT(x, "a; b")` stays one statement.
    """
    ranges = sorted(skip or [])
    index = 0
    line = 1
    start_line = 1
    buffer: list[str] = []
    end_of_text = len(text)
    while index < end_of_text:
        for begin, stop in ranges:
            if begin <= index < stop:
                line += text.count("\n", index, stop)
                index = stop
                buffer.clear()
                start_line = line
                break
        if index >= end_of_text:
            break
        char = text[index]
        if char == '"':
            after = _skip_string(text, index)
            buffer.append(text[index:after])
            line += text.count("\n", index, after)
            index = after
            continue
        if char in _TERMINATORS:
            statement = "".join(buffer).strip()
            if statement:
                yield statement, start_line
            buffer.clear()
            index += 1
            start_line = line
            continue
        if char == "\n":
            line += 1
            if not "".join(buffer).strip():
                # Nothing yet, thus the statement starts on the next line.
                #
                # This is the ONLY line correction the scanner makes.  The
                # caller counts the newlines before the name to place a
                # multi-line statement, and a second correction here
                # counted the leading blank lines twice — three blank lines
                # before `a = 1;` reported line 7 instead of line 4.
                start_line = line
        buffer.append(char)
        index += 1
    statement = "".join(buffer).strip()
    if statement:
        yield statement, start_line


# ── Assignments ────────────────────────────────────────────────────────────

# The operators come from the manual, section "Simple Assignments".  `=`
# carries a negative lookahead so that `==` inside an `ASSERT` is not an
# assignment.  Measured on a Zephyr script:
# `ASSERT(__nrf_kmu_reserved_push_area == (536870912), "...")`.
_ASSIGN_OPS = r"<<=|>>=|\+=|-=|\*=|/=|&=|\|=|=(?!=)"

# A name of the script.  `.` is absent on purpose: the manual says that
# "The special symbol name '.' indicates the location counter", which is a
# position and not a symbol of the program.
_NAME = r"[A-Za-z_$][\w.$]*"

_PLAIN_ASSIGN = re.compile(
    rf"^\s*(?P<name>{_NAME})\s*(?:{_ASSIGN_OPS})\s*(?P<expr>.+)$", re.DOTALL
)
_WRAPPED_ASSIGN = re.compile(
    rf"^\s*(?P<keyword>PROVIDE_HIDDEN|PROVIDE|HIDDEN)\s*\(\s*"
    rf"(?P<name>{_NAME})\s*(?:{_ASSIGN_OPS})\s*(?P<expr>.+?)\s*\)\s*$",
    re.DOTALL,
)
_ENTRY = re.compile(rf'\bENTRY\s*\(\s*"?(?P<name>{_NAME})"?\s*\)')


@dataclass(frozen=True)
class Assignment:
    """One symbol assignment that the grammar matched.

    Attributes:
        name: The symbol name.
        expression: The raw right side.
        provide: True for a `PROVIDE` form.
        hidden: True for `HIDDEN` or `PROVIDE_HIDDEN`.
        offset: Offset of the name inside the statement, so that the caller
            can count the newlines before it and get the line of the file.
    """

    name: str
    expression: str
    provide: bool
    hidden: bool
    offset: int


def parse_assignment(statement: str) -> Assignment | None:
    """Return the assignment in *statement*, or None.

    The match is anchored, thus the assignment must start the statement.
    Every other command of the script lands here too — `ENTRY`, `ASSERT`,
    `KEEP`, an output section header — and each one must return None.
    """
    wrapped = _WRAPPED_ASSIGN.match(statement)
    if wrapped is not None:
        keyword = wrapped.group("keyword")
        return Assignment(
            name=wrapped.group("name"),
            expression=wrapped.group("expr").strip(),
            provide=keyword.startswith("PROVIDE"),
            hidden=keyword.endswith("HIDDEN"),
            offset=wrapped.start("name"),
        )
    plain = _PLAIN_ASSIGN.match(statement)
    if plain is None:
        return None
    return Assignment(
        name=plain.group("name"),
        expression=plain.group("expr").strip(),
        provide=False,
        hidden=False,
        offset=plain.start("name"),
    )


def find_assignment(statement: str) -> Assignment | None:
    """Find an assignment in *statement*, past a placement suffix.

    An output section description ends with a placement suffix that carries
    no semicolon, such as `} > RAM AT > FLASH`.  The statement after the
    closing brace therefore starts with that suffix, and an anchored match
    loses the assignment behind it.  Measured on zbox: 18 names went
    missing, and one of them is `__StackTop` — the single name of that
    build that the index held as `kind="undefined"`.

    The function drops one leading line at a time and matches again.  It
    can find more assignments and it cannot invent one, because every
    candidate still passes the full grammar of `parse_assignment`.

    The returned `offset` is relative to *statement*, thus the caller
    counts newlines in the original text and needs no correction of its
    own.
    """
    consumed = 0
    remaining = statement
    while True:
        found = parse_assignment(remaining)
        if found is not None:
            if not consumed:
                return found
            # Re-base the offset onto the statement the caller holds.
            return Assignment(
                name=found.name,
                expression=found.expression,
                provide=found.provide,
                hidden=found.hidden,
                offset=consumed + found.offset,
            )
        newline = remaining.find("\n")
        if newline < 0:
            return None
        consumed += newline + 1
        remaining = remaining[newline + 1:]


# ── Memory regions ─────────────────────────────────────────────────────────

# The manual gives the grammar as `name [(attr)] : ORIGIN = origin,
# LENGTH = len`, and says that "The keyword ORIGIN may be abbreviated to
# org or o" and "The keyword LENGTH may be abbreviated to len or l".
#
# The match is case-insensitive.  A script that writes `Origin` is not
# valid ld, but such a script never linked, thus it is not in a build
# output and the wider match costs nothing.
#
# The origin expression is lazy and the pattern anchors on the LENGTH
# keyword, not on the first comma.  `ORIGIN = MAX(a,b), LENGTH = c` holds
# a comma inside the origin.
_REGION = re.compile(
    rf"^\s*(?P<name>{_NAME})\s*"
    r"(?:\(\s*(?P<attributes>[^)]*?)\s*\))?\s*:\s*"
    r"(?:ORIGIN|ORG|O)\s*=\s*(?P<origin>.+?)\s*,\s*"
    r"(?:LENGTH|LEN|L)\s*=\s*(?P<length>.+?)\s*$",
    re.IGNORECASE,
)


def parse_regions(body: str, first_line: int, report: Report) -> list[MemoryRegion]:
    """Read the regions of one `MEMORY` block body.

    *first_line* is the line of the first character of *body*, so that each
    region gets the line of the file and not of the block.

    A line that holds a colon and does not match the grammar is counted as
    `unparsed memory region`.  Refusing and counting is the rule of this
    module: a region the reader invents would put a wrong address in front
    of a user.
    """
    regions: list[MemoryRegion] = []
    for offset, raw in enumerate(body.split("\n")):
        line = first_line + offset
        match = _REGION.match(raw)
        if match is None:
            if ":" in raw:
                report.refused["unparsed memory region"] += 1
                log.debug("linker script line %d: cannot read region %r", line, raw)
            continue
        origin = match.group("origin").strip()
        length = match.group("length").strip()
        regions.append(MemoryRegion(
            name=match.group("name"),
            attributes=(match.group("attributes") or "").strip(),
            origin=origin,
            length=length,
            line=line,
            origin_value=evaluate_constant(origin),
            length_value=evaluate_constant(length),
        ))
    return regions


# ── Entry point ────────────────────────────────────────────────────────────


def parse(path: Path) -> LinkerScript | None:
    """Read one linker script.  Returns None when the file is unreadable.

    The order is fixed.  The reader removes the comments, finds the
    `MEMORY` blocks, reads the regions, and then scans the assignments
    outside those blocks.  Without the last part `ORIGIN = 0x100` would
    become a symbol named `ORIGIN`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.debug("cannot read linker script %s", path, exc_info=True)
        return None

    script = LinkerScript(path=path)
    clean = strip_comments(text)

    blocks = find_memory_blocks(clean)
    for start, stop in blocks:
        first_line = clean.count("\n", 0, start) + 1
        script.regions.extend(
            parse_regions(clean[start:stop], first_line, script.report)
        )

    entry = _ENTRY.search(clean)
    if entry is not None:
        script.entry = entry.group("name")
        script.entry_line = clean.count("\n", 0, entry.start()) + 1

    seen: set[str] = set()
    for statement, line in statements(clean, blocks):
        found = find_assignment(statement)
        if found is None:
            continue
        if found.name in seen:
            # A script can assign one name more than once, and the last
            # assignment wins at link time.  The index keeps the first,
            # because that is where a reader looks for the definition, and
            # counts the rest so that the choice is visible.
            script.report.refused["repeated assignment"] += 1
            continue
        seen.add(found.name)
        # `statements` strips the text it yields, thus *line* is the line of
        # character 0, and the newlines before the name give the rest.
        script.symbols.append(LinkerSymbol(
            name=found.name,
            line=line + statement.count("\n", 0, found.offset),
            expression=found.expression,
            value=evaluate_constant(found.expression),
            provide=found.provide,
            hidden=found.hidden,
        ))
    return script


# ── Storage ────────────────────────────────────────────────────────────────

# The USR namespace of this pass.  It exists so that `_clear_previous_pass`
# can delete exactly what the pass wrote: a re-index cannot correct a row
# otherwise, because `insert_symbols_batch` merges on
# `ON CONFLICT(config_hash, usr)` behind a
# `WHERE excluded.is_definition = 1 AND symbols.is_definition = 0` guard, and
# every symbol here is a definition.  The same problem cost the assembly
# pass an is_project fix that looked like it had no effect at all.
_LD_USR_PREFIX = "ld:"

# A linker script symbol is an address, thus a global variable.  The manual
# says an assignment will "define the symbol and place it into the symbol
# table with a global scope", and `varglobal` is the kind the C path uses
# for a global data symbol.
_LD_KIND = "varglobal"


@dataclass
class LinkerResult:
    """What the pass stored, and what it could not.

    Attributes:
        files: How many scripts the pass read.
        symbols: How many symbols it stored.
        skipped_defined: Names it did not store because the index already
            held a definition — a C or an assembly definition is the real
            one, and this pass must not add a second.
        entry: The entry point of the FIRST script that named one, or "".
            First and not last, so the script of the real link wins — the
            Zephyr backend puts it in front of the pre-pass script.
        regions: How many memory regions the pass stored.
        paths: The database paths of the scripts, for the coverage purge.
        report: What the reader refused.
    """

    files: int = 0
    symbols: int = 0
    skipped_defined: int = 0
    entry: str = ""
    regions: int = 0
    paths: set[str] = field(default_factory=set)
    report: Report = field(default_factory=Report)


def _clear_previous_pass(conn, config_hash: str) -> None:
    """Delete every row a previous run of this pass wrote.

    Keyed on the `ld:` USR namespace and not on the file id.  A linker
    script is its own file and shares none with a translation unit, so the
    file id would work here — but the namespace states the intent, and it
    stays correct if a build ever passes a script that a unit also includes.

    The pass has no incremental path: `runner.run` hands it every script of
    the build on every run, thus everything deleted here is about to be
    written again.
    """
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM symbols WHERE config_hash=? AND usr LIKE 'ld:%'",
            (config_hash,),
        )
    ]
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        # Ordered like asm._clear_previous_pass: what selects by symbol_id
        # has to go before the symbols do.
        conn.execute(f"DELETE FROM embeddings WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        conn.execute(f"DELETE FROM llm_analysis WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        try:
            conn.execute(f"DELETE FROM vec_symbols WHERE symbol_id IN ({marks})", chunk)  # noqa: S608
        except sqlite3.OperationalError:
            pass  # sqlite-vec not loaded — the virtual table does not exist
    conn.execute(
        "DELETE FROM symbols WHERE config_hash=? AND usr LIKE 'ld:%'",
        (config_hash,),
    )


def _already_defined(conn, config_hash: str, names: list[str]) -> set[str]:
    """Return the names of *names* that the index already defines.

    A definition from C or from assembly is the real one.  This pass fills
    the gap the compiled files leave — the boundary symbols of a memory
    layout — and must not put a second definition of one name in front of a
    user.

    Asked in one query per chunk rather than one per name: a Zephyr script
    holds about 350 symbols, and 350 round trips per image would show.
    """
    found: set[str] = set()
    for start in range(0, len(names), 500):
        chunk = names[start:start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT DISTINCT name FROM symbols "  # noqa: S608
            f"WHERE config_hash=? AND is_definition=1 AND name IN ({marks})",
            (config_hash, *chunk),
        )
        found.update(row[0] for row in rows)
    return found


def store_scripts(
    conn,
    config_hash: str,
    paths: list[Path],
    project_root: Path,
    vendor_patterns: list[str] | None = None,
    project_patterns: list[str] | None = None,
) -> LinkerResult:
    """Read every script of the build and store what it defines.

    Call this AFTER the C and C++ units and BEFORE the assembly pass:

    * After C, so `_already_defined` can see a definition the compiled code
      makes and leave that name alone.
    * Before assembly, so `asm._declare_referenced_only` finds the name
      already in the index and adds no `kind="undefined"` row for it.  That
      is what removes the placeholder for slot 0 of a vector table:
      measured before this, FM held `_estack` and zbox held `__StackTop`
      with that kind, one per project.

    The first script that names a symbol wins.  Zephyr passes two scripts
    with the same content, and its backend puts the final one first, so the
    line a user follows is the line of the real link.
    """
    result = LinkerResult()
    if not paths:
        return result

    from fw_context_mcp.indexer.db import (
        insert_symbols_batch,
        replace_memory_regions,
        set_entry_point,
        upsert_file,
    )
    from fw_context_mcp.indexer.ops import _normalize_file_path

    vendor = list(vendor_patterns or [])
    project = list(project_patterns or [])

    _clear_previous_pass(conn, config_hash)

    stored_names: set[str] = set()
    regions: list[dict] = []
    for path in paths:
        script = parse(path)
        if script is None:
            continue
        result.files += 1
        result.report.refused.update(script.report.refused)
        # The FIRST script that names an entry point wins, the same rule the
        # symbols follow.  Zephyr passes the script of the real link first.
        if script.entry and not result.entry:
            result.entry = script.entry

        db_path = _normalize_file_path(str(path), project_root)
        result.paths.add(db_path)
        # A linker script is not C.  The column takes 'c' or 'cpp', and the
        # assembly pass registers a .S file the same way — a third value
        # would reach every filter that tests for those two.
        file_id = upsert_file(conn, config_hash, db_path, "c")
        is_project = _is_project_file(str(path), project_root, vendor, project)

        regions.extend(
            {
                "name": region.name,
                "attributes": region.attributes,
                "origin": region.origin,
                "length": region.length,
                "origin_value": region.origin_value,
                "length_value": region.length_value,
                "file_path": db_path,
                "line": region.line,
            }
            for region in script.regions
        )

        fresh = [s for s in script.symbols if s.name not in stored_names]
        defined = _already_defined(conn, config_hash, [s.name for s in fresh])
        rows = []
        for symbol in fresh:
            if symbol.name in defined:
                result.skipped_defined += 1
                continue
            stored_names.add(symbol.name)
            rows.append((
                config_hash, file_id, db_path, symbol.name,
                f"{_LD_USR_PREFIX}{db_path}@{symbol.name}",
                symbol.name, symbol.name, _LD_KIND,
                symbol.line, 1, symbol.line, 1,
                # The expression goes in `signature`, thus `get_symbol_context`
                # shows WHAT the symbol is: `ORIGIN(RAM) + LENGTH(RAM)` says
                # more about `_estack` than any address could.
                symbol.expression, "", None, 0, 0, "", 0, "",
                is_project, 0.0, "",
                # PROVIDE means "define this only if something references it
                # and nothing else defines it", which is what weak means to
                # the linker.  `_resolve_handler` ranks weak below strong,
                # thus a real definition of the same name still wins.
                int(symbol.provide),
            ))
        if rows:
            insert_symbols_batch(conn, rows)
            result.symbols += len(rows)

    # The map and the entry point belong to the build, thus they are written
    # once for every script rather than per file.  Both are written even when
    # empty: a build whose script stopped declaring a region must not keep
    # the old address.
    result.regions = replace_memory_regions(conn, config_hash, regions)
    set_entry_point(conn, config_hash, result.entry)
    return result


def _is_project_file(
    path: str,
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
) -> int:
    """Say whether *path* is code of the team or of the SDK.

    The same rule the C and the assembly paths use.  A script from a
    framework package lives outside the project entirely — the one FM uses
    is under ~/.platformio/packages — and calling it project code would put
    its symbols into `find_dead_code(project_only=True)`.
    """
    from fw_context_mcp.indexer.sdk_detect import _path_matches

    resolved = Path(path).resolve()
    try:
        relative = str(resolved.relative_to(project_root.resolve()))
    except ValueError:
        # Outside the project: vendor unless a project pattern claims it.
        return 1 if any(
            _path_matches(str(resolved), p) for p in project_patterns
        ) else 0
    if any(_path_matches(relative, p) for p in project_patterns):
        return 1
    return 0 if any(_path_matches(relative, p) for p in vendor_patterns) else 1
