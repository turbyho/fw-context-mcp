"""The resolution of the slot facts of a build into call edges.

The walk of each unit records the slot facts (``_slots.py``, tables
``slot_*``).  A fact of one unit can tell the target of a call in an other
unit (`set_cb(read_card)` in one file, `g_cb(v)` in an other), thus the
resolution runs over all the facts of the build, after the units: a step of
the post-processing (``_postprocess._step_resolve_slots``).

The targets of a slot are the functions that reach it: the sources of the
slot, and the targets of each slot that flows into it.  An argument of a
site flows into the parameter of each target of the site, which can give
new targets to an other site; the resolution repeats until no target is
new (a fixed point).  A site gets a ``call`` edge to each target, from the
code that makes the call, at the place of the call:

* origin ``SLOT``: the target reaches the slot of the site;
* origin ``TYPE_ONLY``: the slot does not tell all the targets, thus each
  function whose address the build takes and whose type is the type of the
  call is a target too.  That is the case for a site with no slot, and for
  a slot that code out of view can write (an escape flows into it).
* origin ``SDK``: a rule of the SDK model (``_sdk_model``, table
  ``sdk_links``) says that a function of an SDK runs the function in a
  slot, and no C code of the build does.  The edge goes from that function,
  at its declaration.  A "flow" rule of the model is a flow of the graph:
  the edge that it gives comes from a site, with origin ``SLOT``.

A slot that no source reaches and that does not escape gets no target: no
code of the build puts a function into it.  WHY not the functions of its
type: measured on an STM32 HAL build, the `mutecallback` field of the SAI
driver, which the build never sets, got 5 interrupt handlers of the
application that have its type.

An "address" site (a call of an address that is a number: a bootloader
that starts the application) gets no target: no code of the build is
there.  The site stays a fact, with the text of the address.

Limits:

* An argument goes only into the parameters of the ``SLOT`` targets of a
  site.  A ``TYPE_ONLY`` target is a superset, and its parameters would
  give ``SLOT`` edges that only a guess of the type gave; such a parameter
  escapes instead (``_pass_arguments``).
* An "object" site (``operator()`` of ``std::function`` or of a functor)
  gets no ``TYPE_ONLY`` targets: the facts have no type of a value that an
  object holds.
* The type of a function and of a call must be the same spelling: a C unit
  and a C++ unit can spell one type in two ways (`struct dev *`, `dev *`).
* The type of a pointer to a member names its class: `void (Derived::*)()`
  that holds `&Base::f` does not get `Base::f` by its type only.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from ._slots import OUTSIDE
from .models import RefOrigin

log = logging.getLogger(__name__)

_ORIGINS = int(RefOrigin.SLOT | RefOrigin.TYPE_ONLY | RefOrigin.SDK)
# A site: its place (file, line, column), its ordinal at the place, and the
# unit of a fact of a template instance ('' for the written code).  WHY the
# unit: two units give the sites of an instance at one place, each with its
# own ordinals, thus an argument belongs to the site of its own unit.
_SiteKey = tuple[str, int, int, int, str]


@dataclass
class _Site:
    key: _SiteKey
    caller: str | None
    kind: str
    slot: str
    slot_type: str
    callee: str


@dataclass(frozen=True)
class _SdkCall:
    """A "call" link of the SDK model: *caller* runs the function in *slot*."""

    slot: str
    caller: str
    file: str
    line: int
    col: int


@dataclass
class _Slots:
    """The slot graph of a build: the points-to sets and the flows between slots."""

    targets: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    flows: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    escaped: set[str] = field(default_factory=set)
    # (slot, function) → each slot that gives the function to the slot, and
    # "" for a source of the slot itself.  The tools follow them back to the
    # places where the code stored the function (``slot_targets.via``).
    # WHY each one and not the first: `set1(ev); set2(ev);` stores one
    # function on two paths, and a reader must see the two places.
    via: defaultdict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    _changed: list[str] = field(default_factory=list)

    def add_source(self, slot: str, function: str) -> None:
        self.via[(slot, function)].add("")
        if function not in self.targets[slot]:
            self.targets[slot].add(function)
            self._changed.append(slot)

    def add_flow(self, source: str, target: str) -> None:
        if source != target and target not in self.flows[source]:
            self.flows[source].add(target)
            self._changed.append(source)

    def escape(self, slot: str) -> None:
        if slot not in self.escaped:
            self.escaped.add(slot)
            self._changed.append(slot)

    def propagate(self) -> None:
        """Give each slot the targets and the escapes of each slot that flows into it."""
        while self._changed:
            slot = self._changed.pop()
            targets = self.targets.get(slot, set())
            escaped = slot in self.escaped
            for successor in self.flows.get(slot, ()):
                known = self.targets[successor]
                for function in targets:
                    self.via[(successor, function)].add(slot)
                if not targets <= known:
                    known |= targets
                    self._changed.append(successor)
                if escaped and successor not in self.escaped:
                    self.escaped.add(successor)
                    self._changed.append(successor)


def _add_override_flows(conn: sqlite3.Connection, config_hash: str, slots: _Slots) -> None:
    """The flows of a virtual call: a parameter of a base method into the parameter of each override, and back.

    WHY: a call through a base class (`queue->post(cb)` on an interface) is
    a direct call of the base method, thus its argument goes into a
    parameter of the base method.  The body that runs is an override, and
    it reads its own parameter.  Without the flow a function that an
    interface receives reached no call.  The return value of an override
    is the value of the call of the base method.  The relation is a fact of
    the compiler (``overrides``, built before this step).  A parameter
    slot exists for each parameter that a fact names, thus each pair maps
    the indexes that the facts of the base or of the override hold.
    """
    pairs = conn.execute(
        "SELECT base_usr, derived_usr FROM overrides WHERE config_hash = ?", (config_hash,),
    ).fetchall()
    if not pairs:
        return
    indexes: defaultdict[str, set[str]] = defaultdict(set)
    known = set(slots.flows) | {target for targets in slots.flows.values() for target in targets} | set(slots.targets)
    for row in conn.execute(
        "SELECT slot FROM slot_sites WHERE config_hash = ? AND slot LIKE 'P|%'"
        " UNION SELECT value FROM slot_arguments WHERE config_hash = ? AND value_kind = 'slot' AND value LIKE 'P|%'",
        (config_hash, config_hash),
    ):
        known.add(row[0])
    for slot in known:
        if slot.startswith("P|"):
            code, _, index = slot[2:].rpartition("|")
            indexes[code].add(index)
    for row in pairs:
        base, derived = row["base_usr"], row["derived_usr"]
        for index in indexes.get(base, set()) | indexes.get(derived, set()):
            slots.add_flow(f"P|{base}|{index}", f"P|{derived}|{index}")
        slots.add_flow(f"R|{derived}", f"R|{base}")


def _load(
    conn: sqlite3.Connection, config_hash: str,
) -> tuple[_Slots, dict[str, set[str]], list[_Site], dict, list[_SdkCall]]:
    """Read the slot facts of the build: the graph, the functions of each type, the sites, the arguments.

    The links of the SDK model come with them: a "flow" link is a flow of
    the graph, and a "call" link is a call of a slot with no site.
    """
    slots = _Slots()
    by_type: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        "SELECT function, function_type, slot FROM slot_sources WHERE config_hash = ?", (config_hash,),
    ):
        if row["slot"]:
            slots.add_source(row["slot"], row["function"])
        if row["function_type"] != "lambda":
            by_type[row["function_type"]].add(row["function"])
    for row in conn.execute("SELECT source, target FROM slot_flows WHERE config_hash = ?", (config_hash,)):
        slots.add_flow(row["source"], row["target"])
    _add_override_flows(conn, config_hash, slots)
    calls: list[_SdkCall] = []
    for row in conn.execute(
        "SELECT kind, slot, target, file, line, col FROM sdk_links WHERE config_hash = ?"
        " ORDER BY kind, slot, target",
        (config_hash,),
    ):
        if row["kind"] == "flow":
            slots.add_flow(row["slot"], row["target"])
        else:
            calls.append(_SdkCall(row["slot"], row["target"], row["file"], row["line"], row["col"]))
    # An escape "defined out of the unit" holds only when no unit of the
    # build defines the variable or the function of the slot.
    defined = {
        row[0] for row in conn.execute(
            "SELECT usr FROM symbols WHERE config_hash = ? AND is_definition = 1", (config_hash,),
        )
    }
    for row in conn.execute("SELECT slot, reason FROM slot_escapes WHERE config_hash = ?", (config_hash,)):
        if row["reason"] == OUTSIDE and row["slot"][2:] in defined:
            continue
        slots.escape(row["slot"])
    sites = [
        _Site(
            key=(row["file"], row["line"], row["col"], row["ordinal"], row["unit"]), caller=row["caller"],
            kind=row["kind"], slot=row["slot"], slot_type=row["slot_type"], callee=row["callee"],
        )
        for row in conn.execute(
            "SELECT file, line, col, ordinal, caller, kind, slot, slot_type, callee, unit FROM slot_sites"
            " WHERE config_hash = ? ORDER BY file, line, col, ordinal, unit",
            (config_hash,),
        )
    ]
    arguments: dict[_SiteKey, list[tuple[int, str, str]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT file, line, col, ordinal, arg_index, value_kind, value, unit FROM slot_arguments"
        " WHERE config_hash = ?",
        (config_hash,),
    ):
        key = (row["file"], row["line"], row["col"], row["ordinal"], row["unit"])
        arguments[key].append((row["arg_index"], row["value_kind"], row["value"]))
    return slots, by_type, sites, arguments, calls


def _slot_targets(site: _Site, slots: _Slots) -> set[str]:
    targets = set(slots.targets.get(site.slot, ())) if site.slot else set()
    if site.callee:
        targets.add(site.callee)
    return targets


def _guessed(site: _Site, slots: _Slots, by_type: dict[str, set[str]]) -> set[str]:
    """The ``TYPE_ONLY`` targets of *site*: for a "pointer" site with no slot or with an escaped slot."""
    if site.kind != "pointer" or (site.slot and site.slot not in slots.escaped):
        return set()
    return by_type.get(site.slot_type, set()) - slots.targets.get(site.slot, set())


def _pass_arguments(
    sites: Iterable[_Site], slots: _Slots, arguments: dict[_SiteKey, list[tuple[int, str, str]]],
    by_type: dict[str, set[str]],
) -> None:
    """Pass the arguments of each site into the parameters of its targets, until no target is new.

    A ``TYPE_ONLY`` target gets no argument (see the module docstring), but
    each parameter that the site gives a value escapes: a call through it
    gets ``TYPE_ONLY`` targets.  WHY: without the escape, the parameter
    reads as a slot that no code fills, and the call through it got no
    edge (review of phase 5b: `boot` reaches `apply` by its type only, and
    `g(7)` in `apply` lost its target).
    """
    # (site, target, guessed): a target that the type gave first can later
    # reach the slot, and it then gets the arguments.
    passed: set[tuple[_SiteKey, str, bool]] = set()
    with_arguments = [site for site in sites if site.key in arguments]
    slots.propagate()
    while True:
        before = len(passed)
        for site in with_arguments:
            for target in _slot_targets(site, slots):
                if (site.key, target, False) in passed:
                    continue
                passed.add((site.key, target, False))
                for index, value_kind, value in arguments[site.key]:
                    parameter = f"P|{target}|{index}"
                    if value_kind == "function":
                        slots.add_source(parameter, value)
                    else:
                        slots.add_flow(value, parameter)
            for target in _guessed(site, slots, by_type):
                if (site.key, target, True) in passed:
                    continue
                passed.add((site.key, target, True))
                for index, _value_kind, _value in arguments[site.key]:
                    slots.escape(f"P|{target}|{index}")
        slots.propagate()
        if len(passed) == before:
            return


def _reach(start: str, slots: _Slots) -> set[str]:
    """The slots that *start* flows into, directly or through other slots (not *start* itself)."""
    reached: set[str] = set()
    pending = [start]
    while pending:
        for successor in slots.flows.get(pending.pop(), ()):
            if successor not in reached:
                reached.add(successor)
                pending.append(successor)
    reached.discard(start)
    return reached


def _sdk_edges(config_hash: str, calls: list[_SdkCall], slots: _Slots) -> list[tuple]:
    """The ``SDK`` edges of the "call" links: from the caller of the link to each target of its slot.

    A link whose slot flows into the slot of an inner link gives no edge.
    WHY: a flow gives each target on, thus the inner link has the targets
    too, and its function is the one that runs them.  ESP-IDF declares
    `xTaskCreate` as an inline wrapper of `xTaskCreatePinnedToCore`: the
    wrapper gives no second caller.  A link is inner only when its slot
    does not flow back: a slot that flows back to itself (`reg(f)` stores
    `f`, and `reg(saved)` registers it again), and two links whose slots
    flow into each other, give their edges, because no one of them runs
    the targets in place of the other.  An outer link whose slot flows into
    two inner links gives an edge from each of them: each is a path.
    """
    linked = {call.slot for call in calls}
    reach = {slot: _reach(slot, slots) for slot in linked}
    rows: list[tuple] = []
    for call in calls:
        if any(call.slot not in reach[other] for other in reach[call.slot] & linked):
            continue
        for target in sorted(slots.targets.get(call.slot, ())):
            rows.append((
                config_hash, target, call.file, call.line, call.caller, "call", None, call.col, int(RefOrigin.SDK),
            ))
    return rows


def resolve_slot_edges(conn: sqlite3.Connection, config_hash: str) -> dict[str, int]:
    """Replace the call edges of the slot resolution of the build *config_hash*.

    The edges of the last run go first, because a fact of any unit can
    change the targets of any site.  A row that an other analysis also
    found keeps its row and loses the bits of this resolution only.  Gives
    the counts of the sites and the edges, for the log: ``edges`` counts
    the edges of the sites, ``sdk`` the edges of the SDK model.
    """
    from .db import insert_refs_batch

    conn.execute(
        "DELETE FROM refs WHERE config_hash = ? AND (origins & ?) != 0 AND (origins & ?) = 0",
        (config_hash, _ORIGINS, ~_ORIGINS),
    )
    conn.execute(
        "UPDATE refs SET origins = origins & ? WHERE config_hash = ? AND origins & ?",
        (~_ORIGINS, config_hash, _ORIGINS),
    )
    slots, by_type, sites, arguments, calls = _load(conn, config_hash)
    _pass_arguments(sites, slots, arguments, by_type)

    # The counts of the log see one copy of each site, as the readers do
    # (``db/_indirect.ONE_UNIT_SITES``): each unit that uses a template
    # instance gives its sites.
    counted_unit: dict[tuple, str] = {}
    for site in sites:
        content = (*site.key[:3], site.caller, site.kind, site.slot, site.slot_type, site.callee)
        counted_unit[content] = min(counted_unit.get(content, site.key[4]), site.key[4])
    counts = {"sites": 0, "slot": 0, "type_only": 0, "address": 0, "unresolved": 0}
    rows: list[tuple] = []
    for site in sites:
        targets = slots.targets.get(site.slot, set()) if site.slot else set()
        guessed = _guessed(site, slots, by_type)
        file, line, column, _ordinal, unit = site.key
        for target in sorted(targets):
            rows.append((config_hash, target, file, line, site.caller, "call", None, column, int(RefOrigin.SLOT)))
        for target in sorted(guessed):
            rows.append((
                config_hash, target, file, line, site.caller, "call", None, column, int(RefOrigin.TYPE_ONLY),
            ))
        content = (file, line, column, site.caller, site.kind, site.slot, site.slot_type, site.callee)
        if counted_unit[content] != unit:
            continue
        counts["sites"] += 1
        if site.kind == "address":
            counts["address"] += 1
        elif targets:
            counts["slot"] += 1
        elif guessed:
            counts["type_only"] += 1
        else:
            counts["unresolved"] += 1
    counts["edges"] = len(set(rows))
    sdk_rows = _sdk_edges(config_hash, calls, slots)
    counts["sdk"] = len(set(sdk_rows))
    if rows or sdk_rows:
        insert_refs_batch(conn, rows + sdk_rows)
    _store_targets(conn, config_hash, slots)
    return counts


def _store_targets(conn: sqlite3.Connection, config_hash: str, slots: _Slots) -> None:
    """Replace the targets and the escapes of each slot of the build (tables ``slot_targets``, ``slot_escaped``).

    WHY stored: the tools that answer about one slot (``find_indirect_targets``,
    ``get_symbol_context``) read the same answer that gave the call edges,
    and need not resolve the whole build at each query.
    """
    conn.execute("DELETE FROM slot_targets WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM slot_escaped WHERE config_hash = ?", (config_hash,))
    conn.executemany(
        "INSERT INTO slot_targets (config_hash, slot, function, via) VALUES (?,?,?,?)",
        [
            (config_hash, slot, function, via)
            for slot, functions in slots.targets.items()
            for function in functions
            for via in sorted(slots.via.get((slot, function)) or {""})
        ],
    )
    conn.executemany(
        "INSERT INTO slot_escaped (config_hash, slot) VALUES (?,?)",
        [(config_hash, slot) for slot in slots.escaped],
    )
