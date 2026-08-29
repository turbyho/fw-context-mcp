/*
 * Prose reads like assembly, and the preprocessor keeps it.
 *
 * `clang -E -C` is deliberate: files.content has to match the file a
 * reader opens.  So the reader skips the comments itself, and the state
 * has to cross lines because the comments do.
 *
 * NOTE: this line is the one that started it.  A real CMSIS startup
 * opens exactly like this, and a label pattern anchored on the start of
 * a line takes NOTE for a symbol definition.
 */

    .syntax unified
    .thumb
    .text

    .globl  after_a_comment_block
after_a_comment_block:
    b       .

/* A directive inside a comment is still a comment.
   .globl ghost_from_comment
   ghost_from_comment:
 */

/* Opened and closed on one line. */ .globl closed_on_one_line
closed_on_one_line:
    b       .

    .globl  trailing_comment      /* a comment after the directive */
trailing_comment:                 /* and after the label */
    b       .

/* A comment that ends mid-line, with real code after it. */
/* lead */ .globl code_after_close
code_after_close:
    b       .

/*
 * A block that spans many lines and holds text that looks like every
 * pattern the reader knows:
 *   .weak    ghost_weak
 *   .type    ghost_type, %function
 *   .word    ghost_slot
 *   ghost_label:
 *   .thumb_set ghost_alias, ghost_label
 */

    .globl  after_the_long_block
after_the_long_block:
    b       .

@ An at-sign starts a line comment on ARM.  It cannot hide a label the
@ reader would otherwise take, because a comment character is not a
@ legal first character of a symbol name.
@ ghost_line_comment:

    .globl  after_a_line_comment
after_a_line_comment:
    b       .
