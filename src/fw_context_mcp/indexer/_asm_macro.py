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
  expanding it needs an assembler expression evaluator — which means
  writing a large part of the assembler.
* an expansion that exceeds the depth or statement budget below, which
  is what an unguarded recursion looks like from here.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

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
# bounded, which is what separates them from the conditionals: `.rept 3`
# repeats three times whatever the symbols hold, so expanding them needs
# no expression evaluator.  A `.rept` whose count is an expression is the
# exception, and is refused.
_REPEAT_OPEN = re.compile(r"^\s*\.(rept|irp|irpc)\b(.*)$", re.IGNORECASE)
_REPEAT_CLOSE = re.compile(r"^\s*\.endr\b", re.IGNORECASE)

# One statement with the place it came from.  Repetition bodies carry
# these rather than bare text, because the body sits in the file and its
# own line is the honest answer for a symbol defined there.
Located = tuple[str, int, str]

# Every conditional the manual lists, plus the ARM spellings.  A body
# holding one is refused: which branch it takes is an expression, and
# this does not evaluate expressions.
_CONDITIONAL = re.compile(
    r"^\s*\.(if|ifdef|ifndef|ifb|ifnb|ifc|ifnc|ifeq|ifne|ifgt|ifge|iflt|ifle"
    r"|ifeqs|ifnes|else|elseif|endif)\b",
    re.IGNORECASE,
)

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
    refused: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        if not (self.defined or self.repeated or self.refused):
            return ""
        parts = []
        if self.defined:
            parts.append(f"{self.defined} macro(s) defined")
            parts.append(f"{self.expanded} expanded")
        if self.repeated:
            parts.append(f"{self.repeated} repetition(s) expanded")
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

    def iterations(self) -> list[dict[str, str]] | None:
        """Return the substitutions to make, once per repetition.

        None means refuse: a `.rept` whose count is an expression, which
        this does not evaluate.
        """
        if self.kind == "rept":
            try:
                count = int(self.argument, 0)
            except ValueError:
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

    def __init__(self, report: Report) -> None:
        self.macros: dict[str, Macro] = {}
        self.report = report
        self.counter = 0
        self.emitted = 0
        self.exhausted = False
        self.alternate = False

    def _budget_left(self) -> bool:
        if self.emitted < MAX_STATEMENTS:
            return True
        if not self.exhausted:
            self.exhausted = True
            self.report.refused["statement budget exhausted"] += 1
        return False

    def process(
        self, stream: Iterable[Located], depth: int = 0
    ) -> Iterator[Located]:
        """Yield *stream* with every macro and repetition expanded.

        The recursion is what makes the constructs compose; *depth* stops
        one that does not terminate.
        """
        if depth > MAX_DEPTH:
            self.report.refused["recursion deeper than the limit"] += 1
            return

        collecting: Macro | None = None
        repeat: _Repetition | None = None

        for located in stream:
            _, _, statement = located

            if repeat is not None:
                if _REPEAT_OPEN.match(statement):
                    repeat.depth += 1
                elif _REPEAT_CLOSE.match(statement):
                    repeat.depth -= 1
                    if repeat.depth == 0:
                        yield from self._repeat(repeat, depth)
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

            if not self._budget_left():
                return
            self.emitted += 1
            yield located

        if collecting is not None:
            # `.macro` with no `.endm`.  The assembler rejects the file,
            # so this cannot come from anything a build compiled; drop
            # the body rather than read it as code.
            self.report.refused["unterminated macro"] += 1
        if repeat is not None:
            self.report.refused["unterminated repetition"] += 1

    def _invoke(
        self, macro: Macro, argument_text: str, at: Located, depth: int
    ) -> Iterator[Located]:
        """Yield what one macro invocation produces."""
        if self.alternate:
            self.report.refused["alternate macro mode"] += 1
            return
        if macro.refuse_reason:
            self.report.refused[macro.refuse_reason] += 1
            return
        values = bind(macro, split_arguments(argument_text))
        if values is None:
            self.report.refused["arguments do not match the parameters"] += 1
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

    def _repeat(self, repetition: _Repetition, depth: int) -> Iterator[Located]:
        """Yield what one `.rept` / `.irp` / `.irpc` produces."""
        bindings = repetition.iterations()
        if bindings is None:
            self.report.refused[
                f".{repetition.kind} this cannot evaluate"
            ] += 1
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
    statements: Iterable[Located], report: Report
) -> Iterator[Located]:
    """Yield *statements* with macros and repetitions expanded.

    Definitions are collected as they are met, which is also the order
    the assembler reads them in: a macro cannot be invoked before its
    ``.macro`` line, and ``.purgem`` must undefine before the name can be
    defined again.
    """
    return _Expander(report).process(statements)


def _has_conditional(body: list[str]) -> bool:
    return any(_CONDITIONAL.match(statement) for statement in body)
