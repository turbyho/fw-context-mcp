/*
 * Several statements on one line, separated by `;`.
 *
 * The assembler reads a line that way, and a C-preprocessor macro that
 * expands to more than one statement arrives exactly like this.  Zephyr
 * writes its functions through SECTION_FUNC, which expands to
 *     .section .text.sym, "ax"; .thumb; .balign 4; sym :
 * and a pattern anchored on the start of the line finds `.section`
 * there and never the label — measured, that returned zero symbols on
 * every Zephyr assembly file.
 */

    .syntax unified
    .thumb

/* The C preprocessor DOES expand these, so what the reader sees is
   already the semicolon-separated form. */
#define GTEXT(sym)           .globl sym; .type sym, %function
#define SECTION_FUNC(s, sym) .section .text.sym, "ax"; .thumb; .balign 4; sym :
#define GDATA(sym)           .globl sym; .type sym, %object

    .text

GTEXT(from_gtext)
from_gtext:
    b       .

GTEXT(from_section_func)
SECTION_FUNC(TEXT, from_section_func)
    b       .

    .data
GDATA(from_gdata)
from_gdata:
    .word   0

    .text
/* Hand-written, without any preprocessor macro. */
    .globl  hand_written; .type hand_written, %function
hand_written:
    b       .

/* A label and its directive on one line, label first. */
    .globl  label_first
label_first: b .
