/*
 * Conditionals OUTSIDE a macro body, which the reader evaluates.
 *
 * The assembler takes one branch.  A reader that reads both puts the
 * symbols of the dead branch in the index: each name below that starts
 * with "dead_" must not be found.  Each condition here has an exact
 * value, thus the reader must find exactly what the assembler defines.
 *
 * The rules that the manual does not state were measured with this
 * assembler: `|` binds tighter than `+`, a comparison gives -1, `>>` is a
 * logical shift, and `.ifdef` sees only what comes before it.
 */

    .syntax unified
    .thumb
    .text

    .equ    HAVE_FPU, 1
    .set    LEVEL, HAVE_FPU + 1

    .if     HAVE_FPU != 0
    .globl  fpu_branch
fpu_branch:
    nop
    .else
    .globl  dead_no_fpu_branch
dead_no_fpu_branch:
    nop
    .endif

/* Precedence and values: each test below is true. */
    .if     (1 | 2 + 1) == 4
live_or_before_plus:
    nop
    .endif
    .if     (1 < 2) == -1
live_comparison_is_minus_one:
    nop
    .endif
    .if     (-8 >> 1) == -4
dead_arithmetic_shift:
    nop
    .endif
    .if     (0x7fffffffffffffff + 1) < 0
live_64_bit_wrap:
    nop
    .endif

/* `.elseif` takes the first true branch, and only that one. */
    .if     LEVEL == 1
dead_level_one:
    nop
    .elseif LEVEL == 2
live_level_two:
    nop
    .elseif 1
dead_later_true_branch:
    nop
    .else
dead_else_after_taken:
    nop
    .endif

/* A conditional inside a skipped branch is not evaluated, only matched. */
    .if     0
    .if     1
dead_nested:
    nop
    .endif
    .else
live_else_after_nested:
    nop
    .endif

/* `.ifdef` sees what comes before it. */
    .ifdef  fpu_branch
live_ifdef_earlier_label:
    nop
    .endif
    .ifdef  defined_later
dead_ifdef_later_label:
    nop
    .endif
    .ifndef never_defined
live_ifndef:
    nop
    .endif

/* `.set` may change a value; the later conditional sees the new one. */
    .set    LEVEL, 7
    .if     LEVEL == 7
live_reassigned:
    nop
    .endif

/* A label and a conditional in one statement. */
label_before_if: .if 1
live_after_label_and_if:
    nop
    .endif

/* String and blank tests. */
    .ifc    abc, abc
live_ifc:
    nop
    .endif
    .ifc    ABC,abc
dead_ifc_case:
    nop
    .endif
    .ifeqs  "x","x"
live_ifeqs:
    nop
    .endif
    .ifnb   x
live_ifnb:
    nop
    .endif

/* A macro defined in a skipped branch does not exist. */
    .if     0
    .macro  pick
    .globl  dead_wrong_macro
dead_wrong_macro:
    nop
    .endm
    .else
    .macro  pick
    .globl  live_right_macro
live_right_macro:
    nop
    .endm
    .endif
    pick

/* A comment after `.else` and `.endif`, in both forms. */
    .if     1
live_before_commented_else:
    nop
    .else   @ the other branch
dead_after_commented_else:
    nop
    .endif  // end
    .if     0
dead_before_slash_else:
    nop
    .else   // the other branch
live_after_slash_else:
    nop
    .endif

/* A semicolon in a string separates nothing. */
    .if     1
    .asciz  "x;.endif"
    .else
dead_after_string:
    nop
    .endif

/* A semicolon in an ARM comment separates nothing. */
    .if     0
    nop     @ note; .endif
dead_after_comment_semicolon:
    nop
    .endif

/* A character constant of a double quote opens no string. */
    .if     1
    .byte   '"; .else
dead_after_char_quote:
    nop
    .endif

/* A closing quote belongs to the character constant. */
    .if     1
    .byte   'a';.else
dead_after_closed_char:
    nop
    .endif
    .byte   'O','K'; live_after_char_pair:
    .text

/* In a skipped branch, a label before `.endif` hides the `.endif`. */
    .if     0
skipped_label: .endif
dead_after_hidden_endif:
    nop
    .endif

/* A conditional opened in a repetition body closes after `.endr`. */
    .irp    x, 1
    .if     0
    .endr
dead_after_endr:
    nop
    .endif

/* A repetition count from a constant. */
    .rept   LEVEL - 5
    nop
    .endr

/* A vector table with a conditional slot. */
    .section .isr_vector, "a", %progbits
cond_vectors:
    .word   Reset_Handler
    .if     HAVE_FPU
    .word   FPU_IRQHandler
    .else
    .word   Dead_IRQHandler
    .endif
    .word   Last_IRQHandler

    .text
    .globl  defined_later
defined_later:
    nop
