"""The options of a compiler driver (GCC, clang) that take the NEXT argument as their value.

WHY one table: each reader of a command line must know which argument is
the value of an option, and which is an input file.  A reader that does
not know an option takes its value for an input.  Two readers had their
own lists, and each list lacked options of the other: the preprocessor of
the macro values got ``-iwithprefixbefore newlib/platform_include`` (the
flags of the ESP32 Arduino framework) and read ``newlib/platform_include``
as a source file, thus each unit of the build had no macro values.

Measured 2026-11-13: ``<driver> -E -x c <option> VALUE u.c``, and VALUE is
the value of the option when the driver does not report it as a missing
input file.  Drivers: riscv32-esp-elf-gcc 14.2, arm-zephyr-eabi-gcc 14.3
and clang 23.1.  The candidates were the options of the GCC manual and
the long aliases of GCC that take an argument (``option_map`` of
``gcc.cc``).  The table holds each option that one of the drivers reads
with a separate value, thus a command line of GCC and the libclang
arguments (which hold clang options) both read correctly.  ``-wrapper``
comes from ``gcc -###``.  The probe cannot tell an option whose value is a
file that the preprocessor reads (``--include``, ``--imacros``): the
driver reports the missing file as for an input.  ``option_map`` gives
them an argument.

The joined form of an option (``-Idir``, ``--sysroot=/x``) is one argument
and needs no entry here.
"""

from __future__ import annotations

DRIVER_TAKES_NEXT: frozenset[str] = frozenset({
    # The input language and the output.
    "-x", "--language", "-o", "--output", "-dumpdir", "--dumpdir", "-dumpbase", "--dumpbase",
    "-dumpbase-ext", "--dumpbase-ext", "--dump", "-aux-info", "--output-class-directory",
    "--serialize-diagnostics", "-MJ",
    # Arguments for the other programs that the driver runs.
    "-Xpreprocessor", "-Xassembler", "--for-assembler", "-Xlinker", "--for-linker", "-Xclang",
    "-mllvm", "-Xopenmp-target", "-wrapper", "--param",
    # The link.
    "-T", "-u", "--force-link", "-e", "--entry", "-z", "-L", "--library-directory", "-l",
    "-specs", "--specs", "-G",
    # Directory search (GCC "Options for Directory Search", clang).
    "-B", "--prefix", "-I", "--include-directory", "--include-directory-after", "-iquote",
    "-isystem", "-idirafter", "-iprefix", "--include-prefix", "-iwithprefix",
    "--include-with-prefix", "--include-with-prefix-after", "-iwithprefixbefore",
    "--include-with-prefix-before", "-isysroot", "-imultilib", "--sysroot", "-isystem-after",
    "-iwithsysroot", "-cxx-isystem", "-iframework", "-F", "-ivfsoverlay",
    # The preprocessor.
    "-include", "--include", "-imacros", "--imacros", "-include-pch", "-D", "--define-macro",
    "-U", "--undefine-macro", "-A", "--assert", "-MF", "-MT", "-MQ",
    # The target, the dialect and the setup of the driver (clang).
    "-target", "-arch", "-meabi", "--std", "-working-directory", "--config", "-resource-dir",
    "--resource",
})
