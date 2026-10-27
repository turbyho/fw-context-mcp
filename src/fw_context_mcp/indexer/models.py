"""Data models for the fw-context-mcp index.

WHY separate from symbols.py: symbols.py imports libclang (``clang.cindex``)
and sqlite3.  These dataclass definitions are pure Python — no C extension
bindings, no DB driver.  Keeping them isolated allows other modules to
import the data types without pulling in heavy dependencies or risking
import errors on systems without libclang installed.

Extracted from ``symbols.py`` — pure dataclass definitions with no
AST traversal or DB dependencies.  Imported throughout the codebase
without pulling in libclang or sqlite3.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class RefOrigin(enum.IntFlag):
    """The analysis that found a reference (``refs.origins``, a bit set).

    WHY a set of bits and not one value: one reference site can be found by
    more than one analysis, and each of them is a fact about the edge.  The
    row of a site keeps each one, thus a reader can tell an edge that the
    AST of the compiler gives from an edge that a name resolved.

    * ``DIRECT`` — the AST walk: the cursor of the reference names its target.
    * ``TEXT`` — tokens or source text, with the target resolved by its
      name (``&Class::method`` where the AST is opaque).  The name can be
      wrong; a later step replaces these.
    * ``ASM`` — the assembly reader: a vector table slot or an alias.
    * ``DISPATCH`` — a dispatch bridge of the post-processing: an entry
      point that runs a callback which a call site registered.
    * ``INSTANTIATION`` — the walk of the body of a template instance: a
      call that the template body names through a dependent type, which
      only an instantiation resolves.  The edge is stored on the template
      (caller and target), at the place of the call in the template.  The
      units that found it are in ``ref_producers``.
    * ``SLOT`` — the resolution of the slot facts (``_slot_resolution``):
      an indirect call reaches a function that the code puts into its slot
      or into a slot that flows into it.
    * ``TYPE_ONLY`` — the same resolution, when the slot does not tell the
      targets (no slot, or a slot that code out of view can write): each
      function whose address the build takes and whose type is the type of
      the call.  A superset: the type is a fact of the compiler, but not
      each function of the type is a target.
    """

    DIRECT = 1
    TEXT = 2
    ASM = 4
    DISPATCH = 8
    INSTANTIATION = 16
    SLOT = 32
    TYPE_ONLY = 64


def origins_or_sql(column: str) -> str:
    """Give an SQL aggregate expression: the bitwise OR of *column* over a group.

    SQLite has no bitwise OR aggregate, thus the expression takes the MAX of
    each bit.  It is built from ``RefOrigin``, thus a new bit needs no edit.
    *column* is a column name of the caller's query, never user input.
    """
    return " | ".join(f"MAX({column} & {int(flag)})" for flag in RefOrigin)


def origin_names(origins: int) -> list[str]:
    """Give the names of the bits of *origins*, in bit order (``["direct", "text"]``)."""
    return [str(flag.name).lower() for flag in RefOrigin if origins & flag]


@dataclass
class Reference:
    """An edge from a call or reference site to the symbol it refers to.

    Attributes:
        to_usr: USR of the referenced definition (links to ``Symbol.usr``).
        from_file: Absolute path of the file containing the reference.
        from_line: Source line of the reference expression.
        from_col: Source column of the reference expression, counted from
            1 as libclang counts it, or 0 when the producer has no column
            (the assembly reader, the scan of the source text).  The column
            separates two calls on one line: ``f(); f();`` are two sites.
            For a reference in a macro expansion it is the column of the
            expansion, thus two calls of one expansion share it.  For an
            indirect reference it is the place of the expression that
            names the function (``f`` in ``reg(f)``), so that each
            enclosing call that reaches the reference gives one key.
        from_usr: USR of the enclosing function or method (the caller), or
            None when the reference appears at file scope.
        ref_kind: Classification of the reference — ``"call"`` for direct
            function calls, ``"ref"`` for variable/enum reads, ``"member"``
            for member accesses, ``"indirect"`` for function pointers found
            in call arguments, assignments, variable initializers, or
            struct/array init lists.
        slot_index: Position of this reference inside the construct that
            holds it, counting from zero, or None when the construct has no
            position to give.

            Set for an ``"indirect"`` reference that comes from an element
            of a positional one-dimensional array initializer, where the
            position of the element is its index.  That is what turns "a
            function address appears on line 11 of isr_tables.c" into "a
            function address sits in slot 0" — and a table of function
            addresses is a vector table, whether an assembler wrote it or a
            generator emitted C.

            None wherever a position is not an index: a designated
            initializer (``[5] = &fn``), a struct initializer (which
            numbers fields), a multi-dimensional table (which has no single
            index), and every reference that no initializer holds.  A wrong
            slot number in front of a reader is worse than none, so the
            conditions are strict — see ``symbols._init_list_gives_slots``.
        origins: The analyses that found the reference, a set of
            ``RefOrigin`` bits.  Required: a reference with no origin
            would read as a fact of nothing.

    One row of ``refs`` is one reference site of one caller: the target,
    the place (file, line, column), the kind, the slot and ``from_usr``.
    WHY the caller is part of the identity: a macro that defines several
    functions on one line gives each of them a call at one place of the
    expansion (measured: ``readMR`` and ``writeMR`` of one register macro,
    the ``_u8``/``_u16``/``_u32`` variants of one atomic macro).  A second
    analysis that finds the same site adds its bit to ``origins``
    (``db._refs.insert_refs_batch``).
    """
    to_usr: str        # USR of the referenced definition (links to Symbol.usr)
    from_file: str     # file containing the reference (absolute, as clang reports)
    from_line: int
    from_usr: str | None   # USR of the enclosing function/method (caller), or None
    ref_kind: str      # "call" | "ref" | "member" | "indirect" | "implicit_construct"
    slot_index: int | None = None
    from_col: int = field(kw_only=True)
    origins: int = field(kw_only=True)


@dataclass
class Macro:
    """A preprocessor macro definition (``#define``).

    Only collected when ``PARSE_DETAILED_PROCESSING_RECORD`` is active
    (enabled by default for all translation units).

    Attributes:
        config_hash: Build configuration fingerprint.
        file_id: Foreign key to ``files.id`` in the index database.
        name: The macro name (without leading ``#define``).
        value: The replacement text alone.  The parameter list of a
            function-like macro is NOT part of it — see *params*.
        params: The parameter list of a function-like macro, without its
            parentheses and written ``a, b``.  Empty for an object-like
            macro AND for ``NAME()``, thus *is_function_like* and not this
            string says which of the two a macro is.
        expanded_value: Fully expanded value from ``clang -dM -E`` (set
            later by the macros.py driver; empty during initial parsing).
        line: Source line number (1-based) of the ``#define`` directive.
        is_function_like: ``True`` for function-like macros
            (``#define SQUARE(x) ((x)*(x))``).
        file: Absolute path of the file containing the macro definition.
    """
    config_hash: str = ""
    file_id: int = 0
    name: str = ""
    value: str = ""
    params: str = ""
    expanded_value: str = ""
    line: int = 0
    is_function_like: bool = False
    file: str = ""


@dataclass
class InheritanceRecord:
    """A C++ inheritance edge: ``class Derived : public Base { ... }``."""
    derived_usr: str   # USR of the derived class (child)
    base_usr: str      # USR of the base class (parent)
    access: str        # "public", "protected", or "private"
    is_virtual: bool   # True for virtual inheritance


@dataclass
class PendingDispatch:
    """Dispatch API call detected during indexing, resolved in post-processing.

    Records a likely dispatch-registration pattern (e.g. ``EventQueue::call_every``
    with ``&Class::method`` as an argument) before the dispatch entry point's
    USR is known.  Resolver runs after all translation units are indexed.

    Attributes:
        callee_qn: Qualified name of the dispatch API method called
            (e.g. ``"events::EventQueue::call_every"``).
        target_qn_partial: Partial qualified name of the callback target
            (e.g. ``"ZBLE::watch_ble"``).
        target_name: Unqualified name of the callback target
            (e.g. ``"watch_ble"``).
        file: Source file path, relative to project root.
        line: Source line number (1-based).
        caller_usr: USR of the enclosing function that calls the dispatch API,
            or None when at file scope.
    """
    callee_qn: str
    target_qn_partial: str
    target_name: str
    file: str
    line: int
    caller_usr: str | None
    target_usr: str | None = None


# The slot facts of the indirect calls (indexer/_slots.py).  A slot key is
# `F|<field USR>`, `V|<variable USR>`, `P|<code key>|<index>` or
# `R|<code key>`; the place of a fact is the file (absolute while the
# unit is extracted), the line and the column of the expression.


@dataclass(frozen=True)
class SlotSource:
    """The address of a function or a lambda goes into a slot.

    Attributes:
        function: The code key of the function (`symbols._code_usr`).
        function_type: Its type key (`void (int)`, `M:` for a method with
            `this`, `lambda`).
        slot: The slot that receives it, or "" when nothing known does
            (the address is taken: only its type tells its calls).
    """

    file: str
    line: int
    column: int
    function: str
    function_type: str
    slot: str


@dataclass(frozen=True)
class SlotFlow:
    """The value of the slot *source* goes into the slot *target*."""

    file: str
    line: int
    column: int
    source: str
    target: str


@dataclass(frozen=True)
class SlotSite:
    """An indirect call.

    Attributes:
        ordinal: The position of the site among the sites at its place,
            from 0.  WHY: the place is the start of the call, thus the two
            calls of `f(x)(y)`, and each call of one macro expansion, have
            one place.  The place and the ordinal are the key that an
            argument of the site carries.
        caller: The code key of the code that makes it, or None.
        kind: "pointer" (a call through a function pointer), "object" (the
            ``operator()`` of a callable object), or "address" (a call of
            an address that is a number: the start of the application from
            a bootloader, a routine of the ROM; no code of the build is its
            target).
        slot: The slot that the callee names, or "" (see *reason*).
        slot_type: The type key of the callee.
        reason: Why no slot is known, when *slot* is "".  For an "address"
            site, the source text of the address (`*(uint32_t*)0x08004004`).
        callee: For an "object" site, the code key of the ``operator()``
            that the call names: it is a target of the site too (a functor
            with no wrapper), thus the arguments of the site go into its
            parameters.  "" for a "pointer" site.
    """

    file: str
    line: int
    column: int
    ordinal: int
    caller: str | None
    kind: str
    slot: str
    slot_type: str
    reason: str = ""
    callee: str = ""


@dataclass(frozen=True)
class SlotArgument:
    """A value that the indirect call at the place passes as argument *index*.

    Attributes:
        ordinal: The ordinal of the site at the place (`SlotSite.ordinal`).
        value_kind: "function" (*value* is a code key) or "slot" (*value*
            is a slot key).
    """

    file: str
    line: int
    column: int
    ordinal: int
    index: int
    value_kind: str
    value: str


@dataclass(frozen=True)
class SlotEscape:
    """Code that the tree does not show can write the slot (its address is
    taken, or its value is cast to an integer or to ``void *``)."""

    file: str
    line: int
    column: int
    slot: str
    reason: str


@dataclass
class Symbol:
    """A parsed C/C++ symbol extracted from a translation unit.

    Represents a single declaration or definition encountered during
    libclang AST traversal.  Every symbol carries a ``usr`` that uniquely
    identifies it across translation units, a ``qualified_name`` built
    from semantic parent traversal, and metadata specific to its kind
    (signature for callables, enum values for constants, virtual flags
    for methods, and template relationships for specializations).

    Attributes:
        name: Unqualified symbol name (e.g. ``uart_init``).
        qualified_name: Fully qualified name with ``::`` separators,
            built by traversing semantic parents
            (e.g. ``namespace::Class::method``).
         kind: Symbol kind string — one of ``"function"``, ``"method"``,
             ``"constructor"``, ``"destructor"``, ``"class"``, ``"struct"``,
             ``"enum"``, ``"enum_constant"``, ``"typedef"``,
             ``"varglobal"`` (file/namespace-scope, static class member),
             ``"varlocal"`` (function-local),
             ``"variable"`` (legacy, pre-split),
             ``"field"`` (class/struct member),
             or ``"namespace"``.
        file: Absolute path to the source file containing this symbol.
        line: Start line of the declaration or definition (1-based).
        column: Start column of the declaration or definition (0-based).
        is_definition: True when the cursor is a definition; for
            ``_DECL_KINDS`` (function, function template, method) the
            declaration is indexed even without a definition.
        signature: Human-readable signature for callables — combines
            return type, name, and parameter list.  Empty string for
            non-callable symbols.
        docstring: Raw comment text from above the symbol, with comment
            markers (``/**``, ``//``, ``*``) stripped.  Line breaks are
            preserved so Doxygen tags (``@brief``, ``@param``, ``@return``)
            remain structured for LLM analysis.
        usr: libclang Unified Symbol Resolution — a cross-translation-unit
            identifier that links declarations and definitions of the
            same symbol.
        end_line: Last source line of the definition extent, or 0 when
            the extent is unavailable (e.g. the end lies in a different
            file due to macro expansion).
        enum_value: Integer value for ``enum_constant`` symbols.
            ``None`` for all other symbol kinds.
        is_virtual: True for virtual ``CXX_METHOD`` and destructor
            declarations.
        is_pure_virtual: True for pure virtual methods (marked ``= 0``).
         parent_usr: USR of the enclosing class, struct, template,
             or function (for varlocal).  Empty for free functions
             and file-scope symbols.
        is_template: True when this is a ``CLASS_TEMPLATE``,
            ``FUNCTION_TEMPLATE``, or partial specialization declaration.
        template_usr: USR of the primary template.  Non-empty only when
            this symbol is an instantiation of a template (e.g. a
            ``CLASS_DECL`` that was generated from a ``CLASS_TEMPLATE``).
        link_name: The name the linker resolves to this symbol (its mangled
            name), for a function or variable with external linkage.  Empty
            for all else.
        is_weak: A declaration in the translation unit has the attribute
            `weak` (`__attribute__((weak))`, `[[gnu::weak]]`, through a
            macro too), thus the object file defines the symbol weakly.
            `#pragma weak` is not seen: libclang does not give it.
        is_tentative: A C tentative definition (C11 6.9.2): a file-scope
            object with no initializer and no ``extern`` (`int g_x;`).  It
            is a definition, but a definition with an initializer of the
            same object in another unit wins over it, as the linker merges
            a common symbol (``-fcommon``) into it.
    """
    name: str
    qualified_name: str      # namespace::Class::method
    kind: str                # "function", "class", "struct", "enum", etc.
    file: str                # absolute path
    line: int
    column: int
    is_definition: bool
    signature: str           # return type + params for callables
    docstring: str           # raw comment above the symbol
    usr: str                 # libclang Unified Symbol Resolution
    end_line: int = 0        # last line of the definition extent (0 if unknown)
    enum_value: int | None = None  # value of enum constant (ENUM_CONSTANT_DECL only)
    is_virtual: bool = False          # True for virtual CXX_METHOD
    is_pure_virtual: bool = False     # True for pure virtual (= 0) CXX_METHOD
    parent_usr: str = ""    # USR of enclosing class/struct (empty for free functions)
    is_template: bool = False  # True for CLASS_TEMPLATE, FUNCTION_TEMPLATE declarations
    template_usr: str = ""   # USR of the primary template (non-empty = this is an instantiation)
    source: str = ""        # function/method body text (from libclang extent, only for is_definition=True)
    link_name: str = ""      # the name the linker resolves (external linkage only)
    is_weak: bool = False    # a declaration of the TU makes it weak (`_weak_usrs`)
    is_tentative: bool = False  # a C tentative definition (`int g_x;` at file scope)
