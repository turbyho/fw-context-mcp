/*
 * Conditionals whose value the reader cannot compute.
 *
 * The assembler knows the distance between two labels of one section, and
 * the reader does not: it never computes an address.  Thus it must skip
 * every branch of such a conditional and invent nothing.  Each "maybe_"
 * name below is in the object, and the reader may miss it.
 *
 * After such a conditional, a later `.ifdef` cannot be answered either:
 * the skipped branch could have defined the name.
 */

    .syntax unified
    .thumb
    .text

    .globl  plain_before
plain_before:
    nop
    nop
after_two:

    .if     after_two - plain_before
    .globl  maybe_taken
maybe_taken:
    nop
    .else
    .globl  never_taken
never_taken:
    nop
    .endif

    .ifdef  maybe_taken
maybe_after_ifdef:
    nop
    .endif

    .globl  plain_after
plain_after:
    nop
