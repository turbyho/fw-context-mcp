"""Vector table entries a running program writes, not a build.

A CMSIS target can move its vector table into RAM and fill it at run
time.  ``NVIC_SetVector(IRQn, address)`` is how it does that, and it is
the only record of which handler services that interrupt: the table in
flash holds an alias of the default handler, which is a trap loop.

Measured on the Mbed project, which sets ``CMSIS_VECTAB_VIRTUAL`` and
gives RAM a 256-byte ``RAM_NVIC`` region: 13 registrations across 11
slots, and every one of those slots read as ``unhandled`` because that
is what the static table says.  Among them the tick source (``TIMER1``
-> ``us_ticker_irq_handler``), the low-power tick (``RTC1``), the
console (``UARTE0``), both I2C/SPI instances and USB.  The answer
"nothing services this interrupt" was wrong for all of them.  Two of
the slots are registered from two files each, which is where the
difference between 13 and 11 comes from.

**The name of the function is what identifies the pattern.**  That is
platform knowledge, of the same kind this package already holds in
``intlist`` for Zephyr, and it is deliberate: nothing in the structure
of a call distinguishes "write a vector" from "register a callback with
an index", so a structural rule would either miss this or claim every
indexed callback registration is a vector.  The names are CMSIS, thus
they are the same on every Cortex-M target and vendor SDK.

**The slot is not the IRQ number.**  ``slot_index`` records the IRQ
number, exactly as the argument gives it, and the reader adds
``NVIC_USER_IRQ_OFFSET`` — 16 on Cortex-M — from the macro table of the
same index.  Doing the addition here would write a constant into the
data; doing it at read time keeps it a fact the build stated.
"""

from __future__ import annotations

import logging

import clang.cindex as cx

from .models import Reference

_log = logging.getLogger(__name__)

# refs.ref_kind for a vector slot a call installs at run time.  NOT
# "vector": that one is a slot an assembler wrote into the image, and the
# difference is what a reader needs — one is in the firmware, the other
# happens only once the code runs.
RUNTIME_VECTOR_REF_KIND = "runtime_vector"

# The CMSIS functions that write one entry of the vector table.  The
# double-underscore form is the one in `core_cm*.h`; a target that
# redirects the table (`CMSIS_VECTAB_VIRTUAL`) defines the plain name in
# its own `cmsis_nvic.c` and writes to its RAM copy instead.
_SETVECTOR_NAMES = frozenset({"NVIC_SetVector", "__NVIC_SetVector"})


def _first_of_kind(cursor: cx.Cursor, kind: cx.CursorKind) -> cx.Cursor | None:
    """Return the first cursor of *kind* in *cursor*, itself included.

    The argument of interest is wrapped: an IRQ number arrives as an
    implicit cast around a declaration reference, and a handler address
    as ``(uint32_t) handler``, which adds a C-style cast of its own.
    Walking to the first match unwraps both without naming either.
    """
    try:
        for found in cursor.walk_preorder():
            if found.kind == kind:
                return found
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("walk_preorder failed while reading a vector registration")
    return None


def _irq_number(argument: cx.Cursor) -> int | None:
    """Return the IRQ number *argument* names, or None.

    Only an enumerator answers.  Every SDK declares its interrupts as an
    enum (``TIMER1_IRQn``), and libclang knows the value, so this needs
    no arithmetic and cannot be off by one.

    A variable answers None on purpose.  ``NVIC_SetVector((IRQn_Type)i,
    ...)`` in a loop installs a handler for a slot this cannot name, and
    a guessed number in front of a reader is worse than no row.
    """
    found = _first_of_kind(argument, cx.CursorKind.DECL_REF_EXPR)
    if found is None:
        return None
    referenced = found.referenced
    if referenced is None or referenced.kind != cx.CursorKind.ENUM_CONSTANT_DECL:
        return None
    try:
        value = referenced.enum_value
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("enum_value failed for %s", referenced.spelling)
        return None
    return int(value) if value is not None and value >= 0 else None


def _handler_usr(argument: cx.Cursor) -> str | None:
    """Return the USR of the function *argument* points at, or None.

    The address is passed as ``(uint32_t) handler``, so the function is a
    declaration reference under a cast.  A cast of anything else — a
    computed address, a pointer variable — names no function and answers
    None.
    """
    found = _first_of_kind(argument, cx.CursorKind.DECL_REF_EXPR)
    if found is None:
        return None
    referenced = found.referenced
    if referenced is None or referenced.kind != cx.CursorKind.FUNCTION_DECL:
        return None
    usr = referenced.get_usr()
    return usr or None


def runtime_vector_reference(
    cursor: cx.Cursor, referenced: cx.Cursor | None, cur_fn: str | None
) -> Reference | None:
    """Return the vector slot this call installs, or None.

    *cursor* is a ``CALL_EXPR`` and *referenced* the function it calls,
    which the caller already resolved — asking libclang again would cost
    one call across the FFI boundary for every call expression in the
    build.  The result is a reference from the call site to the handler,
    carrying the IRQ number in ``slot_index``.

    None is the answer whenever any part is not certain: another
    function, too few arguments, an IRQ this cannot name, or an address
    that is not a function.  Every one of those is a slot the reader
    keeps seeing as the static table describes it, which is the state
    before this existed.
    """
    if referenced is None or referenced.spelling not in _SETVECTOR_NAMES:
        return None

    try:
        arguments = list(cursor.get_arguments())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("get_arguments failed for a call to NVIC_SetVector")
        return None
    if len(arguments) < 2:
        return None

    irq = _irq_number(arguments[0])
    handler = _handler_usr(arguments[1])
    if irq is None or handler is None:
        return None

    location = cursor.location
    if location is None or location.file is None:
        return None
    return Reference(
        to_usr=handler,
        from_file=location.file.name,
        from_line=location.line,
        from_usr=cur_fn,
        ref_kind=RUNTIME_VECTOR_REF_KIND,
        slot_index=irq,
    )
