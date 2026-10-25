"""The flow of control of an assembly unit: its routines and their calls.

In assembly a function has no extent: there are labels and instructions.
Which routine a call belongs to follows from where control can go, not
from where the call is written.  A trampoline written after one routine
can be reached only by a jump from another one, and a shared tail can be
reached from several.  Measured (probe s11 of the call edge plan), the
position alone broke the path from a vector to C in three of six families
of startup code.

The model, as disassemblers find functions:

* A routine starts at a label that code can enter from elsewhere: an entry
  that the unit exports or types as code (`_asm_layout._Entries`), the
  target of a call (a subroutine), and a label whose address the unit
  takes (`ldr r0, =X`, `la t0, X`, `.word X`, an alias).  Labels at one
  place (no byte between them) are one routine with several names.
* The body of a routine is the code that its start reaches: by falling
  through to the next instruction, and by a jump to a label that starts no
  routine.  A call returns, unless it calls a routine of the unit that can
  never return (no path of it reaches a return).  The code of a call to a
  label with no symbol (`.L`, `1f`) is part of the body too.  The flow
  continues after such a call only when that code comes back: through
  the link register, through a link that it pushed itself, or through a
  tail call that returns.  A return through a link that it did not push
  (the far branch of Thumb-1) is a return of the routine, and the end of
  an exception is one in any code.
* The edges of a routine: each call; a jump to the start of another
  routine or to a symbol outside the unit (a tail call); falling through
  into the start of another routine that holds code.  A jump to a label
  that starts no routine is no edge: that code is part of the body.  Code
  that several routines reach belongs to each of them.

What this does not claim: the code of a construct that the reader refused
gives no edge (it may not be in the binary), and control passes such a
construct only when a path through its statements reaches its end.  When
a path can leave it (a return, a jump out of it), the routine can return
there.  A
jump whose target is not known (a table branch) ends the flow, and so does
a jump to its own place (`b .`).  A call to a routine outside the unit
returns, as the instruction set says.  Code in a subsection, a section
group or a `unique` section gets no fall through from code in another
piece of it: the order of the pieces in the object is not tracked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._asm_isa import (
    EXCEPTION,
    STACK,
    Decoded,
    Load,
    Move,
    RestoreLink,
    Return,
    SaveLink,
    Transfer,
    decode,
)
from ._asm_layout import (
    _DATA_DIRECTIVE,
    _LOCAL_LABEL_PREFIX,
    _SUBSECTION_RE,
    _Entries,
    _SectionTracker,
)
from ._asm_macro import (
    _IF_OPEN,
    _SUBSTITUTED_LABEL,
    MACRO_OPEN,
    BarrierEffects,
    barrier_effects,
)

Located = tuple[str, int, str]

_NUMERIC_LABEL = re.compile(r"^\s*(\d+)\s*:\s*$")
_NUMERIC_REFERENCE = re.compile(r"^(\d+)([fb])$")
_ALIAS = re.compile(
    r"^\s*\.(?:thumb_set|set|equ)\s+[A-Za-z_.$][\w.$]*\s*,\s*([A-Za-z_.$][\w.$]*)\s*$",
    re.IGNORECASE,
)
_NAME = re.compile(r"^[A-Za-z_.$][\w.$]*$")
# A label of the flow: also one whose name holds the mark of an expansion
# count that is not known (`skip\@3`, `_asm_macro._Expander._count`).
# Such a name is no symbol: it starts no routine and gets no edge.
_FLOW_LABEL = re.compile(r"^\s*([A-Za-z_.$](?:[\w.$]|\\@)*)\s*:")
_COUNTER_MARK = "\\@"
# The directives that put bytes in the stream of a section, from the manual
# of gas (and the literal pools of ARM, `.ltorg` and `.pool`).  The flow
# cannot run through data.  Any other directive writes nothing here: it
# names a symbol, sets an option (`.option norelax`), or writes into
# another section (`.literal` of Xtensa).
_DATA = re.compile(
    r"^\s*(?:\.(?:byte|2byte|4byte|8byte|short|hword|word|int|long|quad|octa"
    r"|ascii|asciz|string|string8|string16|string32|string64|space|skip|zero|fill"
    r"|float|single|double|incbin|ltorg|pool|org|sleb128|uleb128|dc(?:\.\w)?|ds(?:\.\w)?)"
    r"|DC[BDWQ]|SPACE)\b",
    re.IGNORECASE,
)
# A directive that writes one instruction (its encoding as a number).
_INSTRUCTION_DIRECTIVE = re.compile(r"^\s*\.(?:inst|insn)(?:\.\w)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class AsmCall:
    """A call edge of an assembly unit.

    Attributes:
        owners: The names of the routine that makes it (several when
            labels at one place start it).
        target: The symbol it reaches.
        file: The file of the instruction, or of the label that the flow
            falls into.
        line: Its line.
        call: True for a call (it returns), False for a jump or a fall
            through.  Each gives an edge of kind "call", as a tail call of C
            does.
        fall: True when control falls into the label *target* of this
            unit.  No link step redirects a fall: it reaches this label
            even when the label is weak and a strong definition wins the
            link.
    """

    owners: tuple[str, ...]
    target: str
    file: str
    line: int
    call: bool
    fall: bool = False


@dataclass
class _Item:
    """One statement of a section that matters to the flow."""

    kind: str  # "label", "instr", "barrier", "data"
    file: str
    line: int
    ordinal: int
    name: str | None = None
    numeric: str | None = None
    decoded: Decoded = None
    effects: BarrierEffects | None = None


@dataclass
class _Layout:
    """The statements of a unit in the order of each section."""

    sections: dict[str, list[_Item]] = field(default_factory=dict)
    positions: dict[str, tuple[str, int]] = field(default_factory=dict)
    numeric: list[tuple[int, str, str, int]] = field(default_factory=list)
    # The ordinal of each barrier and the numeric labels its text can
    # define (None: any).  A `1b` or `1f` across such a barrier can name a
    # label of the construct, thus its target is not known.
    barriers: list[tuple[int, frozenset[str] | None]] = field(default_factory=list)
    called: set[str] = field(default_factory=set)
    address_taken: set[str] = field(default_factory=set)
    hidden: set[str] | None = field(default_factory=set)


def _layout(statements: list[Located], isa: str) -> _Layout:
    """Sort the statements of a unit into the sections that hold them."""
    layout = _Layout()
    tracker = _SectionTracker()
    unknown = 0
    key = ".text"
    # The sections that hold more than one location counter in this unit:
    # a subsection, a group or `unique`.  Each piece of code there gets its
    # own key, because the reader does not track the order of the pieces.
    split: set[str] = set()
    # A barrier that can switch the section can also put code in any
    # section that the unit used before it.  The code after it then does
    # not follow the code before it: each section gets a new key.
    generation = 0

    def items() -> list[_Item]:
        return layout.sections.setdefault(key, [])

    for ordinal, (file, line, stmt) in enumerate(statements):
        if _SUBSECTION_RE.match(stmt):
            if tracker.current is not None:
                split.add(tracker.current)
            tracker.subsection()
            unknown += 1
            key = f"?{unknown}"
            continue
        move = tracker.feed(stmt)
        if move is not None:
            if tracker.current is not None and not move.plain:
                split.add(tracker.current)
            if tracker.current is None or tracker.current in split:
                unknown += 1
                key = f"?{unknown}"
            else:
                key = tracker.current if generation == 0 else f"{tracker.current}#{generation}"
            continue
        effects = barrier_effects(stmt)
        if effects is not None:
            items().append(_Item("barrier", file, line, ordinal, effects=effects))
            layout.barriers.append((ordinal, _barrier_numbers(effects)))
            if effects.names is None or layout.hidden is None:
                layout.hidden = None
            else:
                layout.hidden |= effects.names
            if effects.section:
                tracker.lose()
                generation += 1
                unknown += 1
                key = f"?{unknown}"
            continue
        label = _FLOW_LABEL.match(stmt)
        if label is not None:
            name = label.group(1)
            layout.positions.setdefault(name, (key, len(items())))
            items().append(_Item("label", file, line, ordinal, name=name))
            continue
        numeric = _NUMERIC_LABEL.match(stmt)
        if numeric is not None:
            layout.numeric.append((ordinal, numeric.group(1), key, len(items())))
            items().append(_Item("label", file, line, ordinal, numeric=numeric.group(1)))
            continue
        text = stmt.strip()
        if not text:
            continue
        if text.startswith("."):
            data = _DATA_DIRECTIVE.match(stmt)
            if data is not None:
                layout.address_taken.update(
                    value.strip() for value in data.group(2).split(",")
                    if _NAME.match(value.strip())
                )
            alias = _ALIAS.match(stmt)
            if alias is not None:
                layout.address_taken.add(alias.group(1))
            if _INSTRUCTION_DIRECTIVE.match(stmt):
                # Its effect on the flow is not decoded: it runs on.
                items().append(_Item("instr", file, line, ordinal))
            elif _DATA.match(stmt):
                items().append(_Item("data", file, line, ordinal))
            # Alignment in code pads with instructions that run on.
            continue
        decoded = decode(isa, stmt)
        if isinstance(decoded, Transfer) and decoded.call and decoded.target is not None:
            if decoded.target != ".":
                layout.called.add(decoded.target)
        if isinstance(decoded, Load):
            layout.address_taken.add(decoded.target)
        items().append(_Item("instr", file, line, ordinal, decoded=decoded))
    return layout


@dataclass
class _Frame:
    """One structure of a barrier text: a conditional, a repetition or a macro.

    Attributes:
        kind: "if", "rept" or "macro".
        start: The index of its opening directive.
        end: The index of its closing directive.
        branches: For a conditional, the directive and the value of each
            branch (`.if`, `.elseif`, `.else`; `.else` has "1").
        value: For a repetition: "1" (it runs), "0" or "?".
    """

    kind: str
    start: int
    end: int = -1
    branches: list[tuple[int, str]] = field(default_factory=list)
    value: str = "1"


class _Structure:
    """The conditionals, repetitions and macros of a barrier text.

    The text holds them as markers with their values (`_asm_macro._marker`):
    `.if 1`, `.if 0`, `.if ?`, `.elseif …`, `.else`, `.endif`, `.rept 1`
    (it runs one time or more), `.rept 0`, `.rept ?`, `.endr`, `.exitm`,
    and `.macro ?` … `.endm` around the text of a macro that it invokes.
    From them the flow knows which branches can run, that a repetition can
    run no time, where `.exitm` goes, and which labels exist when a
    reference resolves.  Unmatched markers give no structure: the text is
    then read as one sequence.
    """

    def __init__(self, text: tuple[str, ...]) -> None:
        self.text = text
        self.frames: dict[int, _Frame] = {}
        self.chain: list[tuple[tuple[int, int], ...]] = [() for _ in text]
        self.scope: list[int] = [-1 for _ in text]
        # Each `.exitm` and the structure it ends: the repetition or the
        # macro text around it (gas 2.40), None for the whole text.
        self.exits: dict[int, _Frame | None] = {}
        if not self._parse():
            self.frames = {}
            self.chain = [() for _ in text]
            self.scope = [-1 for _ in text]
            self.exits = {}
        self.status = self._statuses()

    def _parse(self) -> bool:
        stack: list[_Frame] = []
        chain: list[tuple[int, int]] = []
        for at, statement in enumerate(self.text):
            word, _, value = statement.strip().partition(" ")
            word = word.lower()
            value = value.strip()
            self.scope[at] = next(
                (frame.start for frame in reversed(stack) if frame.kind == "macro"), -1
            )
            if word in (".elseif", ".else", ".endif", ".endr", ".endm"):
                expected = {".endr": "rept", ".endm": "macro"}.get(word, "if")
                if not stack or stack[-1].kind != expected:
                    return False
            if _IF_OPEN.match(statement):
                self.chain[at] = tuple(chain)
                frame = _Frame("if", at, branches=[(at, value if value in ("0", "1") else "?")])
                stack.append(frame)
                chain.append((at, 0))
            elif word in (".elseif", ".else"):
                frame = stack[-1]
                frame.branches.append((at, "1" if word == ".else" else (
                    value if value in ("0", "1") else "?"
                )))
                chain[-1] = (frame.start, len(frame.branches) - 1)
                self.chain[at] = tuple(chain[:-1])
            elif word in (".endif", ".endr"):
                frame = stack.pop()
                frame.end = at
                self.frames[frame.start] = frame
                chain.pop()
                self.chain[at] = tuple(chain)
            elif word == ".rept":
                self.chain[at] = tuple(chain)
                count = int(value) if value.isdigit() else None
                stack.append(_Frame("rept", at, value="?" if count is None else str(min(count, 1))))
                chain.append((at, 0))
            elif statement.strip() == MACRO_OPEN:
                self.chain[at] = tuple(chain)
                stack.append(_Frame("macro", at))
            elif word == ".endm":
                frame = stack.pop()
                frame.end = at
                self.frames[frame.start] = frame
                self.chain[at] = tuple(chain)
            else:
                self.chain[at] = tuple(chain)
                if word == ".exitm":
                    self.exits[at] = next(
                        (frame for frame in reversed(stack) if frame.kind in ("rept", "macro")),
                        None,
                    )
        return not stack

    def _statuses(self) -> dict[tuple[int, int], str]:
        """"taken", "dead" or "maybe" for each branch of each structure."""
        status: dict[tuple[int, int], str] = {}
        for frame in self.frames.values():
            if frame.kind == "rept":
                status[(frame.start, 0)] = {"1": "taken", "0": "dead"}.get(frame.value, "maybe")
                continue
            certain = True
            done = False
            for number, (_, value) in enumerate(frame.branches):
                if done or value == "0":
                    status[(frame.start, number)] = "dead"
                    continue
                status[(frame.start, number)] = "taken" if value == "1" and certain else "maybe"
                certain = False
                done = value == "1"
        return status

    def successors(self, at: int) -> list[int] | None:
        """Where control goes from the marker at *at*; None for any other statement."""
        statement = self.text[at].strip()
        word = statement.partition(" ")[0].lower()
        frame = self.frames.get(at)
        if frame is not None and frame.kind == "if":
            found = []
            for directive, value in frame.branches:
                if value == "0":
                    continue
                found.append(directive + 1)
                if value == "1":
                    # This branch or one before it runs: none goes around.
                    break
            else:
                found.append(frame.end)
            return found
        if frame is not None and frame.kind == "rept":
            return {"1": [at + 1], "0": [frame.end]}.get(frame.value, [at + 1, frame.end])
        if word in (".elseif", ".else"):
            # The end of a branch goes to the `.endif`.
            owner = next((
                frame for frame in self.frames.values()
                if frame.kind == "if" and any(directive == at for directive, _ in frame.branches)
            ), None)
            # No conditional of its own (the text was read as one sequence):
            # it runs on.
            return None if owner is None else [owner.end]
        if word == ".exitm" and at in self.exits:
            # The end of its repetition or macro text; at the top, the
            # end of the text.
            return [self._exit_target(at)]
        return None

    def _exit_target(self, at: int) -> int:
        """Where the `.exitm` at *at* goes: the closing directive of what it ends."""
        frame = self.exits.get(at)
        return len(self.text) if frame is None else frame.end

    def dead(self, at: int) -> bool:
        """Whether the statement at *at* is in a branch that never runs."""
        return any(self.status.get(link) == "dead" for link in self.chain[at])

    def key(self, name: str, at: int) -> str:
        """The key of the label *name* seen from *at*: a name that holds the
        expansion counter (`\\@`) is one in each macro text."""
        return f"{self.scope[at]}:{name}" if "\\@" in name else name

    def resolve(
        self, at: int, number: str, forward: bool, numbers: list[tuple[int, str]],
    ) -> tuple[list[int], bool]:
        """The labels of the text that `<number>f` or `<number>b` at *at* can
        name, and whether it can also name a label outside the text.

        gas resolves it to the nearest label of the number that it
        assembled.  A label in a branch that does not run with *at* is
        never that one; a label that may not run lets the reference go on
        to the next one.
        """
        places = [place for place, name in numbers if name == number]
        candidates = [place for place in places if place > at] if forward else [
            place for place in reversed(places) if place < at
        ]
        found: list[int] = []
        for place in candidates:
            relation = self._relation(at, place)
            if relation == "never":
                continue
            found.append(place)
            if relation == "always":
                return found, False
        return found, True

    def _relation(self, at: int, place: int) -> str:
        """Whether the label at *place* is assembled when *at* runs."""
        mine = dict(self.chain[at])
        certain = True
        for frame, branch in self.chain[place]:
            status = self.status.get((frame, branch), "taken")
            if status == "dead":
                return "never"
            if frame in mine:
                if mine[frame] != branch:
                    return "never"
                continue
            if status != "taken":
                certain = False
        for exit_at in self.exits:
            # `.exitm` between them can end the text that holds the label,
            # an inner macro text too.
            if at < exit_at < place < self._exit_target(exit_at) and not self.dead(exit_at):
                certain = False
        return "always" if certain else "maybe"


def _label_name(statement: str) -> str | None:
    """The name of a label statement that is not numeric, else None."""
    if _NUMERIC_LABEL.match(statement) or not _SUBSTITUTED_LABEL.match(statement):
        return None
    return statement.strip()[:-1].strip()


def _barrier_numbers(effects: BarrierEffects) -> frozenset[str] | None:
    """The numeric labels that a refused construct can define; None: any.

    A label in a branch that never runs is not defined.  A label that a
    substitution names can be any number when its name can start with a
    digit: `1\\@:` when the count is not known, or `\\name:`.
    `.Lskip\\@:` cannot.
    """
    if effects.text is None:
        return None
    structure = _Structure(effects.text)
    numbers: set[str] = set()
    for at, statement in enumerate(effects.text):
        if structure.dead(at):
            continue
        numeric = _NUMERIC_LABEL.match(statement)
        if numeric is not None:
            numbers.add(numeric.group(1))
        elif _SUBSTITUTED_LABEL.match(statement) and (
            statement.lstrip()[:1].isdigit() or statement.lstrip().startswith("\\")
        ):
            return None
    return frozenset(numbers)


def _barrier_flow(effects: BarrierEffects, isa: str) -> tuple[bool, bool]:
    """How control goes through a barrier: (it passes, it can leave elsewhere).

    It passes when a path through its text reaches its end.  The text is a
    small flow of its own (`_Structure`).  A jump to a label of the text
    stays in it: the loop of a dispatch macro (`bnez a2, 1b`, `j 9f`) ends
    at its own label.  A path ends at data, at a return, at a jump whose
    target is not known or is not in the text, and at a jump to its own
    place.  A conditional transfer also runs on.  A loop that never
    reaches the end thus stops the flow, as a return does.

    It can leave elsewhere when a path reaches a return or a jump out of
    the text: the routine can then return there.  A text that is not
    known, or that can switch the section, can do anything.
    """
    if effects.section or effects.text is None:
        return False, True
    text = effects.text
    structure = _Structure(text)
    labels: dict[str, int] = {}
    numbers: list[tuple[int, str]] = []
    for at, statement in enumerate(text):
        numeric = _NUMERIC_LABEL.match(statement)
        if numeric is not None:
            numbers.append((at, numeric.group(1)))
            continue
        name = _label_name(statement)
        if name is not None and not structure.dead(at):
            labels.setdefault(structure.key(name, at), at)
    reached = {0}
    work = [0]
    passes = leaves = False
    while work:
        at = work.pop()
        if at == len(text):
            passes = True
            continue
        following, out = _successors(structure, at, isa, labels, numbers)
        leaves = leaves or out
        for place in following:
            if place not in reached:
                reached.add(place)
                work.append(place)
    return passes, leaves


def _successors(
    structure: _Structure, at: int, isa: str,
    labels: dict[str, int], numbers: list[tuple[int, str]],
) -> tuple[list[int], bool]:
    """The statements of the text that control can reach next from the one
    at *at*, and whether control can also leave the text there."""
    marked = structure.successors(at)
    if marked is not None:
        return marked, False
    statement = structure.text[at]
    following = at + 1
    if _SUBSTITUTED_LABEL.match(statement):
        return [following], False
    if _DATA.match(statement):
        return [], False
    if statement.lstrip().startswith("."):
        # An option, or an instruction written as a number: it runs on.
        return [following], False
    decoded = decode(isa, statement)
    if isinstance(decoded, Return):
        return ([following] if decoded.conditional else []), True
    if not isinstance(decoded, Transfer) or decoded.call:
        return [following], False
    found = [following] if decoded.conditional else []
    if decoded.target == ".":
        return found, False
    if decoded.target is None:
        return found, True
    reference = _NUMERIC_REFERENCE.match(decoded.target)
    if reference is not None:
        number, direction = reference.groups()
        inside, out = structure.resolve(at, number, direction == "f", numbers)
        return found + inside, out
    place = labels.get(structure.key(decoded.target, at))
    if place is None:
        return found, True
    return [*found, place], False


def _holds_code(effects: BarrierEffects | None) -> bool:
    """Whether a refused construct can write an instruction (not only data)."""
    if effects is None or effects.text is None:
        return True
    for statement in effects.text:
        if _SUBSTITUTED_LABEL.match(statement):
            continue
        if not statement.lstrip().startswith(".") or _INSTRUCTION_DIRECTIVE.match(statement):
            return True
    return False


def _writes_bytes(effects: BarrierEffects | None) -> bool:
    """Whether a refused construct can write a byte (code or data).

    A text of labels and directives that write nothing (an option, an
    alignment, as the layout reads them) leaves the labels around it at
    one place.
    """
    return _holds_code(effects) or (
        effects is not None and effects.text is not None
        and any(_DATA.match(statement) for statement in effects.text)
    )


# How the code of a frame can end (`_Walk.ends`).  A frame is a routine,
# or the code of a call to a label with no symbol (a subroutine).
_BACK = "back"  # to the call of the frame: a return through the link
_STACK_OUT = "stack"  # through a link that the frame did not push: the
#                       link of the frame that called it
_EXCEPTION = "exception"  # the end of a handler
_UNKNOWN = "unknown"  # a jump whose target is not known: any of them


@dataclass
class _Walk:
    """What the walk of one frame found."""

    edges: list[AsmCall] = field(default_factory=list)
    ends: set[str] = field(default_factory=set)
    tails: set[str] = field(default_factory=set)
    reached: dict[str, bool] = field(default_factory=dict)

    def edge(self, call: AsmCall) -> None:
        """Keep *call* once: the walk can reach one statement twice, with and
        without a link on the stack."""
        if call not in self.edges:
            self.edges.append(call)


# A place to walk from: the section key, the index of the item, whether
# the start of the routine reaches it by falling through, and whether the
# frame pushed its link before it (`SaveLink`).
_Place = tuple[str, int, bool, bool]


@dataclass(frozen=True)
class UnitFlow:
    """The routines of a unit and their calls.

    Attributes:
        calls: The call edges (`AsmCall`).
        roots: The labels that start a routine.
        code_roots: The roots that code follows: a function, not data whose
            address the unit takes.
        parents: For each label that starts no routine, the routine whose
            code holds it, or None.  That is the routine that reaches it by
            falling through from its start, else the only routine that
            reaches it.
    """

    calls: list[AsmCall]
    roots: frozenset[str]
    code_roots: frozenset[str]
    parents: dict[str, str | None]


class _Flow:
    """Builds the routines of one unit (see the module docstring)."""

    def __init__(self, statements: list[Located], isa: str, entries: _Entries) -> None:
        self.isa = isa
        self.layout = _layout(statements, isa)
        labels = set(self.layout.positions)
        maybe = labels if entries.maybe is None else entries.maybe & labels
        # A label whose export a refused construct can hold starts a
        # routine too: it can be entered from outside.  Its code then has
        # its own name, and a routine that falls into it keeps the path
        # through the edge of the fall.
        self.roots = {
            name for name in labels
            if not name.startswith(_LOCAL_LABEL_PREFIX) and _COUNTER_MARK not in name and (
                name in entries.names or name in maybe
                or name in self.layout.called or name in self.layout.address_taken
            )
        }
        self.groups = self._groups()
        self.code_roots = self._code_roots()
        # The walk of each subroutine, for the routine that calls it; None
        # while it runs.  Valid for one set of routines that never return.
        self.subroutines: dict[tuple[tuple[str, ...], tuple[str, int]], _Walk | None] = {}

    def _groups(self) -> dict[str, tuple[str, ...]]:
        """Map each root to the roots at its place (no byte between them)."""
        groups: dict[str, tuple[str, ...]] = {}
        for items in self.layout.sections.values():
            run: list[str] = []
            for item in [*items, None]:
                if item is not None and item.kind == "label":
                    if item.name in self.roots:
                        run.append(item.name)
                    continue
                if item is not None and item.kind == "barrier" and not _writes_bytes(item.effects):
                    continue
                if run:
                    group = tuple(run)
                    for name in run:
                        groups[name] = group
                run = []
        return groups

    def _numeric_target(self, reference: str, ordinal: int) -> tuple[str, int] | None:
        """The place of `1f` or `1b` from the statement with *ordinal*.

        None when no label matches, or when a barrier between the two can
        define the number: the label of the construct is then the nearer one.
        """
        found = _NUMERIC_REFERENCE.match(reference)
        if found is None:
            return None
        number, direction = found.groups()
        forward = direction == "f"
        candidates = [
            (at, key, index) for at, name, key, index in self.layout.numeric
            if name == number and (at > ordinal if forward else at < ordinal)
        ]
        nearest = (candidates[0] if forward else candidates[-1]) if candidates else None
        for at, numbers in self.layout.barriers:
            if numbers is not None and number not in numbers:
                continue
            if forward and ordinal < at and (nearest is None or at < nearest[0]):
                return None
            if not forward and at < ordinal and (nearest is None or at > nearest[0]):
                return None
        if nearest is None:
            return None
        return nearest[1], nearest[2]

    def walk(self, group: tuple[str, ...], noreturn: set[tuple[str, ...]]) -> _Walk:
        """Find the body of the routine *group*: its edges and how it ends."""
        return self._frame(group, self.layout.positions[group[0]], noreturn, textual=True)

    def _frame(
        self, group: tuple[str, ...], start: tuple[str, int],
        noreturn: set[tuple[str, ...]], textual: bool,
    ) -> _Walk:
        """Walk one frame of *group* from *start*: the routine or a subroutine."""
        result = _Walk()
        work: list[_Place] = [(*start, textual, False)]
        visited: set[tuple[str, int, bool]] = set()
        while work:
            self._walk_from(group, work.pop(), noreturn, visited, work, result)
        return result

    def _subroutine(
        self, group: tuple[str, ...], start: tuple[str, int], noreturn: set[tuple[str, ...]],
    ) -> _Walk:
        """The walk of the subroutine at *start* that *group* calls.

        A subroutine that calls itself (also through another one) reads,
        inside that cycle, as one that comes back: the walk of the cycle
        is not done yet when it asks.
        """
        key = (group, start)
        if key in self.subroutines:
            return self.subroutines[key] or _Walk(ends={_BACK})
        self.subroutines[key] = None
        found = self._frame(group, start, noreturn, textual=False)
        self.subroutines[key] = found
        return found

    @staticmethod
    def _end(decoded: Return, saved: bool) -> str:
        """How the frame ends at *decoded*, when its link is on the stack or not."""
        if decoded.via == EXCEPTION:
            return _EXCEPTION
        if decoded.via == STACK and not saved:
            # The stack holds the link of the frame that called this one.
            return _STACK_OUT
        return _BACK

    def _walk_from(
        self, group: tuple[str, ...], place: _Place,
        noreturn: set[tuple[str, ...]], visited: set[tuple[str, int, bool]],
        work: list[_Place], result: _Walk,
    ) -> None:
        key, index, textual, saved = place
        items = self.layout.sections[key]
        loads: dict[str, str] = {}
        hidden = self.layout.hidden
        while index < len(items):
            if (key, index, saved) in visited:
                return
            visited.add((key, index, saved))
            item = items[index]
            index += 1
            if item.kind == "label":
                loads.clear()
                name = item.name
                if name is not None and name in self.roots and self.groups.get(name) != group:
                    # Control falls into the start of another routine.  When
                    # that start holds data, the flow ends there.
                    if name in self.code_roots:
                        result.edge(AsmCall(
                            group, name, item.file, item.line, call=False, fall=True,
                        ))
                        result.tails.add(name)
                    return
                if name is not None:
                    result.reached[name] = result.reached.get(name, False) or textual
                continue
            if item.kind == "data":
                return
            if item.kind == "barrier":
                passes, leaves = (
                    (False, True) if item.effects is None else _barrier_flow(item.effects, self.isa)
                )
                if leaves:
                    result.ends.add(_UNKNOWN)
                if not passes:
                    return
                loads.clear()
                continue
            decoded = item.decoded
            if isinstance(decoded, (SaveLink, RestoreLink)):
                after = isinstance(decoded, SaveLink)
                if decoded.conditional and after != saved:
                    # The stack holds the link on one path only: walk both.
                    work.append((key, index, textual, saved))
                saved = after
                if not after:
                    # The pop writes the registers of its list too.
                    loads.clear()
                continue
            if isinstance(decoded, Load):
                loads[decoded.register] = decoded.target
                continue
            if isinstance(decoded, Move):
                loads.pop(decoded.register, None)
                continue
            if isinstance(decoded, Return):
                result.ends.add(self._end(decoded, saved))
                if not decoded.conditional:
                    return
                continue
            if not isinstance(decoded, Transfer):
                loads.clear()
                continue
            target = decoded.target
            if target is None and decoded.register is not None:
                target = loads.get(decoded.register)
            loads.clear()
            if decoded.call:
                if self._call(group, item, target, saved, noreturn, result):
                    continue
                return
            if not self._jump(group, item, target, decoded, saved, work, result, hidden):
                return

    def _call(
        self, group: tuple[str, ...], item: _Item, target: str | None, saved: bool,
        noreturn: set[tuple[str, ...]], result: _Walk,
    ) -> bool:
        """Record a call.  Returns whether the flow continues after it."""
        if target is None:
            return True
        if target == ".":
            # A call of its own place calls itself for ever.
            return False
        start = None
        if _NUMERIC_REFERENCE.match(target):
            # A call of a numeric local label: an anonymous subroutine.
            start = self._numeric_target(target, item.ordinal)
            if start is None:
                return True
        elif target in self.layout.positions and target not in self.roots:
            # A call of a label that starts no routine: a `.L` subroutine
            # with no symbol.
            start = self.layout.positions[target]
        if start is not None:
            return self._call_subroutine(group, start, saved, noreturn, result)
        hidden = self.layout.hidden
        if target not in self.layout.positions and (
            hidden is None or target in hidden or target.startswith(_LOCAL_LABEL_PREFIX)
            or _COUNTER_MARK in target
        ):
            return True
        result.edge(AsmCall(group, target, item.file, item.line, call=True))
        callee = self.groups.get(target)
        return callee is None or callee not in noreturn

    def _call_subroutine(
        self, group: tuple[str, ...], start: tuple[str, int], saved: bool,
        noreturn: set[tuple[str, ...]], result: _Walk,
    ) -> bool:
        """Take the code of a subroutine into the frame that calls it.

        Its edges are edges of the routine.  Its ends decide the flow:
        control comes back after the call through a return of the link, a
        tail call, or a routine of the unit that returns.  A return through
        the stack without its own push reads the link of this frame: for
        this frame it is a return through the stack.  The far branch of
        Thumb-1 (`bl .Lfar` to code that ends with `pop {pc}`) is such a
        call: the routine returns there, and nothing comes after the call.
        """
        called = self._subroutine(group, start, noreturn)
        for edge in called.edges:
            result.edge(edge)
        for name in called.reached:
            result.reached.setdefault(name, False)
        back = any(self.groups.get(tail) not in noreturn for tail in called.tails)
        for end in called.ends:
            if end == _STACK_OUT:
                result.ends.add(_BACK if saved else _STACK_OUT)
            elif end == _BACK:
                back = True
            else:
                # The end of a handler, or a jump to a place that is not
                # known: it can also come back.
                result.ends.add(end)
                back = back or end == _UNKNOWN
        return back

    def _jump(
        self, group: tuple[str, ...], item: _Item, target: str | None, decoded: Transfer,
        saved: bool, work: list[_Place], result: _Walk, hidden: set[str] | None,
    ) -> bool:
        """Follow a jump.  Returns whether the flow continues after it.

        A tail call goes back to the call of the frame when its target
        returns.  A jump whose target is not known can go to any place.
        """
        if target == ".":
            # A jump to its own place loops for ever: no edge, no return.
            return decoded.conditional
        place = None
        if target is not None:
            place = self.layout.positions.get(target) or self._numeric_target(target, item.ordinal)
        if target is None or (place is None and _NUMERIC_REFERENCE.match(target)):
            result.ends.add(_UNKNOWN)
        elif place is not None and (target not in self.roots or self.groups.get(target) == group):
            # Code of this frame (the start of the routine included).
            work.append((*place, False, saved))
        elif place is not None:
            result.edge(AsmCall(group, target, item.file, item.line, call=False))
            result.tails.add(target)
        elif hidden is not None and target not in hidden and not target.startswith(
            _LOCAL_LABEL_PREFIX
        ) and _COUNTER_MARK not in target:
            # A symbol outside the unit: a tail call, which can return.
            result.edge(AsmCall(group, target, item.file, item.line, call=False))
            result.ends.add(_BACK)
        else:
            result.ends.add(_UNKNOWN)
        return decoded.conditional

    def build(self) -> UnitFlow:
        """Walk each routine until the routines that never return are known."""
        groups = sorted(set(self.groups.values()))
        noreturn: set[tuple[str, ...]] = set()
        while True:
            self.subroutines = {}
            walks = {group: self.walk(group, noreturn) for group in groups}
            returns = {group for group, walk in walks.items() if walk.ends}
            changed = True
            while changed:
                changed = False
                for group, walk in walks.items():
                    if group in returns:
                        continue
                    if any(self.groups.get(tail) in returns for tail in walk.tails):
                        returns.add(group)
                        changed = True
            found = set(groups) - returns
            if found == noreturn:
                break
            noreturn = found
        calls = [edge for group in groups for edge in walks[group].edges]
        return UnitFlow(
            calls=calls, roots=frozenset(self.roots), code_roots=self.code_roots,
            parents=self._parents(walks),
        )

    def _code_roots(self) -> frozenset[str]:
        """The roots whose labels an instruction follows (or a construct
        that the reader refused and that can hold one)."""
        found: set[str] = set()
        for items in self.layout.sections.values():
            run: list[str] = []
            for item in items:
                if item.kind == "label":
                    if item.name in self.roots:
                        run.append(item.name)
                    continue
                if item.kind == "instr" or (item.kind == "barrier" and _holds_code(item.effects)):
                    found.update(run)
                elif item.kind == "barrier" and not _writes_bytes(item.effects):
                    # No byte: the labels before it are still at the next one.
                    continue
                run = []
        return frozenset(found)

    def _parents(self, walks: dict[tuple[str, ...], _Walk]) -> dict[str, str | None]:
        """Name the routine that holds each label that starts no routine.

        That is the routine in whose text the label lies (the nearest
        start before it in its section) when that routine reaches it, else
        the only routine that reaches it.  A label of a shared tail has
        several routines that reach it, and the text decides between them.
        """
        parents: dict[str, str | None] = {}
        for name, (key, index) in self.layout.positions.items():
            if name in self.roots:
                continue
            reaching = [group for group, walk in walks.items() if name in walk.reached]
            container = self._container(key, index)
            if container is not None and container in reaching:
                parents[name] = container[0]
            elif len(reaching) == 1:
                parents[name] = reaching[0][0]
            else:
                parents[name] = None
        return parents

    def _container(self, key: str, index: int) -> tuple[str, ...] | None:
        """The routine whose start is the nearest one before *index* in *key*."""
        for item in reversed(self.layout.sections[key][:index]):
            if item.kind == "label" and item.name in self.roots:
                return self.groups.get(item.name)
        return None


def unit_flow(statements: list[Located], isa: str, entries: _Entries) -> UnitFlow:
    """Build the routines and the call edges of a unit (see the module docstring)."""
    return _Flow(statements, isa, entries).build()
