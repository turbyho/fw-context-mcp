/*
 * Calls and jumps that name their target, which the reader turns into
 * call edges.  The assembler is the authority: each call or jump to a
 * global or outside symbol leaves a relocation, and the function that
 * holds it (by the symbol table) is the caller.  Each function here has
 * `.type` and `.size`, thus the symbol table and the flow of control give
 * the same caller.  A loop to a local label leaves no relocation and is no
 * edge.
 */

    .syntax unified
    .thumb
    .text

    .global Reset_Handler
    .type   Reset_Handler, %function
Reset_Handler:
    bl      SystemInit
    b       LoopCopy
CopyData:
    adds    r3, r3, #4
LoopCopy:
    cmp     r3, r2
    bcc     CopyData
    bl      init_array
    bl      main
LoopForever:
    b       LoopForever
    .size   Reset_Handler, .-Reset_Handler

    .global HardFault_Handler
    .type   HardFault_Handler, %function
HardFault_Handler:
    tst     lr, #4
    bne.w   fault_from_task
    b.w     fault_common
    .size   HardFault_Handler, .-HardFault_Handler

    .global Dispatch
    .type   Dispatch, %function
Dispatch:
    push    {r4, lr}
    bl      Helper
    bl      .Lprivate
    pop     {r4, pc}
.Lprivate:
    bl      from_private
    bx      lr
    .size   Dispatch, .-Dispatch

    .global Helper
    .type   Helper, %function
Helper:
    cbz     r0, 1f
    bl      helper_work
1:
    bx      lr
    .size   Helper, .-Helper
