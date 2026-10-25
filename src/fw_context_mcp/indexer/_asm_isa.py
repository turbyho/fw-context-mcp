"""Decode the flow of control of one instruction of assembly.

The reader of assembly builds the flow of control of a unit (`_asm_cfg`):
which code a routine reaches, and which calls it makes.  This module says,
for one statement of one instruction set, what it does to that flow:

* a ``Transfer``: a call (the code continues after it) or a jump (it does
  not, unless it is conditional), to a named symbol, to the address in a
  register, or to a place that is not known (a table branch, a load of the
  program counter);
* a ``Return``: the routine ends here (unless it is conditional), through
  the link register, through a link that it reads from the stack, or as
  the end of an exception;
* a ``SaveLink`` or a ``RestoreLink``: it pushes the link register on the
  stack, or pops it into the link register;
* a ``Load``: it puts the address of a named symbol in a register;
* a ``Move``: it writes another value to one register, and does nothing
  else;
* None: any other instruction.  It continues to the next one, and the
  caller must assume that it can write any register.

The mnemonics come from the manuals of the instruction sets (Arm ARM,
RISC-V unprivileged and privileged ISA with the pseudo-instructions of the
assembler manual, Xtensa ISA), and the shapes were measured on the startup
code of the builds that fw-context indexes (probes s7 and s11 of the call
edge plan).  A form that is not listed is None: it continues.  That is
right for every instruction that is no transfer.  A transfer of an
extension that is not listed here would read as one that continues, and
the flow would run on where the code does not: add it to the tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ARM = "arm"
RISCV = "riscv"
XTENSA = "xtensa"


@dataclass(frozen=True)
class Transfer:
    """A call or a jump.

    Attributes:
        call: True for a call (the code continues after it), False for a
            jump.
        target: The symbol the instruction names, or None.
        register: The register that holds the target, or None.  With no
            target and no register, the target is not known.
        conditional: The code also continues after it.
    """

    call: bool
    target: str | None = None
    register: str | None = None
    conditional: bool = False


# How a return finds the place to go back to.  The difference matters in
# the code of a call to a label with no symbol (`bl .Lsub`): a return
# through the link register goes back to that call, and a return through
# the stack goes to the link that the stack holds, which is the link of
# the routine when the subroutine did not push its own.
LINK = "link"
STACK = "stack"
EXCEPTION = "exception"


@dataclass(frozen=True)
class Return:
    """The end of a routine: control goes back to its caller.

    Attributes:
        conditional: The code also continues after it.
        via: `LINK` (`bx lr`, `ret`), `STACK` (`pop {pc}`, `cm.popret`) or
            `EXCEPTION` (`rfe`, `mret`, `subs pc, lr, #4`): the end of a
            handler, whatever code holds it.
    """

    conditional: bool = False
    via: str = LINK


@dataclass(frozen=True)
class SaveLink:
    """The link register goes on the stack (`push {r4, lr}`, `cm.push {ra}`).

    A return through the stack after it reads this link back.
    """

    conditional: bool = False


@dataclass(frozen=True)
class RestoreLink:
    """The link register comes back from the stack (`pop {r4, lr}`,
    `ldr lr, [sp], #4`, `cm.pop {ra}`): the stack no longer holds it."""

    conditional: bool = False


@dataclass(frozen=True)
class Load:
    """``register`` gets the address of ``target``."""

    register: str
    target: str


@dataclass(frozen=True)
class Move:
    """``register`` gets another value, and no other register changes."""

    register: str


Decoded = Transfer | Return | SaveLink | RestoreLink | Load | Move | None

# A bare symbol operand, or a reference to a numeric local label (`1f`,
# `2b`), which the flow resolves by the order of the text.  `.` (the
# location counter) and an expression are not.
# The text of a refused macro can hold the expansion counter in a name
# (`.Lskip\@`): it names a label of the same text.
_SYMBOL = re.compile(
    r"^[A-Za-z_$](?:[\w.$]|\\@)*$|^\.[A-Za-z_$](?:[\w.$]|\\@)*$|^\d+[fb]$"
)
_INSTRUCTION = re.compile(r"^\s*([A-Za-z][\w.]*)\s*(.*?)\s*$")


def _operands(text: str) -> list[str]:
    """Split operands on commas outside brackets and braces."""
    found: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            found.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current or found:
        found.append("".join(current).strip())
    return found


def _symbol(text: str) -> str | None:
    """*text* when it is one named symbol, else None.

    `.` stays `.`: a jump to its own place (`b .`, `j .`) loops for ever.
    """
    if text.strip() == ".":
        return "."
    return text if _SYMBOL.match(text) else None


# ── ARM and Thumb ───────────────────────────────────────────────────────

_ARM_CONDITIONS = ("eq", "ne", "cs", "hs", "cc", "lo", "mi", "pl", "vs", "vc",
                   "hi", "ls", "ge", "lt", "gt", "le", "al")
_ARM_CONDITION_ALTERNATION = "|".join(_ARM_CONDITIONS)
# `bls` is `b` with the condition `ls`, not `bl` with `s`: the pattern must
# match the whole mnemonic, thus `bl` + `s` fails and `b` + `ls` matches.
# `bxns` and `blxns` are the non-secure forms of Armv8-M.
_ARM_BRANCH = re.compile(
    rf"^(bl|blxns|blx|bxns|bxj|bx|b)({_ARM_CONDITION_ALTERNATION})?(\.w|\.n)?$", re.IGNORECASE
)
_ARM_COMPARE_BRANCH = re.compile(r"^(cbz|cbnz)(\.n)?$", re.IGNORECASE)
_ARM_TABLE_BRANCH = re.compile(r"^(tbb|tbh)(\.w)?$", re.IGNORECASE)
_ARM_ALIASES = {
    "sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12",
    "sp": "r13", "lr": "r14", "pc": "r15",
}
_ARM_REGISTER = re.compile(r"^r(?:[0-9]|1[0-5])$")
_ARM_PC = "r15"
_ARM_LR = "r14"
_ARM_SP = "r13"
# Instructions that write their first operand and nothing else (unless
# their address writes back, see `_decode_arm`).
_ARM_MOVES = frozenset({
    "mov", "movs", "mov.w", "movs.w", "mvn", "mvns", "movw", "movt",
    "ldr", "ldr.w", "adr", "adr.w",
})
# The addressing modes of a load of several registers.
_ARM_LDM_MODES = ("ia", "ib", "da", "db", "fd", "fa", "ed", "ea")
# The data instructions that can write pc (with the `s` flag).
_ARM_DATA_ROOTS = (
    "mov", "mvn", "add", "adc", "sub", "sbc", "rsb", "rsc", "and", "orr", "eor", "bic",
)
# Instructions whose first operand is read, not written.
_ARM_NO_DESTINATION = re.compile(r"^(str|stm|push|cmp|cmn|tst|teq|st)", re.IGNORECASE)


def _arm_register(text: str) -> str | None:
    name = text.strip().lower().rstrip("!")
    name = _ARM_ALIASES.get(name, name)
    return name if _ARM_REGISTER.match(name) else None


def _arm_form(mnemonic: str, root: str, flag: bool, modes: tuple[str, ...]) -> bool | None:
    """Whether *mnemonic* is *root* with a condition: None when it is not *root*.

    Both syntaxes count: unified (`subseq`, `ldmfdeq`: flag, mode, then the
    condition) and divided (`subeqs`, `ldmeqfd`: the condition first).
    """
    lowered = mnemonic.lower()
    for width in (".w", ".n"):
        lowered = lowered.removesuffix(width)
    if not lowered.startswith(root):
        return None
    rest = lowered[len(root):]
    condition = f"(?P<c>{_ARM_CONDITION_ALTERNATION})?"
    letter = "s?" if flag else ""
    mode = f"(?:{'|'.join(modes)})?" if modes else ""
    found = re.fullmatch(f"{letter}{mode}{condition}", rest) or re.fullmatch(
        f"{condition}{letter}{mode}", rest
    )
    if found is None:
        return None
    return found.group("c") not in (None, "al")


def _arm_flagged(mnemonic: str, root: str) -> bool:
    """Whether *mnemonic* (a form of *root*) has the `s` flag.

    With pc as the destination the flag also copies SPSR to CPSR: that is
    the return from an exception (`subs pc, lr, #4`, `movs pc, lr`).
    """
    rest = mnemonic.lower().removesuffix(".w").removesuffix(".n")[len(root):]
    condition = f"(?:{_ARM_CONDITION_ALTERNATION})"
    return re.fullmatch(f"s{condition}?|{condition}s", rest) is not None


def _arm_saves_link(mnemonic: str, operands: list[str]) -> SaveLink | None:
    """The push of lr: `push {.., lr}`, `stmdb sp!, {.., lr}`, `str lr, [sp, #-4]!`."""
    if not operands:
        return None
    conditional = _arm_form(mnemonic, "push", False, ())
    if conditional is not None:
        return SaveLink(conditional) if _ARM_LR in _arm_register_list(operands[-1]) else None
    for root in ("stmdb", "stmfd"):
        conditional = _arm_form(mnemonic, root, False, ())
        if conditional is not None and len(operands) == 2:
            pushes = operands[0].replace(" ", "").lower() == "sp!" and (
                _ARM_LR in _arm_register_list(operands[1])
            )
            return SaveLink(conditional) if pushes else None
    conditional = _arm_form(mnemonic, "str", False, ())
    if conditional is not None and len(operands) == 2:
        address = operands[1].replace(" ", "").lower()
        pushes = _arm_register(operands[0]) == _ARM_LR and (
            address.startswith("[sp,#-") and address.endswith("]!")
        )
        return SaveLink(conditional) if pushes else None
    return None


def _arm_restores_link(mnemonic: str, operands: list[str]) -> RestoreLink | None:
    """The pop of lr: `ldr lr, [sp], #4` (`pop {.., lr}` is in `_decode_arm_pc`)."""
    conditional = _arm_form(mnemonic, "ldr", False, ())
    if conditional is None or len(operands) != 3 or _arm_register(operands[0]) != _ARM_LR:
        return None
    return RestoreLink(conditional) if operands[1].replace(" ", "").lower() == "[sp]" else None


def _arm_register_list(text: str) -> set[str]:
    """The registers of `{r4-r7, pc}` (also with `^`)."""
    found: set[str] = set()
    for part in text.strip().rstrip("^").strip().strip("{}").split(","):
        bounds = part.split("-")
        if len(bounds) == 2:
            first, last = _arm_register(bounds[0]), _arm_register(bounds[1])
            if first and last:
                found.update(f"r{n}" for n in range(int(first[1:]), int(last[1:]) + 1))
            continue
        register = _arm_register(part)
        if register:
            found.add(register)
    return found


def _decode_arm_pc(mnemonic: str, operands: list[str]) -> Decoded:
    """Decode an instruction that writes pc, or give None when it is no such one."""
    for root, modes in (("pop", ()), ("ldm", _ARM_LDM_MODES)):
        conditional = _arm_form(mnemonic, root, False, modes)
        if conditional is None or not operands:
            continue
        listed = _arm_register_list(operands[-1])
        from_stack = root == "pop" or _arm_register(operands[0]) == _ARM_SP
        if _ARM_PC not in listed:
            # A pop moves sp: `ldm sp, {lr}` without `!` reads a copy and
            # leaves the link on the stack.
            pops = root == "pop" or operands[0].replace(" ", "").lower() == "sp!"
            return RestoreLink(conditional) if pops and _ARM_LR in listed else None
        if operands[-1].rstrip().endswith("^"):
            # `ldmfd sp!, {r0-r12, pc}^` also copies SPSR to CPSR.
            return Return(conditional, EXCEPTION)
        if from_stack:
            return Return(conditional, STACK)
        return Transfer(call=False, conditional=conditional)
    if not operands or _arm_register(operands[0]) != _ARM_PC:
        return None
    conditional = _arm_form(mnemonic, "ldr", False, ())
    if conditional is not None:
        if len(operands) == 2 and operands[1].startswith("="):
            return Transfer(call=False, target=_symbol(operands[1][1:].strip()),
                            conditional=conditional)
        if len(operands) == 3 and operands[1].replace(" ", "") == "[sp]":
            return Return(conditional, STACK)
        return Transfer(call=False, conditional=conditional)
    for root in _ARM_DATA_ROOTS:
        conditional = _arm_form(mnemonic, root, True, ())
        if conditional is None:
            continue
        source = _arm_register(operands[1]) if len(operands) >= 2 else None
        flagged = _arm_flagged(mnemonic, root)
        if root == "mov" and len(operands) == 2 and source == _ARM_LR:
            return Return(conditional, EXCEPTION if flagged else LINK)
        if root == "mov" and len(operands) == 2 and source is not None and not flagged:
            return Transfer(call=False, register=source, conditional=conditional)
        if root == "sub" and len(operands) == 3 and source == _ARM_LR and flagged:
            # `subs pc, lr, #4`: the return from an exception.
            return Return(conditional, EXCEPTION)
        return Transfer(call=False, conditional=conditional)
    if _ARM_NO_DESTINATION.match(mnemonic):
        return None
    # Another write of pc: a jump to a place that is not known.  Its
    # condition is not parsed, thus it counts as one that can also continue.
    return Transfer(call=False, conditional=True)


def _decode_arm(mnemonic: str, operands: list[str]) -> Decoded:
    lowered = mnemonic.lower()
    branch = _ARM_BRANCH.match(lowered)
    if branch is not None and len(operands) == 1:
        base = branch.group(1)
        conditional = branch.group(2) not in (None, "al")
        if base in ("bx", "bxj", "bxns"):
            register = _arm_register(operands[0])
            if register == _ARM_LR:
                return Return(conditional)
            return Transfer(call=False, register=register, conditional=conditional)
        if base in ("blx", "blxns"):
            register = _arm_register(operands[0])
            if register is not None:
                return Transfer(call=True, register=register, conditional=conditional)
        target = _symbol(operands[0])
        return Transfer(call=base in ("bl", "blx"), target=target, conditional=conditional)
    if _ARM_COMPARE_BRANCH.match(lowered) and len(operands) == 2:
        return Transfer(call=False, target=_symbol(operands[1]), conditional=True)
    if _ARM_TABLE_BRANCH.match(lowered):
        # The target comes from a table in memory.
        return Transfer(call=False)
    if lowered in ("eret", "rfe", "rfeia", "rfedb", "rfefd", "rfeea"):
        return Return(via=EXCEPTION)
    link = _arm_saves_link(lowered, operands) or _arm_restores_link(lowered, operands)
    if link is not None:
        return link
    if _arm_form(lowered, "pop", False, ()) is not None or _arm_form(
        lowered, "ldm", False, _ARM_LDM_MODES
    ) is not None or (operands and _arm_register(operands[0]) == _ARM_PC):
        return _decode_arm_pc(lowered, operands)
    if lowered in ("ldr", "ldr.w") and len(operands) == 2 and operands[1].startswith("="):
        register = _arm_register(operands[0])
        target = _symbol(operands[1][1:].strip())
        if register is not None and target is not None:
            return Load(register, target)
    if lowered in ("adr", "adr.w") and len(operands) == 2:
        register = _arm_register(operands[0])
        target = _symbol(operands[1])
        if register is not None and target is not None:
            return Load(register, target)
    if lowered in _ARM_MOVES and operands:
        if lowered.startswith("ldr") and (
            len(operands) > 2 or any("!" in operand for operand in operands)
        ):
            # `ldr r1, [r0], #4` and `ldr r1, [r0, #4]!` write r0 too.
            return None
        register = _arm_register(operands[0])
        if register is not None:
            return Move(register)
    return None


# ── RISC-V ──────────────────────────────────────────────────────────────

_RISCV_ABI = {
    "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4, "t0": 5, "t1": 6, "t2": 7,
    "s0": 8, "fp": 8, "s1": 9, "a0": 10, "a1": 11, "a2": 12, "a3": 13,
    "a4": 14, "a5": 15, "a6": 16, "a7": 17, "s2": 18, "s3": 19, "s4": 20,
    "s5": 21, "s6": 22, "s7": 23, "s8": 24, "s9": 25, "s10": 26, "s11": 27,
    "t3": 28, "t4": 29, "t5": 30, "t6": 31,
}
_RISCV_X = re.compile(r"^x([0-9]|[12][0-9]|3[01])$")
_RISCV_ZERO = "x0"
_RISCV_RA = "x1"
_RISCV_BRANCHES = frozenset({
    "beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez", "blez",
    "bgez", "bltz", "bgtz", "bgt", "ble", "bgtu", "bleu", "c.beqz", "c.bnez",
})
# How each return goes back: `cm.popret` and `cm.popretz` (Zcmp) read ra
# from the stack, which `cm.push` wrote.
_RISCV_RETURNS = {
    "ret": LINK, "mret": EXCEPTION, "sret": EXCEPTION, "uret": EXCEPTION, "dret": EXCEPTION,
    "cm.popret": STACK, "cm.popretz": STACK,
}
_RISCV_MOVES = frozenset({"li", "mv", "lui", "la", "lla", "auipc", "c.li", "c.mv", "c.lui"})
_OFFSET_REGISTER = re.compile(r"^(-?\w*)\((\w+)\)$")


def _riscv_register(text: str) -> str | None:
    name = text.strip().lower()
    if name in _RISCV_ABI:
        return f"x{_RISCV_ABI[name]}"
    return name if _RISCV_X.match(name) else None


def _zero_offset(text: str) -> bool:
    return text.strip() in ("0", "")


def _decode_riscv(mnemonic: str, operands: list[str]) -> Decoded:
    lowered = mnemonic.lower()
    if lowered in _RISCV_RETURNS:
        return Return(via=_RISCV_RETURNS[lowered])
    if lowered in ("cm.push", "cm.pop") and operands and _RISCV_RA in (
        _riscv_register(part.split("-")[0])
        for part in operands[0].strip("{} ").split(",")
    ):
        # `{ra, s0-s1}` or `{x1, x8-x9}`.
        return SaveLink() if lowered == "cm.push" else RestoreLink()
    if lowered in ("cm.jt", "cm.jalt"):
        # The jump table of Zcmt: the target comes from a table in memory.
        return Transfer(call=lowered == "cm.jalt")
    if lowered == "call" and len(operands) in (1, 2):
        return Transfer(call=True, target=_symbol(operands[-1]))
    if lowered in ("tail", "j", "c.j") and len(operands) == 1:
        return Transfer(call=False, target=_symbol(operands[0]))
    if lowered == "jump" and len(operands) == 2:
        # `jump label, temp`: a far jump that uses temp.
        return Transfer(call=False, target=_symbol(operands[0]))
    if lowered in ("jal", "c.jal") and len(operands) in (1, 2):
        link = _RISCV_RA if len(operands) == 1 else _riscv_register(operands[0])
        return Transfer(call=link != _RISCV_ZERO, target=_symbol(operands[-1]))
    if lowered in ("jalr", "jr", "c.jr", "c.jalr"):
        return _decode_riscv_register_jump(lowered, operands)
    if lowered in _RISCV_BRANCHES and operands:
        return Transfer(call=False, target=_symbol(operands[-1]), conditional=True)
    if lowered in ("la", "lla") and len(operands) == 2:
        register = _riscv_register(operands[0])
        target = _symbol(operands[1])
        if register is not None and target is not None:
            return Load(register, target)
    if lowered in _RISCV_MOVES and operands:
        register = _riscv_register(operands[0])
        if register is not None:
            return Move(register)
    return None


def _decode_riscv_register_jump(mnemonic: str, operands: list[str]) -> Decoded:
    """`jalr rs`, `jalr rd, rs[, imm]`, `jalr rd, imm(rs)`, `jr rs`, `jr imm(rs)`.

    Only an offset of zero leaves the target the address in the register;
    another offset gives a target that is not known.  A jump through `ra`
    with no link is a return.
    """
    if mnemonic in ("jr", "c.jr"):
        link: str | None = _RISCV_ZERO
        rest = operands
    elif len(operands) == 1:
        link, rest = _RISCV_RA, operands
    else:
        link, rest = _riscv_register(operands[0]), operands[1:]
    if link is None or not rest or len(rest) > 2:
        return Transfer(call=link not in (None, _RISCV_ZERO))
    call = link != _RISCV_ZERO
    based = _OFFSET_REGISTER.match(rest[0].strip())
    if based is not None:
        register = _riscv_register(based.group(2))
        offset_zero = len(rest) == 1 and _zero_offset(based.group(1))
    else:
        register = _riscv_register(rest[0])
        offset_zero = len(rest) == 1 or _zero_offset(rest[1])
    if register is None or not offset_zero:
        return Transfer(call=call)
    if not call and register == _RISCV_RA:
        return Return()
    return Transfer(call=call, register=register)


# ── Xtensa ──────────────────────────────────────────────────────────────

_XTENSA_REGISTER = re.compile(r"^a([0-9]|1[0-5])$")
_XTENSA_CALL = re.compile(r"^call(0|4|8|12)$")
_XTENSA_CALLX = re.compile(r"^callx(0|4|8|12)$")
_XTENSA_BRANCHES = frozenset({
    "beq", "bne", "bge", "blt", "bgeu", "bltu", "beqi", "bnei", "bgei",
    "blti", "bgeui", "bltui", "bany", "bnone", "ball", "bnall", "bbc",
    "bbs", "bbci", "bbsi", "beqz", "bnez", "bgez", "bltz", "beqz.n",
    "bnez.n", "bbci.l", "bbsi.l", "bf", "bt",
})
# The zero-overhead loops: they can skip the body to the end label.
_XTENSA_LOOPS = frozenset({"loop", "loopnez", "loopgtz"})
_XTENSA_RETURNS = {
    "ret": LINK, "ret.n": LINK, "retw": LINK, "retw.n": LINK,
    # The returns from an exception or an interrupt.
    **dict.fromkeys(
        ("rfe", "rfue", "rfde", "rfi", "rfwo", "rfwu", "rfdo", "rfdd", "rfme"), EXCEPTION,
    ),
}
_XTENSA_MOVES = frozenset({"movi", "movi.n", "mov", "mov.n"})


def _xtensa_register(text: str) -> str | None:
    name = text.strip().lower()
    return name if _XTENSA_REGISTER.match(name) else None


def _decode_xtensa(mnemonic: str, operands: list[str]) -> Decoded:
    lowered = mnemonic.lower()
    if lowered in _XTENSA_RETURNS:
        return Return(via=_XTENSA_RETURNS[lowered])
    if _XTENSA_CALL.match(lowered) and len(operands) == 1:
        return Transfer(call=True, target=_symbol(operands[0]))
    if _XTENSA_CALLX.match(lowered) and len(operands) == 1:
        return Transfer(call=True, register=_xtensa_register(operands[0]))
    if lowered == "j" and len(operands) == 1:
        return Transfer(call=False, target=_symbol(operands[0]))
    if lowered == "j.l" and len(operands) == 2:
        # `j.l label, aN`: a far jump that uses aN.
        return Transfer(call=False, target=_symbol(operands[0]))
    if lowered == "jx" and len(operands) == 1:
        return Transfer(call=False, register=_xtensa_register(operands[0]))
    if (lowered in _XTENSA_BRANCHES or lowered in _XTENSA_LOOPS) and operands:
        return Transfer(call=False, target=_symbol(operands[-1]), conditional=True)
    if lowered == "movi" and len(operands) == 2:
        register = _xtensa_register(operands[0])
        target = _symbol(operands[1])
        if register is not None and target is not None:
            # `movi aN, sym` loads the address of sym (the assembler puts it
            # in a literal and reads it with l32r).
            return Load(register, target)
    if lowered in _XTENSA_MOVES and operands:
        register = _xtensa_register(operands[0])
        if register is not None:
            return Move(register)
    return None


_DECODERS = {ARM: _decode_arm, RISCV: _decode_riscv, XTENSA: _decode_xtensa}


def decode(isa: str, statement: str) -> Decoded:
    """Decode one instruction statement of *isa*.

    The caller passes no directive and no label.  None for an instruction
    that is no transfer, and for an instruction set this does not know.
    """
    decoder = _DECODERS.get(isa)
    found = _INSTRUCTION.match(statement)
    if decoder is None or found is None:
        return None
    return decoder(found.group(1), _operands(found.group(2)))
