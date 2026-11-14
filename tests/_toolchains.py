"""The toolchains of embedded SDKs for the indexer tests: compile a fixture, then give it to the indexer as a unit of a build.

WHY: fw-context indexes firmware.  A unit of a user has a compiler of an
embedded SDK, and the indexer asks that compiler for the target, the
system headers and the macros (``_driver_query``).  A test that parsed
its fixture without a compiler, or with the compiler of the host, gave
libclang the target of the host: MSVC on Windows, Mach-O on macOS.  No
unit of a user takes that path.

Thus a fixture goes the path of a build:

1. ``write_sources`` writes the sources (LF on each host).
2. ``Toolchain.arguments`` gives the arguments of an entry: the absolute
   path of the driver of the SDK, the flags of the board, the flags of
   the test.  A test that needs its own form of entry (the same file with
   two sets of macros, ``-o build/x.o``) writes the entry with it.
3. ``write_compile_commands`` compiles each entry (``-c``) and writes
   ``compile_commands.json``.  An error of the compiler fails the test: a
   fixture that no compiler accepts verifies nothing.  A fixture of a
   parse error is the reverse: the test names it in ``broken``, and the
   compiler must refuse it.
4. ``build_fixture`` does steps 2 and 3 for a list of sources.
   ``unit_for`` gives the unit that ``compile_commands.parse`` makes of
   one source, with the answer of the driver.

The object files of these compiles go to a temporary directory, not to
the place of ``-o``: a test then finds the same files in its project as
before, and no object file that the build did not make.

Limit: a test that changes a source after ``write_compile_commands``
(the incremental tests change sources between two index runs) indexes a
version that no compiler checked.  Only the first version of the fixture
compiles here.  The backlog of the project has the item.

The fixture ``toolchain`` (``tests/conftest.py``) runs a test once with
each toolchain of ``tests/sdk/versions.toml``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import sdk

if TYPE_CHECKING:
    from clang.cindex import TranslationUnit

    from fw_context_mcp.indexer.compile_commands import CompilationUnit

#: The toolchain of a test of ARM code alone (the fixture ``arm_toolchain``,
#: and the oracle of the assembly corpus, ``tests/fixtures/asm/regenerate.py``).
ARM_TOOLCHAIN_ID = "arm-zephyr"

#: The suffixes that the C++ driver (``g++``) compiles.  The C driver
#: compiles the others: C, and assembly with (``.S``) or without (``.s``)
#: the preprocessor.
_CXX_SUFFIXES = frozenset({".cpp", ".cc", ".cxx", ".c++"})


@dataclass(frozen=True)
class Toolchain:
    """One toolchain of an SDK of the tests, with the flags of the CPU of its board."""

    id: str
    bin_dir: Path
    prefix: str
    flags: tuple[str, ...]

    def driver(self, source: str | Path, language: str | None = None) -> Path:
        """Return the driver that compiles *source*: ``<prefix>g++`` for C++, else ``<prefix>gcc``.

        *language* (``"c"`` or ``"c++"``) selects the driver when the
        suffix does not tell the language of the test.  WHY no other value:
        ``"cpp"`` (the language name of ``CompilationUnit``) selected gcc
        without a word, and the test compiled its C++ as C.
        """
        if language not in (None, "c", "c++"):
            raise ValueError(f"language {language!r}: give 'c' or 'c++'")
        cxx = language == "c++" if language is not None else Path(source).suffix.lower() in _CXX_SUFFIXES
        return self.tool("g++" if cxx else "gcc")

    def tool(self, name: str) -> Path:
        """Return the program ``<prefix><name>`` of the toolchain (``gcc``, ``ar``); fail the test with the repair when it is missing."""
        found = find_driver(self.bin_dir, self.prefix, name)
        if found is None:
            pytest.fail(
                f"toolchain {self.id}: no {self.prefix}{name} in {self.bin_dir}; run {sdk.PROVISION_COMMAND}",
                pytrace=False,
            )
        return found

    def arguments(
        self, source: str, *flags: str, output: str | None = None, language: str | None = None,
    ) -> list[str]:
        """Return the arguments of an entry that compiles *source*.

        The driver of :meth:`driver`, the flags of the board, *flags*,
        ``-c <source>``, and ``-o <output>`` when *output* is given.
        """
        argv = [str(self.driver(source, language)), *self.flags, *flags, "-c", source]
        return [*argv, "-o", output] if output is not None else argv


def find_driver(bin_dir: Path, prefix: str, driver: str) -> Path | None:
    """Return the program ``<prefix><driver>`` of *bin_dir*, or None.

    ``shutil.which`` with the directory as the only path, because it adds
    the ``.exe`` of Windows.  The pre-test of the session
    (``tests/_environment.py``) finds the drivers with this function too,
    thus it checks the same programs that the tests run.
    """
    found = shutil.which(prefix + driver, path=str(bin_dir))
    return None if found is None else Path(found)


def toolchain_ids() -> list[str]:
    """Return the id of each toolchain of ``versions.toml``, in its sequence."""
    return [spec.id for spec in sdk.load_versions().toolchains]


def load_toolchain(toolchain_id: str) -> Toolchain:
    """Return the toolchain *toolchain_id* of ``versions.toml``, in the root of the test SDKs."""
    versions = sdk.load_versions()
    spec = next((spec for spec in versions.toolchains if spec.id == toolchain_id), None)
    if spec is None:
        raise KeyError(f"no toolchain {toolchain_id!r} in {sdk.VERSIONS_FILE}")
    return Toolchain(
        id=spec.id, bin_dir=sdk.toolchain_bin(sdk.sdk_root(), versions, spec), prefix=spec.prefix, flags=spec.flags,
    )


def write_sources(root: Path, sources: Mapping[str, str]) -> list[str]:
    """Write each source of *sources* (name relative to *root*: text) with LF; return the names.

    WHY ``newline="\\n"``: on Windows, text mode writes CRLF, and the column
    and the byte offset of each line then differ from the other hosts.
    """
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return list(sources)


def write_compile_commands(
    path: Path, entries: Sequence[Mapping[str, object]], *, broken: Mapping[str, str] | None = None,
) -> Path:
    """Compile each entry of *entries*, then write them into *path* (``compile_commands.json``).

    Each entry has ``directory``, ``file`` and ``arguments``.  *broken*
    maps the ``file`` of a fixture of an error to a text that the error of
    the compiler must hold (``"undeclared"``, the name of a missing
    header).  WHY the text: a non-zero exit alone also comes from a flag
    that the toolchain refuses, and the fixture then tests another error.
    Each other entry must compile.  Return *path*.

    The compiles run at the same time: the entries are independent.
    """
    expected_errors = dict(broken or {})
    with tempfile.TemporaryDirectory(prefix="fw-context-fixture-") as scratch:
        jobs = []
        for index, entry in enumerate(entries):
            arguments = entry["arguments"]
            assert isinstance(arguments, list), f"entry of {entry['file']}: write the arguments as a list"
            jobs.append(([str(arg) for arg in arguments], Path(str(entry["directory"])), Path(scratch) / f"{index}.o"))
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
            results = list(pool.map(lambda job: _compile(*job), jobs))
    for entry, (argv, _cwd, _obj), result in zip(entries, jobs, results, strict=True):
        _check_compile(str(entry["file"]), argv, result, expected_errors.get(str(entry["file"])))
    write_entries(path, entries)
    return path


def write_entries(path: Path, entries: Sequence[Mapping[str, object]]) -> Path:
    """Write *entries* into *path* without a compile; return *path*.

    Only for entries that compiled before: the restore of the original
    file in a ``finally`` block.  There a compile error would hide the
    failure of the test.
    """
    path.write_text(json.dumps(list(entries), indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _check_compile(
    name: str, argv: list[str], result: subprocess.CompletedProcess[str], expected_error: str | None,
) -> None:
    """Fail the test when the compile of *name* did not end as the fixture expects."""
    output = result.stdout + result.stderr
    command = " ".join(argv)
    if expected_error is None:
        if result.returncode != 0:
            pytest.fail(f"the compiler does not compile {name}:\n$ {command}\n{output}", pytrace=False)
        return
    if result.returncode == 0:
        pytest.fail(f"the fixture {name} must not compile, and it compiles:\n$ {command}", pytrace=False)
    if expected_error not in output:
        pytest.fail(
            f"the compiler refuses {name}, but not with {expected_error!r}:\n$ {command}\n{output}", pytrace=False,
        )


def _compile(argv: list[str], cwd: Path, obj: Path) -> subprocess.CompletedProcess[str]:
    """Run the compile command *argv* in *cwd*, with its object file at *obj*.

    The ``-o`` of *argv* is replaced, or added when there is none.
    """
    if "-o" in argv[:-1]:
        at = argv.index("-o")
        argv = [*argv[:at], "-o", str(obj), *argv[at + 2:]]
    else:
        argv = [*argv, "-o", str(obj)]
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)


def build_fixture(root: Path, sources: Sequence[str], toolchain: Toolchain, flags: Sequence[str] = ()) -> Path:
    """Compile each source of *sources* in *root* with *toolchain*, and write ``root/compile_commands.json``.

    *sources* are names relative to *root*.  *flags* come after the flags
    of the toolchain (``-D``, ``-I``, ``-std``).  Each entry runs in
    *root*: ``-c <source> -o <source>.o``.  Return the path of
    ``compile_commands.json``.
    """
    entries = [
        {"directory": str(root), "file": str(root / name), "arguments": toolchain.arguments(name, *flags, output=f"{name}.o")}
        for name in sources
    ]
    return write_compile_commands(root / "compile_commands.json", entries)


def unit_for(root: Path, source: str, toolchain: Toolchain, flags: Sequence[str] = ()) -> CompilationUnit:
    """Compile *source* (relative to *root*) and return its unit, as ``compile_commands.parse`` gives it.

    The database is ``root/compile_commands.json``, as ``build_fixture``
    writes it: for a test that registers that file as the database of a
    build.  ``unit_of`` keeps the database out of the directory of the test.
    """
    entry = {
        "directory": str(root), "file": str(root / source),
        "arguments": toolchain.arguments(source, *flags, output=f"{source}.o"),
    }
    return _the_unit(root / "compile_commands.json", entry, None)


#: The language of a ``CompilationUnit`` and the driver language that
#: compiles it.  WHY only "c" and "cpp": they are the values of
#: ``CompilationUnit.language``, and one name for each language keeps the
#: callers greppable (``Toolchain.driver`` takes "c" and "c++").
_UNIT_LANGUAGES = {"c": "c", "cpp": "c++"}


def unit_of(
    source: Path, toolchain: Toolchain, flags: Sequence[str] = (), *,
    directory: Path | None = None, language: str | None = None, broken: str | None = None,
) -> CompilationUnit:
    """Compile *source* (a path) in *directory* (default: its directory), and return its unit.

    For a test that parsed one file with a hand-made ``CompilationUnit``:
    the unit now has the driver of *toolchain* and its answer.  *language*
    is the language of the unit (``"c"`` or ``"cpp"``).  A C++ unit of a
    file whose suffix is not C++ (``.c``, ``.h``) gets ``-x c++``, as a
    build gives it.  The unit that the indexer makes must have *language*:
    else the test would run the other language than it states.  The
    ``compile_commands.json`` goes to a temporary directory, thus the
    directory of the test holds no file that it did not write.  *broken*
    makes *source* a fixture of an error: the text that the error of the
    compiler must hold (``write_compile_commands``).
    """
    if language is not None and language not in _UNIT_LANGUAGES:
        raise ValueError(f"language {language!r}: give 'c' or 'cpp'")
    driver_language = _UNIT_LANGUAGES[language] if language is not None else None
    extra: list[str] = []
    if driver_language == "c++" and source.suffix.lower() not in _CXX_SUFFIXES:
        extra = ["-x", "c++"]
    work_dir = directory if directory is not None else source.parent
    entry = {
        "directory": str(work_dir),
        "file": str(source),
        "arguments": toolchain.arguments(str(source), *extra, *flags, language=driver_language),
    }
    with tempfile.TemporaryDirectory(prefix="fw-context-unit-") as scratch:
        unit = _the_unit(Path(scratch) / "compile_commands.json", entry, broken)
    if language is not None:
        assert unit.language == language, f"{source}: the indexer reads a {unit.language} unit, the test asks for {language}"
    return unit


def _the_unit(database: Path, entry: Mapping[str, object], broken: str | None) -> CompilationUnit:
    """Compile the one *entry*, write it into *database*, and return the unit that ``parse`` makes of it."""
    from fw_context_mcp.indexer.compile_commands import parse

    broken_map = {str(entry["file"]): broken} if broken is not None else None
    units = list(parse(write_compile_commands(database, [entry], broken=broken_map)))
    assert len(units) == 1, f"{database} gave {len(units)} units"
    return units[0]


def parse_unit(
    source: Path, toolchain: Toolchain, flags: Sequence[str] = (), *,
    directory: Path | None = None, language: str | None = None, options: int = 0,
) -> TranslationUnit:
    """Compile *source*, then parse it with libclang with the arguments of its unit; return the translation unit.

    For a test that gives libclang a file and its own arguments: the
    arguments now come from the driver of *toolchain* (target, system
    headers, macros), as the indexer gives them.  *directory* and
    *language* are those of ``unit_of``.  *options* are the parse options
    of libclang (``TranslationUnit.PARSE_*``).
    """
    from fw_context_mcp.indexer.symbols import _get_index

    unit = unit_of(source, toolchain, flags, directory=directory, language=language)
    return _get_index().parse(str(unit.file), args=unit.clang_args, options=options)
