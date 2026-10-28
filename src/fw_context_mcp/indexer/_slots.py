"""The slots of the indirect calls: the facts of one translation unit.

An indirect call goes through a slot: a place that holds a function.  A
slot is a field (``F|<USR>``), a variable (``V|<USR>``), a parameter
(``P|<code key>|<index>``) or the return value of a function
(``R|<code key>``).  Its type is a pointer to a function, or an object with
an ``operator()`` (mbed ``Callback``, ``std::function``): ``W:<USR>``.  The
code key is ``symbols._code_usr`` (the template of an instance, the lambda
of a closure).  A field or a static member of a class template instance is
the member of its template (``_Collector.member_usr``).

The walk records facts of the abstract syntax tree only, and matches no
name:

* a **source**: the address of a function or a lambda goes into a slot (or
  into nothing that is known: its address is taken, and only its type
  tells the calls that can reach it);
* a **flow**: the value of one slot goes into another (assignment,
  initialization, argument of a direct call into the parameter, return,
  member initializer, copy of a callable object);
* a **site**: an indirect call, through a slot.  The ``operator()`` of an
  object that is no closure is a site too: the object can hold any
  function (``std::function``).  A call of an address that is a number
  (a bootloader that starts the application) is a site of its own kind;
* an **argument**: a value that a site passes, which goes into a parameter
  of each function that the site reaches (known after the resolution);
* an **escape**: the address of a slot (or of an element of it) is taken,
  a slot value is cast to an integer or to ``void *`` and no known place
  holds it, or the slot is a variable or a return that the unit does not
  define (``OUTSIDE``): code that the tree does not show can write it.

A cast to an integer or to ``void *`` does not stop a value: the place
that holds the integer or the ``void *`` is the slot, and a cast back
reads it (``_Collector.climb``, ``visit_untyped_cast``).  A copy of a
``void *`` place carries the value on (``visit_reference``); a copy of an
integer place does not.  No integer place escapes: what code out of the
build writes into it (a linker symbol, a routine of the ROM, a read of the
flash) is a number, not a function of the build.

The resolution over all the units of a build is ``_slot_resolution``
(phase 5b).  The model and its measurement: probe s3 of the call edge plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import symbols as _sym
from ._libclang import cindex as cx
from .models import SlotArgument, SlotEscape, SlotFlow, SlotSite, SlotSource

_K = cx.CursorKind
_T = cx.TypeKind

_FUNCTIONS = frozenset({
    _K.FUNCTION_DECL, _K.CXX_METHOD, _K.CONSTRUCTOR, _K.DESTRUCTOR,
    _K.CONVERSION_FUNCTION, _K.FUNCTION_TEMPLATE,
})
# The kinds whose address is a function value.
_ADDRESS_KINDS = frozenset({_K.FUNCTION_DECL, _K.CXX_METHOD, _K.FUNCTION_TEMPLATE})
_REFERENCE_EXPRESSIONS = frozenset({_K.DECL_REF_EXPR, _K.MEMBER_REF_EXPR})
_TRANSPARENT = frozenset({_K.UNEXPOSED_EXPR, _K.PAREN_EXPR})
_CASTS = frozenset({
    _K.CSTYLE_CAST_EXPR, _K.CXX_STATIC_CAST_EXPR, _K.CXX_REINTERPRET_CAST_EXPR,
    _K.CXX_CONST_CAST_EXPR, _K.CXX_FUNCTIONAL_CAST_EXPR, _K.CXX_DYNAMIC_CAST_EXPR,
})
_ARRAYS = frozenset({_T.CONSTANTARRAY, _T.INCOMPLETEARRAY, _T.VARIABLEARRAY, _T.DEPENDENTSIZEDARRAY})
# The type of a scalar that can hold a function: a list of it in braces is
# its one element.
_SCALAR_POINTERS = frozenset({_T.POINTER, _T.MEMBERPOINTER})
_FUNCTION_TYPES = frozenset({_T.FUNCTIONPROTO, _T.FUNCTIONNOPROTO})
_CHARACTERS = frozenset({_T.VOID, _T.CHAR_S, _T.CHAR_U, _T.SCHAR, _T.UCHAR})
_INTEGERS = frozenset({
    _T.SHORT, _T.USHORT, _T.INT, _T.UINT, _T.LONG, _T.ULONG, _T.LONGLONG, _T.ULONGLONG,
})
_REFERENCES = frozenset({_T.LVALUEREFERENCE, _T.RVALUEREFERENCE})
# The operands of these are not evaluated (`sizeof`, `alignof`,
# `_Static_assert`): no value of them goes anywhere.  WHY: Zephyr
# `ARRAY_SIZE(handlers)` writes `&(handlers)[0]` inside `sizeof`, which
# made each table of handlers escape (review of phase 5a).
_UNEVALUATED = frozenset({_K.CXX_UNARY_EXPR, _K.STATIC_ASSERT})
_DECLARATORS = frozenset({_K.VAR_DECL, _K.FIELD_DECL, _K.PARM_DECL})
_TYPE_NAMES = frozenset({_K.TYPEDEF_DECL, _K.TYPE_ALIAS_DECL})
# A cast and a compound literal: the operand is the last expression child;
# an expression before it belongs to the written type (`(__typeof__(x))v`).
_TYPED_OPERANDS = _CASTS | {_K.COMPOUND_LITERAL_EXPR}
# C writes a function with no parameters `void (void)`, C++ `void ()`.
_EMPTY_PARAMETERS = re.compile(r"\(void\)")
# The exception specification at the end of a function type spelling.
_EXCEPTION_SPECIFICATION = re.compile(r"\s+(noexcept(\([^)]*\))?|throw\(\))$")
# The name of an operator (`operator()`, `operator=`, `operator new`), and
# not a function whose name starts with the word (`operatorSelect`).
_OPERATOR_NAME = re.compile(r"operator(?![A-Za-z0-9_])")
_B = cx.BinaryOperator
_COMPARISONS = frozenset({_B.Cmp, _B.LT, _B.GT, _B.LE, _B.GE, _B.EQ, _B.NE, _B.LAnd, _B.LOr})
# What a pointer to a slot can do (`_Collector.address_taken`).
_ADDRESS_WRITABLE = "writable"
_ADDRESS_READ_ONLY = "read only"
# The reason of an escape that holds only when the build has no definition
# of the variable or of the function of the slot (``_defined_outside``):
# code out of the build writes it.  The resolution checks the definition
# over all the units (``_slot_resolution``).
OUTSIDE = "defined out of the unit"
# Where a value goes (`_Collector.climb`): ("slot", key), ("argument",
# file, line, column, ordinal, index), ("escape", why) or ("none", why).
_Where = tuple[str | int, ...]


@dataclass
class SlotFacts:
    """The slot facts of one translation unit (see the module docstring).

    *from_instances* holds the facts that only the bodies of the template
    instances give, which the walk of the written code does not.  WHY
    apart: such a fact can lie in a header that an other unit owns, and
    each unit that uses the instance gives it.  The index keeps them with
    the unit that gave them (column ``unit``), and a new parse of that unit
    replaces them; the facts of the written code go with their file.
    """

    sources: list[SlotSource] = field(default_factory=list)
    flows: list[SlotFlow] = field(default_factory=list)
    sites: list[SlotSite] = field(default_factory=list)
    arguments: list[SlotArgument] = field(default_factory=list)
    escapes: list[SlotEscape] = field(default_factory=list)
    from_instances: SlotFacts | None = None


def _kind(cursor: cx.Cursor) -> cx.CursorKind | None:
    """The kind of *cursor*, or None for a kind that the binding does not know."""
    try:
        return cursor.kind
    except ValueError:
        return None


def _place(cursor: cx.Cursor) -> tuple[str, int, int] | None:
    place = cursor.location
    if place.file is None:
        return None
    return place.file.name, place.line, place.column


def _node_key(cursor: cx.Cursor) -> tuple[int, cx.CursorKind | None]:
    return cursor.hash, _kind(cursor)


def _same(a: cx.Cursor, b: cx.Cursor) -> bool:
    """One node of the tree: by kind and by the hash of the node.

    Two paths of the binding (``get_children``, ``get_arguments``) give one
    node with other data, thus ``==`` can fail for it (probe s3).  The hash
    of an expression is the hash of its node (``clang_hashCursor``).  WHY
    not the extent: each operand that one macro expansion writes has the
    extent of the expansion, thus `ESP_ERROR_CHECK(f(pin, on_gpio, 0))`
    gave `on_gpio` the index of `pin`.  Measured: each argument of 13222
    calls had a child with its hash and kind.
    """
    return a == b or _node_key(a) == _node_key(b)


def _unwrap(expression: cx.Cursor) -> cx.Cursor:
    while _kind(expression) in _TRANSPARENT:
        children = list(expression.get_children())
        if len(children) != 1:
            break
        expression = children[0]
    return expression


def _callee(expression: cx.Cursor) -> cx.Cursor:
    """The expression under the parentheses, the implicit casts and the `*` or `&` of a callee."""
    expression = _unwrap(expression)
    while _kind(expression) == _K.UNARY_OPERATOR:
        children = list(expression.get_children())
        if len(children) != 1:
            break
        expression = _unwrap(children[0])
    return expression


def _has_callee(call: cx.Cursor) -> bool:
    """*call* has a callee: its first child.  A construction has none: each child is an argument."""
    children = list(call.get_children())
    return bool(children) and len(children) == len(list(call.get_arguments())) + 1


def _direct_function(call: cx.Cursor) -> cx.Cursor | None:
    """The function that *call* names, or None for a call through a value.

    WHY not ``referenced`` alone: libclang gives the callee of the inner
    call for `pick(a)(5)` and for `table[i](4)` with an ``operator[]``,
    thus such an indirect call was a direct call of `pick` (review of
    phase 5a).  A call names its function when its callee is a reference
    to it.  The callee is the first child; an operator in operator syntax
    (`a = b`, `a(x)`, `a[i]`) has the reference among the operands.  A
    construction has no callee.  libclang gives no ``referenced`` for a
    function name in parentheses (`(h)(5)`, `(*h)(6)`, `(&h)(7)`, as a macro
    writes `(fn)(args)`): the callee tells it there.
    """
    function = call.referenced
    kind = _kind(function) if function is not None else None
    if kind == _K.CONSTRUCTOR:
        return function
    children = list(call.get_children())
    if kind not in _FUNCTIONS:
        if not _has_callee(call):
            return None
        callee = _callee(children[0])
        named = callee.referenced if _kind(callee) in _REFERENCE_EXPRESSIONS else None
        return named if named is not None and _kind(named) in _ADDRESS_KINDS else None
    named = children if _OPERATOR_NAME.match(function.spelling) else children[:1]
    for node in named:
        node = _callee(node)
        if _kind(node) in _REFERENCE_EXPRESSIONS and node.referenced == function:
            return function
    return None


def _member_operator_syntax(call: cx.Cursor, function: cx.Cursor) -> bool:
    """`a = b`, `a(x)` with a member operator: the object is argument 0.

    One child is a bare reference to the operator; a member call `a.f(x)`
    names it by a MEMBER_REF_EXPR.
    """
    return (
        _kind(function) == _K.CXX_METHOD and not function.is_static_method()
        and any(
            _kind(node) == _K.DECL_REF_EXPR and node.referenced == function
            for node in (_unwrap(child) for child in call.get_children())
        )
    )


def _is_object_call(function: cx.Cursor) -> bool:
    """The ``operator()`` of an object that is no closure: a site (see the module docstring)."""
    return (
        _kind(function) == _K.CXX_METHOD and function.spelling == "operator()"
        and not function.is_static_method() and _sym._lambda_of(function) is None
    )


def _is_closure_conversion(function: cx.Cursor) -> bool:
    """The conversion of a closure to a pointer to a function."""
    if _kind(function) != _K.CONVERSION_FUNCTION:
        return False
    closure = function.semantic_parent
    return closure is not None and _sym._lambda_of(closure) is not None


def _closure_conversion_call(path: list[cx.Cursor], at: int) -> cx.Cursor | None:
    """The call of the conversion of a closure whose callee is ``path[at]``, or None.

    A lambda that goes where a pointer to a function goes (`attachInterrupt(
    pin, []{...}, mode)`, `xTaskCreate([](void *){...}, ...)`) is the object
    of a call of that conversion: LAMBDA_EXPR, MEMBER_REF_EXPR of the
    conversion, CALL_EXPR (measured, libclang 22).  The pointer that the
    conversion gives calls the body of the lambda.
    """
    if at < 1 or _kind(path[at]) != _K.MEMBER_REF_EXPR:
        return None
    function = path[at].referenced
    call = path[at - 1]
    if function is None or not _is_closure_conversion(function):
        return None
    if _kind(call) != _K.CALL_EXPR or _pointer_type(call.type) is None:
        return None
    return call


def _strip_arrays(type_: cx.Type) -> cx.Type:
    type_ = type_.get_canonical()
    while type_.kind in _ARRAYS:
        type_ = type_.element_type.get_canonical()
    return type_


def _function_type(type_: cx.Type) -> str:
    """The type key of a function type: `(void)` as `()`, and with no exception specification.

    WHY no `noexcept`: a pointer to a `noexcept` function converts to a
    pointer to the same function type without it, thus `void n() noexcept`
    is a target of a call through `void (*)()`.
    """
    return _type_key(type_.get_canonical().spelling)


def _type_key(spelling: str) -> str:
    return _EXCEPTION_SPECIFICATION.sub("", _EMPTY_PARAMETERS.sub("()", spelling))


def _member_type(member_pointer: cx.Type) -> str:
    """The type key of a pointer to a method: `M:` and its type, which names the class (`void (A::*)()`).

    WHY the class: `(a.*m)()` with `void (A::*)()` cannot call `B::h`, which
    has the same function type.  WHY the spelling of the pointer, and not
    ``Type.get_class_type()``: libclang 22 crashes in it for a class that is
    a template parameter (`T::*`, measured in `<functional>`).
    """
    return "M:" + _type_key(member_pointer.get_canonical().spelling)


def _pointer_type(type_: cx.Type) -> str | None:
    """The function type that a pointer (or a reference) to a function points to."""
    type_ = _strip_arrays(type_)
    if type_.kind in _REFERENCES:
        type_ = type_.get_pointee().get_canonical()
    if type_.kind == _T.POINTER:
        pointee = type_.get_pointee().get_canonical()
        if pointee.kind in _FUNCTION_TYPES:
            return _function_type(pointee)
    if type_.kind == _T.MEMBERPOINTER:
        pointee = type_.get_pointee().get_canonical()
        if pointee.kind in _FUNCTION_TYPES:
            return _member_type(type_)
    if type_.kind in _FUNCTION_TYPES:
        return _function_type(type_)
    return None


def _table_pointer_type(type_: cx.Type) -> str | None:
    """The function type of a pointer to a table of pointers to functions (`act_t *`, `act_t *sets[4]`), or None.

    WHY: a state machine keeps its tables of actions in a table of
    pointers (`sets[SLAVE] = slave_actions;`) and calls through a copy
    (`act_t *set = sets[i]; (*set[j])(ccb);`).  The slot of such a pointer
    holds the functions of the table that it points to, as the slot of an
    array holds the functions of its elements.  The write of `sets[i]` gave
    a flow, but the read was no slot fact, thus the actions had no caller.
    """
    current = _strip_arrays(type_)
    for _ in range(4):
        if current.kind != _T.POINTER:
            return None
        pointee = current.get_pointee().get_canonical()
        found = _pointer_type(pointee)
        if found is not None and pointee.kind not in _FUNCTION_TYPES:
            return found
        current = _strip_arrays(pointee)
    return None


def _points_to(pointer: cx.Type, target: cx.Type) -> bool:
    """*pointer* is a pointer to *target*.

    WHY by the types: the binding gives no operator of a UNARY_OPERATOR.
    `&x` has the type "pointer to the type of x"; `*p` has the type that
    `p` points to, which is never a pointer to the type of `p`.
    """
    pointer = pointer.get_canonical()
    return (
        pointer.kind == _T.POINTER
        and pointer.get_pointee().get_canonical().spelling == target.get_canonical().spelling
    )


def _decays(array: cx.Cursor, parent: cx.Cursor) -> bool:
    """*parent* is the decay of the array *array* to a pointer to its first element."""
    array_type = array.type.get_canonical()
    return array_type.kind in _ARRAYS and _points_to(parent.type, array_type.element_type)


def _code_type(function: cx.Cursor, value: cx.Cursor | None = None) -> str:
    """The type key of a function as a value.

    A method with `this` is a value as `&A::f` only: the type of *value*
    (the `&` of it) is the pointer to the member (`_member_type`).
    """
    if _kind(function) == _K.CXX_METHOD and not function.is_static_method():
        if value is not None and value.type.get_canonical().kind == _T.MEMBERPOINTER:
            return _member_type(value.type)
        return "M:" + _function_type(function.type)
    return _function_type(function.type)


def _record(type_: cx.Type) -> cx.Cursor | None:
    type_ = type_.get_canonical()
    if type_.kind in _REFERENCES:
        type_ = type_.get_pointee().get_canonical()
    if type_.kind != _T.RECORD:
        return None
    declaration = type_.get_declaration()
    if _kind(declaration) is None or declaration.kind == _K.NO_DECL_FOUND:
        return None
    # The definition: the type gives its first declaration, which can be a
    # forward declaration with no member (`struct ops;` in a header).
    return declaration.get_definition() or declaration


class _Collector:
    """Collects the slot facts of one unit (see the module docstring)."""

    def __init__(self) -> None:
        self.facts = SlotFacts()
        # The facts of the walks of instance bodies (`start_walk`), and the
        # sites that they added, by place and ordinal: an argument belongs
        # with its site.
        self.instance_facts = SlotFacts()
        self._in_instance = False
        self._instance_sites: set[tuple[str, int, int, int]] = set()
        self._callable_records: dict[str, bool] = {}
        self._member_usrs: dict[str, str] = {}
        self._records_with_methods: dict[str, bool] = {}
        # The number of sites at each place, and the ordinal of each site
        # node, for the arguments of the site (`SlotSite.ordinal`).
        self._sites_at: dict[tuple[str, int, int], int] = {}
        self._site_ordinals: dict[tuple[tuple[str, int, int], int, cx.CursorKind | None], int] = {}
        # The ordinals of the sites with one content (`SlotSite` but the
        # ordinal), and how many of them the current walk gave
        # (`start_walk`).  WHY: the template and each of its instances show
        # a call that depends on no parameter at one place.  The n-th such
        # site of a walk is the n-th site of the earlier walks, thus the
        # place keeps one site, and two calls of one macro expansion stay
        # two.
        self._sites_like: dict[tuple, list[int]] = {}
        self._walk_sites_like: dict[tuple, int] = {}

    def note_outside(self, expression: cx.Cursor, slot: str) -> None:
        """Record that code out of the build can write *slot*, when *expression* reads a declaration the unit does not define."""
        place = _place(expression)
        if place is not None and _defined_outside(expression):
            self._out().escapes.append(SlotEscape(
                file=place[0], line=place[1], column=place[2], slot=slot, reason=OUTSIDE,
            ))

    def start_walk(self, instance: bool) -> None:
        """Start one walk: the written code of the unit, or the body of one instance."""
        self._walk_sites_like = {}
        self._in_instance = instance

    def _out(self) -> SlotFacts:
        """The facts that the current walk adds to."""
        return self.instance_facts if self._in_instance else self.facts

    # ---- types and slots ------------------------------------------------

    def callable_record(self, declaration: cx.Cursor) -> bool:
        """The record has an ``operator()``, of its own or of a base class."""
        usr = declaration.get_usr()
        if not usr:
            return False
        known = self._callable_records.get(usr)
        if known is not None:
            return known
        self._callable_records[usr] = False  # a cycle of bases ends here
        found = False
        for candidate in (declaration, declaration.get_definition(), declaration.specialized_template):
            if candidate is None:
                continue
            for child in candidate.get_children():
                child_kind = _kind(child)
                if child_kind in (_K.CXX_METHOD, _K.FUNCTION_TEMPLATE) and child.spelling == "operator()":
                    found = True
                elif child_kind == _K.CXX_BASE_SPECIFIER:
                    base = _record(child.type)
                    found = base is not None and self.callable_record(base)
                if found:
                    break
            if found:
                break
        self._callable_records[usr] = found
        return found

    def has_methods(self, record_type: cx.Type) -> bool:
        declaration = _record(record_type)
        if declaration is None:
            return False
        usr = declaration.get_usr()
        known = self._records_with_methods.get(usr)
        if known is None:
            known = next(iter(record_type.get_canonical().get_methods()), None) is not None
            self._records_with_methods[usr] = known
        return known

    def object_type(self, type_: cx.Type) -> str | None:
        """`W:<USR>` when *type_* is a record with an ``operator()``."""
        declaration = _record(_strip_arrays(type_))
        if declaration is None or not self.callable_record(declaration):
            return None
        return "W:" + declaration.get_usr()

    def slot_type(self, type_: cx.Type) -> str | None:
        return _pointer_type(type_) or _table_pointer_type(type_) or self.object_type(type_)

    def member_usr(self, declaration: cx.Cursor) -> str:
        """The USR of *declaration*, or of the template member that it instantiates.

        WHY: the code of a class template names the member of the template
        (`c:@ST>1#T@Box@FI@f`), and the code outside it names the member of
        the instance (`c:@S@Box>#I@FI@f`).  As two slots, a function that
        `b.f = h1` stores never reached the call `f(1)` in `Box::fire`.  A
        member of an implicit instance has the place of the template member
        (as ``symbols._code_usr`` tells for a function); a member of an
        explicit specialization has a place of its own and keeps its USR.
        The references keep the instance member (``_code_usr``: only a
        function maps), but a slot must be one for the two codes.

        A local variable of the body of an implicit instance maps the same
        way, to the variable of the template body (``local_usr``): the walk
        of the instance bodies gives its facts, and one call in a template
        was a site of each instance.
        """
        usr = declaration.get_usr()
        parent = declaration.semantic_parent
        if not usr or parent is None:
            return usr
        if _kind(declaration) == _K.VAR_DECL and _kind(parent) in _sym._INSTANCE_FUNCTION_KINDS:
            return self.local_usr(declaration, parent, usr)
        if _kind(parent) not in _sym._INSTANCE_CLASS_KINDS:
            return usr
        known = self._member_usrs.get(usr)
        if known is not None:
            return known
        current = declaration
        for _ in range(_sym._TEMPLATE_CHAIN_LIMIT):
            record = current.semantic_parent
            template = record.specialized_template if record is not None else None
            if template is None:
                break
            match = next(
                (
                    member for member in template.get_children()
                    if _kind(member) == _kind(current) and member.spelling == current.spelling
                    and _sym._has_place_of(current, member)
                ),
                None,
            )
            if match is None:
                break
            current = match
        mapped = current.get_usr() or usr
        self._member_usrs[usr] = mapped
        return mapped

    def local_usr(self, variable: cx.Cursor, function: cx.Cursor, usr: str) -> str:
        """The USR of the local *variable* of the template body, when *function* is an implicit instance.

        The instance of a function template, or of a method of a class
        template, has the place of its template (``symbols._code_usr``), and
        so has each local variable of its body.  The variable of the
        template body at that place, with that name, is the slot.  An
        explicit specialization has a place of its own and keeps its USR.
        """
        known = self._member_usrs.get(usr)
        if known is not None:
            return known
        template = function
        for _ in range(_sym._TEMPLATE_CHAIN_LIMIT):
            following = template.specialized_template
            if following is None or not _sym._has_place_of(template, following):
                break
            template = following
        mapped = usr
        if template is not function:
            definition = template.get_definition() or template
            for node in definition.walk_preorder():
                if (_kind(node) == _K.VAR_DECL and node.spelling == variable.spelling
                        and _sym._same_place(node, variable)):
                    mapped = node.get_usr() or usr
                    break
        self._member_usrs[usr] = mapped
        return mapped

    def parameter_slot(self, parameter: cx.Cursor) -> str | None:
        function = parameter.semantic_parent
        if function is None or _kind(function) not in _FUNCTIONS:
            return None
        parameters = [child for child in function.get_children() if _kind(child) == _K.PARM_DECL]
        for index, candidate in enumerate(parameters):
            if candidate == parameter:
                return f"P|{_sym._code_usr(function)}|{index}"
        return None

    def declaration_slot(self, declaration: cx.Cursor) -> str | None:
        kind = _kind(declaration)
        if kind == _K.PARM_DECL:
            return self.parameter_slot(declaration)
        if kind not in (_K.FIELD_DECL, _K.VAR_DECL):
            return None
        usr = self.member_usr(declaration)
        if not usr:
            return None
        return ("F|" if kind == _K.FIELD_DECL else "V|") + usr

    def slot_of(self, expression: cx.Cursor) -> tuple[str | None, str]:
        """The slot that an lvalue or a callee names, or (None, why not)."""
        expression = _unwrap(expression)
        kind = _kind(expression)
        if kind in _REFERENCE_EXPRESSIONS:
            declaration = expression.referenced
            if declaration is None:
                return None, "dependent"
            slot = self.declaration_slot(declaration)
            return (slot, "") if slot else (None, f"reference to {_kind(declaration)}")
        if kind == _K.ARRAY_SUBSCRIPT_EXPR:
            children = list(expression.get_children())
            return self.slot_of(children[0]) if children else (None, "array")
        if kind in _CASTS:
            # `((voidFuncPtrArg)handler->fn)(arg)`: a cast from one pointer
            # to a function to an other keeps the value of the slot.  A cast
            # from an integer or from `void *` (`((cb_t) h->handler)(x)`)
            # reads the place that holds the function as that type.
            operand = _cast_operand(expression)
            if operand is not None and (_pointer_type(operand.type) is not None
                                        or self.untracked(operand.type) is not None):
                return self.slot_of(operand)
            return None, "cast"
        if kind == _K.UNARY_OPERATOR:
            children = list(expression.get_children())
            if children and _pointer_type(children[0].type) is not None \
                    and expression.type.get_canonical().kind in _FUNCTION_TYPES:
                return self.slot_of(children[0])  # `(*fp)(x)`
            if children and self.slot_type(expression.type) is not None:
                array = _unwrap(children[0])
                if array != children[0] and _decays(array, children[0]):
                    return self.slot_of(array)  # `*table = f`: the first element
            return None, "dereference"
        if kind == _K.CALL_EXPR:
            function = _direct_function(expression)
            if function is not None and _kind(function) == _K.CONSTRUCTOR:
                return None, "temporary"  # `Obj()(8)`: no return slot holds it
            if function is not None:
                holder = self.accessor_holder(expression, function)
                return (holder or "R|" + _sym._code_usr(function)), ""
            return None, "result of an indirect call"
        return None, f"expression {kind}"

    # ---- where a value goes ---------------------------------------------

    def climb(
        self, node: cx.Cursor, path: list[cx.Cursor], caller: str | None, untyped: str | None = None,
    ) -> _Where:
        """Follow a value up the tree to the place that stores it.

        ('slot', key[, key of the object]) | ('argument', site place,
        ordinal, index) | ('escape', why) | ('none', why).

        A cast to an integer or to `void *` does not stop the value: the
        place that holds the integer (`h->handler = (uint32_t) cb;`, a
        `void *` field of a context) is the slot of the function, and a cast
        back (`(cb_t) h->handler`) reads it (``visit_untyped_cast``).  WHY a
        slot and not an escape: an escape gave each call of the read value
        every function of its type, also the jump of a bootloader to an
        address in flash, which holds no function of the build.  When no
        known place holds the value, it escapes as before.

        *untyped*: the value is untyped from the start, a read of a
        `void *` place (``visit_reference``).
        """
        child = node
        at = len(path) - 1

        def done(result: _Where) -> _Where:
            if untyped and result[0] not in ("slot", "argument"):
                return ("escape", "cast to " + untyped)
            return result

        while at >= 0:
            parent = path[at]
            kind = _kind(parent)
            if (kind in _TRANSPARENT or kind in _TYPED_OPERANDS) and parent.type.get_canonical().kind == _T.BOOL:
                # `bool(f)`, `(bool)f`, `bool b = f`: the truth of the
                # pointer, as `!f`.  A bool holds no function, thus it is no
                # slot.
                return done(("none", "conversion to bool"))
            untracked = self.untracked(parent.type) if kind in _TRANSPARENT or kind in _TYPED_OPERANDS else None
            if untracked and _pointer_type(child.type) is None and untyped is None:
                # A pointer to a slot, not a value of it (a value that an
                # earlier cast made untyped goes on): `address_taken`
                # tells if it escapes (`memcpy(&g, &tab[0], n)` only reads).
                untracked = None
                if kind in _TYPED_OPERANDS or kind == _K.UNEXPOSED_EXPR:
                    return done(("none", "pointer to a slot as an untyped pointer"))
            if kind in _TRANSPARENT:
                if (kind == _K.UNEXPOSED_EXPR and untracked
                        and child.type.get_canonical().kind != _T.RECORD):
                    untyped = untracked
                child, at = parent, at - 1
                continue
            if kind in _TYPED_OPERANDS:
                if untracked:
                    untyped = untracked
                child, at = parent, at - 1
                continue
            if kind == _K.UNARY_OPERATOR:
                parent_type = parent.type.get_canonical()
                if parent_type.kind in (_T.POINTER, _T.MEMBERPOINTER) or parent_type.kind in _FUNCTION_TYPES:
                    child, at = parent, at - 1
                    continue
                return done(("none", "unary operator"))
            if kind == _K.CONDITIONAL_OPERATOR:
                children = list(parent.get_children())
                if children and _same(children[0], child):
                    return done(("none", "condition"))
                child, at = parent, at - 1
                continue
            if kind == _K.ARRAY_SUBSCRIPT_EXPR:
                children = list(parent.get_children())
                if children and _same(children[0], child):
                    child, at = parent, at - 1
                    continue
                return done(("none", "index"))
            if kind == _K.BINARY_OPERATOR:
                children = list(parent.get_children())
                if _sym._is_plain_assignment(parent) and len(children) == 2:
                    if _same(children[1], child):
                        slot, why = self.slot_of(children[0])
                        return done(("slot", slot) if slot else ("none", "left side: " + why))
                    return done(("none", "left side of an assignment"))
                if parent.binary_operator == cx.BinaryOperator.Comma and len(children) == 2:
                    # `(log(), f)`: the value of a comma is its right side.
                    if _same(children[1], child):
                        child, at = parent, at - 1
                        continue
                    return done(("none", "left side of a comma"))
                return done(("none", "operator"))
            if kind == _K.COMPOUND_ASSIGNMENT_OPERATOR:
                return done(("none", "compound assignment"))
            if kind == _K.CALL_EXPR:
                result = self.climb_call(parent, child)
                if result is None:
                    child, at = parent, at - 1
                    continue
                return done(result)
            if kind in (_K.VAR_DECL, _K.FIELD_DECL, _K.PARM_DECL):
                # A PARM_DECL holds its default argument
                # (`void attach(cb_t cb = default_isr)`).
                slot = self.declaration_slot(parent)
                return done(("slot", slot) if slot else ("none", str(kind)))
            if kind == _K.INIT_LIST_EXPR:
                result = self.climb_init_list(parent, child, path, at)
                if result is None:
                    child, at = parent, at - 1
                    continue
                return done(result)
            if kind == _K.RETURN_STMT:
                return done(("slot", "R|" + caller) if caller else ("none", "return outside code"))
            if kind == _K.CONSTRUCTOR:
                return done(self.climb_member_initializer(parent, child))
            if kind == _K.MEMBER_REF_EXPR:
                call = _closure_conversion_call(path, at)
                if call is None:
                    return done(("none", "base of a member"))
                child, at = call, at - 2
                continue
            if kind == _K.CXX_FOR_RANGE_STMT:
                return done(self.climb_range_for(parent, child))
            return done(("none", f"statement {kind}"))
        return done(("none", "top"))

    def climb_range_for(self, loop: cx.Cursor, child: cx.Cursor) -> _Where:
        """`for (cb_t cb : g_tab)`: each element of the range goes into the loop variable.

        A loop variable that is a reference reads the elements as a copy
        does, but it is an alias of an element: a write through it is not
        seen (a known gap of phase 5a, as each reference to a slot).
        """
        if not _is_range(loop, child):
            return ("none", "range for")
        variable = next(c for c in loop.get_children() if _kind(c) == _K.VAR_DECL)
        slot = self.declaration_slot(variable)
        return ("slot", slot) if slot else ("none", "range for")

    def climb_call(self, call: cx.Cursor, child: cx.Cursor) -> _Where | None:
        """Where a value that is an operand of *call* goes; None: up through the call."""
        arguments = list(call.get_arguments())
        index = next((i for i, argument in enumerate(arguments) if _same(argument, child)), None)
        if index is None:
            return ("none", "callee or object")
        function = _direct_function(call)
        if function is None:
            if call.referenced is None and self.object_type(call.type) is not None:
                return None  # an elided copy or move of a callable object
            return self.site_argument(call, index)
        function_kind = _kind(function)
        member_operator = _member_operator_syntax(call, function)
        if member_operator:
            if index == 0:
                return ("none", "object of an operator")
            index -= 1
        if _is_object_call(function):
            # The object can hold any function: the value goes to each
            # function that the site reaches, not into the parameter of
            # `std::function::operator()`, which all its instances share.
            return self.site_argument(call, index)
        record = function.semantic_parent
        callable_record = record is not None and self.object_type(record.type) is not None
        if member_operator:
            if function.spelling == "operator=" and callable_record:
                slot, why = self.slot_of(arguments[0])
                return ("slot", slot) if slot else ("none", "left side: " + why)
            return ("slot", f"P|{_sym._code_usr(function)}|{index}")
        if function_kind == _K.CONSTRUCTOR and callable_record:
            return None  # the callable object carries the value
        if (function_kind != _K.CONSTRUCTOR and self.object_type(call.type) is not None
                and function.result_type.get_canonical().kind not in _REFERENCES):
            # A factory such as mbed `callback(obj, &m)`: it makes a new
            # object.  A method that gives a reference (`S &add(cb_t)`)
            # gives an object that exists, and keeps its parameter.
            return None
        return ("slot", f"P|{_sym._code_usr(function)}|{index}")

    def site_argument(self, call: cx.Cursor, index: int) -> _Where:
        place = _place(call)
        ordinal = self._site_ordinals.get((place, *_node_key(call))) if place is not None else None
        if place is None or ordinal is None:
            return ("none", "argument of a call that is no site")
        return ("argument", *place, ordinal, index)

    def climb_init_list(
        self, init_list: cx.Cursor, child: cx.Cursor, path: list[cx.Cursor], at: int,
    ) -> _Where | None:
        """The field that a value of a record initializer list fills, and the object that the list makes.

        ('slot', field) or ('slot', field, object).  WHY the object too: a
        container gives its elements through a method (`std::array::
        operator[]`), whose body in a template names a field of a dependent
        type, thus no fact connects the field to the method.  The call of
        the method reads the object (`accessor_holder`), and the object
        holds the values of its list.
        """
        list_type = init_list.type.get_canonical()
        if list_type.kind in _ARRAYS or self.scalar_list_type(init_list, path[:at]) is not None:
            # An array list: the slot of an element is the slot of the
            # array.  A pointer in braces (`.one = { f }`, `(cb_t){ f }`):
            # its one element is the value.
            return None
        if list_type.kind != _T.RECORD:
            # A dependent type in a template (`S<T> s = { f }`): no field
            # is known, and the variable is no pointer.
            return ("none", "initializer list")
        usr, _field_type = self.init_list_field(init_list, child)
        if not usr:
            return ("none", "initializer with no field")
        holder_at = at - 1
        while holder_at >= 0 and _kind(path[holder_at]) in _TRANSPARENT:
            holder_at -= 1
        holder = None
        if (holder_at >= 0 and _kind(path[holder_at]) in (_K.VAR_DECL, _K.FIELD_DECL)
                and self.has_methods(list_type)):
            # A record with no method (each struct of C) has no accessor.
            holder = self.declaration_slot(path[holder_at])
        return ("slot", "F|" + usr, holder) if holder else ("slot", "F|" + usr)

    def init_list_field(self, init_list: cx.Cursor, element: cx.Cursor) -> tuple[str | None, cx.Type | None]:
        """The field (USR, type) that *element* of a record initializer list fills, or (None, None).

        The USR is the one of the slot (``member_usr``): the list of an
        instance of a class template (`Box<int> b{.cb = h}`) names the
        field of the instance.
        """
        usr: str | None = None
        field_type: cx.Type | None = None
        if _kind(element) == _K.UNEXPOSED_EXPR:
            usr, _name, _type, field_type = _sym._init_list_element_field(element)
        if usr is None:
            children = list(init_list.get_children())
            index = next((i for i, c in enumerate(children) if _same(c, element)), None)
            fields = _sym._positional_struct_fields(init_list)
            if index is not None and index < len(fields) and fields[index][0]:
                usr, field_type = fields[index][0], fields[index][3]
        if usr is None:
            return None, None
        for field_cursor in init_list.type.get_canonical().get_fields():
            if field_cursor.get_usr() == usr:
                return self.member_usr(field_cursor), field_type
        return usr, field_type

    def init_list_element_type(self, init_list: cx.Cursor, element: cx.Cursor) -> cx.Type | None:
        """The type of the place that *element* of an initializer list fills.

        A pointer in braces (`cb_t a = { w };`) is its one element: the
        place has the type of the list.
        """
        list_type = init_list.type.get_canonical()
        if list_type.kind in _ARRAYS:
            return list_type.element_type
        if list_type.kind == _T.RECORD:
            return self.init_list_field(init_list, element)[1]
        if list_type.kind in _SCALAR_POINTERS:
            return init_list.type
        return None

    def scalar_list_type(self, init_list: cx.Cursor, ancestors: list[cx.Cursor]) -> cx.Type | None:
        """The pointer type of a scalar in braces, or None when *init_list* is no such list.

        *ancestors* are the nodes above the list, the nearest last.  WHY
        the ancestors: libclang gives the list the type `void` when its
        element needs a conversion of a class (`.cb = { w }` with
        `Wrap::operator cb_t`).  The place that holds the list tells the
        type then: a field of a designator, or a position of the enclosing
        list.
        """
        list_type = init_list.type.get_canonical()
        if list_type.kind in _SCALAR_POINTERS:
            return init_list.type
        if list_type.kind != _T.VOID or not ancestors:
            return None
        parent = ancestors[-1]
        place: cx.Type | None = None
        if _kind(parent) == _K.INIT_LIST_EXPR:
            place = self.init_list_element_type(parent, init_list)
        elif (_kind(parent) == _K.UNEXPOSED_EXPR and len(ancestors) >= 2
                and _kind(ancestors[-2]) == _K.INIT_LIST_EXPR):
            place = self.init_list_element_type(ancestors[-2], parent)
        if place is None or place.get_canonical().kind not in _SCALAR_POINTERS:
            return None
        return place

    def climb_member_initializer(self, constructor: cx.Cursor, child: cx.Cursor) -> _Where:
        children = list(constructor.get_children())
        index = next((i for i, c in enumerate(children) if _same(c, child)), None)
        if index is not None:
            for before in range(index - 1, -1, -1):
                member = children[before]
                if _kind(member) == _K.MEMBER_REF and member.referenced is not None:
                    slot = self.declaration_slot(member.referenced)
                    return ("slot", slot) if slot else ("none", "member initializer")
        return ("none", "constructor")

    @staticmethod
    def untracked(type_: cx.Type) -> str | None:
        """`integer` or `void *` (`char *` too): a type that a slot value leaves the tree by."""
        type_ = type_.get_canonical()
        if type_.kind in _INTEGERS:
            return "integer"
        if type_.kind == _T.POINTER and type_.get_pointee().get_canonical().kind in _CHARACTERS:
            return "void *"
        return None

    # ---- facts ----------------------------------------------------------

    def record(
        self, value: tuple[str, str, str], node: cx.Cursor, path: list[cx.Cursor], caller: str | None,
        climb_from: tuple[cx.Cursor, list[cx.Cursor]] | None = None, untyped: str | None = None,
    ) -> _Where | None:
        """Record where *value* goes: ("function", key, type) or ("slot", key, "").

        *climb_from*: the node and its path to climb from, when that is not
        *node* (`visit_overload_set`).  *node* gives the place of the fact.
        *untyped*: *value* is the slot of a `void *` place.  Only a place
        or an argument that takes it gives a fact: a read of such a place
        is in many expressions, and an escape of each one (`if (p)`, `p ==
        NULL`) adds rows and no edge.

        Gives where the value goes (``climb``), or None when *node* has no
        place: the caller adds an escape of the read only when the value
        goes on (``_carries``).
        """
        place = _place(node)
        if place is None:
            return None
        file, line, column = place
        if climb_from:
            result = self.climb(*climb_from, caller)
        else:
            result = self.climb(node, path, caller, untyped)
        if untyped and not _carries(result):
            return result
        if result[0] == "slot":
            for target in (str(key) for key in result[1:]):
                if value[0] == "function":
                    self._out().sources.append(SlotSource(
                        file=file, line=line, column=column, function=value[1],
                        function_type=value[2], slot=target,
                    ))
                elif value[1] != target:
                    self._out().flows.append(SlotFlow(
                        file=file, line=line, column=column, source=value[1], target=target,
                    ))
        elif result[0] == "argument":
            site_file, site_line, site_column, ordinal, index = result[1:6]
            site_key = (str(site_file), int(site_line), int(site_column), int(ordinal))
            owner = self.instance_facts if site_key in self._instance_sites else self.facts
            owner.arguments.append(SlotArgument(
                file=str(site_file), line=int(site_line), column=int(site_column), ordinal=int(ordinal),
                index=int(index), value_kind=value[0], value=value[1],
            ))
            if value[0] == "function":
                # Its address is taken too: a call that only its type
                # resolves can reach it (`g_ops->reg(on_done)`).
                self._out().sources.append(SlotSource(
                    file=file, line=line, column=column, function=value[1],
                    function_type=value[2], slot="",
                ))
        elif value[0] == "function":
            # The address is taken, and nothing known holds it: only its
            # type tells the calls that can reach it.
            self._out().sources.append(SlotSource(
                file=file, line=line, column=column, function=value[1],
                function_type=value[2], slot="",
            ))
        elif result[0] == "escape":
            self._out().escapes.append(SlotEscape(
                file=file, line=line, column=column, slot=value[1], reason=str(result[1]),
            ))
        return result

    def add_site(
        self, call: cx.Cursor, place: tuple[str, int, int], caller: str | None,
        kind: str, slot: str | None, slot_type: str, reason: str, callee: str = "",
    ) -> None:
        content = (place, caller, kind, slot or "", slot_type, reason, callee)
        count = self._walk_sites_like.get(content, 0)
        self._walk_sites_like[content] = count + 1
        earlier = self._sites_like.setdefault(content, [])
        if count < len(earlier):
            # The same call in an other walk: its arguments go to that site.
            self._site_ordinals[(place, *_node_key(call))] = earlier[count]
            return
        ordinal = self._sites_at.get(place, 0)
        self._sites_at[place] = ordinal + 1
        earlier.append(ordinal)
        # The key holds the place: the hash of a node has 32 bits.
        self._site_ordinals[(place, *_node_key(call))] = ordinal
        if self._in_instance:
            self._instance_sites.add((*place, ordinal))
        self._out().sites.append(SlotSite(
            file=place[0], line=place[1], column=place[2], ordinal=ordinal, caller=caller, kind=kind,
            slot=slot or "", slot_type=slot_type, reason=reason, callee=callee,
        ))

    def visit(self, cursor: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        kind = _kind(cursor)
        if kind in _REFERENCE_EXPRESSIONS:
            self.visit_reference(cursor, path, caller)
        elif kind == _K.LAMBDA_EXPR:
            self.visit_lambda(cursor, path, caller)
        elif kind == _K.CALL_EXPR:
            self.visit_call(cursor, path, caller)
        elif kind == _K.INIT_LIST_EXPR:
            self.visit_init_list(cursor, path, caller)
        elif kind in _CASTS:
            self.visit_untyped_cast(cursor, path, caller)

    def visit_untyped_cast(self, cast: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        """A pointer to a function that a cast reads from an integer or a `void *` place: a flow from the place.

        `h->handler = (uint32_t) cb;` stores the function in the place
        `h->handler` (``climb`` goes on past the cast), and `cb_t f = (cb_t)
        h->handler; f(x);` reads it back: the flow `h->handler` → `f` gives
        `f` the functions that the code stored there.  A cast of an integer
        that no place holds (`(entry_t) *(uint32_t *)0x08004004`, the jump
        of a bootloader) reads no slot and gives no fact.

        The type of the place decides, not the type of the operand: casts
        can follow each other (`(entry_t)(void *) APP_ENTRY` reads an
        integer place, `(cb_t)(uintptr_t) tbl[2]` a `void *` place).  A
        place of a function pointer type is read by ``visit_reference`` and
        ``visit_call``, which follow the value through the casts.

        A `void *` place gets its ``OUTSIDE`` escape here: the cast back
        is the read that can call what code out of the build wrote.  A
        `void *` variable, field, element of an array, parameter or return
        is read by ``visit_reference`` and ``visit_call`` too, which give
        the same flow: this visit records the flow only for the places that
        they do not read (an integer, an element through a pointer: `pp[2]`,
        `get()[2]`), and else would give each fact two times.  An integer
        place gets no ``OUTSIDE`` escape (module docstring).
        """
        if _pointer_type(cast.type) is None:
            return
        operand = _cast_operand(cast)
        if operand is None or self.untracked(operand.type) is None:
            return
        held = self.held_value(operand)
        place_type = self.untracked(held.type)
        slot, _why = self.slot_of(held)
        if not slot or place_type is None:
            return
        if place_type == "void *":
            self.note_outside(held, slot)
            if self.reads_void_place(held):
                return
        self.record(("slot", slot, ""), cast, path, caller)

    def held_value(self, expression: cx.Cursor) -> cx.Cursor:
        """The expression under the casts of *expression* that ``slot_of`` goes through: its type is the type of the place."""
        expression = _unwrap(expression)
        while _kind(expression) in _CASTS:
            operand = _cast_operand(expression)
            if operand is None or (_pointer_type(operand.type) is None and self.untracked(operand.type) is None):
                break
            expression = _unwrap(operand)
        return expression

    def reads_void_place(self, operand: cx.Cursor) -> bool:
        """Tell if ``visit_reference`` or ``visit_call`` reads *operand*, a `void *` value, as a place."""
        operand = _unwrap(operand)
        while _kind(operand) == _K.ARRAY_SUBSCRIPT_EXPR:
            # An element of a `void *` array (`tbl[2]`): ``visit_reference``
            # reads it at the array.  An element through a pointer (`pp[2]`
            # with `void **pp`, `get()[2]`) is no place that it or
            # ``visit_call`` reads.
            children = list(operand.get_children())
            if not children:
                return False
            operand = _unwrap(children[0])
            if _kind(operand) == _K.ARRAY_SUBSCRIPT_EXPR:
                continue  # `t[1][2]`
            if _kind(operand) not in _REFERENCE_EXPRESSIONS or operand.type.get_canonical().kind not in _ARRAYS:
                return False
        kind = _kind(operand)
        if kind in _REFERENCE_EXPRESSIONS:
            declaration = operand.referenced
            return (declaration is not None and _kind(declaration) in (_K.VAR_DECL, _K.PARM_DECL, _K.FIELD_DECL)
                    and self.untracked(_strip_arrays(declaration.type)) == "void *")
        if kind == _K.CALL_EXPR:
            function = _direct_function(operand)
            return (function is not None and _kind(function) != _K.CONSTRUCTOR
                    and not _is_closure_conversion(function))
        return False

    def visit_init_list(self, init_list: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        """An element that a conversion function makes the function pointer of its place.

        `{ .cb = Wrap(fa) }` converts the `Wrap` by its `operator cb_t`.
        WHY here: libclang gives the syntactic form of an initializer list,
        which holds no call of the conversion, thus `visit_call` does not
        see it (it sees the call in `cb_t g = Wrap(fa);`).  The return slot
        of the conversion goes into the place.  A record with no conversion
        to the type of the place, or with more than one, gives no fact.
        """
        for element in init_list.get_children():
            value = element
            if _kind(element) == _K.UNEXPOSED_EXPR and element.type.get_canonical().kind == _T.VOID:
                # A designator (`.cb = v`): the value is its last child.
                children = list(element.get_children())
                if not children:
                    continue
                value = children[-1]
            # The record first: the type of the place reads the fields of
            # the list (`_positional_struct_fields`), which costs a pass
            # over all the elements, thus only a class element may ask.
            record = _record(value.type)
            if record is None:
                continue
            place_type = self.scalar_list_type(init_list, path) or self.init_list_element_type(init_list, element)
            wanted = _pointer_type(place_type) if place_type is not None else None
            if wanted is None:
                continue
            conversions = [
                child for child in record.get_children()
                if _kind(child) == _K.CONVERSION_FUNCTION and _pointer_type(child.result_type) == wanted
            ]
            if len(conversions) == 1:
                returned = "R|" + _sym._code_usr(conversions[0])
                self.record(("slot", returned, ""), element, [*path, init_list], caller)

    def visit_lambda(self, lambda_: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        """A lambda is a source; converted to a pointer, it has the type of the pointer.

        WHY the type of the pointer: the call of the converted pointer calls
        the body of the lambda, and a slot that escapes reaches its targets
        by their type only.  An initializer list shows its written form, in
        which the conversion is not visible (measured, libclang 22): the
        type of the field or of the element that the lambda fills tells it.
        """
        at = len(path) - 1
        while at >= 0 and _kind(path[at]) in _TRANSPARENT:
            at -= 1
        call = _closure_conversion_call(path, at)
        target: cx.Type | None = None
        if call is not None:
            target = call.type
        elif at >= 0 and _kind(path[at]) == _K.INIT_LIST_EXPR:
            element = path[at + 1] if at + 1 < len(path) else lambda_
            target = self.init_list_element_type(path[at], element)
        function_type = (_pointer_type(target) if target is not None else None) or "lambda"
        self.record(("function", _sym._lambda_usr(lambda_), function_type), lambda_, path, caller)

    def visit_reference(self, cursor: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        declaration = cursor.referenced
        if declaration is None:
            return
        kind = _kind(declaration)
        if kind == _K.OVERLOADED_DECL_REF:
            self.visit_overload_set(cursor, declaration, path, caller)
            return
        if kind in _ADDRESS_KINDS:
            # The callee of a call is no value (also `(h)(5)`, `(*h)(6)`).
            at = len(path) - 1
            while at >= 0 and (_kind(path[at]) in _TRANSPARENT or _kind(path[at]) == _K.UNARY_OPERATOR):
                at -= 1
            if at >= 0 and _kind(path[at]) == _K.CALL_EXPR and _has_callee(path[at]):
                first = next(iter(path[at].get_children()))
                if _same(_callee(first), cursor):
                    return
            if at >= 0 and _kind(path[at]) == _K.MEMBER_REF_EXPR:
                return
            if _OPERATOR_NAME.match(declaration.spelling):
                return
            parent = len(path) - 1
            while parent >= 0 and _kind(path[parent]) in _TRANSPARENT:
                parent -= 1
            value = path[parent] if parent >= 0 and _kind(path[parent]) == _K.UNARY_OPERATOR else None
            self.record(
                ("function", _sym._code_usr(declaration), _code_type(declaration, value)), cursor, path, caller,
            )
            return
        if kind in (_K.VAR_DECL, _K.PARM_DECL, _K.FIELD_DECL):
            # A `void *` place can hold a function that a cast stored
            # (``climb``): a copy of it (`void *p = c.user;`, `x->arg = arg`,
            # `return c.user;`) carries the function on.  WHY not an
            # integer place: each `int a = b;` and each integer argument
            # gave a flow, 300 times the flows of a project (measured: 215
            # to 65 121 on 215 units, 1 166 with `void *` only).  A chain of
            # integer copies (`uint32_t` parameter into a table) is a known
            # gap; the store and the cast back of one place stay.
            # An element of a `void *` array (`void *tbl[4]`) is a `void *`
            # place too: `void *x = tbl[0];` carries the function on.
            untyped = None
            array = False
            if self.slot_type(declaration.type) is None:
                untyped = self.untracked(_strip_arrays(declaration.type))
                if untyped != "void *":
                    return
                array = declaration.type.get_canonical().kind in _ARRAYS
            slot = self.declaration_slot(declaration)
            place = _place(cursor)
            if not slot or place is None:
                return
            # A `void *` place escapes as a typed slot does: a write through
            # a pointer to it (`q_recv(&msg)`) or code out of the build
            # (`extern void *opaque`) can store a function there.
            if self.address_taken(cursor, path, declaration) == _ADDRESS_WRITABLE:
                self._out().escapes.append(SlotEscape(
                    file=place[0], line=place[1], column=place[2], slot=slot, reason="address taken",
                ))
            if array and not _reads_element(cursor, path):
                # The array itself (`memset(tbl, 0, n)`, `run(a)` with `void
                # *a[]`) is a pointer to the place, not a value that it holds:
                # ``climb`` follows a `void *` value through each conversion,
                # and would carry the pointer on as a function.
                return
            # A pointer to the slot flows on too: a table that a function
            # gets as a pointer (`dispatch(fns, n)`, `table[i](x)` in it)
            # reads the values of the slot through the parameter.
            result = self.record(("slot", slot, ""), cursor, path, caller, untyped=untyped)
            # A call reads a typed slot (`g_cb(1)`), which ``record`` does not
            # see.  A `void *` place is read by a copy, or by a cast back
            # (``visit_untyped_cast``): an other read (`if (p)`, `p == NULL`)
            # adds a row and no edge.
            if untyped is None or _carries(result):
                self.note_outside(cursor, slot)

    def visit_overload_set(
        self, cursor: cx.Cursor, overload_set: cx.Cursor, path: list[cx.Cursor], caller: str | None,
    ) -> None:
        """An overloaded name that is an element of an initializer list: the type of its place picks the function.

        WHY only there: libclang gives the syntactic form of an initializer
        list, thus `{ ov }` and `{ .d = ov }` name the overload set, not the
        function that the conversion chose, as `g = ov` does.  Elsewhere the
        set is the callee of a dependent call in a template (`h(x)`), which
        stores no function.  The pick (`symbols._resolve_overload_set`)
        gives no function when it is not certain.

        The climb starts at the element: the `&` of `{ &A::m }` has the type
        of the set, not a pointer type, and would stop it.  The type key
        comes from the place for the same reason: the `&` gives no member
        pointer type, and the template of `{ tf<double> }` gives its own
        dependent type.  The pick made the function fit the place.
        """
        at = len(path) - 1
        element = cursor
        while at >= 0 and (_kind(path[at]) in _TRANSPARENT or _kind(path[at]) == _K.UNARY_OPERATOR):
            element = path[at]
            at -= 1
        if at < 0 or _kind(path[at]) != _K.INIT_LIST_EXPR:
            return
        place_type = self.init_list_element_type(path[at], element)
        expected = _sym._storage_type(place_type) if place_type is not None else None
        function_type = _pointer_type(expected) if expected is not None else None
        if expected is None or function_type is None:
            return
        for function in _sym._resolve_overload_set(overload_set, expected):
            self.record(
                ("function", _sym._code_usr(function), function_type), cursor, path, caller,
                climb_from=(element, path[: at + 1]),
            )

    @staticmethod
    def address_taken(reference: cx.Cursor, path: list[cx.Cursor], declaration: cx.Cursor) -> str:
        """Tell if *reference* gives a pointer to its slot, and if the pointer can write it.

        `&g_cb`, `&handlers[i]`, `&s->cbs[i]`, and an array of slots that
        decays to a pointer (`handler_t *p = handlers;`) that is not an
        element access (`handlers[i]`, `*handlers`) give a pointer.  Gives
        ``_ADDRESS_WRITABLE``, ``_ADDRESS_READ_ONLY`` or "" (no pointer).

        WHY read-only: a slot that is `const` (`static const init_fn_t
        fns[]`) gets its values from its initializer only, and a pointer
        that the implicit conversions make a pointer to `const` (`memcpy(&g,
        &tab[0], n)` reads `tab`) cannot write it.  An escape makes the
        resolution fall back to the type of the slot.
        """
        child = reference
        decayed = False
        at = len(path) - 1
        pointer_at: int | None = None
        while at >= 0:
            parent = path[at]
            kind = _kind(parent)
            if kind in _TRANSPARENT:
                decayed = decayed or _decays(child, parent)
            elif kind == _K.ARRAY_SUBSCRIPT_EXPR:
                children = list(parent.get_children())
                if not children or not _same(children[0], child):
                    break
                decayed = False
            elif kind == _K.UNARY_OPERATOR:
                if _points_to(parent.type, child.type):
                    pointer_at = at
                    break
                if not decayed:
                    break
                decayed = False
            else:
                break
            child, at = parent, at - 1
        if pointer_at is None:
            if not decayed:
                return ""
            pointer_at = at + 1  # the outermost implicit node of the decay
        if _strip_arrays(declaration.type).is_const_qualified():
            return _ADDRESS_READ_ONLY
        while pointer_at > 0 and _kind(path[pointer_at - 1]) in _TRANSPARENT:
            pointer_at -= 1
        user = path[pointer_at - 1] if pointer_at > 0 else None
        if (user is not None and _kind(user) == _K.BINARY_OPERATOR
                and user.binary_operator in _COMPARISONS):
            return _ADDRESS_READ_ONLY  # `if (g_tab == 0)` keeps no pointer
        pointer = path[pointer_at].type.get_canonical()
        if pointer.kind == _T.POINTER and pointer.get_pointee().is_const_qualified():
            return _ADDRESS_READ_ONLY
        return _ADDRESS_WRITABLE

    def visit_call(self, call: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        place = _place(call)
        if place is None:
            return
        function = _direct_function(call)
        if function is not None:
            if (_kind(function) != _K.CONSTRUCTOR and not _is_closure_conversion(function)
                    and self.slot_type(call.type) is not None):
                # The result of a function that returns a function value: its
                # return slot flows on (`fn_t f = pick(a);`).  A constructor
                # passes its arguments up (`climb_call`), and the conversion
                # of a closure passes the lambda up (`climb`): no return
                # slot of them holds a value.
                returned = self.accessor_holder(call, function) or "R|" + _sym._code_usr(function)
                self.note_outside(call, returned)
                self.record(("slot", returned, ""), call, path, caller)
            elif (_kind(function) != _K.CONSTRUCTOR and not _is_closure_conversion(function)
                    and (untyped := self.untracked(call.type)) == "void *"):
                # A `void *` that a function gives (`void *u =
                # get_user(c);`) can hold a function that a cast stored: its
                # return slot flows on as a read of a place.  Not an integer,
                # for the reason of ``visit_reference``.  Its ``OUTSIDE``
                # escape only when the value goes on: each `memcpy(d, s, n);`
                # gave a row and no edge.  A cast back of the call gives it
                # (``visit_untyped_cast``).
                returned = "R|" + _sym._code_usr(function)
                if _carries(self.record(("slot", returned, ""), call, path, caller, untyped=untyped)):
                    self.note_outside(call, returned)
            if _is_object_call(function):
                target = self.call_object(call, function)
                slot, why = self.slot_of(target) if target is not None else (None, "implicit object")
                object_type = self.object_type(target.type) if target is not None else None
                # The operator() of a template (`std::function`, mbed
                # `Callback`) is one for all its instances: no argument of the
                # site goes into its parameters.
                record = function.semantic_parent
                own_operator = record is not None and record.specialized_template is None
                self.add_site(
                    call, place, caller, "object", slot, object_type or "", why,
                    callee=_sym._code_usr(function) if own_operator else "",
                )
            return
        if not _has_callee(call):
            return
        children = list(call.get_children())
        callee = _unwrap(children[0])
        if (_kind(callee) == _K.BINARY_OPERATOR
                and callee.binary_operator in (cx.BinaryOperator.PtrMemD, cx.BinaryOperator.PtrMemI)):
            # `(obj.*m)(x)`, `(this->*m)(x)`: the pointer to the member is
            # the second operand.
            operands = list(callee.get_children())
            callee = operands[1] if len(operands) == 2 else callee
        pointer_type = _pointer_type(callee.type) or _pointer_type(children[0].type)
        if pointer_type is None:
            return
        slot, why = self.slot_of(callee)
        address = _integer_operand(callee) if not slot else None
        if address is not None:
            # A jump to an address that is a number: the start of the
            # application from a bootloader, a routine of the ROM.  No code
            # of the build is the target, thus the site gets no target of
            # its type; the text of the address tells where it goes.
            self.add_site(call, place, caller, "address", None, pointer_type, _text(address, call))
            return
        # The read of the callee records its ``OUTSIDE`` escape
        # (``visit_reference``, ``visit_call``, ``visit_untyped_cast``).
        self.add_site(call, place, caller, "pointer", slot, pointer_type, why)

    def accessor_holder(self, call: cx.Cursor, function: cx.Cursor) -> str | None:
        """The slot of the object, when *call* gives an element of it by reference through a template, or None.

        `handlers[i]` on a `std::array<cb_t, N>`: the body of `operator[]`
        in the template names a field of a dependent type, thus no fact
        connects the field to the method, and the return slot of the method
        is one for all the instances of the template (each `std::array` of
        the build, of each element type).  The call reads and writes the
        slot of the object instead, which the initializer list of the
        object fills (`climb_init_list`).  A method of a class that is no
        template keeps its return slot: its body gives the flow
        (`return _cb;`).  A method that gives its own object (`*this`:
        `std::function::operator=`, `Chain &add(cb)`) gives no element.
        """
        result = function.result_type.get_canonical()
        if (_kind(function) != _K.CXX_METHOD or function.is_static_method()
                or result.kind != _T.LVALUEREFERENCE or self.slot_type(call.type) is None
                or not _sym._is_implicit_instance(function)):
            return None
        record = function.semantic_parent
        returned_record = _record(result)
        if (record is not None and returned_record is not None
                and returned_record.get_usr() == record.get_usr()):
            return None
        target = self.call_object(call, function)
        return self.slot_of(target)[0] if target is not None else None

    @staticmethod
    def call_object(call: cx.Cursor, function: cx.Cursor) -> cx.Cursor | None:
        """The object of a call of a member: argument 0 in operator syntax, else the base of the callee."""
        if _member_operator_syntax(call, function):
            arguments = list(call.get_arguments())
            return arguments[0] if arguments else None
        children = list(call.get_children())
        member = _unwrap(children[0]) if children else None
        if member is None or _kind(member) != _K.MEMBER_REF_EXPR:
            return None
        bases = list(member.get_children())
        return bases[0] if bases else None


def _integer_operand(callee: cx.Cursor) -> cx.Cursor | None:
    """The integer that a cast of the callee makes a pointer to a function, or None.

    `((void (*)(void))(*(uint32_t *)0x08004004))()`: the operand of the
    cast is an integer that names no slot.  ``visit_call`` asks for it
    only when the callee has no slot: an integer that a place holds
    (`((cb_t) h->handler)(x)`) is the slot of that place
    (``_Collector.slot_of``).  A cast from `void *` is not one: a `void *`
    field can hold a function.
    """
    callee = _unwrap(callee)
    while _kind(callee) in _CASTS:
        operand = _cast_operand(callee)
        if operand is None:
            return None
        if operand.type.get_canonical().kind in _INTEGERS:
            return operand
        callee = _unwrap(operand)
    return None


def _defined_outside(expression: cx.Cursor) -> bool:
    """Tell if the value of *expression* comes from a declaration that the unit does not define.

    An `extern` variable with no definition in the unit (`extern void
    *opaque;`), an element of one, and the call of a function with no body
    in the unit (a library that the build links).  The definition can be in
    an other unit of the build, thus the fact is a candidate: the
    resolution keeps it only when the index has no definition
    (``OUTSIDE``).
    """
    expression = _unwrap(expression)
    while _kind(expression) in (_K.ARRAY_SUBSCRIPT_EXPR, _K.UNARY_OPERATOR) or _kind(expression) in _CASTS:
        children = [c for c in expression.get_children() if _is_expression(c)]
        if not children:
            return False
        expression = _unwrap(children[0] if _kind(expression) == _K.ARRAY_SUBSCRIPT_EXPR else children[-1])
    kind = _kind(expression)
    if kind in _REFERENCE_EXPRESSIONS:
        declaration = expression.referenced
        return (declaration is not None and _kind(declaration) == _K.VAR_DECL
                and declaration.storage_class == cx.StorageClass.EXTERN
                and declaration.get_definition() is None)
    if kind == _K.CALL_EXPR:
        # A pure virtual method has no body by design: the call reaches an
        # override, whose return the override flows give (``_slot_resolution``).
        function = _direct_function(expression)
        return (function is not None and _kind(function) != _K.CONSTRUCTOR
                and not function.is_pure_virtual_method()
                and function.get_definition() is None)
    return False


def _is_range(loop: cx.Cursor, child: cx.Cursor) -> bool:
    """Tell if *child* is the range of the range-for *loop* (`tbl` in `for (void *p : tbl)`), not its body."""
    children = list(loop.get_children())
    at = next((i for i, c in enumerate(children) if _kind(c) == _K.VAR_DECL), None)
    return at is not None and at + 1 < len(children) and _same(children[at + 1], child)


def _reads_element(reference: cx.Cursor, path: list[cx.Cursor]) -> bool:
    """Tell if *reference*, an array, is read as an element (`tbl[i]`, `*tbl`, `t[i][j]`, `for (void *p : tbl)`).

    WHY not ``climb``: it follows the decay of the array and each
    subscript the same way, thus it cannot tell an element from a row or
    from the array itself.  The first node above the subscripts and the
    dereferences that is not an array is the element.  A pointer to an
    element (`get(&tbl[1])`) goes on as the pointer to a scalar place does
    (`get(&p)`), with the escape of ``address_taken``.
    """
    child = reference
    at = len(path) - 1
    while at >= 0:
        parent = path[at]
        kind = _kind(parent)
        if kind == _K.CXX_FOR_RANGE_STMT:
            # Each element goes into the loop variable (``climb_range_for``).
            return _is_range(parent, child)
        if kind == _K.ARRAY_SUBSCRIPT_EXPR:
            children = list(parent.get_children())
            if not children or not _same(children[0], child):
                return False  # the array is not the base
        elif kind == _K.UNARY_OPERATOR:
            if not _points_to(child.type, parent.type):
                return False  # `&tbl`, `!tbl`: no element
        elif kind not in _TRANSPARENT:
            return False
        if kind not in _TRANSPARENT and parent.type.get_canonical().kind not in _ARRAYS:
            return True
        child, at = parent, at - 1
    return False


def _carries(result: _Where | None) -> bool:
    """Tell if a value that ``record`` followed goes into a place or into an argument of a site."""
    return result is not None and result[0] in ("slot", "argument")


def _cast_operand(cast: cx.Cursor) -> cx.Cursor | None:
    """The operand of *cast*: its last expression child (an expression before it belongs to the type)."""
    return next((c for c in reversed(list(cast.get_children())) if _is_expression(c)), None)


def _text(cursor: cx.Cursor, within: cx.Cursor) -> str:
    """The source text of *cursor* inside *within*, from its tokens (`*(uint32_t*)0x08004004`, `APP_RESET`).

    WHY inside *within*: the extent of an operand that a macro writes
    starts in the `#define` (measured, libclang 22), thus its tokens held
    the definition and the code between it and the call.  The tokens at
    the call give the name of the macro.
    """
    bounds = within.extent
    if bounds.start.file is None:
        return ""
    file = bounds.start.file.name
    start = cursor.extent.start
    if start.file is None or start.file.name != file or not bounds.start.offset <= start.offset:
        # The extent starts in the macro: the text starts where the macro
        # is used, the place of the expansion.
        start = cursor.location
    text = ""
    for token in cursor.get_tokens():
        place = token.location
        if (place.file is None or place.file.name != file
                or not start.offset <= place.offset < bounds.end.offset):
            continue
        spelling = token.spelling
        if text and (text[-1].isalnum() or text[-1] == "_") and (spelling[0].isalnum() or spelling[0] == "_"):
            text += " "
        text += spelling
    return text


def _is_expression(cursor: cx.Cursor) -> bool:
    kind = _kind(cursor)
    return kind is not None and kind.is_expression()


def _ends_before(cursor: cx.Cursor, place: cx.SourceLocation) -> bool:
    """*cursor* ends at or before *place*, in the same file."""
    end = cursor.extent.end
    return (
        end.file is not None and place.file is not None
        and end.file.name == place.file.name and end.offset <= place.offset
    )


def _children(cursor: cx.Cursor) -> Iterator[cx.Cursor]:
    """The children of *cursor* that the walk visits: the ones that the program evaluates.

    An expression that only names a type (`__typeof__(handlers[0]) tmp =
    h2;`, `decltype(g_cb) local`, `(__typeof__(x))v`) or that is not
    evaluated (`sizeof`) gives no fact: it gave false flows and escapes
    (review of phase 5a).  The type part of a declaration ends before the
    name of the declaration.  Limit: in a macro expansion each node has the
    place of the expansion, thus the type part of a declaration that one
    macro writes stays in the walk.
    """
    kind = _kind(cursor)
    if kind in _UNEVALUATED:
        return iter(())
    if kind in _DECLARATORS:
        place = cursor.location
        return iter([
            child for child in cursor.get_children()
            if not (_is_expression(child) and _ends_before(child, place))
        ])
    if kind in _TYPE_NAMES:
        return iter([child for child in cursor.get_children() if not _is_expression(child)])
    if kind in _TYPED_OPERANDS:
        children = list(cursor.get_children())
        operand = next((child for child in reversed(children) if _is_expression(child)), None)
        return iter([child for child in children if child is operand or not _is_expression(child)])
    if kind == _K.GENERIC_SELECTION_EXPR:
        children = list(cursor.get_children())
        return iter(children[1:])  # the controlling expression is not evaluated
    return iter(cursor.get_children())


def _walk(
    collector: _Collector,
    root: cx.Cursor,
    path: list[cx.Cursor],
    caller: str | None,
    skip_files: frozenset[str] | None,
    resolve: Callable[[str], Path],
    *,
    nested_definitions: bool,
) -> None:
    """Give each cursor below *root* to *collector*, with its path and its code key.

    *nested_definitions* False leaves out a function or a class that is
    defined inside the body (a method of a local class): in an instance
    body it is code of its own, and an instance of it is walked when a
    call names it (``symbols._body_cursors``).
    """
    collector.start_walk(instance=not nested_definitions)
    stack: list[tuple[cx.Cursor, Iterator[cx.Cursor], str | None]] = [(root, _children(root), caller)]
    while stack:
        parent, children, code = stack[-1]
        cursor = next(children, None)
        if cursor is None:
            stack.pop()
            path.pop()
            continue
        kind = _kind(cursor)
        if kind is None:
            continue
        place = cursor.location
        if skip_files and place.file is not None and str(resolve(place.file.name)) in skip_files:
            continue
        is_definition = kind in _FUNCTIONS and cursor.is_definition()
        if not nested_definitions and (is_definition or kind in _sym._INSTANCE_CLASS_KINDS):
            continue
        inner = code
        if is_definition:
            inner = _sym._code_usr(cursor)
        elif kind == _K.COMPOUND_STMT and _kind(parent) == _K.LAMBDA_EXPR:
            inner = _sym._lambda_usr(parent)
        collector.visit(cursor, path, code)
        stack.append((cursor, _children(cursor), inner))
        path.append(cursor)


def collect_slot_facts(
    tu: cx.TranslationUnit,
    skip_files: frozenset[str] | None,
    resolve: Callable[[str], Path],
    instances: Sequence[cx.Cursor] = (),
) -> SlotFacts:
    """Walk *tu* and the bodies of its template *instances*, and give their slot facts (see the module docstring).

    A subtree of a file in *skip_files* is left out, as in the walk of the
    references: an earlier unit gave its facts.  The code key of a cursor
    is the function whose definition holds it, or the lambda whose body
    holds it.

    *instances* are the definitions that ``symbols._walk_template_instances``
    walked.  WHY their bodies: a call in a template body that depends on a
    parameter has no target in the written code (`post(&Q::run<F>, e)`),
    and only the instance shows the function whose address goes into the
    parameter.  The keys of an instance are the keys of its template
    (``_code_usr``, ``member_usr``), thus the facts of two instances at one
    place are one fact.  A body is walked where it lies, also in a header
    of the skip set, as the calls of the instances are.
    """
    collector = _Collector()
    root = tu.cursor
    _walk(collector, root, [root], None, skip_files, resolve, nested_definitions=True)
    for definition in instances:
        _walk(collector, definition, [definition], _sym._code_usr(definition), None, resolve,
              nested_definitions=False)
    facts = collector.facts
    # A macro that expands more than one time at one place
    # (`FOR_EACH(DEFINE_OPS)`) gives equal facts, because each expansion has
    # the place of the macro.  A copy adds nothing to the resolution, and
    # find_indirect_targets would show the place two times.  The sites stay
    # as they are: the ordinal tells apart two calls at one place.  A fact
    # of an instance that the written code gives too is that fact.
    instance = collector.instance_facts
    for name in ("sources", "flows", "arguments", "escapes"):
        written = list(dict.fromkeys(getattr(facts, name)))
        setattr(facts, name, written)
        known = set(written)
        setattr(instance, name, [fact for fact in dict.fromkeys(getattr(instance, name)) if fact not in known])
    facts.from_instances = instance
    return facts
