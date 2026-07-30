"""Data models for the fw-context-mcp index.

Extracted from ``symbols.py`` — pure dataclass definitions with no
AST traversal or DB dependencies.  Imported throughout the codebase
without pulling in libclang or sqlite3.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Reference:
    """An edge from a call or reference site to the symbol it refers to.

    Attributes:
        to_usr: USR of the referenced definition (links to ``Symbol.usr``).
        from_file: Absolute path of the file containing the reference.
        from_line: Source line of the reference expression.
        from_usr: USR of the enclosing function or method (the caller), or
            None when the reference appears at file scope.
        ref_kind: Classification of the reference — ``"call"`` for direct
            function calls, ``"ref"`` for variable/enum reads, ``"member"``
            for member accesses, ``"indirect"`` for function pointers found
            in call arguments, assignments, variable initializers, or
            struct/array init lists.
    """
    to_usr: str        # USR of the referenced definition (links to Symbol.usr)
    from_file: str     # file containing the reference (absolute, as clang reports)
    from_line: int
    from_usr: str | None   # USR of the enclosing function/method (caller), or None
    ref_kind: str      # "call" | "ref" | "member" | "indirect" | "implicit_construct"


@dataclass
class Macro:
    """A preprocessor macro definition (``#define``).

    Only collected when ``PARSE_DETAILED_PROCESSING_RECORD`` is active
    (enabled by default for all translation units).

    Attributes:
        config_hash: Build configuration fingerprint.
        file_id: Foreign key to ``files.id`` in the index database.
        name: The macro name (without leading ``#define``).
        value: Raw value text from the ``#define`` directive.
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
class IndirectCallSite:
    """A call site where a function pointer is invoked through a field or variable.

    Unlike ``Reference`` (which points to a resolved FUNCTION_DECL), this
    records the FIELD_DECL or VAR_DECL that holds the function pointer —
    the actual function called depends on runtime state.

    Attributes:
        from_file: Absolute path of the file containing the call.
        from_line: Source line of the call expression.
        from_usr: USR of the enclosing function or method, or None.
        expr_text: Callee expression text, e.g. ``"driver.onData"`` or
            ``"stored_callback"``.
        target_usr: USR of the function pointer field or variable being
            called (the FIELD_DECL / VAR_DECL, not the target function).
        target_name: Display name of the field or variable, e.g. ``"onData"``.
        fn_ptr_type: Function pointer type signature string,
            e.g. ``"void (*)(uint8_t *, size_t)"``.
    """
    from_file: str
    from_line: int
    from_usr: str | None
    expr_text: str
    target_usr: str
    target_name: str
    fn_ptr_type: str


@dataclass
class FnPointerAssignment:
    """A function assigned to a function pointer field, variable, or parameter.

    Records both sides of ``field = &function`` and ``register(&function)``
    patterns.  The *lhs_usr* links to the FIELD_DECL, VAR_DECL, or PARM_DECL
    that receives the function pointer; *rhs_usr* links to the FUNCTION_DECL
    that is assigned.

    Together with ``IndirectCallSite``, this enables Phase 3 linking:
    ``fp_assignments.lhs_usr = indirect_call_sites.target_usr`` answers
    "which functions can be called through this field?"

    Attributes:
        from_file: Absolute path of the file containing the assignment.
        from_line: Source line of the assignment expression.
        lhs_usr: USR of the field, variable, or parameter that receives
            the function pointer.
        lhs_name: Display name of the left-hand side, e.g. ``"onData"``
            or ``"cb"``.
        rhs_usr: USR of the function being assigned.
        rhs_name: Display name of the assigned function, e.g. ``"handler"``.
        fn_ptr_type: Function pointer type signature string,
            e.g. ``"void (*)(uint8_t *, size_t)"``.
        method: How the assignment was detected — ``"assignment"``
            (BINARY_OPERATOR), ``"call_arg"`` (CALL_EXPR argument),
            ``"var_init"`` (VAR_DECL initializer), or ``"init_list"``
            (INIT_LIST_EXPR struct/array init).
        from_usr: USR of the enclosing function or method, or None.
    """
    from_file: str
    from_line: int
    lhs_usr: str
    lhs_name: str
    rhs_usr: str
    rhs_name: str
    fn_ptr_type: str
    method: str
    from_usr: str | None


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