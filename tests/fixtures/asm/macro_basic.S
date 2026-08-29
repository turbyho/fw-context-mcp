/*
 * .macro / .endm, the plain forms.
 *
 * These belong to the ASSEMBLER, not to the C preprocessor, so
 * `clang -E` passes them through untouched and a reader that stops at
 * the preprocessor never sees the symbols they define.  Measured on
 * zbox-ecb-fw, that is 43 of 54 vector slots.
 */

    .syntax unified
    .thumb
    .text

/* No arguments. */
    .macro  no_args
    .globl  from_no_args
from_no_args:
    b       .
    .endm

    no_args

/* One argument.  This is the shape a real nRF startup writes. */
    .macro  IRQ handler
    .weak   \handler
    .set    \handler, from_no_args
    .endm

    IRQ  POWER_CLOCK_IRQHandler
    IRQ  RADIO_IRQHandler

/* Arguments separated by commas, and by blanks: the manual says either. */
    .macro  pair_commas first, second
    .globl  \first
\first:
    b       .
    .globl  \second
\second:
    b       .
    .endm

    .macro  pair_blanks third fourth
    .globl  \third
\third:
    b       .
    .globl  \fourth
\fourth:
    b       .
    .endm

    pair_commas comma_one, comma_two
    pair_blanks blank_one blank_two

/* An invocation may pass its arguments separated by blanks even when
   the definition used commas.  The separator is a property of neither. */
    pair_commas mixed_one mixed_two

/* A macro that defines nothing is still a macro. */
    .macro  emits_nothing
    .endm

    emits_nothing

/*
 * Defined and never invoked.  The assembler emits NOTHING for this, so
 * `ghost_never_invoked` is in no object file and in no binary.
 *
 * A reader that does not know `.macro` reads the body as ordinary
 * assembly and reports the label anyway — an invented symbol, which is
 * worse than a missing one: it puts a name in lookup_symbol that no
 * linker, debugger or reader will ever find.  Skipping macro bodies is
 * therefore a prerequisite for expanding them, and an improvement even
 * on its own.
 */
    .macro  never_called
    .globl  ghost_never_invoked
ghost_never_invoked:
    b       .
    .endm
