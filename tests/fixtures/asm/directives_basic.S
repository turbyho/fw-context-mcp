/*
 * The directives that name a symbol, with no macro facility involved.
 *
 * A label is what defines a symbol; .global and .weak say how it is
 * exported and .type says what it is, and any of the three may come
 * before or after the label.
 */

    .syntax unified
    .thumb

    .text

/* .globl and .global are the same directive spelled two ways.  A reader
   that knows only one returns nothing on half the world's assembly. */
    .globl  spelled_globl
    .type   spelled_globl, %function
spelled_globl:
    b       .
    .size   spelled_globl, . - spelled_globl

    .global spelled_global
spelled_global:
    b       .

/* The directive after the label, which is the other order irq_cm4f.S
   uses: .type, then .global, then the label. */
declared_after:
    b       .
    .globl  declared_after

/* A label with no directive at all.  GNU as still records it, as a
   local symbol, and it is still a definition of that name. */
plain_local_label:
    b       .

/* .weak makes a definition another object may override. */
    .weak   weak_handler
    .type   weak_handler, %function
weak_handler:
    b       .

/* .type object marks data rather than code. */
    .globl  a_data_symbol
    .type   a_data_symbol, %object
    .data
a_data_symbol:
    .word   0
    .size   a_data_symbol, . - a_data_symbol

    .text

/* An alias has no label of its own: the directive IS the definition.
   .thumb_set is the ARM spelling, .set and .equ the generic ones. */
    .globl  alias_via_thumb_set
    .thumb_set alias_via_thumb_set, spelled_globl

    .globl  alias_via_set
    .set    alias_via_set, spelled_globl

    .globl  alias_via_equ
    .equ    alias_via_equ, spelled_globl

/*
 * The same directives with a NUMBER, or an expression, instead of a
 * symbol.  The assembler records these too — as absolute symbols — and
 * they are what a startup file uses for structure offsets and magic
 * values.  Found by comparing the reader against the object files the
 * builds had already produced: irq_cm4f.S defines TCB_SP_OFS and four
 * more this way, and the STM32 startup defines BootRAM.
 */
    .equ    a_numeric_constant, 56
    .equ    a_hex_constant, 0xF1E0F85F
    .set    a_computed_constant, 8 * 4 + 1
    .globl  an_exported_constant
    .equ    an_exported_constant, 1

/* A local label is a branch target, not a symbol.  GNU as keeps the
   .L prefix out of the object symbol table entirely, so nm never shows
   these and neither may the reader. */
    .globl  uses_local_labels
uses_local_labels:
.L_loop:
    subs    r3, #4
    bgt     .L_loop
.LC0:
    b       .LC0

/* Dollar and period are legal in a symbol name. */
    .globl  has$dollar
has$dollar:
    b       .

    .globl  has.period
has.period:
    b       .
