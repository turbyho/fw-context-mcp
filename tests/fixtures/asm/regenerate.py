#!/usr/bin/env python
"""Regenerate expected.toml from the assembler.

    python tests/fixtures/asm/regenerate.py

Run this after adding a corpus file or changing one.  The expectations
are not written by hand: `arm-none-eabi-gcc` assembles each file and
`arm-none-eabi-nm` reads the symbol table, which is the only authority on
what a file really defines.  Relocations answer the same question for a
vector table — `.word Sym` makes one, a reserved `.word 0` does not.

`tests/test_asm_corpus.py` re-derives the same answer whenever an ARM
toolchain is installed and fails if it disagrees, so a stale file is
caught rather than trusted.  This script exists so that regenerating is
reproducible for anyone, rather than living in somebody's shell history.

COMPLETE below says the reader is expected to reproduce `defined`
exactly.  A file listed False still carries its true expectations; the
flag is what turns the equality assertion on once the construct lands.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

CORPUS = Path(__file__).parent
ARM_BIN = Path.home() / ".platformio/packages/toolchain-gccarmnoneeabi/bin"
# gcc, not as: a `.S` is "assembly that must be preprocessed", and `as`
# alone does not run cpp.  Driving it through gcc is what a build does.
GCC = ARM_BIN / "arm-none-eabi-gcc"
NM = ARM_BIN / "arm-none-eabi-nm"
OBJDUMP = ARM_BIN / "arm-none-eabi-objdump"
FLAGS = ["-mcpu=cortex-m4", "-mthumb", "-c"]

COMPLETE = {
    "altmacro": False,
    "comments": True,
    "directives_basic": True,
    "macro_args": True,
    "macro_basic": True,
    "macro_control": True,
    "macro_substitution": True,
    "refuse": False,
    "repeat": True,
    "statements_semicolon": True,
    "vector_table": True,
}

DESCRIPTIONS = {
    "altmacro": "Alternate macro mode, which the reader refuses rather "
                "than guesses at.",
    "comments": "Prose and directives inside block comments, which the "
                "preprocessor keeps and the reader must skip.",
    "directives_basic": "Every directive that names a symbol, with no "
                        "macro facility involved.",
    "macro_args": "Macro argument defaults, :req, :vararg, keyword calls "
                  "and quoting.",
    "macro_basic": "The plain .macro forms, and a macro never invoked.",
    "macro_control": ".exitm and .purgem.",
    "macro_substitution": "\\name, \\() and \\@ inside a body.",
    "refuse": "Constructs an expander must refuse rather than guess at.",
    "repeat": ".rept, .irp and .irpc.",
    "statements_semicolon": "Several statements on one line, the shape a "
                            "C-preprocessor macro expands into.",
    "vector_table": "A vector table, its reserved slots and its weak "
                    "aliases of the default handler.",
}


def assemble(path: Path, out_dir: Path) -> Path:
    obj = out_dir / f"{path.stem}.o"
    run = subprocess.run(
        [str(GCC), *FLAGS, "-o", str(obj), str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if run.returncode:
        raise SystemExit(f"{path.name} does not assemble:\n{run.stderr}")
    return obj


def symbols(obj: Path) -> tuple[set[str], set[str]]:
    """Return (defined, undefined) from the object's symbol table."""
    out = subprocess.run([str(NM), str(obj)], capture_output=True,
                         text=True, timeout=60)
    defined, undefined = set(), set()
    for line in out.stdout.splitlines():
        parts = line.split()
        # Two fields means `U`, referenced and not defined; three means an
        # address, a type and a name.
        if len(parts) == 2 and parts[0] == "U":
            undefined.add(parts[1])
        elif len(parts) == 3:
            defined.add(parts[2])
    return defined, undefined


def vector_targets(obj: Path) -> set[str]:
    """Return the symbols the object's VECTOR sections point at.

    Restricted to those sections on purpose.  A relocation in `.text` is
    an ordinary reference — the STM32 startup makes five for the linker
    script's section boundaries, which its copy loop reads — and counting
    them here would ask the reader to report constants as table slots.
    The section is what separates the two, for the oracle exactly as for
    the reader.
    """
    out = subprocess.run([str(OBJDUMP), "-r", str(obj)], capture_output=True,
                         text=True, timeout=60)
    ordinary = re.compile(
        r"^\.(?:text|data|bss|rodata|init|fini|note|comment|debug|ARM)\b")
    targets: set[str] = set()
    section = ""
    for line in out.stdout.splitlines():
        header = re.match(r"RELOCATION RECORDS FOR \[([^\]]+)\]", line)
        if header:
            section = header.group(1)
            continue
        parts = line.split()
        if len(parts) == 3 and parts[1].startswith("R_") \
                and not ordinary.match(section):
            targets.add(parts[2])
    return targets


def quoted(items: set[str]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in sorted(items)) + "]"


def main() -> int:
    if not (GCC.exists() and NM.exists() and OBJDUMP.exists()):
        print(f"no ARM toolchain under {ARM_BIN}", file=sys.stderr)
        return 1

    lines = [
        "# Generated by regenerate.py, not written by hand.",
        "#",
        "# `defined` is what arm-none-eabi-nm reports for the object each",
        "# corpus file assembles to, which is the only authority on what a",
        "# file really defines.  `undefined` is what it names and does not",
        "# define — a vector slot pointing at a linker symbol looks like",
        "# that.  `vectors` comes from the object's relocations.",
        "#",
        "# `complete` says the reader is expected to reproduce `defined`",
        "# exactly.  Where it is false the reader is still forbidden to",
        "# invent: its answer must stay a subset.",
        "",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(CORPUS.glob("*.S")):
            key = path.stem
            if key not in COMPLETE:
                raise SystemExit(
                    f"{path.name} is not listed in COMPLETE and DESCRIPTIONS"
                )
            obj = assemble(path, Path(tmp))
            defined, undefined = symbols(obj)
            slots = vector_targets(obj)
            lines.append(f"[{key}]")
            # A TOML literal string: the descriptions carry backslashes,
            # which a basic string would read as escapes.
            lines.append(f"description = '{DESCRIPTIONS[key]}'")
            lines.append(f"complete = {str(COMPLETE[key]).lower()}")
            lines.append(f"defined = {quoted(defined)}")
            if undefined:
                lines.append(f"undefined = {quoted(undefined)}")
            if slots:
                lines.append(f"vectors = {quoted(slots)}")
            lines.append("")

    (CORPUS / "expected.toml").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {CORPUS / 'expected.toml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
