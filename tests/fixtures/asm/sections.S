/*
 * Section switches around a vector table.
 *
 * gas appends to a section when the code returns to it, and keeps a
 * stack for `.pushsection`/`.popsection` and the previous section for
 * `.previous`.  Measured with objdump -r: h0..h4 are at offsets 0..16 of
 * .isr_vector, o0 and o1 at 0 and 4 of .other_table, and `in_text` is a
 * constant of .text, not a slot.
 */

    .syntax unified
    .thumb
    .section .isr_vector, "a", %progbits
    .word h0
    .text
    nop
    .section .isr_vector, "a", %progbits
    .word h1
    .section .other_table, "a", %progbits
    .word o0
    .section .isr_vector, "a", %progbits
    .word h2
    .pushsection .text
    .word in_text
    .popsection
    .word h3
    .section .other_table, "a", %progbits
    .previous
    .word h4
    .previous
    .word o1
