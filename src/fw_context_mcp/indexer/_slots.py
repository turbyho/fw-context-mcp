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
  function (``std::function``);
* an **argument**: a value that a site passes, which goes into a parameter
  of each function that the site reaches (known after the resolution);
* an **escape**: the address of a slot (or of an element of it) is taken,
  or a slot value is cast to an integer or to ``void *``: code that the
  tree does not show can write it.

The resolution over all the units of a build is ``_slot_resolution``
(phase 5b).  The model and its measurement: probe s3 of the call edge plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
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
# The name of an operator (`operator()`, `operator=`, `operator new`), and
# not a function whose name starts with the word (`operatorSelect`).
_OPERATOR_NAME = re.compile(r"operator(?![A-Za-z0-9_])")
_B = cx.BinaryOperator
_COMPARISONS = frozenset({_B.Cmp, _B.LT, _B.GT, _B.LE, _B.GE, _B.EQ, _B.NE, _B.LAnd, _B.LOr})
# What a pointer to a slot can do (`_Collector.address_taken`).
_ADDRESS_WRITABLE = "writable"
_ADDRESS_READ_ONLY = "read only"
# Where a value goes (`_Collector.climb`): ("slot", key), ("argument",
# file, line, column, ordinal, index), ("escape", why) or ("none", why).
_Where = tuple[str | int, ...]


@dataclass
class SlotFacts:
    """The slot facts of one translation unit (see the module docstring)."""

    sources: list[SlotSource] = field(default_factory=list)
    flows: list[SlotFlow] = field(default_factory=list)
    sites: list[SlotSite] = field(default_factory=list)
    arguments: list[SlotArgument] = field(default_factory=list)
    escapes: list[SlotEscape] = field(default_factory=list)


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
    return _EMPTY_PARAMETERS.sub("()", type_.get_canonical().spelling)


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
            return "M:" + _function_type(pointee)
    if type_.kind in _FUNCTION_TYPES:
        return _function_type(type_)
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


def _code_type(function: cx.Cursor) -> str:
    """The type key of a function as a value: `M:` for a method with `this`."""
    spelling = _function_type(function.type)
    if _kind(function) == _K.CXX_METHOD and not function.is_static_method():
        return "M:" + spelling
    return spelling


def _record(type_: cx.Type) -> cx.Cursor | None:
    type_ = type_.get_canonical()
    if type_.kind in _REFERENCES:
        type_ = type_.get_pointee().get_canonical()
    if type_.kind != _T.RECORD:
        return None
    declaration = type_.get_declaration()
    if _kind(declaration) is None or declaration.kind == _K.NO_DECL_FOUND:
        return None
    return declaration


class _Collector:
    """Collects the slot facts of one unit (see the module docstring)."""

    def __init__(self) -> None:
        self.facts = SlotFacts()
        self._callable_records: dict[str, bool] = {}
        self._member_usrs: dict[str, str] = {}
        self._records_with_methods: dict[str, bool] = {}
        # The number of sites at each place, and the ordinal of each site
        # node, for the arguments of the site (`SlotSite.ordinal`).
        self._sites_at: dict[tuple[str, int, int], int] = {}
        self._site_ordinals: dict[tuple[tuple[str, int, int], int, cx.CursorKind | None], int] = {}

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
        return _pointer_type(type_) or self.object_type(type_)

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
        """
        usr = declaration.get_usr()
        parent = declaration.semantic_parent
        if not usr or parent is None or _kind(parent) not in _sym._INSTANCE_CLASS_KINDS:
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
                return "R|" + _sym._code_usr(function), ""
            return None, "result of an indirect call"
        return None, f"expression {kind}"

    # ---- where a value goes ---------------------------------------------

    def climb(
        self, node: cx.Cursor, path: list[cx.Cursor], caller: str | None,
    ) -> _Where:
        """Follow a value up the tree to the place that stores it.

        ('slot', key[, key of the object]) | ('argument', site place,
        ordinal, index) | ('escape', why) | ('none', why).
        """
        child = node
        at = len(path) - 1
        while at >= 0:
            parent = path[at]
            kind = _kind(parent)
            untracked = self.untracked(parent.type) if kind in _TRANSPARENT or kind in _CASTS else None
            if untracked and _pointer_type(child.type) is None:
                # A pointer to a slot, not a value of it: `address_taken`
                # tells if it escapes (`memcpy(&g, &tab[0], n)` only reads).
                untracked = None
                if kind in _CASTS or kind == _K.UNEXPOSED_EXPR:
                    return ("none", "pointer to a slot as an untyped pointer")
            if kind in _TRANSPARENT:
                if (kind == _K.UNEXPOSED_EXPR and untracked
                        and child.type.get_canonical().kind != _T.RECORD):
                    return ("escape", "cast to " + untracked)
                child, at = parent, at - 1
                continue
            if kind in _CASTS:
                if untracked:
                    return ("escape", "cast to " + untracked)
                child, at = parent, at - 1
                continue
            if kind == _K.UNARY_OPERATOR:
                parent_type = parent.type.get_canonical()
                if parent_type.kind in (_T.POINTER, _T.MEMBERPOINTER) or parent_type.kind in _FUNCTION_TYPES:
                    child, at = parent, at - 1
                    continue
                return ("none", "unary operator")
            if kind == _K.CONDITIONAL_OPERATOR:
                children = list(parent.get_children())
                if children and _same(children[0], child):
                    return ("none", "condition")
                child, at = parent, at - 1
                continue
            if kind == _K.ARRAY_SUBSCRIPT_EXPR:
                children = list(parent.get_children())
                if children and _same(children[0], child):
                    child, at = parent, at - 1
                    continue
                return ("none", "index")
            if kind == _K.BINARY_OPERATOR:
                children = list(parent.get_children())
                if _sym._is_plain_assignment(parent) and len(children) == 2:
                    if _same(children[1], child):
                        slot, why = self.slot_of(children[0])
                        return ("slot", slot) if slot else ("none", "left side: " + why)
                    return ("none", "left side of an assignment")
                return ("none", "operator")
            if kind == _K.COMPOUND_ASSIGNMENT_OPERATOR:
                return ("none", "compound assignment")
            if kind == _K.CALL_EXPR:
                result = self.climb_call(parent, child)
                if result is None:
                    child, at = parent, at - 1
                    continue
                return result
            if kind in (_K.VAR_DECL, _K.FIELD_DECL, _K.PARM_DECL):
                # A PARM_DECL holds its default argument
                # (`void attach(cb_t cb = default_isr)`).
                slot = self.declaration_slot(parent)
                return ("slot", slot) if slot else ("none", str(kind))
            if kind == _K.INIT_LIST_EXPR:
                result = self.climb_init_list(parent, child, path, at)
                if result is None:
                    child, at = parent, at - 1
                    continue
                return result
            if kind == _K.RETURN_STMT:
                return ("slot", "R|" + caller) if caller else ("none", "return outside code")
            if kind == _K.CONSTRUCTOR:
                return self.climb_member_initializer(parent, child)
            if kind == _K.MEMBER_REF_EXPR:
                call = _closure_conversion_call(path, at)
                if call is None:
                    return ("none", "base of a member")
                child, at = call, at - 2
                continue
            if kind == _K.CXX_FOR_RANGE_STMT:
                return self.climb_range_for(parent, child)
            return ("none", f"statement {kind}")
        return ("none", "top")

    def climb_range_for(self, loop: cx.Cursor, child: cx.Cursor) -> _Where:
        """`for (cb_t cb : g_tab)`: each element of the range goes into the loop variable.

        A loop variable that is a reference reads the elements as a copy
        does, but it is an alias of an element: a write through it is not
        seen (a known gap of phase 5a, as each reference to a slot).
        """
        children = list(loop.get_children())
        at = next((i for i, c in enumerate(children) if _kind(c) == _K.VAR_DECL), None)
        if at is None or at + 1 >= len(children) or not _same(children[at + 1], child):
            return ("none", "range for")
        slot = self.declaration_slot(children[at])
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
        the method reads the object (`visit_call`), and the object holds
        the values of its list.
        """
        list_type = init_list.type.get_canonical()
        if list_type.kind in _ARRAYS:
            return None
        if list_type.kind != _T.RECORD:
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
        """The type of the place that *element* of an initializer list fills."""
        list_type = init_list.type.get_canonical()
        if list_type.kind in _ARRAYS:
            return list_type.element_type
        if list_type.kind == _T.RECORD:
            return self.init_list_field(init_list, element)[1]
        return None

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
    ) -> None:
        """Record where *value* goes: ("function", key, type) or ("slot", key, "")."""
        place = _place(node)
        if place is None:
            return
        file, line, column = place
        result = self.climb(node, path, caller)
        if result[0] == "slot":
            for target in (str(key) for key in result[1:]):
                if value[0] == "function":
                    self.facts.sources.append(SlotSource(
                        file=file, line=line, column=column, function=value[1],
                        function_type=value[2], slot=target,
                    ))
                elif value[1] != target:
                    self.facts.flows.append(SlotFlow(
                        file=file, line=line, column=column, source=value[1], target=target,
                    ))
        elif result[0] == "argument":
            site_file, site_line, site_column, ordinal, index = result[1:6]
            self.facts.arguments.append(SlotArgument(
                file=str(site_file), line=int(site_line), column=int(site_column), ordinal=int(ordinal),
                index=int(index), value_kind=value[0], value=value[1],
            ))
        elif value[0] == "function":
            # The address is taken, and nothing known holds it: only its
            # type tells the calls that can reach it.
            self.facts.sources.append(SlotSource(
                file=file, line=line, column=column, function=value[1],
                function_type=value[2], slot="",
            ))
        elif result[0] == "escape":
            self.facts.escapes.append(SlotEscape(
                file=file, line=line, column=column, slot=value[1], reason=str(result[1]),
            ))

    def add_site(
        self, call: cx.Cursor, place: tuple[str, int, int], caller: str | None,
        kind: str, slot: str | None, slot_type: str, reason: str,
    ) -> None:
        ordinal = self._sites_at.get(place, 0)
        self._sites_at[place] = ordinal + 1
        # The key holds the place: the hash of a node has 32 bits.
        self._site_ordinals[(place, *_node_key(call))] = ordinal
        self.facts.sites.append(SlotSite(
            file=place[0], line=place[1], column=place[2], ordinal=ordinal, caller=caller, kind=kind,
            slot=slot or "", slot_type=slot_type, reason=reason,
        ))

    def visit(self, cursor: cx.Cursor, path: list[cx.Cursor], caller: str | None) -> None:
        kind = _kind(cursor)
        if kind in _REFERENCE_EXPRESSIONS:
            self.visit_reference(cursor, path, caller)
        elif kind == _K.LAMBDA_EXPR:
            self.visit_lambda(cursor, path, caller)
        elif kind == _K.CALL_EXPR:
            self.visit_call(cursor, path, caller)

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
            self.record(
                ("function", _sym._code_usr(declaration), _code_type(declaration)), cursor, path, caller,
            )
            return
        if kind in (_K.VAR_DECL, _K.PARM_DECL, _K.FIELD_DECL):
            if self.slot_type(declaration.type) is None:
                return
            slot = self.declaration_slot(declaration)
            place = _place(cursor)
            if not slot or place is None:
                return
            if self.address_taken(cursor, path, declaration) == _ADDRESS_WRITABLE:
                self.facts.escapes.append(SlotEscape(
                    file=place[0], line=place[1], column=place[2], slot=slot, reason="address taken",
                ))
            # A pointer to the slot flows on too: a table that a function
            # gets as a pointer (`dispatch(fns, n)`, `table[i](x)` in it)
            # reads the values of the slot through the parameter.
            self.record(("slot", slot, ""), cursor, path, caller)

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
                self.record(("slot", "R|" + _sym._code_usr(function), ""), call, path, caller)
            self.read_through_accessor(call, function, place)
            if _is_object_call(function):
                target = self.call_object(call, function)
                slot, why = self.slot_of(target) if target is not None else (None, "implicit object")
                object_type = self.object_type(target.type) if target is not None else None
                self.add_site(call, place, caller, "object", slot, object_type or "", why)
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
        self.add_site(call, place, caller, "pointer", slot, pointer_type, why)

    def read_through_accessor(self, call: cx.Cursor, function: cx.Cursor, place: tuple[str, int, int]) -> None:
        """A method that gives a slot of its object by reference: the object flows into its return slot.

        `handlers[i](1)` on a `std::array<cb_t, N>`: the site reads
        `R|operator[]`, and the values of the list of `handlers` are in the
        slot of the object (`climb_init_list`).  The return slot is one for
        each instance of the template, as a field is one for each object of
        a struct.
        """
        if (_kind(function) != _K.CXX_METHOD or function.is_static_method()
                or function.result_type.get_canonical().kind != _T.LVALUEREFERENCE
                or self.slot_type(call.type) is None):
            return
        target = self.call_object(call, function)
        holder = self.slot_of(target)[0] if target is not None else None
        returned = "R|" + _sym._code_usr(function)
        if holder and holder != returned:
            self.facts.flows.append(SlotFlow(
                file=place[0], line=place[1], column=place[2], source=holder, target=returned,
            ))

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


def collect_slot_facts(
    tu: cx.TranslationUnit,
    skip_files: frozenset[str] | None,
    resolve: Callable[[str], Path],
) -> SlotFacts:
    """Walk *tu* and give its slot facts (see the module docstring).

    A subtree of a file in *skip_files* is left out, as in the walk of the
    references: an earlier unit gave its facts.  The code key of a cursor
    is the function whose definition holds it, or the lambda whose body
    holds it.
    """
    collector = _Collector()
    root = tu.cursor
    stack: list[tuple[cx.Cursor, Iterator[cx.Cursor], str | None]] = [(root, _children(root), None)]
    path: list[cx.Cursor] = [root]
    while stack:
        parent, children, caller = stack[-1]
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
        inner = caller
        if kind in _FUNCTIONS and cursor.is_definition():
            inner = _sym._code_usr(cursor)
        elif kind == _K.COMPOUND_STMT and _kind(parent) == _K.LAMBDA_EXPR:
            inner = _sym._lambda_usr(parent)
        collector.visit(cursor, path, caller)
        stack.append((cursor, _children(cursor), inner))
        path.append(cursor)
    return collector.facts
