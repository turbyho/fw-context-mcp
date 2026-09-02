/*
 * Macro arguments: defaults, qualifiers, and how a call supplies them.
 *
 * Every rule here is from the manual's `.macro` node, and every one is
 * checked by assembling this file: what ends up in the symbol table is
 * what the argument really expanded to.
 */

    .syntax unified
    .thumb
    .text

/* A default value, given after the name with `=`. */
    .macro  with_default name=default_was_used
    .globl  \name
\name:
    b       .
    .endm

    with_default
    with_default default_was_overridden

/*
 * The manual's own shape: a default on the FIRST argument and none on
 * The second.  Called as `reserve_str A,B` both are given; called as
 * `reserve_str ,B` the first takes its default.
 */
    .macro  reserve_str p1=first_defaulted p2
    .globl  \p1
\p1:
    b       .
    .globl  \p2
\p2:
    b       .
    .endm

    reserve_str both_given_one, both_given_two
    reserve_str , second_only

/* `:req` means the call must supply a non-blank value. */
    .macro  needs_value p:req
    .globl  \p
\p:
    b       .
    .endm

    needs_value required_was_given

/* `:vararg` collects every remaining argument, comma and all. */
    .macro  head_and_rest first, rest:vararg
    .globl  \first
\first:
    b       .
    .word   \rest
    .endm

    head_and_rest vararg_head, 1, 2, 3

/* Keyword arguments, in any order, and with earlier values omitted. */
    .macro  sum_like from=0, to=0
    .globl  by_position_\from\()_\to
by_position_\from\()_\to:
    b       .
    .endm

    sum_like 1, 2
    sum_like to=4, from=3
    sum_like to=6

/*
 * Only DOUBLE QUOTES are stripped while establishing an argument.  The
 * quoted value below therefore becomes a plain symbol name; parentheses
 * and brackets would have stayed part of the value, so they are not
 * used to build a label here.
 */
    .macro  quoted name
    .globl  \name
\name:
    b       .
    .endm

    quoted "quotes_were_stripped"
