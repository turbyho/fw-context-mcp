/*
 * .rept, .irp and .irpc: bounded repetition, all closed by .endr.
 *
 * None of the three needs expression evaluation as long as the count is
 * a literal, which is what separates them from the conditional forms in
 * refuse.S.
 */

    .syntax unified
    .thumb
    .text

/* `.rept COUNT` repeats the body COUNT times.  A count of zero is
   allowed and generates nothing. */
    .globl  repeated_data
repeated_data:
    .rept   3
    .word   0
    .endr

/*
 * A count of zero assembles the body zero times, so a label inside is
 * defined nowhere.  A reader that walks the body as ordinary assembly
 * reports it anyway — the same invention a never-invoked macro causes,
 * which is why the bodies of these directives are skipped until they
 * are expanded properly.
 */
    .rept   0
    .globl  ghost_from_rept_zero
ghost_from_rept_zero:
    .word   0xdead
    .endr

/* `.irp SYMBOL,VALUES...` runs the body once per value, with \SYMBOL
   bound to each in turn. */
    .irp    which, alpha, beta, gamma
    .globl  irp_\which
irp_\which:
    b       .
    .endr

/* With no values at all the body runs once, with the symbol bound to
   the null string. */
    .irp    nothing
    .globl  irp_ran_once
irp_ran_once:
    b       .
    .endr

/* `.irpc SYMBOL,VALUES` runs the body once per CHARACTER. */
    .irpc   digit, 123
    .globl  irpc_\digit
irpc_\digit:
    b       .
    .endr

/* A quoted value list: the quotes are not part of any value. */
    .irp    which, "quoted_one", "quoted_two"
    .globl  \which
\which:
    b       .
    .endr

/* Nesting: a repetition inside a repetition. */
    .irp    outer, a, b
    .irp    inner, 1, 2
    .globl  nest_\outer\()_\inner
nest_\outer\()_\inner:
    b       .
    .endr
    .endr

/* A repetition inside a macro body, which only expands when invoked. */
    .macro  three_of name
    .rept   3
    .word   0
    .endr
    .globl  \name
\name:
    b       .
    .endm

    three_of from_macro_with_rept
