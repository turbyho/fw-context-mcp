"""The USR of a cursor: a key of the file of a file-local USR, the place of each tag type, and the target of a typedef.

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

A tag type (a struct, union, enum or class) has no place in its USR.  A
type with a name writes the name (`c:@S@config`).  In C a type exists in
its unit only, thus two drivers can each define `struct config`, and the
index held one symbol for the two: one body, and the members of both.  An
anonymous type has no name to write: a member of a record writes `@Sa`,
`@Ua` or `@Ea` (`c:@S@thread@Ua`), an anonymous enum writes the name of its
first constant (`c:@Ea@ST_IDLE`), and a struct that a typedef names writes
the typedef (`c:@SA@cfg_t`).  The unnamed type of a named field
(`struct { ... } f;`) writes the base name of its file and an offset
(`c:@S@outer@S@drv.h@15`), thus two files of one base name can give one
USR there too.  Measured: two typedef-named structs of one
name in two `.c` files of Zephyr, 10 anonymous enums of the BLE controller
of Mbed (a master and a slave file each), 5 structs of Zephyr with two
anonymous unions, two branches of `#if` that define `struct sigaction`, and
two copies of a header in Mbed.

``cursor_usr`` writes after the component of each tag type the place of its
definition: `~<file key>:<line>:<column>`
(`c:@S@config~drv.c~1a3865a8:12:8`).  The members, the methods and the
constants of the type take the mark through the prefix of their USR.  The
unnamed type of a named field gets the key of its file in place of the
base name (`@S@drv.h~1a3865a8@15`): libclang wrote its place already.  A
specialization of a class template has the place of the definition of its
template: in C++ a template and a list of arguments are one type
(``_place_owner``).  A unit that declares the template and does not define
it takes that place from the other units of the build
(``TypeDecisions.templates``).  A
named type with no definition in the unit (`struct config;`, an opaque
type) keeps the USR of libclang: the unit cannot tell which definition it
is.  The index holds no such USR: it keeps the definition of a type and not
a forward declaration, and each reference that it keeps (a call, a member,
a constant) needs the definition.  Measured on three indexes: no type
symbol with no definition, and no reference to a type.

WHY the place of the definition, and not the structure of the type (its
members, as the C rule of compatible types and Kythe name a record): one
definition gave two structures over the units of one build.  CMSIS writes
`__I` as `volatile const` in C and as `volatile` in C++, the units of a
library of Zephyr see other members of the `psa_*` structs than the
application does, and libclang writes a dependent type of a template with
other spaces in two units.  The index keeps one body for one place, thus
one place is one type.

That is the identity of a C type: a tag has no linkage in C.  In C++ a
named type at the scope of a file or a namespace has external linkage, and
the one definition rule makes each definition of it one type: a copy of a
header of the SDK in the project gives two places of one class, and the
linker joins its methods by name.  The build decides which definitions
are one type (``_odr.py``) from the merge key of each one (``odr_place``,
``merge_key``) and its file, and ``type_decisions`` lets ``cursor_usr``
leave out their marks.  A type in the body of a type follows the merge of
that type: it is a member of one class.  The decision names a file and
not a line, thus an edit that moves the lines of a copy does not change it.

In C a type in the body of a type has the scope of the file, and libclang
writes no type around it (`c:@S@B`), but C++ does (`c:@S@A@S@B`).
``cursor_usr`` writes the form of C++ in C too (``_cxx_usr``): a C unit and
a C++ unit that read one header give one USR to each such type, its members
and its constants.  A type in the body of a type in a function keeps the
form of C (``_in_body_in_c``): the USR of the function differs in C and C++.

The closure type of a lambda is an anonymous class too (`@Sa`): two lambdas
of one scope with no place in their USR (the scope of a file, of a
namespace, of a class) gave one `operator()`, and now give two.

A typedef is a second name of a type, and C lets a header declare it
again (`uint32_t` in `_stdint.h` and `stdint-gcc.h`).  libclang writes the
file of the first declaration of the unit into the USR of a typedef
outside a system header (`c:md.h@T@info_t`), thus the order of the includes
decided the USR: `callback_function_t` of three headers gave three USRs.
``cursor_usr`` writes the USR of a typedef at the scope of a file or
namespace with no file, and with a key of its target after it:
`c:@T@info_t~<8 hex digits>`.  The key names a named tag by its USR (a unit
can see it opaque), an anonymous tag by its place, a type that the
compiler declares (no location) by its name, and a character type that
C++ has as a keyword (`wchar_t`) by the integer type that C declares for
it.  Two typedefs of one name and
two targets thus give two USRs (`psa_global_data_t` of two `.c` files of
Zephyr).  A typedef in a class cannot be declared again: it is a member,
and the USR of the class (with its place) names it, not the file that
libclang writes before it, which is a different file for each copy of a
header.  A typedef in a function has the place of the function in its USR.
These get no key.  An
alias template gets the key of the target of its pattern: libclang gives
the template and its pattern one USR.

Measured on 1469 units of a Zephyr, an ESP32, an Mbed and an STM32 build:
one place gave one USR, except the limits below; no two USRs of libclang
got one new USR, except the typedefs that the order of the includes
divided.

Limits:

* The anonymous types that one expansion of a macro writes have one
  place, thus they keep one USR, as before.
* A USR that holds a tag type as the type of an argument (a
  specialization `cv<cfg_t>`, a parameter of a C++ function) holds its
  component with no mark.  Two such types of one name thus still give one
  USR there.  The fast test of ``cursor_usr`` finds the component all the
  same, thus such a USR costs a walk of the semantic parents that finds no
  type to mark.
* A typedef names a named tag by its USR, thus
  `typedef struct point {...} point;` in two `.c` files gives two structs
  and one typedef.
* libclang gives a USR in C that is not the USR in C++ for a `static inline`
  function of a header (C++ writes the signature).  A type in the body of
  such a function thus gets one USR in each language.  ``_internal_usrs``
  joins the references to the function, not to its local types.
* A class template with two definitions that the build does not merge
  (two main files) has no single place in the build.  A specialization of
  it in a unit that does not define it keeps its own place.  Two such
  definitions break the ODR.
* A typedef whose target differs in C and C++ (`#ifdef __cplusplus`) gets
  one USR in each language at one place.
* An anonymous member of a merged C++ class (`union { ... };`) and the
  unnamed type of a field of it (`enum { ... } mode;`) keep the place of
  each copy: no rule of the language pairs the anonymous members of two
  copies.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import functools
import hashlib
import os
import re
import weakref
from collections.abc import Iterator, Mapping
from typing import NamedTuple

import clang.cindex as cx

from ..path_key import canonical_path


class Place(NamedTuple):
    """The place of the definition of a type: the key of its file (``file_key``), its line and its column.

    *file* is None for a type with no file (a type that the compiler
    declares): such a type has no place to write.
    """

    file: str | None
    line: int
    column: int


@dataclasses.dataclass(frozen=True)
class TypeDecisions:
    """The decisions of the build that the USRs of each unit follow (``_odr.py``).

    *merged* holds ``(merge key, file key)`` of each definition of a C++
    type that the ODR makes one: its USR leaves out its place.

    *templates* gives, by the merge key of a class template, the place of
    its definition in the build: a unit that declares the template and does
    not define it takes the place from here (``_place_owner``).  A template
    with no single place in the build has no key: the parse then gives the
    same USRs as for a template that no unit looked for, thus two equal
    decisions are one dict.
    """

    merged: frozenset[tuple[str, str]] = frozenset()
    templates: Mapping[str, Place] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class PlaceRecord:
    """What the USRs of a context read from the decisions of the build (``type_decisions``).

    *files* holds the file key of each place that a USR holds, written or
    left out by the merge.  *templates* holds the merge key of each class
    template whose place a USR took from ``TypeDecisions.templates``, also
    when the build had none.
    """

    files: set[str] = dataclasses.field(default_factory=set)
    templates: set[str] = dataclasses.field(default_factory=set)


# The decisions of the build and the file keys of its merged definitions
# (``type_decisions``).
_DECISIONS: contextvars.ContextVar[tuple[TypeDecisions, frozenset[str]]] = contextvars.ContextVar(
    "fw_context_type_decisions", default=(TypeDecisions(), frozenset()),
)

# What the USRs of this context read from the decisions (``type_decisions``);
# None outside a context.
_RECORD: contextvars.ContextVar[PlaceRecord | None] = contextvars.ContextVar(
    "fw_context_place_record", default=None,
)

# The separator of the parts of a merge key (``merge_key``): a control
# character, which no USR and no spelling of a type holds.
_KEY_SEPARATOR = "\x1f"

# The component of a tag type in a USR of libclang: a type with a name
# (`@S@uart`, `@E@mode`), a class or union template or a partial
# specialization (`@ST>1#T@Box`, `@UT>1#T@Var`, `@SP>`), a member with no
# name (`@Sa`, `@Ua`, `@Ea`), or a type that a typedef names (`@SA@cfg_t`).
# A fast test: a USR with none gets no walk of the semantic parents.
_TAG_COMPONENT = re.compile(r"@[SUE](?:[aA@]|$)|@[SU][TP]>")

# The letter of the kind of a type in a USR (``USRGenerator::VisitTagDecl``).
_TYPE_LETTERS = {
    cx.CursorKind.STRUCT_DECL: "S", cx.CursorKind.CLASS_DECL: "S",
    cx.CursorKind.CLASS_TEMPLATE: "S", cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION: "S",
    cx.CursorKind.UNION_DECL: "U", cx.CursorKind.ENUM_DECL: "E",
}

# The end of the USR of an anonymous enum: `@Ea` and the name of its first
# constant, or `@Ea` alone for an enum with no constant.
_ANONYMOUS_ENUM_END = re.compile(r"@Ea(?:@[^@]*)?$")

_TYPEDEF_KINDS = frozenset({
    cx.CursorKind.TYPEDEF_DECL, cx.CursorKind.TYPE_ALIAS_DECL, cx.CursorKind.TYPE_ALIAS_TEMPLATE_DECL,
})

# The scopes of a typedef that C or C++ lets a header declare again.
_FILE_SCOPES = frozenset({cx.CursorKind.TRANSLATION_UNIT, cx.CursorKind.NAMESPACE, cx.CursorKind.LINKAGE_SPEC})

# The file that libclang writes before a typedef with no linkage outside a
# system header: a base name and a scope (`c:md.h@T@`, `c:x.h@N@`), not an
# offset (`c:x.c@312@...`, a typedef in a function).
_TYPEDEF_FILE = re.compile(r"^c:([^@]+)@(?=[A-Za-z])")

_TK = cx.TypeKind
_REFERENCE_KINDS = frozenset({
    _TK.POINTER, _TK.LVALUEREFERENCE, _TK.RVALUEREFERENCE, _TK.BLOCKPOINTER, _TK.MEMBERPOINTER,
})
_SIZED_ARRAY_KINDS = frozenset({_TK.CONSTANTARRAY, _TK.VECTOR})
_UNSIZED_ARRAY_KINDS = frozenset({_TK.INCOMPLETEARRAY, _TK.VARIABLEARRAY, _TK.DEPENDENTSIZEDARRAY})

# The predefined macro that names the integer type that C declares for a
# character type that C++ has as a keyword: `<stddef.h>` declares `wchar_t`
# as `__WCHAR_TYPE__`, and C11 7.28 makes `char16_t` and `char32_t` of
# `<uchar.h>` the types `uint_least16_t` and `uint_least32_t`.  WHY not
# `__CHAR32_TYPE__`: clang gives msp430 `unsigned int` there and `long
# unsigned int` for `uint_least32_t` (measured with libclang 22).
_CHARACTER_MACROS = {
    _TK.WCHAR: "__WCHAR_TYPE__", _TK.CHAR16: "__UINT_LEAST16_TYPE__", _TK.CHAR32: "__UINT_LEAST32_TYPE__",
}

# The kind of each integer type by the multiset of its type specifiers, as
# the sorted words of the text of a macro.  WHY a multiset: C11 6.7.2p2 lets
# the specifiers come in any order, and the text is not always the one of
# clang.  clang writes `unsigned short` (``TargetInfo::getTypeName``), GCC
# writes `short unsigned int`, and a build through a GCC driver gives
# libclang the macros of GCC (``_driver_query.libclang_args``).  Plain
# `char` is not here: the text does not tell if it is signed.
_INTEGER_SPECIFIERS = {
    "SCHAR": ("signed char",),
    "UCHAR": ("unsigned char",),
    "SHORT": ("short", "signed short", "short int", "signed short int"),
    "USHORT": ("unsigned short", "unsigned short int"),
    "INT": ("int", "signed", "signed int"),
    "UINT": ("unsigned", "unsigned int"),
    "LONG": ("long", "signed long", "long int", "signed long int"),
    "ULONG": ("unsigned long", "unsigned long int"),
    "LONGLONG": ("long long", "signed long long", "long long int", "signed long long int"),
    "ULONGLONG": ("unsigned long long", "unsigned long long int"),
}
_MACRO_INTEGER_KINDS: dict[tuple[str, ...], str] = {
    tuple(sorted(text.split())): kind for kind, texts in _INTEGER_SPECIFIERS.items() for text in texts
}

# The kind of the integer type of each macro of ``_CHARACTER_MACROS``, for
# each unit (``_character_kinds``).  Weak keys: the cache keeps no unit.
_CHARACTER_KINDS: weakref.WeakKeyDictionary[cx.TranslationUnit, dict[str, str]] = weakref.WeakKeyDictionary()


@functools.lru_cache(maxsize=16384)
def file_key(path: str) -> str:
    """The key of the file *path* in a USR: ``<base name>~<8 hex digits of the SHA-1 of the resolved path>``."""
    resolved = str(canonical_path(path))
    digest = hashlib.sha1(resolved.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()
    return f"{os.path.basename(resolved)}~{digest[:8]}"


@contextlib.contextmanager
def type_decisions(decisions: TypeDecisions) -> Iterator[PlaceRecord]:
    """Let ``cursor_usr`` follow the *decisions* of the build in this context.

    ``cursor_usr`` writes no place mark for a type whose definition
    *decisions* merges (``merge_key``), and it takes the place of a class
    template that the unit does not define from *decisions*
    (``_place_owner``).

    The context gives a ``PlaceRecord``, which ``cursor_usr`` fills with the
    file key of each place that a USR of the context holds, and with the
    merge key of each template whose place it looked for.  A change of the
    decisions parses again only the units whose record holds a file or a
    template of the change (``runner._settle_odr_merge``).

    WHY a context and not an argument: each caller of ``cursor_usr`` (the
    walk of the symbols, the references, the slots, the overrides) must give
    one USR for one type, and the decisions belong to the build, not to a
    call.  ``symbols.extract_all`` sets the context around each call of
    ``cursor_usr`` of a unit, in the thread that parses the unit: the index
    run, or a thread of the MCP server for ``reindex_file``
    (``asyncio.to_thread`` runs it in a copy of the context).  A value that
    a thread sets is visible only in the context of that thread, thus two
    parses never see the record of the other.
    """
    record = PlaceRecord()
    token = _DECISIONS.set((decisions, frozenset(file for _, file in decisions.merged)))
    record_token = _RECORD.set(record)
    try:
        yield record
    finally:
        _RECORD.reset(record_token)
        _DECISIONS.reset(token)


def odr_place(cursor: cx.Cursor) -> tuple[str, int, int] | None:
    """The merge key, the line and the column of the definition *cursor* of a type that the ODR can merge, else None.

    The build groups the places of the definitions by this key, and merges
    a group of two or more files (``_odr.py``).  The file of the place is the
    file of the symbol of *cursor*.  The line and the column give the place
    of a class template to the units that declare it and do not define it
    (``TypeDecisions.templates``).

    The place is the place of *cursor*, also for a specialization whose
    USR takes the place of its template (``_place_owner``): the row must
    not depend on the decisions of the build, or on the templates that the
    unit sees, else the decision that the rows give could change with the
    order of the claims and with each parse.  The merge of the key of such
    a specialization is not read when the build has a place of its
    template.

    None for a type in the body of a type (``_enclosing_type``): it follows
    the merge of that type (``_merged_mark``).  libclang gives such a type
    an other key in C (`c:@S@B`) than in C++ (`c:@S@A@S@B`), thus a group of
    its own depended on the language of the unit that claims the header.
    """
    if (
        cursor.kind not in _TYPE_LETTERS or not cursor.is_definition()
        or _enclosing_type(cursor) is not None
    ):
        return None
    location = cursor.location
    if location.file is None:
        return None
    key = merge_key(cursor)
    if key is None:
        return None
    return key, location.line, location.column


def _enclosing_type(owner: cx.Cursor) -> cx.Cursor | None:
    """The type whose body holds the definition of the type *owner*, else None.

    WHY the lexical parent and not the semantic parent: in C a struct in a
    struct has the scope of the file (its semantic parent is the unit), and
    a C++ definition out of the body (`struct A::B { ... };`) has the
    semantic parent `A` and is a definition of its own.
    """
    definition = owner.get_definition()
    parent = definition.lexical_parent if definition is not None else None
    return parent if parent is not None and parent.kind in _TYPE_LETTERS else None


def merge_key(cursor: cx.Cursor) -> str | None:
    """The key of the type *cursor* in the ODR merge: the same text for each definition of one C++ type, else None.

    The compiler decides which types the ODR can make one: a type with
    external linkage (``Cursor.linkage``).  libclang gives the same linkage
    in C and in C++: a named type at the scope of a file or a namespace and
    a type in it are external, a type of an anonymous namespace, of a
    function, and an anonymous enum are not (measured with libclang 22).
    A type at the scope of a file has one key in C and in C++.  A type in
    the body of a type has two (`c:@S@B` in C, `c:@S@A@S@B` in C++), thus
    it has no place to merge (``odr_place``): it follows the merge of the
    type around it (``_merged_mark``).

    An anonymous member (`union { ... };`) is external too, but two of one
    record have one USR, and nothing else of the compiler pairs the members
    of two definitions: it gets no key.

    The key is the USR of libclang.  That USR is the same text for two
    specializations whose arguments differ only in the qualifiers of a
    function type (`is_fn<R()>` and `is_fn<R() const>`, measured: libstdc++
    and Mbed), thus the key of a type in or under a specialization adds the
    canonical type of each such specialization, which the compiler prints
    with these qualifiers.
    """
    if cursor.is_anonymous() or cursor.linkage != cx.LinkageKind.EXTERNAL:
        return None
    spellings: list[str] = []
    node: cx.Cursor | None = cursor
    while node is not None and node.kind != cx.CursorKind.TRANSLATION_UNIT:
        if node.kind in _TYPE_LETTERS and node.specialized_template is not None:
            spellings.append(node.type.get_canonical().spelling)
        node = node.semantic_parent
    return cursor.get_usr() + "".join(f"{_KEY_SEPARATOR}{spelling}" for spelling in reversed(spellings))


def _place_owner(node: cx.Cursor) -> tuple[cx.Cursor, Place | None]:
    """The declaration whose definition gives the place of the type *node*, and that place (None: none).

    A specialization of a class template (`Box<int>`, `Box<int>::Inner`) is
    one type with its template and its arguments: the C++ rule makes the
    explicit specialization of one template for one list of arguments one
    entity, and each unit that uses it must declare it ([temp.expl.spec]).
    Thus each specialization has the place of the definition of its
    template, which ``specialized_template`` gives: the pattern, or the
    partial specialization that an instance comes from.

    WHY the template and not the definition of the specialization: libclang
    gives the definition of an instance the place of the declaration that
    the unit sees last.  That is the pattern for an implicit instance, and
    the line of `extern template struct Box<int>;` or of
    `template struct Box<int>;` for an explicit instantiation, thus one
    instance got three places.  libclang 22 does not tell the kind of a
    specialization (no ``TSK`` in its API), and the rule of the template
    needs none.

    A unit can declare a template and not define it
    (`template <class T> struct traits;`), and define an explicit
    specialization of it.  The place of the template then comes from the
    build (``_template_place_of_build``): a unit that sees the definition
    and a unit that does not must give one USR.  When the build has no
    single place of the template, the specialization is its own owner: no
    unit then sees a definition of the template, and the definition of the
    specialization is the only place.  A partial specialization is a
    template of its own: it is its own owner.
    """
    owner = node
    while owner.kind != cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION:
        template = owner.specialized_template
        if template is None:
            break
        if template.get_definition() is None:
            place = _template_place_of_build(template)
            if place is None:
                break
            return template, place
        owner = template
    return owner, _definition_place(owner)


def _definition_place(owner: cx.Cursor) -> Place | None:
    """The place of the definition of *owner* in this unit, or None when the unit has none (or it has no file)."""
    definition = owner.get_definition()
    if definition is None:
        return None
    location = definition.location
    if location.file is None:
        return None
    return Place(file_key(location.file.name), location.line, location.column)


def _template_place_of_build(template: cx.Cursor) -> Place | None:
    """The place of the definition of the class template *template* in the build (``TypeDecisions.templates``).

    The unit declares the template and does not define it.  The record of
    the context gets the key also when the build has no place (None): a
    later definition of the template in the build changes the USRs of this
    unit.
    """
    key = merge_key(template)
    if key is None:
        return None
    record = _RECORD.get()
    if record is not None:
        record.templates.add(key)
    return _DECISIONS.get()[0].templates.get(key)


def merged_mark_of_key(key: str) -> str:
    """The mark that a merged definition with the merge key *key* writes in place of its place.

    "" (the USR of libclang), except for a type in or under a
    specialization: libclang gives the same USR to the specializations whose
    arguments differ only in the qualifiers of a function type, and the hash
    of the key tells them apart (``merge_key``).
    """
    if _KEY_SEPARATOR not in key:
        return ""
    return "~" + hashlib.sha1(key.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()[:8]


def _place_mark(place: Place) -> str:
    return f"~{place.file}:{place.line}:{place.column}"


def _merged_mark(node: cx.Cursor, place: Place) -> str | None:
    """The mark of the type *node* when the context merges its definition at *place* (``type_decisions``), else None.

    The mark of a merged type is ``merged_mark_of_key``: "" (the USR of
    libclang), or the hash of its merge key in or under a specialization.

    A type in the body of a type (``_enclosing_type``) follows the merge of
    that type: "" when it is merged, else None.  In C++ such a type is a
    member, and the members of one class are one entity in each definition
    of the class.  The mark of the type around it, in the USR before it,
    tells the specializations apart.  In C the USR of such a type does not
    hold the type around it: ``_with_places`` writes it then
    (``_in_body_in_c``).
    """
    decisions, files = _DECISIONS.get()
    if not files:
        return None
    owner, _ = _place_owner(node)
    key = merge_key(owner)
    if key is None:
        return None
    enclosing = _enclosing_type(owner)
    if enclosing is not None:
        outer = _type_place(enclosing, _TYPE_LETTERS[enclosing.kind], enclosing.get_usr())
        if outer is None or outer.file is None or _merged_mark(enclosing, outer) is None:
            return None
        return ""
    if (key, place.file) not in decisions.merged:
        return None
    return merged_mark_of_key(key)


def cursor_usr(cursor: cx.Cursor) -> str:
    """The USR of *cursor* ("" for none), with the file key of a file-local USR, the place of each tag type, and the target of a typedef."""
    usr = cursor.get_usr()
    if not usr.startswith("c:"):
        return usr
    if cursor.kind in _TYPEDEF_KINDS:
        if _at_file_scope(cursor):
            return _typedef_usr(cursor, usr)
        parent = cursor.semantic_parent
        if parent is not None and parent.kind in _TYPE_LETTERS:
            # A typedef of a class is a member: the USR of the class (with
            # its place) names it, not the file of the copy of a header that
            # libclang writes before it.
            usr = _without_file(cursor, usr)
    if _TAG_COMPONENT.search(usr):
        # A file-local USR can hold a tag type with no offset of its own: a
        # member of an anonymous namespace (`c:x.cpp@aN@S@AN@Ua`).
        usr = _with_places(cursor, usr)
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


def _with_places(cursor: cx.Cursor, usr: str) -> str:
    """*usr* with the place of each tag type of *cursor* and its semantic parents after its component.

    The unnamed type of a named field gets the key of its file in place of
    the base name that libclang wrote (``_field_type_file``), and no mark.

    A type in the body of a type gets the USR that C++ gives it, also in C
    (``_cxx_usr``), and its own mark after it.

    The USR of such a type is a prefix of *usr* (the USR of a member is the
    USR of the type and the component of the member).  When it is not, *usr*
    stays as libclang wrote it: no guess.  A local variable inside a type
    has a USR of its own place (`c:drv.c@312@...`), which holds no such
    prefix and needs no mark.
    """
    # Each edit replaces usr[start:end] with a text: a mark is an edit with
    # start == end at the end of the prefix of its type.
    edits: list[tuple[int, int, str]] = []
    record = _RECORD.get()
    node: cx.Cursor | None = cursor
    while node is not None and node.kind != cx.CursorKind.TRANSLATION_UNIT:
        letter = _TYPE_LETTERS.get(node.kind)
        if letter is not None:
            prefix = node.get_usr()
            place = _type_place(node, letter, prefix)
            if place is not None:
                if place.file is None or not usr.startswith(prefix):
                    return usr
                if record is not None:
                    record.files.update(_decision_files(node, place))
                mark = _merged_mark(node, place)
                if mark is None:
                    mark = _place_mark(place)
                if _in_body_in_c(node):
                    edits.append((0, len(prefix), _cxx_usr(node) + mark))
                elif mark:
                    edits.append((len(prefix), len(prefix), mark))
            elif node.is_anonymous():
                edit = _field_type_file(node, letter, prefix)
                if edit is not None:
                    if not usr.startswith(prefix):
                        return usr
                    edits.append(edit)
        node = node.semantic_parent
    # From the end of *usr* to its start: an edit does not move the place of
    # an edit before it.
    for start, end, text in sorted(edits, reverse=True):
        usr = usr[:start] + text + usr[end:]
    return usr


def _in_body_in_c(node: cx.Cursor) -> bool:
    """True when *node* is a type in the body of a type, with the scope of the file: C, where libclang writes no type around it.

    WHY the scope of the file and not each scope other than a type: a type
    in the body of a type in a function of C has the scope of the function,
    and libclang writes the place of the function into its USR
    (`c:drv.c@25@F@f@S@B`).  C++ writes the function with its signature
    (`@F@f#`), thus the form of C++ cannot make the two USRs one.  The USR
    keeps the form of C, with the mark of the type after its component.
    """
    if _enclosing_type(node) is None:
        return False
    parent = node.semantic_parent
    return parent is not None and parent.kind == cx.CursorKind.TRANSLATION_UNIT


def _cxx_usr(node: cx.Cursor) -> str:
    """The USR that C++ gives *node*, a type in the body of a type at the scope of a file in C, with no mark of its own.

    In C a type in the body of a type has the scope of the file, and its
    USR does not hold the type around it (`c:@S@B`).  C++ writes the type
    around it, with the mark of that type (`c:@S@A~<place>@S@B`).  One
    header that a C unit and a C++ unit read thus gave one definition two
    USRs, and the references of the units of the language that did not
    claim the header had no symbol.  Measured: 456 references of C units
    to the fields of a struct in a struct (two file systems and an event
    queue of Mbed), 2026-11-04.

    WHY the form of C++ and not the form of C: the USR of C does not hold
    the type around it, and the USR of C++ does.  The mark of the type
    itself tells apart two `struct B` of two structs in each form.  The USR
    of the type around it is ``cursor_usr``, thus it holds the mark of that
    type, or the form of C++ again when it is in the body of a type too.
    """
    enclosing = _enclosing_type(node)
    if enclosing is None:
        return node.get_usr()
    return cursor_usr(enclosing) + node.get_usr()[len("c:"):]


def _decision_files(node: cx.Cursor, place: Place) -> Iterator[str]:
    """The file key of each place whose merge decides the mark of the type *node* at *place*.

    The place of *node*, and the places of the types whose bodies hold its
    definition: a type in the body of a type follows the merge of that type
    (``_merged_mark``).  In C such a type has the scope of the file, thus
    the type around it is not a semantic parent that ``_with_places`` walks.
    """
    if place.file is not None:
        yield place.file
    enclosing = _enclosing_type(_place_owner(node)[0])
    if enclosing is not None:
        outer = _type_place(enclosing, _TYPE_LETTERS[enclosing.kind], enclosing.get_usr())
        if outer is not None and outer.file is not None:
            yield from _decision_files(enclosing, outer)


def _field_type_file(node: cx.Cursor, letter: str, usr: str) -> tuple[int, int, str] | None:
    """The edit that puts ``file_key`` in place of the file in *usr*, the USR of the unnamed type of a named field.

    libclang writes the place of such a type (`struct { ... } f;`) as the
    base name of its file and an offset (`@S@drv.h@15`,
    ``USRGenerator::VisitTagDecl``).  Two files of one base name can thus
    give one USR: two copies of a header, when the text before the type is
    the same, and the ODR merge removes the mark of the record that holds
    the type in each copy.  The key of the file makes it the place of one
    file, as ``_with_file_key`` does for the prefix of a USR.

    None when *usr* does not end with the file and the offset of the
    location of *node*: the USR stays as libclang wrote it, no guess.
    """
    location = node.location
    if location.file is None:
        return None
    base = os.path.basename(location.file.name)
    end = f"@{letter}@{base}@{location.offset}"
    if not usr.endswith(end):
        return None
    start = len(usr) - len(end) + len(letter) + 2
    return start, start + len(base), file_key(location.file.name)


def _type_place(node: cx.Cursor, letter: str, usr: str) -> Place | None:
    """The place that identifies the tag type *node* with the USR *usr*, or None when its USR needs none.

    * An anonymous type: its declaration, which is its definition.  Its
      ``Place.file`` is None when the declaration has no file.
    * The unnamed type of a named field (`struct { ... } f;`): None.
      libclang writes its place (`@S@drv.c@15`), and ``_field_type_file``
      puts the key of the file in it.
    * A named type: its definition, or the definition of its template for
      a specialization (``_place_owner``).  None when the unit has none (an
      opaque type): the USR stays nominal.  None for a type that the
      compiler declares (`__va_list_tag`): it has no place, and no second
      definition.
    """
    if _is_anonymous_type(node, letter, usr):
        location = node.location
        return Place(file_key(location.file.name) if location.file is not None else None, location.line, location.column)
    if node.is_anonymous():
        return None
    return _place_owner(node)[1]


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
    but libclang writes its place (`@S@drv.c@15`), not this component: the
    test is False.

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


def _at_file_scope(cursor: cx.Cursor) -> bool:
    parent = cursor.semantic_parent
    return parent is not None and parent.kind in _FILE_SCOPES


def _typedef_usr(cursor: cx.Cursor, usr: str) -> str:
    """The USR of the typedef *cursor* at the scope of a file or namespace: no file, and the key of its target.

    A typedef in an anonymous namespace keeps its file: the scope is the
    file.
    """
    usr = _without_file(cursor, usr)
    if not usr.startswith("c:@"):
        usr = _with_file_key(cursor, usr)
    key = _target_key(_typedef_target(cursor))
    digest = hashlib.sha1(key.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()
    return f"{usr}~{digest[:8]}"


def _without_file(cursor: cx.Cursor, usr: str) -> str:
    """*usr* of the typedef *cursor* with no file before it, when the file is the one of its declaration.

    libclang writes the file of the declaration before the USR of a typedef
    with no linkage outside a system header (`c:md.h@T@info_t`,
    `c:p.h@N@drv@S@Parser@T@size_type`).  The scope names the typedef: a
    file, a namespace, a class.  A typedef in an anonymous namespace keeps
    the file, because that scope is the file.
    """
    match = _TYPEDEF_FILE.match(usr)
    if match is None or _in_anonymous_namespace(cursor):
        return usr
    canonical = cursor.canonical
    file = canonical.location.file if canonical is not None else None
    if file is None or os.path.basename(file.name) != match.group(1):
        return usr
    return "c:@" + usr[match.end():]


def _typedef_target(cursor: cx.Cursor) -> cx.Type:
    """The type that the typedef *cursor* names.

    WHY the pattern of an alias template: libclang gives an alias template
    (`template <class T> using rm_t = ...`) and the alias that is its pattern
    one USR, and the walk reaches the two.  The index keeps the pattern, and
    a reference names the template.  The two thus need one key, and only the
    pattern has a target.  libclang always visits the pattern as a child of
    the template (``CursorVisitor::VisitTypeAliasTemplateDecl``).
    """
    if cursor.kind != cx.CursorKind.TYPE_ALIAS_TEMPLATE_DECL:
        return cursor.underlying_typedef_type
    for child in cursor.get_children():
        if child.kind == cx.CursorKind.TYPE_ALIAS_DECL:
            return child.underlying_typedef_type
    raise ValueError(f"the alias template {cursor.spelling} has no pattern")


def _in_anonymous_namespace(cursor: cx.Cursor) -> bool:
    node = cursor.semantic_parent
    while node is not None and node.kind != cx.CursorKind.TRANSLATION_UNIT:
        if node.kind == cx.CursorKind.NAMESPACE and not node.spelling:
            return True
        node = node.semantic_parent
    return False


def _target_key(type_: cx.Type) -> str:
    """The key of the type *type_* as the target of a typedef: the same text for the same type in each unit.

    WHY the canonical type: `uint32_t` is `__uint32_t` in one header and
    `__UINT32_TYPE__` in an other, and the two are `unsigned int`.  A named
    tag is its USR and not its place: a unit can see it opaque.  A pointer
    takes the rule of its pointee, and so does each part of a function or an
    array.  A type that the compiler declares has no location (`__va_list`
    of ARM is `std::__va_list` in C++): its name is the key.

    WHY a character type of C++ gets the kind of an integer type: C++ has
    `wchar_t`, `char16_t`, `char32_t` and `char8_t` as keywords, and C
    declares each as a typedef of an integer type.  One header thus gave a
    typedef of `wchar_t` one USR in C and an other in C++.  The key of such
    a type is the kind of the type that C declares (``_character_kinds``):
    `char8_t` is `unsigned char` (C23 7.30).
    """
    canonical = type_.get_canonical()
    qualifiers = ("c" if canonical.is_const_qualified() else "") + ("v" if canonical.is_volatile_qualified() else "")
    kind = canonical.kind
    if kind in _REFERENCE_KINDS:
        return f"{qualifiers}{kind.name}({_target_key(canonical.get_pointee())})"
    if kind in _SIZED_ARRAY_KINDS:
        return f"{qualifiers}{kind.name}{canonical.element_count}({_target_key(canonical.element_type)})"
    if kind in _UNSIZED_ARRAY_KINDS:
        return f"{qualifiers}{kind.name}({_target_key(canonical.element_type)})"
    if kind == _TK.FUNCTIONPROTO:
        arguments = ",".join(_target_key(argument) for argument in canonical.argument_types())
        variadic = ",..." if canonical.is_function_variadic() else ""
        return f"{qualifiers}F({arguments}{variadic}){_target_key(canonical.get_result())}"
    if kind == _TK.FUNCTIONNOPROTO:
        # `void f()` of a header is a function with no prototype in C and
        # one with no arguments in C++: one text, thus one key.
        return f"{qualifiers}F(){_target_key(canonical.get_result())}"
    if kind in (_TK.RECORD, _TK.ENUM):
        declaration = canonical.get_declaration()
        if declaration.location.file is None:
            return f"{qualifiers}builtin:{declaration.spelling}"
        letter = _TYPE_LETTERS.get(declaration.kind)
        if letter is not None and (
            declaration.is_anonymous() or _is_anonymous_type(declaration, letter, declaration.get_usr())
        ):
            return qualifiers + cursor_usr(declaration)
        nominal = declaration.get_usr()
        return qualifiers + (nominal if nominal.startswith("c:@") else _with_file_key(declaration, nominal))
    if kind in _CHARACTER_MACROS:
        return qualifiers + _character_kinds(canonical.translation_unit)[_CHARACTER_MACROS[kind]]
    if kind == _TK.UNEXPOSED and _unqualified_spelling(canonical) == "char8_t":
        # libclang has no kind of `char8_t` of C++20.
        return qualifiers + "UCHAR"
    return qualifiers + (canonical.spelling if kind == _TK.UNEXPOSED else kind.name)


def _unqualified_spelling(type_: cx.Type) -> str:
    """The spelling of *type_* with no `const` and no `volatile`: libclang writes the qualifiers into it."""
    return " ".join(word for word in type_.spelling.split() if word not in ("const", "volatile"))


def _character_kinds(unit: cx.TranslationUnit) -> dict[str, str]:
    """The kind of the integer type of each macro of ``_CHARACTER_MACROS`` in *unit*, by the name of the macro.

    The macros are cursors of the parse with no file, when the parse keeps
    the preprocessing record (``symbols.extract_all`` does).  The last
    definition wins: a `-D` of the command line comes after the macros of
    the compiler.  The text can give the specifiers in any order
    (``_MACRO_INTEGER_KINDS``).

    Raises ValueError when *unit* has no such macro, or a text that is not
    an integer type with a known sign (``_INTEGER_SPECIFIERS``: plain
    `char` is not there): the key would then depend on
    the language of the unit, and that is the fault that this function
    prevents.
    """
    kinds = _CHARACTER_KINDS.get(unit)
    if kinds is not None:
        return kinds
    wanted = frozenset(_CHARACTER_MACROS.values())
    texts: dict[str, str] = {}
    for node in unit.cursor.get_children():
        if node.kind == cx.CursorKind.MACRO_DEFINITION and node.location.file is None and node.spelling in wanted:
            texts[node.spelling] = " ".join(token.spelling for token in list(node.get_tokens())[1:])
    kinds = {}
    for macro in sorted(wanted):
        if macro not in texts:
            raise ValueError(
                f"the unit {unit.spelling} has no predefined macro {macro}: parse it with the preprocessing record"
            )
        kind = _MACRO_INTEGER_KINDS.get(tuple(sorted(texts[macro].split())))
        if kind is None:
            raise ValueError(
                f"the unit {unit.spelling} gives {macro} as {texts[macro]!r}, which is not an integer type with a known sign"
            )
        kinds[macro] = kind
    _CHARACTER_KINDS[unit] = kinds
    return kinds
