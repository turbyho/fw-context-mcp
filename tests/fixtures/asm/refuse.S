/*
 * Constructs an expander must REFUSE rather than guess at.
 *
 * A macro body may branch, and the manual's own `sum` example recurses
 * and is terminated only by `.if \to-\from`.  Expanding either needs an
 * assembler expression evaluator, which means writing a large part of
 * the assembler.
 *
 * The rule for everything here is the same: skip the macro and say so.
 * A missing symbol means something is not found.  An INVENTED symbol
 * puts a name in lookup_symbol that is in no binary, and no linker,
 * debugger or reader will ever find it.
 *
 * The expectations for this file are therefore deliberately small: the
 * symbols outside the refused macros, and nothing from inside them.
 */

    .syntax unified
    .thumb
    .text

/* Assembled normally, before anything refusable. */
    .globl  plain_before
plain_before:
    b       .

/* A body that branches on its argument. */
    .macro  conditional_body which
    .if     \which
    .globl  taken_branch
taken_branch:
    b       .
    .else
    .globl  other_branch
other_branch:
    b       .
    .endif
    .endm

    conditional_body 1

/*
 * The manual's own recursion, near enough verbatim.  Terminated by
 * `.if`, so an expander without expression evaluation cannot decide
 * when to stop and must not start.
 *
 * The argument goes into DATA, not into a label: `\from` arrives as the
 * unevaluated text `(1+1)`, which is a fine expression and an invalid
 * symbol name.  The manual's example has the same shape for the same
 * reason.
 */
    .globl  recursion_target
recursion_target:
    .macro  sum from=0, to=3
    .long   \from
    .if     \to-\from
    sum     "(\from+1)",\to
    .endif
    .endm

    sum 0,3

/* Assembled normally, after them. */
    .globl  plain_after
plain_after:
    b       .
