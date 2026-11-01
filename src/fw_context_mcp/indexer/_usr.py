"""The USR of a cursor: a key of the file of a file-local USR, and the place of each anonymous type.

WHY this module exists: libclang writes the location of a declaration into
the USR of each symbol with no external name: a `static` function or
variable (`c:drv.c@F@isr`), a local variable or parameter
(`c:drv.c@312@F@isr@arg`), a macro (`c:drv.h@744@macro@DRV_H`), and the
members of an anonymous namespace.  The location is the BASE NAME of the
file of the canonical declaration.  Two files of one base name in two
directories thus give one USR, and the index holds one symbol for two:
one body, one row of references, and the facts of both.  Measured: the
two `fatal.c` of Zephyr (`LOG_MODULE_REGISTER` defines `__log_level` in
each), and a copy of an SDK header in a project.

``cursor_usr`` replaces the base name with ``file_key``: the base name and
8 hex digits of the SHA-1 of the resolved path.  The hash has no ``@``,
thus a reader can split a USR at ``@`` as before; a path can hold one
(``AsyncTCP@3.4.10``).  The base name stays for the reader.  The path is
absolute: the extraction does not know the root of the project, and a
project that moves gets a new ``config_hash`` all the same.

The file of the prefix is the file of the canonical declaration of the
cursor: clang writes the location of the canonical declaration
(``USRGenerator::GenLoc``).  Measured on 107 units of a Zephyr, an Mbed and
an ESP32 build (3.65 million USRs with a prefix): the canonical
declaration gave each prefix, and no two USRs of libclang got one new USR.
A USR whose prefix is not the base name of that file stays as libclang
wrote it: no guess.

An anonymous type has no name for libclang to write, and no location in its
USR either.  A member of a record writes `@Sa`, `@Ua` or `@Ea`
(`c:@S@thread@Ua`), thus two anonymous unions of one struct gave one USR.
An anonymous enum writes the name of its first constant (`c:@Ea@ST_IDLE`),
and a struct that a typedef names writes the typedef (`c:@SA@cfg_t`), thus
two state machines of two `.c` files gave one enum, and one constant for
each pair of constants.  Measured: 10 such enums of the BLE controller of
Mbed (a master and a slave file each), and 5 structs of Zephyr with two
anonymous unions.  ``cursor_usr`` writes after the component of each
anonymous type the place of its declaration: `~<file key>:<line>:<column>`
(`c:@Ea@ST_IDLE~sm.c~1a3865a8:12:1@ST_IDLE`).  An anonymous type has no
forward declaration, thus each unit that reads it reads that one place,
and the mark cannot split one type into two.  The members and the
constants of the type take the mark through the prefix of their USR.
The closure type of a lambda is an anonymous class too (`@Sa`): two lambdas
of one scope with no place in their USR (the scope of a file, of a
namespace, of a class) gave one `operator()`, and now give two.
``_is_anonymous_type`` finds such a type from its declaration, and reads
the end of the USR only where that end is the component of the type itself
(an anonymous enum).  Measured
on 1254 units of a Zephyr, an Mbed and an ESP32 build: no two USRs of
libclang got one new USR; each anonymous type with no such component had a
place in its USR (`@S@zconfig.h@11622`); and each type whose USR ends with
`@SA@<name>` but is no such type was a specialization (`std::codecvt`).

Limits:

* The anonymous types that one expansion of a macro writes have one
  place, thus they keep one USR, as before.
* A USR that holds an anonymous type as the type of an argument (a
  specialization `cv<cfg_t>`, a parameter of a function) holds its
  component with no mark.  Two such types of one name thus still give one
  USR there.  The fast test of ``cursor_usr`` finds the component all the
  same, thus such a USR costs a walk of the semantic parents that finds no
  type to mark.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re

import clang.cindex as cx

# The component of an anonymous type in a USR of libclang: a member with no
# name (`@Sa`, `@Ua`, `@Ea`), or a type that a typedef names (`@SA@cfg_t`).
# A fast test: a USR with none gets no walk of the semantic parents.
_ANONYMOUS_COMPONENT = re.compile(r"@[SUE]a(?:@|$)|@[SUE]A@")

# The letter of the kind of a type in a USR (``USRGenerator::VisitTagDecl``).
_TYPE_LETTERS = {
    cx.CursorKind.STRUCT_DECL: "S", cx.CursorKind.CLASS_DECL: "S",
    cx.CursorKind.UNION_DECL: "U", cx.CursorKind.ENUM_DECL: "E",
}

# The end of the USR of an anonymous enum: `@Ea` and the name of its first
# constant, or `@Ea` alone for an enum with no constant.
_ANONYMOUS_ENUM_END = re.compile(r"@Ea(?:@[^@]*)?$")


@functools.lru_cache(maxsize=16384)
def file_key(path: str) -> str:
    """The key of the file *path* in a USR: ``<base name>~<8 hex digits of the SHA-1 of the resolved path>``."""
    resolved = os.path.realpath(path)
    digest = hashlib.sha1(resolved.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()
    return f"{os.path.basename(resolved)}~{digest[:8]}"


def cursor_usr(cursor: cx.Cursor) -> str:
    """The USR of *cursor* ("" for none), with the file key of a file-local USR and the place of each anonymous type."""
    usr = cursor.get_usr()
    if not usr.startswith("c:"):
        return usr
    if _ANONYMOUS_COMPONENT.search(usr):
        # A file-local USR can hold an anonymous type with no offset of its
        # own: a member of an anonymous namespace (`c:x.cpp@aN@S@AN@Ua`).
        usr = _with_anonymous_places(cursor, usr)
    if usr.startswith("c:@"):
        return usr
    return _with_file_key(cursor, usr)


def _with_file_key(cursor: cx.Cursor, usr: str) -> str:
    """*usr* with ``file_key`` in place of the base name that it starts with."""
    base, separator, rest = usr[2:].partition("@")
    if not separator:
        return usr
    canonical = cursor.canonical
    file = canonical.location.file if canonical is not None else None
    # WHY os.path.basename and not Path: this runs for each reference of a
    # unit, and Path builds an object each time (measured: 1.1 against
    # 0.1 microseconds a call).
    if file is None or os.path.basename(file.name) != base:
        return usr
    return f"c:{file_key(file.name)}@{rest}"


def _with_anonymous_places(cursor: cx.Cursor, usr: str) -> str:
    """*usr* with the place of each anonymous type of *cursor* and its semantic parents after its component.

    The USR of such a type is a prefix of *usr* (the USR of a member is the
    USR of the type and the component of the member).  When it is not, *usr*
    stays as libclang wrote it: no guess.  A local variable inside an
    anonymous type has a USR of its own place (`c:drv.c@312@...`), which
    holds no such prefix and needs no mark.
    """
    marks: list[tuple[int, str]] = []
    node: cx.Cursor | None = cursor
    while node is not None and node.kind != cx.CursorKind.TRANSLATION_UNIT:
        letter = _TYPE_LETTERS.get(node.kind)
        if letter is not None:
            prefix = node.get_usr()
            if _is_anonymous_type(node, letter, prefix):
                location = node.location
                if location.file is None or not usr.startswith(prefix):
                    return usr
                marks.append((len(prefix), f"~{file_key(location.file.name)}:{location.line}:{location.column}"))
        node = node.semantic_parent
    # From the longest prefix to the shortest: a mark does not move the
    # place of a mark before it.
    for at, mark in sorted(marks, reverse=True):
        usr = usr[:at] + mark + usr[at:]
    return usr


def _is_anonymous_type(node: cx.Cursor, letter: str, usr: str) -> bool:
    """True when *node*, a type with the USR *usr*, is an anonymous type that *usr* writes as such.

    WHY from the declaration and not from the text of the USR: the USR of a
    specialization holds the USRs of its arguments, thus the end of
    `std::codecvt<char, char, mbstate_t>` is `@SA@_mbstate_t` (measured: 7
    such classes of Mbed and ESP32).  ``USRGenerator::VisitTagDecl`` writes:

    * an anonymous type: `@Sa`, `@Ua`, or `@Ea` and the name of its first
      constant;
    * a type with no name that a typedef names: `@SA@<typedef name>`.
      libclang gives such a type the typedef as its spelling, and
      ``is_anonymous`` is False.  A specialization is never such a type:
      it has the name of its template, also when its USR ends the same
      way (`foo<n::foo>`).

    The unnamed type of a named field (`struct { ... } f;`) is anonymous,
    but libclang writes its place (`@S@drv.c@15`), not this component: it
    needs no mark, and the test is False.

    WHY the end of *usr* for an anonymous enum, and not its first constant:
    ``get_children`` of libclang collects each child, thus a walk to the
    first constant costs the number of constants, for each constant and
    each reference to one (measured: 1 ms a call for 2000 constants).  The
    end is exact here: the component of the type is the last one, a name of
    a constant has no ``@``, and an anonymous type is no specialization.
    """
    if node.is_anonymous():
        if letter == "E":
            return _ANONYMOUS_ENUM_END.search(usr) is not None
        return usr.endswith(f"@{letter}a")
    if node.get_num_template_arguments() >= 0:
        return False
    return usr.endswith(f"@{letter}A@{node.spelling}")
