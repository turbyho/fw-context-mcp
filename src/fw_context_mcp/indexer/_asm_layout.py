"""The layout of an assembly unit: its sections, labels and entries.

Shared by the readers of `asm.py` (symbols, vector tables) and the flow of
control (`_asm_cfg.py`).  Each one reads the expanded statements of a unit
(`asm._statements`) and needs the same answers: which section a statement
is in, which label is a label, which labels the unit exports or types as
code.  One place for them keeps those answers the same everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._asm_macro import NO_BYTES, BarrierEffects, barrier_effects

# Directives that name a symbol.  GNU as accepts both spellings of the
# global one, and the projects measured use `.global`; a regex that knows
# only `.globl` returns nothing on any of them.
# `.global` and `.weak` take a list of names: `.global a, b` exports both.
_GLOBAL_RE = re.compile(r"^\s*\.(?:globl|global)\s+(.+?)\s*$")
_WEAK_RE = re.compile(r"^\s*\.weak\s+(.+?)\s*$")
_TYPE_RE = re.compile(r"^\s*\.type\s+([A-Za-z_.$][\w.$]*)\s*,\s*[%@#]?(\w+)")
_NAME = re.compile(r"^[A-Za-z_.$][\w.$]*$")
# The symbol types of `.type` that are code, and those that are data.  The
# last `.type` of a name counts, as for gas.  `notype` is neither.
_CODE_TYPES = frozenset({"function", "gnu_indirect_function"})
_DATA_TYPES = frozenset({"object", "tls_object", "common", "gnu_unique_object"})


def _listed(pattern: re.Pattern[str], stmt: str) -> list[str]:
    """The names that a `.global` or `.weak` statement lists."""
    found = pattern.match(stmt)
    if found is None:
        return []
    return [name.strip() for name in found.group(1).split(",") if _NAME.match(name.strip())]
_LABEL_RE = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*:")

# GNU as treats a label with this prefix as local to the file and keeps it
# out of the object symbol table.  A compiler-written branch target is
# spelled that way — startup_NRF52840.S has `.LC0` and `.LC1` around its
# copy loop — and indexing them puts names in `lookup_symbol` that no
# linker, debugger or reader will ever ask for.
_LOCAL_LABEL_PREFIX = ".L"

# Data directives that build a table, and the sections that hold code or
# ordinary data rather than a table.  The directive is captured: a
# position is the ordinal of an entry, and that is a slot index only while
# every entry of the table has one size.  One spelling always has one
# size, whatever the target (`.word` is 4 bytes on ARM and 2 on x86).
_DATA_DIRECTIVE = re.compile(
    r"^\s*(\.word|\.long|\.int|\.4byte|\.quad|\.8byte|\.short|\.hword|\.2byte"
    r"|DCD|DCQ|DCW)\s+(.+?)\s*$",
    re.IGNORECASE,
)
# A section switch, with the full name (`.vec-a` and `.vec-b` are two
# sections) and the rest of the directive.
_SECTION_RE = re.compile(
    r'^\s*\.(section|pushsection)\s+("[^"]*"|[^\s,]+)\s*,?\s*(.*?)\s*$', re.IGNORECASE
)
_SHORT_SECTION_RE = re.compile(r"^\s*\.(text|data|bss|rodata)\b\s*(.*?)\s*$", re.IGNORECASE)
# The section stack of gas: `.pushsection` saves the current section and
# switches, `.popsection` returns to the saved one, `.previous` swaps the
# current section with the one before the last switch.
_POP_SECTION_RE = re.compile(r"^\s*\.popsection\b", re.IGNORECASE)
_PREVIOUS_SECTION_RE = re.compile(r"^\s*\.previous\b", re.IGNORECASE)
_SUBSECTION_RE = re.compile(r"^\s*\.subsection\b", re.IGNORECASE)
# What may follow the name of a section with the positions still known:
# flags without a group (`G` makes one section of each group name) and a
# type.  A subsection number or `unique,N` gives a section with its own
# location counter, which this does not track.
_PLAIN_SECTION_REST = re.compile(r'^(?:"[A-Za-z]*"\s*(?:,\s*[@%]\w+\s*)?)?$')
# Alignment writes padding only where the location counter is not aligned.
# Before the first entry it moves the table as a whole, which leaves the
# ordinals of the entries as they are.
_ALIGN_RE = re.compile(r"^\s*\.(?:align|balign|p2align)\b", re.IGNORECASE)


@dataclass
class _SectionMove:
    """What one section directive did.

    Attributes:
        name: The current section after it, or None when not known.
        plain: The section has one location counter that this tracks: no
            subsection number, no group, no `unique`.
    """

    name: str | None
    plain: bool = True


class _SectionTracker:
    """The current section of gas, as the statements of a unit move it.

    gas starts in `.text`.  `.pushsection` saves the current section and
    the one before it, `.popsection` returns to them, and `.previous` swaps
    the current section with the one before the last switch.
    """

    def __init__(self) -> None:
        self.current: str | None = ".text"
        self.previous: str | None = None
        self.saved: list[tuple[str | None, str | None]] | None = []

    def feed(self, stmt: str) -> _SectionMove | None:
        """Move the section by *stmt*; None when it is no section directive."""
        switch = _SECTION_RE.match(stmt) or _SHORT_SECTION_RE.match(stmt)
        if switch is not None:
            groups = switch.groups()
            name = groups[1].strip('"') if len(groups) == 3 else f".{groups[0].lower()}"
            rest = groups[-1]
            if len(groups) == 3 and groups[0].lower() == "pushsection" and self.saved is not None:
                self.saved.append((self.current, self.previous))
            self.previous, self.current = self.current, name
            # `.text 0` is subsection 0, the one that `.text` names.
            plain = (
                _PLAIN_SECTION_REST.match(rest) is not None if len(groups) == 3
                else rest in ("", "0")
            )
            return _SectionMove(name, plain)
        if _STRUCT_RE.match(stmt):
            # `.struct` and `.offset` switch to the absolute section: the
            # labels there are offsets of a structure, not code and not a
            # table.  What `.previous` returns to after it is not measured.
            self.current, self.previous = ABSOLUTE_SECTION, None
            return _SectionMove(ABSOLUTE_SECTION)
        if _POP_SECTION_RE.match(stmt):
            # An empty stack is an error of gas; an unknown one (after a
            # barrier) gives an unknown section.
            self.current, self.previous = self.saved.pop() if self.saved else (None, None)
            return _SectionMove(self.current)
        if _PREVIOUS_SECTION_RE.match(stmt):
            self.current, self.previous = self.previous, self.current
            return _SectionMove(self.current)
        return None

    def subsection(self) -> None:
        """Move to another subsection of the current section (`.subsection N`).

        `.previous` then returns to the subsection before, which is in the
        same section.  The tracker names sections, not subsections: the
        caller must know that the name now holds more than one location
        counter.
        """
        self.previous = self.current

    def lose(self) -> None:
        """Forget the section: statements that are not known can move it."""
        self.current = self.previous = None
        self.saved = None


_THUMB_FUNC_RE = re.compile(r"^\s*\.thumb_func\b", re.IGNORECASE)
_STRUCT_RE = re.compile(r"^\s*\.(?:struct|offset)\b", re.IGNORECASE)
# The name this reader gives the absolute section of `.struct`.
ABSOLUTE_SECTION = "*ABS*"
_SIZE_RE = re.compile(r"^\s*\.size\s+([A-Za-z_.$][\w.$]*)\s*,", re.IGNORECASE)


@dataclass(frozen=True)
class _Entries:
    """The entry labels of a unit: the labels that own code.

    An entry is a label that the unit exports or types as code: `.global`,
    `.weak`, `.type name, %function`, `.size`, or the label after
    `.thumb_func`.  A label typed `%object` is data and owns no code.  The
    other labels are local labels of the entry before them.  This is a
    convention of naming, not a fact of the assembler: without `.size` gas
    gives a function no extent, and a debugger names an address by the
    nearest label.  The convention gives `find_callers(main)` the answer
    `Reset_Handler`, not the loop label before the call.

    Attributes:
        names: The entry labels.
        maybe: The names that a refused construct can export, thus can be
            entries; None when any name can be.
    """

    names: frozenset[str]
    maybe: frozenset[str] | None


def _entries(statements: list[tuple[str, int, str]]) -> _Entries:
    """Find the entry labels of a unit.  The directives can come after the label."""
    names: set[str] = set()
    types: dict[str, str] = {}
    maybe: set[str] | None = set()
    thumb_next = False
    for _file, _line, stmt in statements:
        label = _LABEL_RE.match(stmt)
        if label is not None:
            # gas gives `.thumb_func` to the next label it keeps: a `.L`
            # label is not one.
            if thumb_next and not label.group(1).startswith(_LOCAL_LABEL_PREFIX):
                names.add(label.group(1))
                thumb_next = False
            continue
        names.update(_listed(_GLOBAL_RE, stmt))
        names.update(_listed(_WEAK_RE, stmt))
        size = _SIZE_RE.match(stmt)
        if size is not None:
            names.add(size.group(1))
        typed = _TYPE_RE.match(stmt)
        if typed is not None:
            types[typed.group(1)] = typed.group(2)
        if _THUMB_FUNC_RE.match(stmt):
            thumb_next = True
        effects = barrier_effects(stmt)
        if effects is not None:
            if effects.exports is None:
                maybe = None
            elif maybe is not None:
                maybe |= effects.exports
    names.update(name for name, kind in types.items() if kind in _CODE_TYPES)
    objects = {name for name, kind in types.items() if kind in _DATA_TYPES}
    return _Entries(
        names=frozenset(names - objects),
        maybe=None if maybe is None else frozenset(maybe),
    )


@dataclass
class _Owned:
    """The owner of the code of one section at the current point.

    Attributes:
        owners: The entries that own the code here: more than one when
            several entries are at one place (no byte between them), none
            when no entry came yet, None when it is not known.
        group_open: No byte was written since the last entry, thus a next
            entry is at the same place.
    """

    owners: tuple[str, ...] | None = ()
    group_open: bool = False


class _Ownership:
    """Which entry owns the code at each point of the statements of a unit.

    Each section keeps its own owner: a function can switch to another
    section and back (`.pushsection`), and the code after the return still
    belongs to it.  `.size` of an owner ends it.  A barrier that can define
    or export an entry makes the owner unknown, because its hidden text can
    start another function.  An alignment keeps a group of entries open: in
    code it pads with instructions that run into the next entry.
    """

    def __init__(self, entries: _Entries) -> None:
        self.entries = entries
        self.tracker = _SectionTracker()
        self.sections: dict[str, _Owned] = {}

    def _state(self) -> _Owned | None:
        current = self.tracker.current
        if current is None:
            return None
        if current == ABSOLUTE_SECTION:
            # No code is there: a fresh state that no entry ever owns.
            return _Owned()
        return self.sections.setdefault(current, _Owned())

    def owners(self) -> tuple[str, ...] | None:
        """The entries that own the code at this point, or None when not known."""
        state = self._state()
        return None if state is None else state.owners

    def feed(self, stmt: str) -> None:
        """Move the point past *stmt*."""
        if self.tracker.feed(stmt) is not None:
            return
        if _SUBSECTION_RE.match(stmt):
            self.tracker.subsection()
            return
        effects = barrier_effects(stmt)
        if effects is not None:
            self._barrier(effects)
            return
        state = self._state()
        if state is None:
            return
        label = _LABEL_RE.match(stmt)
        if label is not None:
            name = label.group(1)
            if name in self.entries.names:
                joined = state.group_open and state.owners
                state.owners = (*state.owners, name) if joined and state.owners else (name,)
                state.group_open = True
            elif self.entries.maybe is None or name in self.entries.maybe:
                # A refused construct can export it: it can be an entry.
                state.owners = None
                state.group_open = False
            return
        size = _SIZE_RE.match(stmt)
        if size is not None:
            if state.owners and size.group(1) in state.owners:
                state.owners = tuple(owner for owner in state.owners if owner != size.group(1))
            return
        if NO_BYTES.match(stmt) or _ALIGN_RE.match(stmt):
            return
        state.group_open = False

    def _barrier(self, effects: BarrierEffects) -> None:
        # The hidden text can start an entry when it exports a name, or when
        # it defines a name that is (or can be) an entry.
        maybe = self.entries.maybe
        names = effects.names
        if effects.exports is None or effects.exports or names is None:
            defines_entry = True
        elif not names:
            defines_entry = False
        else:
            defines_entry = maybe is None or bool(names & (self.entries.names | maybe))
        if effects.section:
            self.tracker.lose()
            for owned in self.sections.values():
                if defines_entry:
                    owned.owners = None
                if effects.emits:
                    # It can write bytes into any section.
                    owned.group_open = False
            return
        state = self._state()
        if state is None:
            return
        if defines_entry:
            state.owners = None
        if effects.emits:
            state.group_open = False
