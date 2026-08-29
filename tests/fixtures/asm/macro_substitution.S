/*
 * How an argument reaches the body: \name, and the separators.
 *
 * The manual warns twice that naive substitution goes wrong, because a
 * colon or a period can be pulled into the identifier being replaced.
 * Both warnings are exercised here.
 */

    .syntax unified
    .thumb
    .text

/* `\()` expands to nothing and separates the argument from what follows. */
    .macro  joined prefix, suffix
    .globl  \prefix\()_\suffix
\prefix\()_\suffix:
    b       .
    .endm

    joined  head, tail

/*
 * The manual's label caveat: `\l:` may be read as one identifier.  The
 * two remedies it gives are a blank before the colon, and `\()`.
 */
    .macro  label_with_blank l
    .globl  \l
\l :
    b       .
    .endm

    .macro  label_with_paren l
    .globl  \l
\l\():
    b       .
    .endm

    label_with_blank blank_before_colon
    label_with_paren paren_before_colon

/*
 * `\@` counts every macro the assembler has executed.  It is only
 * meaningful inside a body and produces a number, so a name built from
 * it is unpredictable to a reader that does not count along.  Used here
 * on data, not on a label, so the expected symbol set stays fixed.
 *
 * The manual also documents `\+`, a per-macro execution count.  It is
 * NOT here: the oracle for this corpus is binutils 2.40 (xPack ARM),
 * which rejects it with "bad expression", while the manual installed on
 * this machine is 2.47.  The documentation therefore describes a newer
 * assembler than the toolchains that build the firmware — worth knowing
 * before treating the manual as a statement about what a real build can
 * contain.  A reader may accept `\+` regardless: a file the toolchain
 * refuses never reaches an index.
 */
    .macro  counters
    .word   \@
    .endm

    .globl  uses_counters
uses_counters:
    counters
    counters
