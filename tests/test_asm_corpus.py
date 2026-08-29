"""The assembly reader, against a corpus the assembler itself validates.

`tests/fixtures/asm/` holds one `.S` file per family of GNU as
constructs, written from the manual rather than from whatever a given
SDK on a given machine happens to write.  `expected.toml` beside it holds
what each file really defines, produced by `arm-none-eabi-nm` — the only
authority on the question — so these tests run with no toolchain at all.

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

import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer.asm import extract_symbols, extract_vectors, preprocess

CORPUS = Path(__file__).parent / "fixtures" / "asm"
EXPECTED = tomllib.loads((CORPUS / "expected.toml").read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(
    shutil.which("clang") is None, reason="the preprocessor is clang"
)

# The toolchain PlatformIO installs.  Not on PATH, which is why it is
# named in full rather than looked up.
_ARM_BIN = Path.home() / ".platformio/packages/toolchain-gccarmnoneeabi/bin"
_ARM_GCC = _ARM_BIN / "arm-none-eabi-gcc"
_ARM_NM = _ARM_BIN / "arm-none-eabi-nm"

needs_arm = pytest.mark.skipif(
    not (_ARM_GCC.exists() and _ARM_NM.exists()),
    reason="no ARM toolchain to assemble the corpus with",
)


def _read(path: Path) -> tuple[set[str], set[str]]:
    """Return (symbols, vector slots) the reader finds in *path*."""
    unit = SimpleNamespace(file=path, directory=path.parent, clang_args=[])
    source = preprocess(unit)
    assert source is not None, f"the preprocessor could not read {path.name}"
    return (
        {s.name for s in extract_symbols(source)},
        {v.name for v in extract_vectors(source)},
    )


def _corpus_files() -> list[str]:
    return sorted(EXPECTED)


@pytest.mark.parametrize("name", _corpus_files())
class TestAgainstTheAssembler:
    def test_the_reader_invents_nothing(self, name: str):
        """The rule that holds for every file, implemented or not."""
        entry = EXPECTED[name]
        found, _ = _read(CORPUS / f"{name}.S")

        invented = found - set(entry["defined"])

        assert not invented, (
            f"{name}.S: the reader reports {sorted(invented)}, which the "
            f"assembler does not define.  A name in lookup_symbol that is "
            f"in no binary is worse than a missing one."
        )

    def test_the_reader_misses_nothing(self, name: str):
        entry = EXPECTED[name]
        if not entry["complete"]:
            pytest.skip(f"{name}.S covers constructs the reader does not read yet")
        found, _ = _read(CORPUS / f"{name}.S")

        assert found == set(entry["defined"])

    def test_the_vector_slots_match(self, name: str):
        entry = EXPECTED[name]
        if "vectors" not in entry:
            pytest.skip(f"{name}.S has no vector table")
        _, slots = _read(CORPUS / f"{name}.S")

        assert slots == set(entry["vectors"]), (
            "the expected set comes from the object's relocations: a "
            "`.word Sym` makes one and a reserved `.word 0` does not"
        )


@needs_arm
@pytest.mark.parametrize("name", _corpus_files())
class TestTheCorpusItself:
    """The corpus must be real assembly, and expected.toml must match it.

    This is the check a hand-written corpus actually needs.  Inventing
    syntax no assembler accepts, then asserting against it, produces
    tests that pass and prove nothing.
    """

    @staticmethod
    def _assemble(path: Path, out_dir: Path) -> subprocess.CompletedProcess:
        obj = out_dir / f"{path.stem}.o"
        return subprocess.run(
            [str(_ARM_GCC), "-mcpu=cortex-m4", "-mthumb", "-c",
             "-o", str(obj), str(path)],
            capture_output=True, text=True, timeout=60,
        )

    def test_the_file_assembles(self, name: str, tmp_path: Path):
        run = self._assemble(CORPUS / f"{name}.S", tmp_path)

        assert run.returncode == 0, f"{name}.S does not assemble:\n{run.stderr}"

    def test_expected_toml_still_matches_the_assembler(
        self, name: str, tmp_path: Path
    ):
        """Catches drift between the corpus and the file generated from it."""
        path = CORPUS / f"{name}.S"
        assert self._assemble(path, tmp_path).returncode == 0
        out = subprocess.run(
            [str(_ARM_NM), str(tmp_path / f"{name}.o")],
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
