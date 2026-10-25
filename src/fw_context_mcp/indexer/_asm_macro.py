"""Expand GNU as macros in a stream of assembly statements.

`.macro` belongs to the ASSEMBLER, not to the C preprocessor, so
`clang -E` leaves it untouched and everything it defines is invisible to
a reader that stops at the preprocessor.  Measured on the Mbed project, that
is 43 of 54 vector slots: its startup writes

    .macro  IRQ handler
    .weak   \\handler
    .set    \\handler, Default_Handler
    .endm

    IRQ  POWER_CLOCK_IRQHandler

and none of those handler names ever becomes a symbol.

Written from the manual (`as.info`, nodes `Macro`, `Exitm`, `Purgem`),
not from the shapes that happen to appear in the SDKs on one machine —
platforms differ and the manual does not.

THE RULE THAT DECIDES EVERY OPEN QUESTION: never invent a symbol.  A
construct this cannot expand exactly is refused, counted and skipped.  A
missing symbol means something is not found; an invented one puts a name
in `lookup_symbol` that is in no binary, and no linker, debugger or
reader will ever find it.  Refusal also keeps a narrow test sample
honest: an unsupported construct in someone else's project degrades to
the old behaviour, never to a wrong answer.

What cannot be done exactly, and is therefore refused:

* a body holding `.if` / `.ifdef` / `.ifc` and friends.  The manual's own
  `sum` example recurses and is terminated only by `.if \\to-\\from`, so
  expanding it needs the values of the arguments at each level.
* an expansion that exceeds the depth or statement budget below, which
  is what an unguarded recursion looks like from here.

A conditional OUTSIDE a macro body is evaluated (``_asm_expr``): the
assembler takes one branch, and both branches in the index put dead code
there.  A condition that has no exact value is refused like a macro.

A refusal leaves a ``BARRIER`` statement in the stream.  The statements
that a refused construct holds are unknown, and they can hold a label, a
section change or a call.  A reader that tracks state across statements
(which function owns an instruction, which register holds a symbol, the
position in a vector table) must not carry that state across a barrier.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass, field

from ._asm_expr import evaluate

log = logging.getLogger(__name__)

# The statement text of a barrier: a place where unknown statements were
# skipped.  No assembly statement can hold a NUL character, thus no
# pattern of a reader matches it by accident.
BARRIER = "\x00barrier"
# The prefix of a statement whose text may be a comment: the comment
# character of the target is not known.  The text after the prefix is
# what the line holds there.  The expander reads it like the body of a
# refused construct and gives a BARRIER in its place.
UNSURE = "\x00unsure:"

# A macro may invoke a macro.  Twenty levels is far past anything a
# startup file writes and still stops an unguarded recursion long before
# the interpreter's own limit.
MAX_DEPTH = 20
# Total statements one unit may produce through expansion.  A guard for
# the case the depth limit cannot see: many shallow expansions, each
# cheap, that together would take an index run out of its timeout.
MAX_STATEMENTS = 200_000

_DEFINE = re.compile(r"^\s*\.macro\s+([A-Za-z_.$][\w.$]*)\s*(.*)$", re.IGNORECASE)
_END = re.compile(r"^\s*\.endm\b", re.IGNORECASE)
_EXITM = re.compile(r"^\s*\.exitm\b", re.IGNORECASE)
_PURGEM = re.compile(r"^\s*\.purgem\s+([A-Za-z_.$][\w.$]*)", re.IGNORECASE)
_INVOKE = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*(.*)$")

# Alternate macro mode changes the rules this file implements: arguments
# may be delimited by `'…'` or `<…>`, `!` escapes any single character,
# `%expr` substitutes the value of an expression, `&` separates like
# `\()`, keyword arguments stop working, and `LOCAL` mints a unique name
# per expansion.
#
# Applying the ordinary rules inside it does not merely miss symbols, it
# INVENTS them: measured, a `LOCAL scratch` body made the expander report
# `scratch`, which is in no object file because the assembler renames it
# on every expansion, while the real symbol behind `<bracket_delimited>`
# went missing.  So an invocation in this mode is refused instead.
_ALTMACRO = re.compile(r"^\s*\.altmacro\b", re.IGNORECASE)
_NOALTMACRO = re.compile(r"^\s*\.noaltmacro\b", re.IGNORECASE)

# The repetition directives.  All three end at `.endr`, and all three are
# bounded: `.rept 3` repeats three times whatever the symbols hold.  The
# count of a `.rept` is an absolute expression; when it has no exact value
# (a symbol with no known value), the repetition is refused.
_REPEAT_OPEN = re.compile(r"^\s*\.(rept|irp|irpc)\b(.*)$", re.IGNORECASE)
_REPEAT_CLOSE = re.compile(r"^\s*\.endr\b", re.IGNORECASE)

# One statement with the place it came from.  Repetition bodies carry
# these rather than bare text, because the body sits in the file and its
# own line is the honest answer for a symbol defined there.
Located = tuple[str, int, str]

# Every conditional the manual lists.  A macro body holding one is
# refused: which branch it takes depends on the arguments of each level.
_CONDITIONAL = re.compile(
    r"^\s*\.(if|ifdef|ifndef|ifnotdef|ifb|ifnb|ifc|ifnc|ifeq|ifne|ifgt|ifge"
    r"|iflt|ifle|ifeqs|ifnes|else|elseif|endif)\b",
    re.IGNORECASE,
)
# The directives that open a conditional, with their argument.
_IF_OPEN = re.compile(
    r"^\s*\.(if|ifdef|ifndef|ifnotdef|ifb|ifnb|ifc|ifnc|ifeq|ifne|ifgt|ifge"
    r"|iflt|ifle|ifeqs|ifnes)\b(.*)$",
    re.IGNORECASE,
)
_IF_ELSEIF = re.compile(r"^\s*\.elseif\b(.*)$", re.IGNORECASE)
# The assembler accepts nothing after `.else` and `.endif` but a comment,
# thus the word alone decides: `.else @ note` is `.else`.
_IF_ELSE = re.compile(r"^\s*\.else\b", re.IGNORECASE)
_INCLUDE = re.compile(r"^\s*\.include\b", re.IGNORECASE)
_IF_END = re.compile(r"^\s*\.endif\b", re.IGNORECASE)

# `name:` at the start of a statement, and the statement after it.  The
# assembler reads `KEXC_areg: .if 4` as a label and a conditional, and a
# macro expansion gives `stacking_is_interrupt: addi sp, sp, -32`.  `(?!:)`
# keeps `::` out.  A numeric local label (`1:`) is a label too.
_LABEL_HEAD = re.compile(r"^(\s*(?:[A-Za-z_.$][\w.$]*|\d+)\s*:)(?!:)(.*)$")
# What defines a symbol, for `.ifdef` and for the values of `.if`.
_LABEL_ONLY = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*:\s*$")
_ASSIGN = re.compile(
    r"^\s*(?:\.(set|equ|equiv|eqv)\s+([A-Za-z_.$][\w.$]*)\s*,"
    r"|([A-Za-z_.$][\w.$]*)\s*=(?!=))\s*(.*)$",
    re.IGNORECASE,
)
_THUMB_SET = re.compile(r"^\s*\.thumb_set\s+([A-Za-z_.$][\w.$]*)\s*,", re.IGNORECASE)
# Directives that make a symbol whose `.ifdef` answer this does not know:
# a common symbol is not a definition in the usual sense, and no build
# measured uses one in a conditional.
_COMMON = re.compile(r"^\s*\.(?:comm|lcomm)\s+([A-Za-z_.$][\w.$]*)", re.IGNORECASE)
_PLAIN_NAME = re.compile(r"^[A-Za-z_.$][\w.$]*$")
# A label whose name a macro argument supplies, such as `\who:` or
# `\name\()_end:`.  A label cannot open or close a conditional.
_SUBSTITUTED_LABEL = re.compile(r"^\s*[\\\w.$()@]+\s*:\s*$")
_COMMENT_START = re.compile(r"[@#]")
_QUOTED_PAIR = re.compile(r'^"([^"\\]*)"\s*,\s*"([^"\\]*)"$')

# The states of one open conditional.
_TAKING = "taking"    # the current branch is assembled
_WAITING = "waiting"  # no branch taken yet; the current one is not
_DONE = "done"        # an earlier branch was taken
_UNKNOWN = "unknown"  # a condition had no exact value; skip to `.endif`

# `\name`, `\()` and `\@` — the three substitutions the manual defines
# for a macro body.  One pattern rather than one pass per parameter, so
# that `\p` cannot match inside `\p1`: the manual's own `plus1 p, p1`
# example is that trap.
_SUBSTITUTION = re.compile(r"\\(\(\)|@|[A-Za-z_][\w]*)")


@dataclass(frozen=True)
class Parameter:
    """One formal parameter of a macro.

    Attributes:
        name: The identifier a body writes as ``\\name``.
        default: Value used when the call supplies none, from ``name=X``.
        required: ``name:req`` — the call must supply a non-blank value.
        vararg: ``name:vararg`` — takes every remaining argument.
    """

    name: str
    default: str | None = None
    required: bool = False
    vararg: bool = False


@dataclass
class Macro:
    """A definition, and whether it may be expanded at all.

    Attributes:
        name: The macro name.
        parameters: Its formal parameters, in order.
        body: The statements between ``.macro`` and ``.endm``, verbatim.
        refuse_reason: Non-empty when the body holds something this
            cannot expand exactly.  The definition is kept all the same,
            so that an invocation can be counted and reported rather
            than silently read as ordinary assembly.
    """

    name: str
    parameters: list[Parameter]
    body: list[str]
    refuse_reason: str = ""


@dataclass
class Report:
    """What one unit's expansion did, for the log line and the tests."""

    defined: int = 0
    expanded: int = 0
    repeated: int = 0
    conditionals: int = 0
    refused: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        if not (self.defined or self.repeated or self.conditionals or self.refused):
            return ""
        parts = []
        if self.defined:
            parts.append(f"{self.defined} macro(s) defined")
            parts.append(f"{self.expanded} expanded")
        if self.repeated:
            parts.append(f"{self.repeated} repetition(s) expanded")
        if self.conditionals:
            parts.append(f"{self.conditionals} conditional(s) evaluated")
        for reason, count in sorted(self.refused.items()):
            parts.append(f"{count} refused ({reason})")
        return ", ".join(parts)


def parse_parameters(text: str) -> list[Parameter]:
    """Read the formal parameter list of a ``.macro`` line.

    The manual allows commas or blanks between parameters, a default
    after ``=``, and the qualifiers ``:req`` and ``:vararg``.
    """
    parameters: list[Parameter] = []
    for raw in re.split(r"[,\s]+", text.strip()):
        if not raw:
            continue
        name, _, default = raw.partition("=")
        required = vararg = False
        if name.lower().endswith(":req"):
            name, required = name[:-4], True
        elif name.lower().endswith(":vararg"):
            name, vararg = name[:-7], True
        if not name:
            continue
        parameters.append(
            Parameter(name, default if default or "=" in raw else None,
                      required, vararg)
        )
    return parameters


def split_arguments(text: str) -> list[str]:
    """Split an invocation's argument text the way the manual describes.

    Arguments are separated by commas or blanks.  One containing either
    is enclosed in ``()``, ``[]`` or ``"..."``, and **only the double
    quotes are stripped** — parentheses and brackets stay part of the
    value.  Inside a quoted argument two adjacent quotes stand for one,
    and a backslash escapes the next character without being kept.

    An empty argument is real: ``reserve_str ,B`` gives the first
    parameter its default, which is the manual's own example.
    """
    text = text.strip()
    if not text:
        return []

    arguments: list[str] = []
    index = 0
    length = len(text)

    def skip_blanks(i: int) -> int:
        while i < length and text[i].isspace():
            i += 1
        return i

    index = skip_blanks(index)
    while index < length:
        if text[index] == ",":
            arguments.append("")
            index = skip_blanks(index + 1)
            continue
        collected: list[str] = []
        depth = 0
        while index < length:
            char = text[index]
            if char == '"':
                index += 1
                while index < length:
                    if text[index] == "\\" and index + 1 < length:
                        collected.append(text[index + 1])
                        index += 2
                        continue
                    if text[index] == '"':
                        if index + 1 < length and text[index + 1] == '"':
                            collected.append('"')
                            index += 2
                            continue
                        index += 1
                        break
                    collected.append(text[index])
                    index += 1
                continue
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif depth == 0 and (char == "," or char.isspace()):
                break
            collected.append(char)
            index += 1
        arguments.append("".join(collected))
        index = skip_blanks(index)
        if index < length and text[index] == ",":
            index = skip_blanks(index + 1)
            if index >= length:
                arguments.append("")
    return arguments


def bind(macro: Macro, arguments: list[str]) -> dict[str, str] | None:
    """Match an invocation's arguments to the formal parameters.

    Returns the binding, or None when the call cannot be satisfied — a
    missing ``:req`` value, or a keyword naming no parameter.  None means
    refuse: a body expanded from a wrong binding defines wrong names.

    Keyword arguments may come in any order and may leave earlier values
    out, which is the manual's ``sum to=6`` case.
    """
    values: dict[str, str] = {
        p.name: (p.default if p.default is not None else "")
        for p in macro.parameters
    }
    names = {p.name for p in macro.parameters}
    next_positional = 0
    for position, argument in enumerate(arguments):
        keyword, sign, value = argument.partition("=")
        if sign and keyword.strip() in names:
            values[keyword.strip()] = value
            continue
        if next_positional >= len(macro.parameters):
            return None
        parameter = macro.parameters[next_positional]
        if parameter.vararg:
            # Everything left belongs to it, commas included.  Sliced by
            # POSITION, not by searching for the value: `m a, a, b` has
            # the same text twice, and searching would take the slice
            # from the first one and repeat an argument.
            values[parameter.name] = ",".join(arguments[position:])
            break
        if argument:
            values[parameter.name] = argument
        next_positional += 1

    for parameter in macro.parameters:
        if parameter.required and not values.get(parameter.name):
            return None
    return values


def substitute(statement: str, values: dict[str, str], counter: int) -> str:
    """Replace ``\\name``, ``\\()`` and ``\\@`` in one body statement.

    An unknown ``\\name`` is left exactly as it stands.  Replacing it
    with nothing would be a guess, and a guess here writes a symbol name.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "()":
            return ""
        if token == "@":
            return str(counter)
        if token in values:
            return values[token]
        return match.group(0)

    return _SUBSTITUTION.sub(replace, statement)


@dataclass
class _Repetition:
    """A `.rept`, `.irp` or `.irpc` while its body is being collected.

    Attributes:
        kind: ``"rept"``, ``"irp"`` or ``"irpc"``.
        argument: The text after the directive — a count, or a symbol
            and its values.
        body: The statements between the directive and its ``.endr``.
        depth: How many are open, so a nested ``.endr`` does not close
            the outer one.
    """

    kind: str
    argument: str
    body: list[Located]
    depth: int

    def iterations(
        self, symbols: dict[str, int | None]
    ) -> list[dict[str, str]] | None:
        """Return the substitutions to make, once per repetition.

        *symbols* holds the absolute values known at this point.  None
        means refuse: a `.rept` whose count has no exact value.
        """
        if self.kind == "rept":
            count = evaluate(self.argument, symbols)
            if count is None:
                return None
            # "A count of zero is allowed, but nothing is generated.
            # Negative counts are not allowed."
            return [{} for _ in range(max(0, count))]

        symbol, _, values = self.argument.partition(",")
        symbol = symbol.strip()
        if not symbol:
            return None
        if self.kind == "irpc":
            # One iteration per CHARACTER of the (possibly quoted) value.
            text = values.strip().strip('"')
            return [{symbol: char} for char in text] or [{symbol: ""}]
        # `.irp`: one iteration per value, and with no values at all the
        # body runs once with the symbol bound to the null string.
        items = [v for v in split_arguments(values) if v != ""]
        return [{symbol: item} for item in items] or [{symbol: ""}]


class _Expander:
    """Holds the definitions, the mode and the budgets while a unit is read.

    One recursive routine handles definitions, invocations and
    repetitions, so the ways they nest all work without a case for each:
    a `.rept` inside a macro body, a macro invoked inside an `.irp` body,
    an `.irp` inside an `.irp`, and a macro invoked from a macro.
    """

    def __init__(
        self, report: Report, defsym: bool = False, comments_removed: bool = False
    ) -> None:
        self.macros: dict[str, Macro] = {}
        self.report = report
        self.counter = 0
        self.emitted = 0
        self.exhausted = False
        self.alternate = False
        # Each symbol that an emitted statement defined, with its absolute
        # value, or None when it has none that this knows (a label).  The
        # assembler decides a conditional in one pass, thus only what came
        # BEFORE it counts: measured, `.ifdef` of a label defined later in
        # the file is false.
        self.values: dict[str, int | None] = {}
        # Names whose `.ifdef` answer is not known.
        self.uncertain_names: set[str] = set()
        # True after the first barrier: the skipped statements can have
        # defined or changed any symbol.
        self.uncertain = False
        # `--defsym` defines symbols that no statement shows.
        self.defsym = defsym
        # The comments of the target are already removed: an `@` or `#`
        # left in an argument is part of it (an ARM immediate `#0`).
        self.comments_removed = comments_removed
        # True when the conditional state of the rest of the unit is not
        # known: then nothing more is read.
        self.stopped = False
        # The open conditionals, innermost last, and how many are open
        # inside a skipped branch.  One stack for the whole unit, not one
        # for each expansion: measured, an `.if 0` in the body of an `.irp`
        # skips the statements after its `.endr` up to the `.endif`.
        self.frames: list[str] = []
        self.skipped = 0

    def _budget_left(self) -> bool:
        if self.emitted < MAX_STATEMENTS:
            return True
        if not self.exhausted:
            self.exhausted = True
            self.report.refused["statement budget exhausted"] += 1
        return False

    def _emit(self, located: Located) -> Iterator[Located]:
        """Yield one statement, and record what it defines."""
        if not self._budget_left():
            return
        self.emitted += 1
        self._record(located[2])
        yield located

    def _barrier(self, at: Located) -> Iterator[Located]:
        """Yield a barrier at the place of a refused construct."""
        self.uncertain = True
        if not self._budget_left():
            return
        self.emitted += 1
        yield at[0], at[1], BARRIER

    def _record(self, statement: str) -> None:
        """Note the symbol that *statement* defines, for later conditionals."""
        label = _LABEL_ONLY.match(statement)
        if label is not None:
            self.values.setdefault(label.group(1), None)
            return
        assign = _ASSIGN.match(statement)
        if assign is not None:
            name = assign.group(2) or assign.group(3)
            if name == ".":
                # `. = expr` moves the location counter.  It defines no
                # symbol, and `.` has no value that this knows.
                return
            # `.eqv` is evaluated at each USE, not here, thus it has no
            # value at this point.
            lazy = (assign.group(1) or "").lower() == "eqv"
            self.values[name] = None if lazy else evaluate(assign.group(4), self._symbols())
            return
        alias = _THUMB_SET.match(statement)
        if alias is not None:
            self.values[alias.group(1)] = None
            return
        common = _COMMON.match(statement)
        if common is not None:
            self.uncertain_names.add(common.group(1))

    def _symbols(self) -> dict[str, int | None]:
        """The values an expression may use.  None of them after a barrier."""
        return {} if self.uncertain else self.values

    def _defined(self, name: str) -> bool | None:
        """Whether *name* is defined at this point, or None when unknown."""
        if name in self.values:
            return True
        if self.uncertain or self.defsym or name in self.uncertain_names:
            return None
        return False

    def _condition(self, kind: str, argument: str) -> bool | None:
        """Return whether the conditional *kind* takes its branch, or None.

        None: the answer is not known exactly, and no branch may be taken.
        """
        if kind in ("ifdef", "ifndef", "ifnotdef"):
            if not _PLAIN_NAME.match(argument):
                return None
            defined = self._defined(argument)
            if defined is None:
                return None
            return defined if kind == "ifdef" else not defined
        if not self.comments_removed and _COMMENT_START.search(argument):
            # `@` and `#` start a line comment on some targets, and this
            # does not know the target: the argument can end earlier.
            return None
        if kind in ("ifb", "ifnb"):
            return (argument == "") == (kind == "ifb")
        if kind in ("ifc", "ifnc"):
            same = _same_strings(argument)
            return None if same is None else same == (kind == "ifc")
        if kind in ("ifeqs", "ifnes"):
            pair = _QUOTED_PAIR.match(argument)
            if pair is None:
                return None
            return (pair.group(1) == pair.group(2)) == (kind == "ifeqs")
        value = evaluate(argument, self._symbols())
        if value is None:
            return None
        tests = {
            "if": value != 0, "ifne": value != 0, "ifeq": value == 0,
            "ifgt": value > 0, "ifge": value >= 0,
            "iflt": value < 0, "ifle": value <= 0,
        }
        return tests[kind]

    def process(
        self, stream: Iterable[Located], depth: int = 0
    ) -> Iterator[Located]:
        """Yield *stream* with every macro, repetition and conditional resolved.

        The recursion is what makes the constructs compose.  The depth
        limit is checked where a recursion starts (`_invoke`, `_repeat`),
        because there the place of the refusal is known.
        """
        collecting: Macro | None = None
        repeat: _Repetition | None = None

        for raw in stream:
            if self.stopped:
                return
            # In a skipped branch the assembler reads a conditional only at
            # the start of a line: measured, `lbl: .endif` there does not
            # end the branch.  Elsewhere `lbl: .if 4` is a label, then a
            # conditional.
            skipping = (
                repeat is None and collecting is None
                and bool(self.frames) and self.frames[-1] != _TAKING
            )
            pieces = (raw,) if skipping else _split_labels((raw,))
            for located in pieces:
                _, _, statement = located

                if repeat is not None:
                    if _REPEAT_OPEN.match(statement):
                        repeat.depth += 1
                    elif _REPEAT_CLOSE.match(statement):
                        repeat.depth -= 1
                        if repeat.depth == 0:
                            yield from self._repeat(repeat, located, depth)
                            repeat = None
                            continue
                    repeat.body.append(located)
                    continue

                if collecting is not None:
                    if _END.match(statement):
                        if not collecting.refuse_reason and _has_conditional(
                            collecting.body
                        ):
                            collecting.refuse_reason = "conditional body"
                        self.macros[collecting.name] = collecting
                        self.report.defined += 1
                        collecting = None
                    else:
                        collecting.body.append(statement)
                    continue

                if statement.startswith(UNSURE):
                    yield from self._unsure(located, skipping)
                    continue

                if self.frames and self.frames[-1] != _TAKING:
                    yield from self._skip(located)
                    continue

                handled = yield from self._conditional(located)
                if handled:
                    continue

                if _INCLUDE.match(statement):
                    # The included file is not read here, and it can define
                    # symbols, macros and code.
                    # Measured: a conditional that the included file opens
                    # stays open after it, thus the rest of the unit is
                    # unknown too.
                    self.stopped = True
                    yield from self._refuse(".include this does not read", located)
                    continue

                if _ALTMACRO.match(statement):
                    self.alternate = True
                    continue
                if _NOALTMACRO.match(statement):
                    self.alternate = False
                    continue

                defined = _DEFINE.match(statement)
                if defined is not None:
                    collecting = Macro(
                        name=defined.group(1),
                        parameters=parse_parameters(defined.group(2)),
                        body=[],
                    )
                    continue

                purged = _PURGEM.match(statement)
                if purged is not None:
                    self.macros.pop(purged.group(1), None)
                    continue

                opened = _REPEAT_OPEN.match(statement)
                if opened is not None:
                    repeat = _Repetition(
                        kind=opened.group(1).lower(),
                        argument=opened.group(2).strip(),
                        body=[],
                        depth=1,
                    )
                    continue

                invocation = _INVOKE.match(statement)
                if invocation is not None:
                    macro = self.macros.get(invocation.group(1))
                    if macro is not None:
                        yield from self._invoke(
                            macro, invocation.group(2), located, depth
                        )
                        continue

                if self.exhausted:
                    return
                yield from self._emit(located)

        if collecting is not None:
            # `.macro` with no `.endm`.  The assembler rejects the file,
            # so this cannot come from anything a build compiled; drop
            # the body rather than read it as code.
            self.report.refused["unterminated macro"] += 1
        if repeat is not None:
            self.report.refused["unterminated repetition"] += 1
        if depth == 0 and self.frames:
            # `.if` with no `.endif`: the assembler rejects the file too.
            # Only the outer stream can tell: a conditional may open in the
            # body of a repetition and close after its `.endr`.
            self.report.refused["unterminated conditional"] += 1

    def _unsure(self, located: Located, skipping: bool) -> Iterator[Located]:
        """Read text that may be a comment, or may be statements.

        When it can change a conditional, nothing after it is known.  In a
        taken branch it can also define any symbol, thus it is a barrier.
        """
        hidden = located[2].removeprefix(UNSURE).split(";")
        if self._can_change_nesting(hidden, frozenset()):
            self.stopped = True
            self.report.refused["rest of the unit after it"] += 1
        if not skipping:
            yield from self._refuse("text that may be a comment", located)

    def _skip(self, located: Located) -> Iterator[Located]:
        """Read one statement of a skipped branch: only its conditionals."""
        statement = located[2]
        if _IF_OPEN.match(statement):
            self.skipped += 1
        elif _IF_END.match(statement):
            if self.skipped:
                self.skipped -= 1
            else:
                self.frames.pop()
        elif self.skipped == 0 and self.frames[-1] == _WAITING:
            if _IF_ELSE.match(statement):
                self.frames[-1] = _TAKING
            elif (elseif := _IF_ELSEIF.match(statement)) is not None:
                self.report.conditionals += 1
                taken = self._condition("if", elseif.group(1).strip())
                if taken is None:
                    self.frames[-1] = _UNKNOWN
                    yield from self._refuse("conditional this cannot evaluate", located)
                elif taken:
                    self.frames[-1] = _TAKING

    def _conditional(self, located: Located) -> Generator[Located, None, bool]:
        """Handle a conditional directive in a taken branch.

        Returns True when *located* was one, and yields a barrier when its
        condition has no exact value.
        """
        statement = located[2]
        opened = _IF_OPEN.match(statement)
        if opened is not None:
            self.report.conditionals += 1
            taken = self._condition(opened.group(1).lower(), opened.group(2).strip())
            if taken is None:
                self.frames.append(_UNKNOWN)
                yield from self._refuse("conditional this cannot evaluate", located)
            else:
                self.frames.append(_TAKING if taken else _WAITING)
            return True
        if _IF_ELSE.match(statement) or _IF_ELSEIF.match(statement):
            # The branch that ends here was taken, thus every later one is
            # skipped.
            if self.frames:
                self.frames[-1] = _DONE
            else:
                self.report.refused["conditional with no .if"] += 1
            return True
        if _IF_END.match(statement):
            if self.frames:
                self.frames.pop()
            else:
                self.report.refused["conditional with no .if"] += 1
            return True
        return False

    def _refuse(
        self, reason: str, at: Located, body: list[str] | None = None
    ) -> Iterator[Located]:
        """Count a refusal and leave a barrier at its place.

        *body* is the text of the refused construct.  When it can open or
        close a conditional that outlives it (measured: an `.if` in an
        `.irp` body can end after the `.endr`), the conditional state of
        the rest of the unit is not known, and the reading stops.
        """
        self.report.refused[reason] += 1
        if body is not None and self._can_change_nesting(body, frozenset()):
            self.stopped = True
            self.report.refused["rest of the unit after it"] += 1
        yield from self._barrier(at)

    def _can_change_nesting(self, body: list[str], seen: frozenset[str]) -> bool:
        """Whether expanding *body* can leave a conditional open or closed.

        That is also an `.else` or `.elseif` of a conditional opened
        before the body, and an `.endif` that closes one.  True also when it
        is not known: a directive that a macro argument supplies (`\\dir`),
        a macro defined inside, an `.include`, or a recursion.
        """
        depth = 0
        for statement in body:
            for _, _, piece in _split_labels((("", 0, statement),)):
                if _SUBSTITUTED_LABEL.match(piece):
                    continue
                if (
                    piece.lstrip().startswith("\\")
                    or _DEFINE.match(piece)
                    or _INCLUDE.match(piece)
                ):
                    return True
                if depth == 0 and (_IF_ELSE.match(piece) or _IF_ELSEIF.match(piece)):
                    # It changes the branch of a conditional opened before.
                    return True
                if _IF_OPEN.match(piece):
                    depth += 1
                elif _IF_END.match(piece):
                    depth -= 1
                    if depth < 0:
                        # It closes a conditional opened before.
                        return True
                elif (invocation := _INVOKE.match(piece)) is not None:
                    macro = self.macros.get(invocation.group(1))
                    if macro is not None:
                        if macro.name in seen or self._can_change_nesting(
                            macro.body, seen | {macro.name}
                        ):
                            return True
        return depth != 0

    def _invoke(
        self, macro: Macro, argument_text: str, at: Located, depth: int
    ) -> Iterator[Located]:
        """Yield what one macro invocation produces."""
        if self.alternate:
            yield from self._refuse("alternate macro mode", at, macro.body)
            return
        if macro.refuse_reason:
            yield from self._refuse(macro.refuse_reason, at, macro.body)
            return
        if depth + 1 > MAX_DEPTH:
            yield from self._refuse("recursion deeper than the limit", at, macro.body)
            return
        values = bind(macro, split_arguments(argument_text))
        if values is None:
            yield from self._refuse("arguments do not match the parameters", at, macro.body)
            return

        self.counter += 1
        counter = self.counter
        self.report.expanded += 1
        file, line, _ = at

        def body() -> Iterator[Located]:
            for raw in macro.body:
                statement = substitute(raw, values, counter)
                if _EXITM.match(statement):
                    return
                # The location is the INVOCATION's.  A reader looking for
                # POWER_CLOCK_IRQHandler wants the startup line that says
                # `IRQ POWER_CLOCK_IRQHandler`, not a line inside a
                # `.macro` that names no handler at all.
                yield file, line, statement

        yield from self.process(body(), depth + 1)

    def _repeat(
        self, repetition: _Repetition, at: Located, depth: int
    ) -> Iterator[Located]:
        """Yield what one `.rept` / `.irp` / `.irpc` produces.

        *at* is the `.endr` that closed it: the place of a barrier when
        the repetition is refused.
        """
        bindings = repetition.iterations(self._symbols())
        if bindings is None:
            yield from self._refuse(
                f".{repetition.kind} this cannot evaluate", at,
                [statement for _, _, statement in repetition.body],
            )
            return
        if depth + 1 > MAX_DEPTH:
            yield from self._refuse(
                "recursion deeper than the limit", at,
                [statement for _, _, statement in repetition.body],
            )
            return
        self.report.repeated += 1

        def body() -> Iterator[Located]:
            for values in bindings:
                for file, line, raw in repetition.body:
                    # The location is the body statement's own.  Unlike a
                    # macro body, this one is right here in the file, so
                    # the more precise answer is available.
                    yield file, line, substitute(raw, values, self.counter)

        yield from self.process(body(), depth + 1)


def expand_stream(
    statements: Iterable[Located], report: Report, *,
    defsym: bool = False, comments_removed: bool = False,
) -> Iterator[Located]:
    """Yield *statements* with macros and repetitions expanded.

    Definitions are collected as they are met, which is also the order
    the assembler reads them in: a macro cannot be invoked before its
    ``.macro`` line, and ``.purgem`` must undefine before the name can be
    defined again.

    *defsym* says that the command of the unit defines symbols with
    ``--defsym``: then ``.ifdef`` of a name that no statement defines has
    no known answer.  *comments_removed* says that the comments of the
    target are gone, thus an ``@`` or ``#`` in an argument is part of it.
    """
    return _Expander(report, defsym, comments_removed).process(statements)


def _has_conditional(body: list[str]) -> bool:
    return any(_CONDITIONAL.match(statement) for statement in body)


def _split_labels(stream: Iterable[Located]) -> Iterator[Located]:
    """Yield each `name:` prefix of a statement as a statement of its own.

    The assembler reads `KEXC_areg: .if 4` as a label and then a
    conditional, and a macro expansion gives `label: addi sp, sp, -32`.
    Each reader matches a statement from its start, thus without this the
    part after the label is lost: the conditional is not seen, and the
    instruction is not read.
    """
    for file, line, statement in stream:
        rest = statement
        while (head := _LABEL_HEAD.match(rest)) is not None and head.group(2).strip():
            yield file, line, head.group(1)
            rest = head.group(2)
        yield file, line, rest


def _same_strings(argument: str) -> bool | None:
    """Compare the two strings of `.ifc a,b`, or None when unsure.

    Measured: the comparison is case-sensitive, and blanks after the comma
    do not count.  A single-quoted string (the other form the manual
    allows) and a blank before the comma are not measured, thus not read.
    """
    first, comma, second = argument.partition(",")
    if not comma or first != first.rstrip():
        return None
    second = second.strip()
    if first.startswith("'") or second.startswith("'"):
        return None
    return first == second
