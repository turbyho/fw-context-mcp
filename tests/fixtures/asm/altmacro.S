/*
 * Alternate macro mode, which the expander refuses rather than guesses.
 *
 * `.altmacro` changes the rules the expander implements: arguments may
 * be delimited by `'…'` or `<…>`, `!` escapes any single character,
 * `%expr` substitutes the value of an expression, `&` separates like
 * `\()`, keyword arguments stop working, and `LOCAL` mints a unique name
 * for every expansion.
 *
 * Applying the ordinary rules here does not merely miss symbols, it
 * INVENTS them — which is why refusing is the answer.  Measured before
 * the refusal existed: the expander reported `scratch` from the LOCAL
 * below, a name the assembler renames on every expansion so that it is
 * in no object file, and missed the real symbol behind the angle
 * brackets.
 */

    .syntax unified
    .thumb
    .text

/* Ordinary mode, to prove the refusal starts and stops where it should. */
    .macro before_alternate name
    .globl \name
\name:
    b       .
    .endm

    before_alternate defined_before

    .altmacro

/* LOCAL: the symbol this defines is NOT `scratch`. */
    .macro with_local
    LOCAL scratch
scratch:
    b       scratch
    .endm

    with_local
    with_local

/* Angle brackets delimit a string in this mode only. */
    .macro named who
    .globl \who
\who:
    b       .
    .endm

    named <bracket_delimited>

    .noaltmacro

/* Back to ordinary mode: expansion works again. */
    before_alternate defined_after
