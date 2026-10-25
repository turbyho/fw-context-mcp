"""The flow of control of one instruction (indexer/_asm_isa.py), per instruction set."""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer._asm_isa import (
    ARM,
    EXCEPTION,
    RISCV,
    STACK,
    XTENSA,
    Load,
    Move,
    RestoreLink,
    Return,
    SaveLink,
    Transfer,
    decode,
)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("bl SystemInit", Transfer(call=True, target="SystemInit")),
        ("BL main", Transfer(call=True, target="main")),
        ("blx foo", Transfer(call=True, target="foo")),
        ("blx r0", Transfer(call=True, register="r0")),
        ("BLX R3", Transfer(call=True, register="r3")),
        ("b Fault_Handler", Transfer(call=False, target="Fault_Handler")),
        ("b.w far", Transfer(call=False, target="far")),
        # `bls` is `b` + `ls`, not `bl` + `s`.
        ("bls near", Transfer(call=False, target="near", conditional=True)),
        ("blt near", Transfer(call=False, target="near", conditional=True)),
        ("bleq foo", Transfer(call=True, target="foo", conditional=True)),
        ("bne.n 1b", Transfer(call=False, target="1b", conditional=True)),
        ("cbz r1, done", Transfer(call=False, target="done", conditional=True)),
        ("b .", Transfer(call=False, target=".")),
        ("bxns lr", Return()),
        ("blxns r1", Transfer(call=True, register="r1")),
        # Divided syntax: the condition before the mode.
        ("ldmeqfd sp!, {r4, pc}", Return(conditional=True, via=STACK)),
        ("ldmfd sp!, {r0-r12, pc}^", Return(via=EXCEPTION)),
        ("ldr pc, =Reset_Handler", Transfer(call=False, target="Reset_Handler")),
        ("movs pc, lr", Return(via=EXCEPTION)),
        # `cs` is a condition, not the flag.
        ("movcs pc, lr", Return(conditional=True)),
        ("subeqs pc, lr, #4", Return(conditional=True, via=EXCEPTION)),
        # A load with writeback also writes the base register.
        ("ldr r1, [r0], #4", None),
        ("ldr r1, [r0, #4]!", None),
        ("bx r0", Transfer(call=False, register="r0")),
        ("bx lr", Return()),
        ("bxeq lr", Return(conditional=True)),
        ("pop {r4, pc}", Return(via=STACK)),
        ("popne {r4-r7, pc}", Return(conditional=True, via=STACK)),
        ("ldmia sp!, {r4, pc}", Return(via=STACK)),
        ("ldm r0, {r1, pc}", Transfer(call=False)),
        ("ldr pc, [sp], #4", Return(via=STACK)),
        ("ldr pc, [r1, r0, lsl #2]", Transfer(call=False)),
        ("mov pc, lr", Return()),
        ("mov pc, r2", Transfer(call=False, register="r2")),
        ("subs pc, lr, #4", Return(via=EXCEPTION)),
        ("sub pc, lr, #4", Transfer(call=False)),
        ("push {r4, lr}", SaveLink()),
        ("stmdb sp!, {r4-r7, lr}", SaveLink()),
        ("str lr, [sp, #-4]!", SaveLink()),
        ("push {r4, r5}", None),
        # A conditional push or pop holds the link on one path only.
        ("pusheq {lr}", SaveLink(conditional=True)),
        ("pop {r4, lr}", RestoreLink()),
        ("popne {lr}", RestoreLink(conditional=True)),
        ("ldmia sp!, {r4, lr}", RestoreLink()),
        ("ldr lr, [sp], #4", RestoreLink()),
        # Without `!` the link stays on the stack.
        ("ldm sp, {r4, lr}", None),
        ("tbb [pc, r0]", Transfer(call=False)),
        ("add pc, pc, r0", Transfer(call=False)),
        ("ldr r0, =SystemInit", Load("r0", "SystemInit")),
        ("ldr.w r1, =handler", Load("r1", "handler")),
        ("adr r2, table", Load("r2", "table")),
        ("ldreq r0, =maybe", None),
        ("mov r0, r1", Move("r0")),
        ("ldr r1, [r2]", Move("r1")),
        ("movs r0, #1", Move("r0")),
        ("str pc, [sp]", None),
        ("nop", None),
        ("svc 0", None),
    ],
)
def test_arm(statement, expected):
    assert decode(ARM, statement) == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("call soc_reset_hook", Transfer(call=True, target="soc_reset_hook")),
        ("call t0, far_function", Transfer(call=True, target="far_function")),
        ("tail __reset", Transfer(call=False, target="__reset")),
        ("j loop", Transfer(call=False, target="loop")),
        ("jal z_prep_c", Transfer(call=True, target="z_prep_c")),
        ("jal ra, __soc_save_context", Transfer(call=True, target="__soc_save_context")),
        ("jal zero, out", Transfer(call=False, target="out")),
        ("jal t0, __riscv_save_0", Transfer(call=True, target="__riscv_save_0")),
        ("jalr t1", Transfer(call=True, register="x6")),
        ("jalr ra, t1, 0", Transfer(call=True, register="x6")),
        ("jalr ra, 0(t1)", Transfer(call=True, register="x6")),
        ("jalr ra, 8(t1)", Transfer(call=True)),
        ("jr t0", Transfer(call=False, register="x5")),
        ("jr ra", Return()),
        ("ret", Return()),
        ("mret", Return(via=EXCEPTION)),
        ("j .", Transfer(call=False, target=".")),
        # Zcmp: a pop that returns, and the jumps through the table of jvt.
        ("cm.push {ra, s0}, -16", SaveLink()),
        ("cm.pop {ra, s0}, 16", RestoreLink()),
        ("cm.push {x1, x8-x9}, -16", SaveLink()),
        ("cm.popret {ra, s0}, 16", Return(via=STACK)),
        ("cm.popretz {ra}, 16", Return(via=STACK)),
        ("cm.jt 3", Transfer(call=False)),
        ("cm.jalt 40", Transfer(call=True)),
        ("beqz a0, 1f", Transfer(call=False, target="1f", conditional=True)),
        ("bne t0, t1, skip", Transfer(call=False, target="skip", conditional=True)),
        ("la t0, _isr_wrapper", Load("x5", "_isr_wrapper")),
        ("li a0, 3", Move("x10")),
        ("ecall", None),
        ("addi t0, t0, 3", None),
    ],
)
def test_riscv(statement, expected):
    assert decode(RISCV, statement) == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("call0 _xt_panic", Transfer(call=True, target="_xt_panic")),
        ("call4 panicHandler", Transfer(call=True, target="panicHandler")),
        ("callx4 a12", Transfer(call=True, register="a12")),
        ("j _xt_lowint1", Transfer(call=False, target="_xt_lowint1")),
        ("jx a0", Transfer(call=False, register="a0")),
        ("j.l far_handler, a2", Transfer(call=False, target="far_handler")),
        ("ret", Return()),
        ("retw.n", Return()),
        ("rfe", Return(via=EXCEPTION)),
        ("rfi 2", Return(via=EXCEPTION)),
        ("bnei a0, 4, _xt_handle_exc",
         Transfer(call=False, target="_xt_handle_exc", conditional=True)),
        ("bbci.l a4, 31, 1f", Transfer(call=False, target="1f", conditional=True)),
        ("loopnez a3, .Lend", Transfer(call=False, target=".Lend", conditional=True)),
        ("movi a12, longjmp", Load("a12", "longjmp")),
        ("movi a3, 0", Move("a3")),
        ("l32i a12, a12, 0", None),
        # `rfr` moves a floating point register: it is no return.
        ("rfr a2, f0", None),
    ],
)
def test_xtensa(statement, expected):
    assert decode(XTENSA, statement) == expected


def test_an_unknown_instruction_set_decodes_nothing():
    assert decode("mips", "jal foo") is None
