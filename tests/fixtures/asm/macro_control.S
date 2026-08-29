/*
 * .exitm and .purgem: leaving a body early, and undefining a macro.
 *
 * The manual states you cannot define two macros with the same name
 * unless `.purgem` came between, so a reader that keeps the first
 * definition after a redefinition would expand the wrong body.
 */

    .syntax unified
    .thumb
    .text

/* `.purgem` undefines, so the same name may be defined again. */
    .macro  same_name
    .globl  first_definition
first_definition:
    b       .
    .endm

    same_name

    .purgem same_name

    .macro  same_name
    .globl  second_definition
second_definition:
    b       .
    .endm

    same_name

/*
 * `.exitm` leaves the body early.  Unconditional here on purpose: the
 * conditional form needs expression evaluation, which belongs in
 * refuse.S rather than in a file whose expectations must be exact.
 */
    .macro  stops_early
    .globl  before_exitm
before_exitm:
    b       .
    .exitm
    .globl  after_exitm
after_exitm:
    b       .
    .endm

    stops_early
