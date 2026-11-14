"""The assembly reader, against a corpus the assembler itself validates.

`tests/fixtures/asm/` holds one `.S` file per family of GNU as
constructs, written from the manual rather than from whatever a given
SDK on a given machine happens to write.  `expected.toml` beside it holds
what each file really defines, produced by the `nm` of the ARM toolchain
of the test SDKs (``arm-zephyr``) — the only authority on the question.
The reader reads each file as the unit that the driver of that toolchain
gives (``tests/_toolchains.py``).  WHY ARM alone: the corpus is Cortex-M
assembly (Thumb, the comments `@` of ARM), which no other toolchain
assembles.

Two assertions, and the first one is the important one:

* **the reader never invents.**  Its answer must stay inside what the
  assembler recorded.  A missing symbol means something is not found; an
  invented one puts a name in ``lookup_symbol`` that is in no binary,
  and no linker, debugger or reader will ever find it.
* **for a file whose constructs the reader implements** (``complete``),
  the two sets are equal.

A file for a construct that is not implemented yet still carries its
true expectations.  Flipping ``complete`` is what turns the equality
assertion on when the construct lands.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tomllib
from pathlib import Path

import pytest

from fw_context_mcp.indexer.asm import (
    extract_calls,
    extract_symbols,
    extract_vectors,
    preprocess,
)
from tests._toolchains import Toolchain, find_driver, unit_of

CORPUS = Path(__file__).parent / "fixtures" / "asm"
EXPECTED = tomllib.loads((CORPUS / "expected.toml").read_text(encoding="utf-8"))


def _tool(toolchain: Toolchain, name: str) -> str:
    """Return the program *name* (``nm``) of *toolchain*; the pre-test checked the test SDKs."""
    found = find_driver(toolchain.bin_dir, toolchain.prefix, name)
    assert found is not None, f"no {toolchain.prefix}{name} in {toolchain.bin_dir}"
    return str(found)


def _read(path: Path, toolchain: Toolchain) -> tuple[set[str], set[str]]:
    """Return (symbols, vector slots) the reader finds in *path*, read as the unit of *toolchain*.

    The driver of the ARM toolchain gives the target, and the reader cuts
    the comments of that target (`@`).
    """
    source = preprocess(unit_of(path, toolchain))
    assert source is not None, f"the preprocessor could not read {path.name}"
    return (
        {s.name for s in extract_symbols(source)},
        {v.name for v in extract_vectors(source)},
    )


def _corpus_files(*, complete: bool | None = None) -> list[str]:
    """The files of the corpus; with *complete*, only the files of that ``complete`` flag."""
    return sorted(name for name, entry in EXPECTED.items() if complete is None or entry["complete"] is complete)


class TestAgainstTheAssembler:
    """WHY each test takes the files it applies to: a file that a test does
    not apply to was a skip, and a skip reads as a pass."""

    @pytest.mark.parametrize("name", _corpus_files())
    def test_the_reader_invents_nothing(self, name: str, arm_toolchain: Toolchain):
        """The rule that holds for every file, implemented or not."""
        entry = EXPECTED[name]
        found, _ = _read(CORPUS / f"{name}.S", arm_toolchain)

        invented = found - set(entry["defined"])

        assert not invented, (
            f"{name}.S: the reader reports {sorted(invented)}, which the "
            f"assembler does not define.  A name in lookup_symbol that is "
            f"in no binary is worse than a missing one."
        )

    @pytest.mark.parametrize("name", _corpus_files(complete=True))
    def test_the_reader_misses_nothing(self, name: str, arm_toolchain: Toolchain):
        found, _ = _read(CORPUS / f"{name}.S", arm_toolchain)

        assert found == set(EXPECTED[name]["defined"])

    @pytest.mark.parametrize("name", _corpus_files(complete=False))
    def test_an_incomplete_file_still_has_a_name_that_the_reader_misses(self, name: str, arm_toolchain: Toolchain):
        """A file of ``complete = false`` covers constructs that the reader does not read yet.

        When the reader learns them, this test fails: set ``complete = true``
        in expected.toml, and the test above then holds the file.
        """
        found, _ = _read(CORPUS / f"{name}.S", arm_toolchain)

        assert set(EXPECTED[name]["defined"]) - found, f"{name}.S: the reader reads it whole now"

    @pytest.mark.parametrize("name", _corpus_files())
    def test_the_vector_slots_match(self, name: str, arm_toolchain: Toolchain):
        """A file without ``vectors`` in expected.toml has no vector table: the reader must find no slot."""
        _, slots = _read(CORPUS / f"{name}.S", arm_toolchain)

        assert slots == set(EXPECTED[name].get("vectors", [])), (
            "the expected set comes from the object's relocations: a "
            "`.word Sym` makes one and a reserved `.word 0` does not"
        )

    @pytest.mark.parametrize("name", sorted(name for name, entry in EXPECTED.items() if "calls" in entry))
    def test_the_call_edges_match(self, name: str, arm_toolchain: Toolchain):
        """Each call or jump relocation is an edge of the function that holds it.

        Only the files with ``calls`` in expected.toml: the oracle takes the
        edges from the relocations, and the assembler resolves a branch to
        a label of the same section without one.  A file without ``calls``
        thus says nothing about its branches (``conditionals.S`` has one).
        """
        source = preprocess(unit_of(CORPUS / f"{name}.S", arm_toolchain))
        assert source is not None
        edges = {f"{'+'.join(call.owners)}>{call.target}" for call in extract_calls(source)}

        assert edges == set(EXPECTED[name]["calls"])


@pytest.mark.parametrize("name", _corpus_files())
class TestTheCorpusItself:
    """The corpus must be real assembly, and expected.toml must match it.

    This is the check a hand-written corpus actually needs.  Inventing
    syntax no assembler accepts, then asserting against it, produces
    tests that pass and prove nothing.
    """

    @staticmethod
    def _assemble(path: Path, out_dir: Path, toolchain: Toolchain) -> subprocess.CompletedProcess:
        obj = out_dir / f"{path.stem}.o"
        return subprocess.run(
            [str(toolchain.driver(path)), *toolchain.flags, "-c", "-o", str(obj), str(path)],
            capture_output=True, text=True, timeout=60,
        )

    def test_the_file_assembles(self, name: str, tmp_path: Path, arm_toolchain: Toolchain):
        run = self._assemble(CORPUS / f"{name}.S", tmp_path, arm_toolchain)

        assert run.returncode == 0, f"{name}.S does not assemble:\n{run.stderr}"

    def test_expected_toml_still_matches_the_assembler(
        self, name: str, tmp_path: Path, arm_toolchain: Toolchain
    ):
        """Catches drift between the corpus and the file generated from it."""
        path = CORPUS / f"{name}.S"
        assert self._assemble(path, tmp_path, arm_toolchain).returncode == 0
        out = subprocess.run(
            [_tool(arm_toolchain, "nm"), str(tmp_path / f"{name}.o")],
            capture_output=True, text=True, timeout=60,
        )
        defined = set()
        for line in out.stdout.splitlines():
            parts = line.split()
            # Three fields means an address, a type and a name; two means
            # `U`, referenced and not defined.
            if len(parts) == 3:
                defined.add(parts[2])

        assert defined == set(EXPECTED[name]["defined"]), (
            f"{name}.S and expected.toml disagree; regenerate the file"
        )

    def test_expected_calls_still_match_the_assembler(self, name: str, tmp_path: Path, arm_toolchain: Toolchain):
        """The call edges of expected.toml come from the relocations (regenerate.py)."""
        spec = importlib.util.spec_from_file_location("regenerate", CORPUS / "regenerate.py")
        assert spec is not None and spec.loader is not None
        regenerate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(regenerate)
        path = CORPUS / f"{name}.S"
        assert self._assemble(path, tmp_path, arm_toolchain).returncode == 0

        edges = regenerate.call_edges(tmp_path / f"{name}.o", arm_toolchain)

        assert edges == set(EXPECTED[name].get("calls", [])), (
            f"{name}.S and expected.toml disagree; regenerate the file"
        )
