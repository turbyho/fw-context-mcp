# Assembly corpus

One `.S` file per family of GNU as constructs, written from the
documentation (`as.info`, nodes `Macro`, `Altmacro`, `Irp`, `Irpc`,
`Rept`, `Purgem`, `Exitm`) rather than from whatever a given SDK on a
given machine happens to write.  Platforms differ; the manual does not.

## The oracle

Every file is assembled by `arm-none-eabi-as` and its symbol table read
with `arm-none-eabi-nm`.  **What the assembler puts in the symbol table
is the truth**; a hand-written expectation list can only claim.  The
tests in `tests/test_asm_corpus.py` compare the reader against that
truth, and skip when no ARM assembler is installed.

This catches the one risk a hand-written corpus really has: inventing
syntax that no assembler accepts, then "passing" tests against it.

## The two assertions

For every file, always:

* **the reader never invents** — its symbol set is a subset of the
  assembler's.  A missing symbol means something is not found; an
  invented one puts a name in `lookup_symbol` that is in no binary.

For a file whose constructs the reader implements (`complete = true` in
`expected.toml`), additionally:

* **the reader misses nothing** — the two sets are equal.

A file for a construct that is not implemented yet therefore still
carries its true expectations, and the equality assertion turns on by
flipping one flag when the construct lands.

## What nm reports

Symbols with an address are definitions; `U` means referenced and not
defined, which is what a vector slot naming a linker symbol looks like.
`.L`-prefixed labels never appear — GNU as keeps them out of the object
symbol table, which is why the reader drops them too.
