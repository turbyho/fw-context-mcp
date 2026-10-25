"""The routines of an assembly unit and their calls (indexer/_asm_cfg.py).

Each test is one shape of real startup code (probe s11 of the call edge
plan): which routine owns a call follows from where control can go.
"""

from __future__ import annotations

from fw_context_mcp.indexer._asm_cfg import unit_flow
from fw_context_mcp.indexer._asm_isa import ARM, RISCV, XTENSA
from fw_context_mcp.indexer._asm_layout import _entries
from fw_context_mcp.indexer._asm_macro import BarrierEffects, barrier_effects, make_barrier


def unit_flow_of(isa: str, statements: list[str]):
    located = [("a.S", i + 1, line) for i, line in enumerate(statements)]
    return unit_flow(located, isa, _entries(located))


def _flow(isa: str, lines: list[str]):
    statements = [("a.S", i + 1, line) for i, line in enumerate(lines)]
    return unit_flow(statements, isa, _entries(statements))


def _edges(flow) -> set[tuple[str, str, bool]]:
    return {("+".join(call.owners), call.target, call.call) for call in flow.calls}


def test_the_loops_of_a_startup_belong_to_its_entry():
    """STM32: `bl main` belongs to Reset_Handler, not to the loop label."""
    flow = _flow(ARM, [
        ".global Reset_Handler", "Reset_Handler:", "bl SystemInit",
        "b LoopCopy", "Copy:", "nop", "LoopCopy:", "bcc Copy", "bl main",
        "Forever:", "b Forever",
    ])

    assert _edges(flow) == {
        ("Reset_Handler", "SystemInit", True), ("Reset_Handler", "main", True),
    }
    assert flow.parents == {"Copy": "Reset_Handler", "LoopCopy": "Reset_Handler",
                            "Forever": "Reset_Handler"}


def test_entries_at_one_place_own_the_code_together():
    flow = _flow(ARM, [".global a", ".global b", "a:", ".balign 4", "b:", "bl f", "bx lr"])

    assert _edges(flow) == {("a+b", "f", True)}


def test_a_called_label_starts_a_routine():
    """ESP-IDF: `_xt_kernel_exc` has no `.type`, and `call0` reaches it."""
    flow = _flow(XTENSA, [
        ".global vec", "vec:", "call0 kexc", "rfe",
        "kexc:", "call0 panic", "rfe",
    ])

    assert _edges(flow) == {("vec", "kexc", True), ("kexc", "panic", True)}
    assert "kexc" in flow.code_roots


def test_a_trampoline_belongs_to_the_routine_that_jumps_to_it():
    """ESP-IDF: `_xt_to_alloca_exc` follows a `rfe` of another routine."""
    flow = _flow(XTENSA, [
        ".global dbg", "dbg:", "call0 dbg_work", "rfe",
        "to_alloca:", "call0 alloca_exc",
        ".global user", "user:", "bnei a0, 4, to_alloca", "ret",
    ])

    assert ("user", "alloca_exc", True) in _edges(flow)
    assert not any(owner == "dbg" and target == "alloca_exc" for owner, target, _ in _edges(flow))
    assert flow.parents["to_alloca"] == "user"


def test_falling_into_another_routine_is_an_edge():
    """Zephyr RISC-V: `_isr_wrapper` runs into `z_riscv_thread_start`."""
    flow = _flow(RISCV, [
        ".global wrapper", "wrapper:", "nop",
        ".global start", "start:", "jal ra, restore", "ret",
    ])

    assert _edges(flow) == {("wrapper", "start", False), ("start", "restore", True)}


def test_a_shared_tail_belongs_to_each_routine_that_reaches_it():
    """RTX: PendSV_Handler jumps into the code of SVC_Handler."""
    flow = _flow(ARM, [
        ".global svc", "svc:", "bl svc_work", "tail:", "bl shared", "bx lr",
        ".global pendsv", "pendsv:", "bl pend_work", "b tail",
    ])

    edges = _edges(flow)
    assert ("svc", "shared", True) in edges and ("pendsv", "shared", True) in edges
    assert not any(target == "tail" for _, target, _ in edges)
    assert flow.parents["tail"] == "svc"


def test_a_call_of_a_routine_that_never_returns_ends_the_flow():
    flow = _flow(ARM, [
        ".global main_like", "main_like:", "bl spin", "bl after", "bx lr",
        "spin:", "b spin",
    ])

    assert _edges(flow) == {("main_like", "spin", True)}


def test_a_call_of_an_outside_routine_returns():
    """The instruction set says a call returns; outside the unit nothing else is known."""
    flow = _flow(ARM, [".global f", "f:", "bl panic", "bl after", "bx lr"])

    assert ("f", "after", True) in _edges(flow)


def test_a_barrier_whose_jumps_stay_inside_passes():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(),
        text=("1:", "addi a2, a2, -1", "bnez a2, 1b", "j 9f", "nop", "9:"),
    ))
    flow = _flow(XTENSA, [".global isr", "isr:", hidden, "call0 int_exit", "rfe"])

    assert ("isr", "int_exit", True) in _edges(flow)


def test_a_barrier_that_can_leave_ends_the_flow():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=("j elsewhere",),
    ))
    flow = _flow(XTENSA, [".global isr", "isr:", hidden, "call0 int_exit", "rfe"])

    assert _edges(flow) == set()


def test_a_numeric_label_is_followed():
    flow = _flow(RISCV, [".global f", "f:", "j 1f", "nop", "1:", "call g", "ret"])

    assert _edges(flow) == {("f", "g", True)}


def test_a_call_of_a_local_subroutine_is_part_of_the_caller():
    flow = _flow(ARM, [
        ".global f", "f:", "bl .Lsub", "bx lr", ".Lsub:", "bl inner", "bx lr",
    ])

    assert _edges(flow) == {("f", "inner", True)}


def test_a_register_call_needs_the_load_right_before():
    loaded = _flow(ARM, [".global r", "r:", "ldr r0, =SystemInit", "blx r0", "bx lr"])
    written = _flow(ARM, [
        ".global r", "r:", "ldr r0, =SystemInit", "ldr r0, [r1]", "blx r0", "bx lr",
    ])

    assert _edges(loaded) == {("r", "SystemInit", True)}
    assert _edges(written) == set()


def test_a_jump_to_an_unknown_place_ends_the_flow():
    flow = _flow(ARM, [".global f", "f:", "ldr pc, [r1, r0, lsl #2]", "bl after"])

    assert _edges(flow) == set()


def test_data_ends_the_flow_and_an_option_does_not():
    data = _flow(RISCV, [".global f", "f:", ".word 0", "call after", "ret"])
    option = _flow(RISCV, [".global f", "f:", ".option push", "call after", "ret"])

    assert _edges(data) == set()
    assert _edges(option) == {("f", "after", True)}


def test_a_label_whose_address_is_taken_starts_a_routine():
    """`la t0, handler; csrw mtvec, t0` installs a handler."""
    flow = _flow(RISCV, [
        ".global start", "start:", "la t0, handler", "csrw mtvec, t0", "ret",
        "handler:", "call work", "mret",
    ])

    assert ("handler", "work", True) in _edges(flow)
    assert "handler" in flow.code_roots


def test_a_jump_to_an_outside_symbol_is_a_tail_call():
    flow = _flow(ARM, [".global f", "f:", "ldr r0, =_start", "bx r0"])

    assert _edges(flow) == {("f", "_start", False)}


def test_a_name_that_a_barrier_can_define_gives_no_edge():
    hidden = make_barrier(BarrierEffects(False, True, None, frozenset(), text=("nop",)))
    flow = _flow(ARM, [".global f", "f:", hidden, "b maybe_local"])

    assert _edges(flow) == set()


def test_a_jump_to_its_own_place_never_returns():
    flow = _flow(ARM, [
        ".global caller", "caller:", "bl spin", "bl after", "bx lr",
        ".global spin", "spin:", "b .",
    ])

    assert _edges(flow) == {("caller", "spin", True)}


def test_the_return_of_a_subroutine_does_not_return_from_the_routine():
    """`bl .Lsub` comes back to the call: `f` itself never returns."""
    flow = _flow(ARM, [
        ".global f", "f:", "bl .Lsub", "b .", ".Lsub:", "bx lr",
        ".global g", "g:", "bl f", "bl after", "bx lr",
    ])

    assert _edges(flow) == {("g", "f", True)}


def test_a_barrier_that_loops_for_ever_ends_the_flow():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=("1:", "j 1b"),
    ))
    flow = _flow(XTENSA, [".global isr", "isr:", hidden, "call0 int_exit", "rfe"])

    assert _edges(flow) == set()


def test_a_barrier_with_a_conditional_exit_passes():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=("beqz a2, elsewhere", "nop"),
    ))
    flow = _flow(XTENSA, [".global isr", "isr:", hidden, "call0 int_exit", "rfe"])

    assert ("isr", "int_exit", True) in _edges(flow)


def test_a_numeric_label_that_a_barrier_can_define_is_not_known():
    """`1f` can name the `1:` of the refused construct, which is nearer."""
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=("1:", "nop"),
    ))
    flow = _flow(RISCV, [".global f", "f:", "j 1f", hidden, "nop", "1:", "call g", "ret"])

    assert _edges(flow) == set()


def test_a_barrier_with_other_numeric_labels_keeps_the_target():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=("2:", "nop"),
    ))
    flow = _flow(RISCV, [".global f", "f:", "j 1f", hidden, "nop", "1:", "call g", "ret"])

    assert _edges(flow) == {("f", "g", True)}


def test_code_does_not_fall_into_another_subsection():
    """gas puts subsection 1 after all of subsection 0."""
    numbered = _flow(RISCV, [".global f", "f:", "nop", ".text 1", ".global g", "g:", "ret"])
    directive = _flow(RISCV, [
        ".global f", "f:", "nop", ".subsection 1", ".global g", "g:", "ret",
    ])
    zero = _flow(RISCV, [".global f", "f:", "nop", ".text 0", ".global g", "g:", "ret"])

    assert _edges(numbered) == set()
    assert _edges(directive) == set()
    assert _edges(zero) == {("f", "g", False)}


def test_a_barrier_of_data_only_holds_no_code():
    hidden = make_barrier(BarrierEffects(
        False, True, frozenset(), frozenset(), text=(".word 1",),
    ))
    flow = _flow(ARM, [".global table", "table:", hidden])

    assert "table" in flow.roots
    assert "table" not in flow.code_roots


def test_falling_into_data_is_no_edge():
    flow = _flow(RISCV, [".global f", "f:", "nop", ".global table", "table:", ".word 0"])

    assert _edges(flow) == set()


def test_a_fall_is_marked():
    flow = _flow(RISCV, [".global a", "a:", "nop", ".global b", "b:", "ret"])

    assert [call.fall for call in flow.calls] == [True]


def test_a_return_inside_a_barrier_returns_from_the_routine():
    """The routine can return in the refused construct: its callers go on."""
    for text in (("bx lr",), None, ("b ext_func",), ("bxeq lr",)):
        hidden = make_barrier(BarrierEffects(False, True, frozenset(), frozenset(), text=text))
        flow = _flow(ARM, [
            ".global helper", "helper:", hidden, "b .",
            ".global Reset", "Reset:", "bl helper", "bl main", "b .",
        ])

        assert ("Reset", "main", True) in _edges(flow), text


def test_a_barrier_that_can_switch_the_section_cuts_each_section():
    """Its code can be in `.text` between `a` and `b`."""
    for text in (("b far",), None):
        hidden = make_barrier(BarrierEffects(True, True, frozenset(), frozenset(), text=text))
        flow = _flow(RISCV, [
            ".text", ".global a", "a:", "nop", ".data", ".word 1", hidden,
            ".text", ".global b", "b:", "ret",
        ])

        assert _edges(flow) == set(), text


def test_a_barrier_that_writes_no_byte_keeps_the_labels_at_one_place():
    for text in ((), ("lbl:",), (".balign 4",)):
        hidden = make_barrier(BarrierEffects(False, False, frozenset(), frozenset(), text=text))
        flow = _flow(RISCV, [
            ".global a", "a:", "nop", ".global b", "b:", hidden, "call work", "ret",
        ])

        assert _edges(flow) == {("a", "b", False), ("b", "work", True)}, text
        assert "b" in flow.code_roots


def test_a_subroutine_that_never_comes_back_ends_the_flow():
    flow = _flow(ARM, [
        ".global Reset", "Reset:", "bl .Lhang", ".global Next", "Next:", "bx lr",
        ".Lhang:", "b .",
    ])

    assert _edges(flow) == set()


def test_the_far_branch_of_thumb1_returns_from_the_routine():
    """`bl .Lfar` is a jump: the `pop {pc}` there reads the link of `f`."""
    flow = _flow(ARM, [
        ".global f", "f:", "push {r4, lr}", "bl .Lfar", ".ltorg",
        ".Lfar:", "bl work", "pop {r4, pc}",
        ".global g", "g:", "bl f", "bl after", "bx lr",
    ])

    assert _edges(flow) == {("f", "work", True), ("g", "f", True), ("g", "after", True)}


def test_a_subroutine_that_pushes_its_own_link_comes_back():
    flow = _flow(ARM, [
        ".global f", "f:", "bl .Lsub", "bl after", "b .",
        ".Lsub:", "push {lr}", "bl inner", "pop {pc}",
        ".global g", "g:", "bl f", "bl later", "bx lr",
    ])

    assert _edges(flow) == {("f", "inner", True), ("f", "after", True), ("g", "f", True)}


def test_the_end_of_an_exception_in_a_subroutine_returns_from_the_routine():
    flow = _flow(ARM, [
        ".global isr", "isr:", "bl .Lexit", "b .", ".Lexit:", "subs pc, lr, #4",
        ".global g", "g:", "bl isr", "bl after", "bx lr",
    ])

    assert ("g", "after", True) in _edges(flow)


def test_a_numeric_label_that_a_macro_argument_names_is_not_known():
    hidden = make_barrier(BarrierEffects(
        False, True, None, frozenset(), text=("1\\@:", "nop"),
    ))
    flow = _flow(RISCV, [
        ".global f", "f:", "j 1f", hidden, "nop", "1:", "call g", "ret", ".global g", "g:", "ret",
    ])

    assert ("f", "g", True) not in _edges(flow)


def test_a_label_of_a_substitution_that_cannot_be_numeric_keeps_the_target():
    """`.Lskip\\@:` becomes `.Lskip0`: it is no `1:`."""
    hidden = make_barrier(BarrierEffects(
        False, True, None, frozenset(), text=(".Lskip\\@:", "nop"),
    ))
    flow = _flow(RISCV, [
        ".global f", "f:", "j 1f", hidden, "nop", "1:", "call g", "ret", ".global g", "g:", "ret",
    ])

    assert ("f", "g", True) in _edges(flow)


def _refused(text: str) -> list[str]:
    from fw_context_mcp.indexer._asm_macro import Report, expand_stream

    return [s for _, _, s in expand_stream(
        (("a.S", i + 1, line) for i, line in enumerate(text.splitlines())), Report(),
    )]


def test_a_conditional_that_is_not_known_can_be_skipped():
    """gas can take no branch: the code after it is reached."""
    flow = unit_flow_of(ARM, _refused(
        ".global Reset\nReset:\n  .if UNKNOWN\n  b fast_path\n  .else\n  bl slow_init\n"
        "  .endif\n  bl main\n  b .\n"
    ))

    assert ("Reset", "main", True) in _edges(flow)


def test_the_branches_of_a_refused_conditional_are_alternatives():
    """Both branches return: no path reaches the code after the conditional."""
    flow = unit_flow_of(RISCV, _refused(
        ".global f\nf:\n  .if UNKNOWN\n  ret\n  .else\n  ret\n  .endif\n  call after\n  ret\n"
    ))

    assert _edges(flow) == set()


def test_a_refused_repetition_can_run_no_time():
    flow = unit_flow_of(RISCV, _refused(
        ".global _start\n_start:\n  .rept UNKNOWN\n  ret\n  .endr\n  call main\n  ret\n"
    ))

    assert ("_start", "main", True) in _edges(flow)


def test_a_pop_of_the_link_ends_the_saved_link():
    """`.LS2` pops the link of C: C does not go on after `bl .LS1`."""
    flow = _flow(ARM, [
        ".global C", "C:", "push {lr}", "bl .LS1", "bl afterS1", "pop {pc}",
        ".LS1:", "push {lr}", "pop {lr}", "bl .LS2", "bl afterS2", "bx lr",
        ".LS2:", "pop {pc}",
    ])

    assert ("C", "afterS1", True) not in _edges(flow)


def test_a_conditional_push_walks_both_paths():
    """With the push, `pop {pc}` comes back; without it, it returns from f."""
    flow = _flow(ARM, [
        ".global f", "f:", "bl .Lsub", "bl after", "b .",
        ".Lsub:", "pusheq {lr}", "pop {pc}",
        ".global g", "g:", "bl f", "bl later", "bx lr",
    ])

    assert {("f", "after", True), ("g", "later", True)} <= _edges(flow)


def test_a_conditional_with_a_known_value_takes_its_branch():
    """`EXIT 1` ends with `ret`: no fall into the next routine."""
    flow = unit_flow_of(RISCV, _refused(
        ".macro EXIT save\n  .if \\save\n  ret\n  .endif\n.endm\n"
        ".global a\na:\n  call g\n  EXIT 1\n.global b\nb:\n  call h\n  ret\n"
    ))

    assert ("a", "b", False) not in _edges(flow)
    assert ("a", "g", True) in _edges(flow)


def test_a_conditional_with_a_known_value_skips_its_branch():
    flow = unit_flow_of(RISCV, _refused(
        ".macro EXIT save\n  .if \\save\n  ret\n  .endif\n.endm\n"
        ".global a\na:\n  call g\n  EXIT 0\n.global b\nb:\n  call h\n  ret\n"
    ))

    assert ("a", "b", False) in _edges(flow)


def test_a_pop_into_a_loaded_register_forgets_the_load():
    flow = _flow(ARM, [
        ".global f", "f:", "push {r3, lr}", "ldr r3, =foo", "pop {r3, lr}", "bx r3",
    ])

    assert _edges(flow) == set()


def test_exitm_ends_the_macro_text():
    """gas can leave the macro before `bx lr`: the code after it is reached."""
    flow = unit_flow_of(ARM, _refused(
        ".macro m a\n  .if \\a\n  .exitm\n  .endif\n  bx lr\n  .endm\n"
        ".global f\nf:\n  m UNKNOWN\n  bl after\n  b .\n"
    ))

    assert ("f", "after", True) in _edges(flow)


def test_a_repetition_inside_a_refused_text_can_run_no_time():
    flow = unit_flow_of(ARM, _refused(
        ".macro m\n  .if UNKNOWN\n  .endif\n  .rept COUNT\n  bx lr\n  .endr\n.endm\n"
        ".global f\nf:\n  m\n  bl after\n  b .\n"
    ))

    assert ("f", "after", True) in _edges(flow)


def test_a_numeric_label_of_a_sibling_branch_is_not_the_target():
    """In the `.if` branch, `1f` names the `1:` after the conditional, which
    runs on; the `1:` of the `.else` branch (a return) is not assembled
    with it."""
    hidden = make_barrier(BarrierEffects(False, True, frozenset(), frozenset(), text=(
        ".if ?", "j 1f", ".else", "1:", "ret", ".endif", "1:", "nop",
    )))
    flow = _flow(RISCV, [".global f", "f:", hidden, "call after", "ret"])

    assert _edges(flow) == {("f", "after", True)}


def test_a_local_label_of_the_counter_keeps_the_names_and_the_text():
    """`cbz r0, .Lskip\\@` names the label of its own expansion."""
    effects = barrier_effects(_refused(
        ".macro m\n  .if UNKNOWN\n  .endif\n  cbz r0, .Lskip\\@\n  bx lr\n.Lskip\\@:\n.endm\n"
        ".global f\nf:\n  m\n  bl after\n  b .\n"
    )[2])

    assert effects is not None and effects.names is not None
    assert effects.text is not None and "cbz r0, .Lskip0" in effects.text


def test_labels_of_an_expansion_with_an_unknown_count_name_each_other():
    """The count after `guarded` is not known: `skip\\@2` is a mark, and the
    jump still reaches its label.  No edge names the mark."""
    flow = unit_flow_of(ARM, _refused(
        ".macro inner\n  nop\n.endm\n.macro guarded\n  .if UNKNOWN\n  inner\n  .endif\n.endm\n"
        ".macro over2\n  b skip\\@\n  .word 0\nskip\\@:\n  bl after2\n.endm\n"
        ".global f\nf:\n  guarded\n  over2\n  bx lr\n"
    ))

    assert ("f", "after2", True) in _edges(flow)
    assert not any("\\@" in target for _, target, _ in _edges(flow))


def test_an_else_with_no_conditional_in_the_text_does_not_crash():
    flow = unit_flow_of(ARM, _refused(
        ".macro ELSE_\n  .else\n.endm\n"
        ".global f\nf:\n  bl a\n  .if 1\n  bl b\n  ELSE_\n  bl c\n  .endif\n  bx lr\n"
    ))

    assert ("f", "a", True) in _edges(flow)


def test_exitm_in_a_repetition_goes_to_the_end_of_the_repetition():
    flow = unit_flow_of(ARM, _refused(
        ".macro m\n  .if UNKNOWN\n  .endif\n  .rept 2\n  nop\n  .exitm\n  .endr\n  bx lr\n.endm\n"
        ".global f\nf:\n  m\n  bl after\n  b .\n"
    ))

    assert ("f", "after", True) not in _edges(flow)


def test_exitm_can_skip_a_label_of_an_inner_macro_text():
    """`1f` can then name a `1:` after the text."""
    from fw_context_mcp.indexer._asm_cfg import _barrier_flow

    effects = BarrierEffects(False, True, frozenset(), frozenset(), text=(
        "j 1f", ".exitm", ".macro ?", "1:", "nop", ".endm",
    ))

    assert _barrier_flow(effects, RISCV) == (True, True)
