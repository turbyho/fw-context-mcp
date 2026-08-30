/*
 * A vector table: an array of addresses the hardware jumps through.
 *
 * Nothing CALLS a handler — the table is its only entry point, which is
 * why a handler looks like dead code until the table is indexed.
 */

    .syntax unified
    .thumb

/*
 * Address constants in the ORDINARY section, before any table.  A real
 * STM32 startup writes exactly these, with a comment saying the linker
 * script defines them, and its copy loop reads them.  They are NOT slots
 * of any table: matching `.word` wherever it appears took them for slots,
 * and once undefined targets began to be declared the index claimed the
 * vector table reached `_sidata`.
 *
 * The section is what tells them apart.  A linker script places the
 * table at the reset address by its section name, so the section is part
 * of the contract between the startup file and the linker.
 */
    .word   _sidata
    .word   _sdata
    .word   _ebss

    .section .vectors, "a"
    .globl  __Vectors
__Vectors:
    .long   __StackTop                  /* a linker symbol, not a handler */
    .long   Reset_Handler
    .long   NMI_Handler
    .long   HardFault_Handler
    .long   0                           /* reserved: a number, not a slot */
    .long   0
    .word   SysTick_Handler             /* .word and .long both occur */
    .word   TIM2_IRQHandler

/* The ARM assembler spelling of the same thing. */
    .globl  legacy_table
legacy_table:
    .word   Reset_Handler

    .text

    .globl  Reset_Handler
    .type   Reset_Handler, %function
Reset_Handler:
    b       .

    .globl  Default_Handler
    .type   Default_Handler, %function
Default_Handler:
    b       .

/* A handler with a body of its own: serviced by assembly. */
    .globl  NMI_Handler
NMI_Handler:
    b       .

/*
 * The weak-alias pair.  This is how CMSIS says "assembly does not
 * service this interrupt, C may override it".  The slot still points
 * somewhere, so it still gets an edge — where that edge LANDS is what
 * says whether the interrupt is serviced.
 */
    .weak   HardFault_Handler
    .thumb_set HardFault_Handler, Default_Handler

    .weak   SysTick_Handler
    .thumb_set SysTick_Handler, Default_Handler

    .weak   TIM2_IRQHandler
    .thumb_set TIM2_IRQHandler, Default_Handler
