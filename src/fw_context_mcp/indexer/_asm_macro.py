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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ._asm_expr import evaluate

log = logging.getLogger(__name__)

# The statement text of a barrier: a place where unknown statements were
# skipped.  No assembly statement can hold a NUL character, thus no
# pattern of a reader matches it by accident.  A barrier statement is this
# prefix and its effects (see `make_barrier`).
BARRIER = "\x00barrier"
# The prefix of a statement whose text may be a comment: the comment
# character of the target is not known.  The text after the prefix is
# what the line holds there.  The expander reads it like the body of a
# refused construct and gives a BARRIER in its place.
UNSURE = "\x00unsure:"


@dataclass(frozen=True)
class BarrierEffects:
    """What the statements behind a barrier can do.

    The text of a refused construct is mostly known: the body of a macro,
    the body of a repetition, the branches of a conditional.  Only which
    part of it the assembler takes is not.  Thus a reader can keep its
    state across a barrier whose text cannot touch that state: a refused
    macro that writes only instructions changes no section and defines no
    label, and the function that owns the code around it stays the owner.

    Attributes:
        section: The text can switch the section.
        emits: The text can write bytes (an instruction, data, padding).
        names: The symbols the text can define (a label, `.set`), or None
            when they are not known (a name that a macro argument gives).
        exports: The names the text can export or type (`.global`, `.weak`,
            `.type`, `.size`), or None when not known.  Such a name can be
            the owner of code, also of code outside the barrier.
        text: The statements of the text that write bytes (instructions and
            data, of all its branches, in order), its labels and the
            directives of its conditionals, or None when not known.  The
            flow of control reads them: a path through them to the end
            reaches the code after the barrier.  A construct that the
            assembler can skip as a whole (`OPTIONAL_OPEN`) is in a
            conditional of its own.
    """

    section: bool
    emits: bool
    names: frozenset[str] | None
    exports: frozenset[str] | None
    text: tuple[str, ...] | None = None


# The effects of statements that are not known at all: an `.include`, a
# directive that a macro argument gives.
FULL = BarrierEffects(section=True, emits=True, names=None, exports=None, text=None)
# Separates the statements of the text in a barrier statement: no statement
# holds it.
_TEXT_SEPARATOR = "\x01"
# The conditional around the text of a construct that the assembler can
# skip as a whole: a conditional with no known value, a repetition whose
# count can be zero, text that can be a comment.  `?` is no condition of
# gas: the reader of the flow takes both ways.
OPTIONAL_OPEN = ".if ?"
OPTIONAL_CLOSE = ".endif"
# The text of a macro that a refused text invokes.
MACRO_OPEN = ".macro ?"
MACRO_CLOSE = ".endm"
# A local label of the expansion counter: `.Lskip\@:`.
_COUNTER_LOCAL_LABEL = re.compile(r"^\s*\.L(?:[\w.$]|\\@)*\\@(?:[\w.$]|\\@)*\s*:\s*$")
# A name in an expression: when the refused text can set it, its value
# there is not known.
_EXPRESSION_NAME = re.compile(r"[A-Za-z_.$][\w.$]*")


def optional(effects: BarrierEffects) -> BarrierEffects:
    """The effects of a text that the assembler can also skip as a whole."""
    if effects.text is None:
        return effects
    return BarrierEffects(
        section=effects.section, emits=effects.emits, names=effects.names,
        exports=effects.exports, text=(OPTIONAL_OPEN, *effects.text, OPTIONAL_CLOSE),
    )


def make_barrier(effects: BarrierEffects) -> str:
    """Give the barrier statement that carries *effects*."""
    def names(value: frozenset[str] | None) -> str:
        return "?" if value is None else ",".join(sorted(value))

    flags = ("s" if effects.section else "") + ("e" if effects.emits else "")
    text = "?" if effects.text is None else _TEXT_SEPARATOR.join(effects.text)
    # The text comes last: a statement can hold `|`, a name cannot.
    return f"{BARRIER}|{flags}|{names(effects.names)}|{names(effects.exports)}|{text}"


def is_barrier(statement: str) -> bool:
    """Whether *statement* is a barrier."""
    return statement.startswith(BARRIER)


def barrier_effects(statement: str) -> BarrierEffects | None:
    """Give the effects of a barrier statement, or None for any other."""
    if not is_barrier(statement):
        return None
    parts = statement.removeprefix(BARRIER).split("|", 4)
    if len(parts) != 5:
        return FULL

    def names(value: str) -> frozenset[str] | None:
        if value == "?":
            return None
        return frozenset(name for name in value.split(",") if name)

    return BarrierEffects(
        section="s" in parts[1], emits="e" in parts[1],
        names=names(parts[2]), exports=names(parts[3]),
        text=None if parts[4] == "?" else tuple(
            statement for statement in parts[4].split(_TEXT_SEPARATOR) if statement
        ),
    )

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
# A name can hold the mark of an expansion count that is not known
# (`skip\@3`, `_Expander._count`).
_LABEL_HEAD = re.compile(r"^(\s*(?:[A-Za-z_.$](?:[\w.$]|\\@)*|\d+)\s*:)(?!:)(.*)$")
# A statement that is one label: never a macro invocation, also when its
# name starts with the name of a macro (`wait_ready\@3:`).
_LABEL_STATEMENT = re.compile(r"^\s*(?:[A-Za-z_.$](?:[\w.$]|\\@)*|\d+)\s*:\s*$")
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
_NUMERIC_LABEL = re.compile(r"^\s*\d+\s*:\s*$")
# A directive that switches the section, or the location counter of one.
_SECTION_SWITCH = re.compile(
    r"^\s*\.(?:section|pushsection|popsection|previous|subsection|text|data|bss"
    r"|struct|offset)\b",
    re.IGNORECASE,
)
# A directive that exports or types a name: it can make a label the owner
# of code.  `.thumb_func` names no symbol: it marks the next label.
_EXPORT = re.compile(
    r"^\s*\.(globl|global|weak|type|size|thumb_func|hidden|protected|internal)\b\s*(.*)$",
    re.IGNORECASE,
)
_COMMON_NAME = re.compile(r"^\s*\.(?:comm|lcomm)\s+([^\s,]+)", re.IGNORECASE)
# A statement that writes no byte: a label, an assignment, and the
# directives that only describe symbols or the state of the assembler.
NO_BYTES = re.compile(
    r"^\s*(?:$|[A-Za-z_.$][\w.$]*\s*:\s*$|(?!\.\s*=)[A-Za-z_.$][\w.$]*\s*=(?!=)"
    r"|\.(?:globl|global|weak|type|size|set|equ|equiv|eqv|thumb_set|thumb_func"
    r"|syntax|thumb|arm|code|cpu|fpu|arch|eabi_attribute|file|ident|loc|hidden"
    r"|protected|internal|local|extern|fnstart|fnend|cantunwind|cfi_\w+)\b)",
    re.IGNORECASE,
)

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


def substitute(statement: str, values: dict[str, str], counter: int | str | None) -> str:
    """Replace ``\\name``, ``\\()`` and ``\\@`` in one body statement.

    An unknown ``\\name`` is left exactly as it stands.  Replacing it
    with nothing would be a guess, and a guess here writes a symbol name.
    With no *counter* (the count of an expansion that is not done), ``\\@``
    stays too: a label that holds it has a name that is not known.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "()":
            return ""
        if token == "@":
            return match.group(0) if counter is None else str(counter)
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


@dataclass
class _RunFrame:
    """A conditional or a repetition that `_Runs` is in."""

    kind: str  # "if" or "rept"
    parent: str
    status: str
    seen: list[str] = field(default_factory=list)
    times: int | None = 1
    exited: str = "no"


@dataclass
class _RepetitionExit:
    """Whether an `.exitm` ended a repetition: "no", "maybe" or "yes"."""

    state: str = "no"


class _Runs:
    """Whether the statement at this point of a refused text runs.

    The answer is "yes", "no" or "maybe", from the markers of the text
    (`_Expander._marker`): the value of each conditional, the count of each
    repetition, and an `.exitm` before it.  An `.exitm` in a repetition
    ends the repetition (measured, gas 2.40), else the macro text.
    """

    def __init__(self, base: str) -> None:
        self.base = base
        self.frames: list[_RunFrame] = []
        self.exited = "no"

    def status(self) -> str:
        current = self.frames[-1].status if self.frames else self.base
        exits = [self.exited, *(frame.exited for frame in self.frames if frame.kind == "rept")]
        if "yes" in exits:
            return "no"
        if "maybe" in exits and current == "yes":
            return "maybe"
        return current

    def times(self) -> int | None:
        """How many times the statement runs when it runs: the product of
        the counts of its repetitions, or None when one is not known."""
        product = 1
        for frame in self.frames:
            if frame.kind == "rept":
                if frame.times is None:
                    return None
                product *= frame.times
        return product

    def feed(self, marker: str) -> None:
        """Move past the marker *marker*."""
        word, _, value = marker.partition(" ")
        if word == ".if":
            parent = self.status()
            self.frames.append(_RunFrame("if", parent, _branch(parent, [], value), [value]))
        elif word in (".elseif", ".else") and self.frames and self.frames[-1].kind == "if":
            frame = self.frames[-1]
            branch_value = "1" if word == ".else" else value
            frame.status = _branch(frame.parent, frame.seen, branch_value)
            frame.seen.append(branch_value)
        elif word == ".rept":
            parent = self.status()
            times = int(value) if value.isdigit() else None
            status = "no" if times == 0 or parent == "no" else (parent if times else "maybe")
            self.frames.append(_RunFrame("rept", parent, status, times=times))
        elif word in (".endif", ".endr") and self.frames and self.frames[-1].kind == (
            "if" if word == ".endif" else "rept"
        ):
            self.frames.pop()
        elif word == ".exitm":
            current = self.status()
            if current == "no":
                return
            owner = next((frame for frame in reversed(self.frames) if frame.kind == "rept"), None)
            if current == "yes":
                if owner is None:
                    self.exited = "yes"
                else:
                    owner.exited = "yes"
            elif owner is None:
                self.exited = "maybe" if self.exited == "no" else self.exited
            elif owner.exited == "no":
                owner.exited = "maybe"


def _branch(parent: str, seen: list[str], value: str) -> str:
    """Whether a branch with *value* runs after branches with the values *seen*."""
    if parent == "no" or value == "0" or "1" in seen:
        return "no"
    if value == "1" and parent == "yes" and all(earlier == "0" for earlier in seen):
        return "yes"
    return "maybe"


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
        # The value of `\@` in the next expansion: gas counts from 0
        # (`_count`).  Not known after a refused text that can invoke a
        # macro or not.
        self.counter = 0
        self.counter_known = True
        # The repetitions being expanded, innermost last: whether an
        # `.exitm` of their body ran (`_repeat`).
        self.repetition_exits: list[_RepetitionExit] = []
        # Whether the text that `_scan` read last can run an `.exitm` of
        # its own level (not of a repetition or a macro inside it).
        self.scan_exits = False
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
        # The text of a conditional whose condition has no exact value, from
        # the place of that condition up to its `.endif`, and that place.
        # Its barrier comes at the `.endif`, with the effects of the text.
        self.region: list[str] | None = None
        self.region_at: Located | None = None

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

    def _barrier(self, at: Located, effects: BarrierEffects) -> Iterator[Located]:
        """Yield a barrier with *effects* at the place of a refused construct.

        The names that the skipped text can define lose their values here,
        and all values go when those names are not known.
        """
        if effects.names is None:
            self.uncertain = True
        else:
            for name in effects.names:
                self.values.pop(name, None)
                self.uncertain_names.add(name)
        if not self._budget_left():
            return
        self.emitted += 1
        yield at[0], at[1], make_barrier(effects)

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

                if self._conditional(located):
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

                invocation = (
                    None if _LABEL_STATEMENT.match(statement) else _INVOKE.match(statement)
                )
                if invocation is not None:
                    macro = self.macros.get(invocation.group(1))
                    if macro is not None:
                        yield from self._invoke(
                            macro, invocation.group(2), located, depth
                        )
                        continue

                if _EXITM.match(statement):
                    # It ends the repetition around it (`_repeat` reads the
                    # state before its next statement).  An `.exitm` of a
                    # macro body never comes here (`_invoke`).
                    if self.repetition_exits:
                        self.repetition_exits[-1].state = "yes"
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
        if skipping and self.frames and self.frames[-1] == _UNKNOWN and self.region is not None:
            self.region.extend(hidden)
        nesting, effects = self._scan(hidden)
        if nesting:
            self.stopped = True
            self.report.refused["rest of the unit after it"] += 1
        if not skipping:
            self.report.refused["text that may be a comment"] += 1
            yield from self._barrier(located, optional(effects))

    def _skip(self, located: Located) -> Iterator[Located]:
        """Read one statement of a skipped branch: only its conditionals.

        In a conditional with no known branch the text is kept: its barrier
        comes at the `.endif`, with what the text can do.
        """
        statement = located[2]
        closing = _IF_END.match(statement) is not None and self.skipped == 0
        if self.frames[-1] == _UNKNOWN and self.region is not None and not closing:
            self.region.append(statement)
        if _IF_OPEN.match(statement):
            self.skipped += 1
        elif _IF_END.match(statement):
            if self.skipped:
                self.skipped -= 1
            else:
                if self.frames.pop() == _UNKNOWN:
                    yield from self._close_region()
        elif self.skipped == 0 and self.frames[-1] == _WAITING:
            if _IF_ELSE.match(statement):
                self.frames[-1] = _TAKING
            elif (elseif := _IF_ELSEIF.match(statement)) is not None:
                self.report.conditionals += 1
                taken = self._condition("if", elseif.group(1).strip())
                if taken is None:
                    self.frames[-1] = _UNKNOWN
                    self._open_region(located)
                elif taken:
                    self.frames[-1] = _TAKING

    def _open_region(self, at: Located) -> None:
        """Start to keep the text of a conditional with no known branch."""
        self.report.refused["conditional this cannot evaluate"] += 1
        self.region = []
        self.region_at = at

    def _close_region(self) -> Iterator[Located]:
        """End such a conditional: one barrier with what its text can do."""
        region, at = self.region, self.region_at
        self.region = self.region_at = None
        if region is None or at is None:
            return
        # The text holds the branches of its own conditional, thus its
        # `.else` and `.endif` change no nesting.  An `.include` or a
        # `.macro` in it can: the reading stops, as for a refused body.
        if any(_INCLUDE.match(piece) or _DEFINE.match(piece) for piece in region):
            self.stopped = True
            self.report.refused["rest of the unit after it"] += 1
        _, effects = self._scan(region)
        if self.scan_exits and self.repetition_exits:
            # The region can end the repetition around it.
            exit_state = self.repetition_exits[-1]
            exit_state.state = "yes" if exit_state.state == "yes" else "maybe"
        # No branch can be the one the assembler takes: the text of the
        # region holds `.else` of its own conditional, the `.if` is around it.
        yield from self._barrier(at, optional(effects))

    def _conditional(self, located: Located) -> bool:
        """Handle a conditional directive in a taken branch.

        Returns True when *located* was one.  A condition with no exact value
        starts a region, whose barrier comes at its `.endif`.
        """
        statement = located[2]
        opened = _IF_OPEN.match(statement)
        if opened is not None:
            self.report.conditionals += 1
            taken = self._condition(opened.group(1).lower(), opened.group(2).strip())
            if taken is None:
                self.frames.append(_UNKNOWN)
                self._open_region(located)
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
        self, reason: str, at: Located, body: list[str] | None = None,
        skippable: bool = False,
    ) -> Iterator[Located]:
        """Count a refusal and leave a barrier at its place.

        *body* is the text of the refused construct, or None when it is not
        known (`.include`).  When the text can open or close a conditional
        that outlives it (measured: an `.if` in an `.irp` body can end after
        the `.endr`), the conditional state of the rest of the unit is not
        known, and the reading stops.  The barrier carries what the text can
        do (`_scan`).  *skippable*: the assembler can take none of the text
        (a repetition whose count can be zero).
        """
        self.report.refused[reason] += 1
        if body is None:
            effects = FULL
        else:
            nesting, effects = self._scan(body, base="maybe" if skippable else "yes")
            if nesting:
                self.stopped = True
                self.report.refused["rest of the unit after it"] += 1
            if skippable:
                effects = optional(effects)
        yield from self._barrier(at, effects)

    def _count(self) -> str:
        """The value of `\\@` in an expansion that starts now; then count it.

        gas gives the expansions 0, 1, 2 and so on.  When the count is not
        known, the value is a mark `\\@<n>`, one for each expansion: the
        labels of one expansion still name each other in the flow, and no
        symbol gets a number that can be wrong (`_asm_layout` does not index
        a name that holds `\\@`).
        """
        value = self._current()
        self.counter += 1
        return value

    def _current(self) -> str:
        """The value of `\\@` at this point, with no new expansion.  A
        repetition takes it once for all its iterations (measured, gas
        2.40: `.irp x,a,b` with `i\\@\\x:` and a macro call gives `i0a` and
        `i0b`, and the two calls get 0 and 1)."""
        return str(self.counter) if self.counter_known else f"\\@{self.counter}"

    def _marker(self, piece: str, names: set[str] | None) -> str | None:
        """The statement that marks a directive of structure in a barrier text.

        A conditional gets its value (`.if 1`, `.if 0`, `.if ?`), a
        repetition its count (`.rept 3`, `.rept 0`, `.rept ?`; `.irp` and
        `.irpc` count their values).  The value is not known when the
        directive holds a substitution that was not made, or a name that
        the text can set before it.  None: *piece* is no such directive.
        """
        opened = _IF_OPEN.match(piece)
        if opened is not None:
            return f".if {self._known(opened.group(1).lower(), opened.group(2).strip(), names)}"
        elseif = _IF_ELSEIF.match(piece)
        if elseif is not None:
            return f".elseif {self._known('if', elseif.group(1).strip(), names)}"
        if _IF_ELSE.match(piece):
            return ".else"
        if _IF_END.match(piece):
            return ".endif"
        repeat = _REPEAT_OPEN.match(piece)
        if repeat is not None:
            kind, argument = repeat.group(1).lower(), repeat.group(2).strip()
            if "\\" in argument or (kind == "rept" and self._unknown_in(argument, names)):
                return ".rept ?"
            iterations = _Repetition(kind, argument, [], 0).iterations(self._symbols())
            return ".rept ?" if iterations is None else f".rept {len(iterations)}"
        if _REPEAT_CLOSE.match(piece):
            return ".endr"
        if _EXITM.match(piece):
            return ".exitm"
        return None

    def _known(self, kind: str, argument: str, names: set[str] | None) -> str:
        """`1`, `0` or `?`: the value of a conditional in a barrier text."""
        if self._unknown_in(argument, names):
            return "?"
        taken = self._condition(kind, argument)
        return "?" if taken is None else ("1" if taken else "0")

    @staticmethod
    def _unknown_in(argument: str, names: set[str] | None) -> bool:
        """Whether *argument* holds a value that the text can change."""
        return "\\" in argument or names is None or any(
            name in names for name in _EXPRESSION_NAME.findall(argument)
        )

    def _scan(
        self, body: list[str], seen: frozenset[str] = frozenset(),
        names_before: set[str] | frozenset[str] | None = frozenset(), base: str = "maybe",
    ) -> tuple[bool, BarrierEffects]:
        """Read the text of a refused construct: what can it change?

        Returns (nesting, effects).  *nesting* is True when expanding the
        text can leave a conditional open or closed: an `.else` or
        `.elseif` of a conditional opened before the text, or an `.endif`
        that closes one.  *effects* says what the text can do to the state
        of a reader (`BarrierEffects`).

        A macro that the text invokes is read too, with its arguments when
        they bind.  Anything not known gives the answer "can": a directive
        that a macro argument supplies (`\\dir`), a macro defined inside, an
        `.include`, a recursion.

        *names_before*: the names that a text around this one set before
        it, thus a conditional here has no known value on them.  *base*:
        whether the text runs ("yes": the body of a refused macro, "maybe":
        a branch of no known value, a repetition of no known count), which
        decides how a macro that it invokes counts for `\\@` (`_Runs`).
        """
        depth = 0
        nesting = False
        section = emits = False
        names: set[str] | None = None if names_before is None else set(names_before)
        runs = _Runs(base)
        exports: set[str] | None = set()
        written: list[str] | None = []

        def merge(other: BarrierEffects) -> None:
            nonlocal section, emits, names, exports, written
            section |= other.section
            emits |= other.emits
            names = None if names is None or other.names is None else names | other.names
            exports = (
                None if exports is None or other.exports is None else exports | other.exports
            )
            written = (
                None if written is None or other.text is None else written + list(other.text)
            )

        def write(statement: str) -> None:
            nonlocal emits
            emits = True
            if written is not None:
                written.append(statement.strip())

        for statement in body:
            for _, _, piece in _split_labels((("", 0, statement),)):
                text = piece.strip()
                if not text:
                    continue
                if _NUMERIC_LABEL.match(piece):
                    if written is not None:
                        written.append(text)
                    continue
                label = _LABEL_ONLY.match(piece)
                if label is not None:
                    if names is not None:
                        names.add(label.group(1))
                    if written is not None:
                        written.append(text)
                    continue
                if _SUBSTITUTED_LABEL.match(piece):
                    # A label whose name a substitution gives.  A local label
                    # of the expansion counter (`.Lskip\@:`) names no other
                    # symbol.  Any other one can be any name, a numeric one
                    # too.  The text keeps it as written.
                    if not _COUNTER_LOCAL_LABEL.match(piece):
                        names = None
                    if written is not None:
                        written.append(text)
                    continue
                if text.startswith("\\") or _DEFINE.match(piece) or _INCLUDE.match(piece):
                    return True, FULL
                marker = self._marker(piece, names)
                if marker is not None:
                    runs.feed(marker)
                if marker is not None and written is not None:
                    # The flow reads the branches as branches, a repetition
                    # as one that can run no time, and `.exitm` as the end
                    # of its macro (`_asm_cfg._Structure`).
                    written.append(marker)
                if depth == 0 and (_IF_ELSE.match(piece) or _IF_ELSEIF.match(piece)):
                    nesting = True
                    continue
                if _IF_OPEN.match(piece):
                    depth += 1
                    continue
                if _IF_END.match(piece):
                    depth -= 1
                    if depth < 0:
                        nesting = True
                    continue
                if "\\" in piece.replace("\\@", "") or ("\\@" in piece and text.startswith(".")):
                    # A substitution that was not made is left in place: the
                    # name it makes (`lab\@:`, `.set \name\@, 1`) and what it
                    # writes are not known.  Only the counter in an
                    # instruction (`cbz r0, .Lskip\@`) keeps the text: it
                    # names a label of the same expansion.
                    names = exports = None
                    if _SECTION_SWITCH.match(piece):
                        section = True
                    elif not NO_BYTES.match(piece):
                        emits = True
                        written = None
                    continue
                if _CONDITIONAL.match(piece) or any(
                    pattern.match(piece) for pattern in (
                        _REPEAT_OPEN, _REPEAT_CLOSE, _END, _EXITM, _PURGEM,
                        _ALTMACRO, _NOALTMACRO,
                    )
                ):
                    continue
                if _SECTION_SWITCH.match(piece):
                    section = True
                    continue
                assign = _ASSIGN.match(piece)
                alias = _THUMB_SET.match(piece)
                if assign is not None or alias is not None:
                    name = alias.group(1) if alias is not None else (
                        assign.group(2) or assign.group(3) if assign is not None else ""
                    )
                    if name == ".":
                        # `. = expr` moves the location counter: it writes
                        # bytes (padding), and defines no symbol.
                        write(piece)
                    elif names is not None:
                        names.add(name)
                    continue
                common = _COMMON_NAME.match(piece)
                if common is not None:
                    if names is not None:
                        names.add(common.group(1))
                    write(piece)
                    continue
                export = _EXPORT.match(piece)
                if export is not None:
                    directive = export.group(1).lower()
                    listed = [n.strip() for n in export.group(2).split(",")]
                    # `.type` and `.size` name one symbol; the others a list.
                    exported = listed[:1] if directive in ("type", "size") else listed
                    if directive == "thumb_func" or not all(_PLAIN_NAME.match(n) for n in exported):
                        exports = None
                    elif exports is not None:
                        exports.update(exported)
                    continue
                invocation = _INVOKE.match(piece)
                macro = self.macros.get(invocation.group(1)) if invocation else None
                if macro is not None and invocation is not None:
                    run = runs.status()
                    if macro.name in seen:
                        self.counter_known = self.counter_known and run == "no"
                        return True, FULL
                    values = bind(macro, split_arguments(invocation.group(2)))
                    if values is None:
                        self.counter_known = self.counter_known and run == "no"
                        return True, FULL
                    # gas expands it when this point runs: it counts for
                    # `\@` there.  When that is not sure, or it runs a count
                    # of times that is not known, the count after it is not
                    # known either.
                    counter: str | None = None
                    once = run == "yes" and runs.times() == 1
                    if run != "no":
                        if runs.times() != 1:
                            # Several expansions in one text: no one number
                            # names them all.
                            self.counter_known = False
                        counter = self._count()
                        if not once:
                            self.counter_known = False
                    inner_nesting, inner = self._scan(
                        [substitute(raw, values, counter) for raw in macro.body],
                        seen | {macro.name}, names,
                        "no" if run == "no" else ("yes" if once else "maybe"),
                    )
                    nesting |= inner_nesting
                    # Its own scope: `.exitm` ends it, and its `\@` is
                    # another number than the one of the text around it.
                    merge(BarrierEffects(
                        section=inner.section, emits=inner.emits, names=inner.names,
                        exports=inner.exports,
                        text=None if inner.text is None else (
                            MACRO_OPEN, *inner.text, MACRO_CLOSE
                        ),
                    ))
                    continue
                if not NO_BYTES.match(piece):
                    write(piece)
        self.scan_exits = runs.exited != "no"
        return nesting or depth != 0, BarrierEffects(
            section=section, emits=emits,
            names=frozenset(names) if names is not None else None,
            exports=frozenset(exports) if exports is not None else None,
            text=tuple(written) if written is not None else None,
        )

    def _invoke(
        self, macro: Macro, argument_text: str, at: Located, depth: int
    ) -> Iterator[Located]:
        """Yield what one macro invocation produces."""
        if self.alternate:
            # gas expands it: it counts for `\@`.
            self._count()
            yield from self._refuse("alternate macro mode", at, macro.body)
            return
        if macro.refuse_reason:
            # The arguments bind: the text is the body with them in place.
            # gas expands it, thus it counts for `\@` as an expansion does.
            bound = bind(macro, split_arguments(argument_text))
            counter = None if bound is None else self._count()
            text = macro.body if bound is None else [
                substitute(raw, bound, counter) for raw in macro.body
            ]
            yield from self._refuse(macro.refuse_reason, at, text)
            return
        if depth + 1 > MAX_DEPTH:
            self._count()
            yield from self._refuse("recursion deeper than the limit", at, macro.body)
            return
        values = bind(macro, split_arguments(argument_text))
        if values is None:
            yield from self._refuse("arguments do not match the parameters", at, macro.body)
            return

        counter = self._count()
        self.report.expanded += 1
        file, line, _ = at

        def body() -> Iterator[Located]:
            nested = 0
            for raw in macro.body:
                statement = substitute(raw, values, counter)
                # `.exitm` in a repetition of the body ends that repetition
                # (`_repeat`), not the macro (measured, gas 2.40).
                if _REPEAT_OPEN.match(statement):
                    nested += 1
                elif _REPEAT_CLOSE.match(statement):
                    nested -= 1
                elif _EXITM.match(statement) and nested == 0:
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
        the repetition is refused.  `.exitm` in the body ends all of its
        iterations (measured, gas 2.40).
        """
        bindings = repetition.iterations(self._symbols())
        if bindings is None:
            yield from self._refuse(
                f".{repetition.kind} this cannot evaluate", at,
                [statement for _, _, statement in repetition.body], skippable=True,
            )
            return
        if depth + 1 > MAX_DEPTH:
            yield from self._refuse(
                "recursion deeper than the limit", at,
                [statement for _, _, statement in repetition.body],
            )
            return
        self.report.repeated += 1
        # One value of `\@` for every iteration (`_current`).
        counter = self._current()
        exit_state = _RepetitionExit()
        frames_before = len(self.frames)

        def body() -> Iterator[Located]:
            # `.exitm` ends all the iterations (measured, gas 2.40).  The
            # expander tells it when it takes one (`process`), thus an
            # `.exitm` in a branch that does not run ends nothing.
            self.repetition_exits.append(exit_state)
            try:
                for number, values in enumerate(bindings):
                    for index, (file, line, raw) in enumerate(repetition.body):
                        if exit_state.state == "yes":
                            # gas also leaves the conditionals of the body.
                            del self.frames[frames_before:]
                            return
                        if exit_state.state == "maybe":
                            rest = [
                                substitute(statement, values, counter)
                                for _, _, statement in repetition.body[index:]
                            ] + [
                                substitute(statement, later, counter)
                                for later in bindings[number + 1:]
                                for _, _, statement in repetition.body
                            ]
                            yield from self._rest_after_exit(rest, (file, line, raw), frames_before)
                            return
                        # The location is the body statement's own.  Unlike a
                        # macro body, this one is right here in the file, so
                        # the more precise answer is available.
                        yield file, line, substitute(raw, values, counter)
            finally:
                self.repetition_exits.remove(exit_state)

        yield from self.process(body(), depth + 1)

    def _rest_after_exit(
        self, rest: list[str], at: Located, frames_before: int
    ) -> Iterator[Located]:
        """Leave a barrier for the rest of a repetition that an `.exitm` of
        a refused text can end: the assembler takes all of it, or none."""
        self.report.refused["repetition after an .exitm of no known branch"] += 1
        nesting, effects = self._scan(rest)
        if nesting:
            self.stopped = True
            self.report.refused["rest of the unit after it"] += 1
        del self.frames[frames_before:]
        yield from self._barrier(at, optional(effects))


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
