"""The C++ types that the ODR makes one, and the places of the class templates of a build.

``cursor_usr`` writes the place of the definition of each named type into
its USR (``_usr.py``).  That is the identity of a C type: a tag has no
linkage in C, thus `struct config` of two drivers is two types.  It is not
the identity of a C++ type with external linkage.  By the one definition
rule (C++ [basic.def.odr]), each definition of such a type in a program is
the same type, and the linker joins the methods of the definitions by name.
A project with a copy of a header of its SDK has two definitions in two
files: the units that include the copy of the SDK call the methods, and the
unit of the copy in the project defines them.  Measured on an Mbed project
(two such classes): 130 calls reached a declaration with no definition, and
2 callbacks of a slot lost their edge.

A rule "C++ by the name, C by the place" is not a rule of one USR for one
type: a struct of a header that C units and C++ units read would get one
USR in each language (measured: 298 such definitions of an ESP32 build, 232
of an STM32 build, 920 of an Mbed build).  Thus each unit writes the place,
and the build merges the definitions of one C++ type: a group of one merge
key (``_usr.merge_key``) with definitions in two or more files that C++
units read.  A definition in a file that only C units read keeps its place:
the C rule applies there.  A type in the body of a type has no group: it
follows the merge of that type (``_usr._merged_mark``), thus the key of
each group is one text in C and in C++.

The merge does not compare the definitions of a group.  Two headers that
C++ units read and that define two different `struct config` give one type
with the members of the two: the ODR makes the name one type, two
different definitions of it make the program wrong, and the linker joins
their methods all the same (decided by the user, 2026-11-03).  A group with
a definition in the main file of a unit stays as it is: two `.cpp` files
that each define a helper `struct Ctx` also break the ODR, but the index
shows the two definitions as they are written (decided by the user,
2026-11-02).  compile_commands.json names each main file, thus this is a
fact of the build, not a guess.

The decision names a file, not a line: an edit that moves the lines of a
copy changes no decision.  It needs the definitions of all units, and a
unit writes its USRs before the decision: the index run stores the
definitions (``db/_type_places``), decides, and parses again each unit
whose rows change (``runner``).

The build also gives the place of a class template to the units that
declare it and do not define it (``template_places_of_build``): such a unit
can define an explicit specialization of it, and its USR must be the USR
that a unit which sees the definition gives (``_usr._place_owner``).  This
place names a line, thus an edit that moves the definition of the template
parses these units again.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Set
from pathlib import Path
from typing import NamedTuple

from ._usr import Place, TypeDecisions, file_key
from .db import load_merged_types, load_template_places


def load_type_decisions(conn, config_hash: str) -> TypeDecisions:
    """Return the decisions that the stored rows of *config_hash* follow: a parse of a unit must follow them too."""
    templates = {key: Place(*place) for key, place in load_template_places(conn, config_hash).items()}
    return TypeDecisions(load_merged_types(conn, config_hash), templates)


class StoredPlace(NamedTuple):
    """A stored definition of a type that the ODR can merge: its file, its merge key, its line and column."""

    path: Path
    key: str
    line: int
    column: int


def merged_types_of_build(
    places: Iterable[StoredPlace], cxx_files: Set[Path], main_files: Set[Path],
) -> frozenset[tuple[str, str]]:
    """Return ``(merge key, file key)`` of each definition that the build merges.

    *places* holds each stored definition of a type that the ODR can merge.
    *cxx_files* holds the resolved path of each file that a C++ unit reads,
    and *main_files* the resolved main file of each unit.
    """
    groups: dict[str, set[Path]] = defaultdict(set)
    for place in places:
        if place.path in cxx_files:
            groups[place.key].add(place.path)
    return frozenset(
        (key, file_key(str(path)))
        for key, paths in groups.items()
        if len(paths) > 1 and not paths & main_files
        for path in paths
    )


def template_places_of_build(
    places: Iterable[StoredPlace], merged: Set[tuple[str, str]], keys: Iterable[str],
) -> dict[str, Place]:
    """Return the place of the definition of each class template of *keys* that has one place in the build.

    *keys* holds the merge key of each template that a unit declared and did
    not define (``_usr.PlaceRecord.templates``).  A template with
    definitions in one file has that place.  A template whose definitions
    the build merges (*merged*) has the place of one of them: each gives the
    same mark (``_usr.merged_mark_of_key``), and the first in the order of
    the places is the same in each run.  A template with no definition, or
    with definitions in two files that the build does not merge (two main
    files: the ODR is broken), has no key: the build has no single place.
    """
    wanted = set(keys)
    found: dict[str, set[Place]] = defaultdict(set)
    for place in places:
        if place.key in wanted:
            found[place.key].add(Place(file_key(str(place.path)), place.line, place.column))
    return {
        key: min(definitions)
        for key, definitions in found.items()
        if len(definitions) == 1 or all((key, place.file) in merged for place in definitions)
    }


def decide_types(
    places: Iterable[StoredPlace], cxx_files: Set[Path], main_files: Set[Path], keys: Iterable[str],
) -> TypeDecisions:
    """Return the decisions of a build from its stored definitions.

    *cxx_files* and *main_files* are the ones of ``merged_types_of_build``,
    and *keys* the merge key of each template that a stored unit looked for
    (``odr_unit_places``).  The index run decides with this function, and
    ``reindex_file`` compares its answer with the stored decisions: the two
    must agree on each rule.
    """
    stored = list(places)
    merged = merged_types_of_build(stored, cxx_files, main_files)
    return TypeDecisions(merged, template_places_of_build(stored, merged, keys))


def files_of_keys(places: Iterable[StoredPlace], keys: Set[str]) -> frozenset[Path]:
    """Return the file of each definition in *places* with a merge key of *keys*.

    A unit that reads none of these files writes none of their places in a
    USR: a place comes from the definition of the type, which the unit reads.
    The caller gives each key of a change of the merge, not only the files
    of the change: the symbol row of a merged type is the row of the unit
    that stored it first (``db/_symbols.insert_symbols_batch``).  When the file of that row
    leaves the group, the other files of the group must store the
    definition again, also when their own merge did not change.
    """
    return frozenset(place.path for place in places if place.key in keys)
