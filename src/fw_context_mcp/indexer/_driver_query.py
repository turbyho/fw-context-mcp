"""Ask the GCC driver of a build for its system headers and its predefined macros.

WHY: a GCC cross compiler adds its system include directories and its
target macros on its own.  They are not in ``compile_commands.json``, thus
libclang must get them from somewhere else.  Before this module the indexer
guessed the directories from the layout of the toolchain and used the macros
of clang.  Three faults came from that, measured on the indexes of one
machine:

- A target that libclang has no backend for (xtensa) got no toolchain
  headers at all.  libclang then used the headers of the HOST: 252 of 757
  files of an ESP32 index came from ``/usr/include`` (glibc and the host
  libstdc++), and ``#if __XTENSA__`` read as false.
- The guessed order put libstdc++ after the C library, while GCC searches
  libstdc++ first.
- clang reports ``__GNUC__`` 4, thus a header that requires GCC 6.3 stopped
  every unit of an Arduino build with ``#error``.

The driver gives all three answers itself, as clangd ``--query-driver``
does:

- ``<gcc> -x <lang> -E -v -`` prints the target triple and the system
  include directories in search order.
- ``<gcc> -x <lang> -dM -E -`` prints the predefined macros.

WHY no allowlist: the query runs the compiler that ``compile_commands.json``
names.  The build of the project runs that compiler too, together with the
code of the repository (Makefile, CMake, build scripts), and fw-context
starts that build itself (``init``, the automatic build of an index run).
An allowlist for the compiler alone thus protects nothing.  An earlier
release had one (``[index] query_driver``), and a compiler outside it got
guessed directories and a much worse parse.

WHY a proxy target: libclang has no backend for some GCC targets (xtensa),
and ``--target`` for such a triple stops the parse with
``TranslationUnitLoadError``.  The proxy is an x86 ELF target with the same
type sizes.  The macros of the real target replace the macros of the proxy
through ``-undef``, thus the preprocessor sees the real target.  x86 and not
ARM: the inline assembly of the ESP32 SDK uses the register constraint
``a``, which x86 accepts and ARM does not (722 errors in 116 units with ARM,
0 with x86).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

#: Time limit for one driver call.  A driver that runs the preprocessor on
#: an empty input answers in milliseconds; a hang must not stop the index.
_QUERY_TIMEOUT_S = 30.0

#: A GCC driver name ends in one of these, after an optional version suffix
#: (``arm-none-eabi-gcc``, ``xtensa-esp32-elf-g++``, ``gcc-12``, ``cc``).
#: It keeps the query away from a binary that is not a GCC driver: a clang
#: or a vendor compiler does not answer the query in the GCC format.
_GCC_DRIVER_NAME = re.compile(r"(?:^|-)(?:gcc|g\+\+|cc|c\+\+)(?:-\d+(?:\.\d+)*)?(?:\.exe)?$")

#: Flags that can change the predefined macros or the system directories of
#: the driver.  An include path, a ``-D`` and a warning flag cannot, and
#: leaving them out keeps the number of distinct queries small.
#:
#: WHY ``-f`` is a list and not a prefix: most ``-f`` flags change no
#: predefine, and some write files even under ``-E``.  Measured with GCC 16:
#: ``-fdiagnostics-format=sarif-file`` wrote ``null.sarif`` into the working
#: directory of the query.  The build writes such a file for its own source,
#: and the query must not add one more with another name.  A specs file of
#: the build can still add such a flag; the file then goes to the build
#: directory, as for the build.
_PREDEFINE_PREFIXES = ("-std=", "-ansi", "-O", "-m", "-pthread", "-nostdinc")
_PREDEFINE_F_FLAGS = frozenset({
    "-fshort-wchar", "-fno-short-wchar", "-fsigned-char", "-funsigned-char",
    "-fno-signed-char", "-fno-unsigned-char", "-fno-inline",
    "-funsafe-math-optimizations", "-fno-unsafe-math-optimizations",
    "-fassociative-math", "-fno-associative-math", "-freciprocal-math", "-fno-reciprocal-math",
    "-fsized-deallocation", "-fno-sized-deallocation",
    "-fshort-enums", "-fno-short-enums", "-fexceptions", "-fno-exceptions",
    "-frtti", "-fno-rtti", "-fpic", "-fPIC", "-fpie", "-fPIE", "-fno-pic", "-fno-pie",
    "-fopenmp", "-ffast-math", "-fno-fast-math", "-ffinite-math-only", "-fno-math-errno",
    "-ffreestanding", "-fhosted", "-fno-builtin", "-fsingle-precision-constant",
    "-fstack-protector", "-fstack-protector-all", "-fstack-protector-strong", "-fno-stack-protector",
    "-fgnu89-inline", "-fno-gnu89-inline", "-fchar8_t", "-fno-char8_t",
})
_PREDEFINE_F_PREFIXES = ("-fsanitize=",)

#: The path flags of the two-token form: the value is the next token.
#: GCC takes ``-specs file`` and ``--specs file`` too, not only the joined form.
_PATH_WITH_VALUE = frozenset({"--sysroot", "-isysroot", "-B", "-specs", "--specs"})

#: The path flags of the joined form (``-B<dir>``, ``--sysroot=<dir>``).
#: No prefix starts another one, thus the order of the test does not matter.
#: The bare two-token forms are in ``_PATH_WITH_VALUE`` and match first.
_JOINED_PATH_FLAGS = ("--sysroot=", "-isysroot", "--specs=", "-specs=", "-B")

#: Macros that clang defines itself, also under ``-undef``.  A ``-D`` for
#: one of them redefines a builtin and gives nothing but an error:
#: ``__FLT_EVAL_METHOD__`` of xtensa GCC 13 gave "redefining builtin macro"
#: in 213 units of one ESP-IDF build.
_CLANG_OWNED_MACROS = (
    "__STDC", "__cplusplus", "__STDCPP", "__has_include",
    "__FILE__", "__LINE__", "__DATE__", "__TIME__", "__TIMESTAMP__",
    "__COUNTER__", "__BASE_FILE__", "__INCLUDE_LEVEL__", "__FLT_EVAL_METHOD__",
)

#: Macro families that announce a type libclang 18 does not parse on the
#: proxy or on an older target.  libstdc++ switches to ``_Float32`` and
#: ``0.0bf16`` when it sees them, and every unit then stops with errors.
#: Measured with host g++ 16.2.1: 20 errors and the fatal "too many errors"
#: in a unit with 12 std headers.
_UNPARSABLE_TYPE_MACROS = (
    "__FLT16_", "__FLT32_", "__FLT64_", "__FLT128_", "__FLT32X_", "__FLT64X_", "__FLT128X_",
    "__BFLT16_", "__SIZEOF_FLOAT80__", "__SIZEOF_FLOAT128__",
)

#: The internal include directory of GCC: ``.../lib/gcc/<triple>/<version>/include``.
#: ``include-fixed`` is not matched, because it holds fixed C library headers.
_GCC_INTERNAL_INCLUDE = re.compile(r"[/\\]lib[/\\]gcc[/\\][^/\\]+[/\\][^/\\]+[/\\]include$")

#: The ACLE level that clang 18 predefines for an AArch32 target.  clang's
#: ``arm_acle.h`` only tests that the macro is defined, thus the value of
#: AArch64 (202420 in clang 22) gives the same header.
#: The ``arm_acle.h`` of clang stops with ``#error`` without it, and GCC 12
#: does not define it.
_CLANG_ARM_ACLE = "200"

_DEFINE_LINE = re.compile(r"^#define (?P<name>[A-Za-z_]\w*)(?P<params>\([^)]*\))?(?: (?P<value>.*))?$")


@dataclass(frozen=True)
class DriverInfo:
    """What one GCC driver says about a build.

    Attributes:
        triple: The target triple of the driver (``xtensa-esp32-elf``).
        include_dirs: The system include directories, in search order.
        macros: The predefined macros as (name with parameter list, value).
    """

    triple: str
    include_dirs: tuple[str, ...]
    macros: tuple[tuple[str, str], ...]

    def macro(self, name: str) -> str | None:
        """Give the value of one object-like macro, or None when it is not defined."""
        for defined, value in self.macros:
            if defined == name:
                return value
        return None


def is_gcc_driver_name(compiler: Path) -> bool:
    """Say if the file name of *compiler* is the name of a GCC driver."""
    return bool(_GCC_DRIVER_NAME.search(compiler.name))


def known_compilers(entries: Iterable[Mapping[str, object]]) -> dict[str, Path]:
    """Map the file name of each compiler to a path that an entry gives with a directory.

    WHY: one build can write the same compiler with and without its
    directory.  Measured on a PlatformIO ESP32 build: 70 of 116 entries
    name ``.../bin/xtensa-esp32-elf-g++``, and 46 name ``xtensa-esp32-elf-gcc``
    or ``xtensa-esp32-elf-g++`` alone.  That toolchain is not on ``PATH``.
    """
    found: dict[str, Path] = {}
    for entry in entries:
        token = compiler_token(entry)
        if token and ("/" in token or "\\" in token):
            found.setdefault(Path(token).name, Path(token))
    return found


def resolve_compiler(token: str, cwd: Path, known: Mapping[str, Path]) -> Path | None:
    """Give the absolute path of the compiler *token*, or None when no file matches.

    A bare name is looked up in this order:

    1. The same name with a directory, in another entry of the build.
    2. The same name in the directory of another compiler of the build, in
       entry order: a build that writes ``g++`` with a directory can write
       ``gcc`` without one, and the two are in one ``bin`` directory.
    3. ``PATH``.  The index then depends on the environment, and a warning
       says so once.

    The path is made absolute and normalized, but a symbolic link is NOT
    followed: a ccache masquerade link ``/usr/lib/ccache/bin/gcc`` must run
    as ``gcc``, while its target ``/usr/bin/ccache`` rejects ``-E``.
    """
    if "/" in token or "\\" in token:
        return _existing(Path(token), cwd)
    if token in known:
        found = _existing(known[token], cwd)
        if found is not None:
            return found
    for directory in dict.fromkeys(p.parent for p in known.values()):
        found = _existing(directory / token, cwd)
        if found is not None:
            return found
    on_path = shutil.which(token)
    if on_path is None:
        return None
    _warn_path_lookup(token, on_path)
    return Path(os.path.normpath(os.path.abspath(on_path)))


def _existing(path: Path, cwd: Path) -> Path | None:
    candidate = path if path.is_absolute() else cwd / path
    if not candidate.is_file():
        return None
    return Path(os.path.normpath(str(candidate)))


def predefine_flags(args: Sequence[str]) -> tuple[str, ...]:
    """Keep the flags of *args* that can change what the driver predefines or where it searches.

    The flags go to the query exactly as the build gives them, and the query
    runs in the directory of the entry, as the build does (see
    ``query_gcc_driver``).  Thus a relative path means the same file for
    the query as for the build.  These path flags change the answer:

    - ``--sysroot`` and ``-isysroot``: the root of the system headers.
    - ``-B``: the directory of ``cc1`` and of the startup files.
    - ``-specs`` and ``--specs``, joined or two tokens: a specs file can add
      include directories and macros.  Measured on a Zephyr build:
      ``-specs=picolibc.specs`` puts ``picolibc/include`` first; without it
      the query gave the newlib headers, which the build does not use.

    WHY the build value and no filter: the build runs the same driver with
    the same flags, and with the code of the repository, thus a filter on
    these flags protects nothing and costs a correct answer.

    Left out on purpose: ``-f`` flags outside ``_PREDEFINE_F_FLAGS`` (see
    there), and each flag that cannot change a predefine or a system
    directory (``-I``, ``-D``, warnings), which keeps the number of
    distinct queries small.
    """
    kept: list[str] = []
    path_flag = ""
    for token in args:
        if path_flag:
            kept += [path_flag, token]
            path_flag = ""
        elif token in _PATH_WITH_VALUE:
            path_flag = token
        elif token.startswith(_JOINED_PATH_FLAGS):
            kept.append(token)
        elif token.startswith("-f"):
            if token in _PREDEFINE_F_FLAGS or token.startswith(_PREDEFINE_F_PREFIXES):
                kept.append(token)
        elif token.startswith(_PREDEFINE_PREFIXES):
            kept.append(token)
    return tuple(kept)


def parse_verbose_output(stderr: str, cwd: Path) -> tuple[str, list[str]]:
    """Read the target triple and the ``#include <...>`` directories from ``gcc -v``.

    The directories come back resolved, because GCC writes them relative to
    its own binary (``bin/../lib/gcc/...``) and one directory must have one
    spelling in the index.

    *cwd* is the directory in which the driver ran.  GCC writes a directory
    of a relative flag as relative too: ``--sysroot=sr`` gives
    ``sr/usr/include``, ``-Btools/`` gives ``tools/include``.  Such a path is
    relative to *cwd*, not to the directory of this process.  Measured:
    resolved against this process it named ``/sr/usr/include``, which does
    not exist, and libclang stopped with "file not found".
    """
    triple = ""
    dirs: list[str] = []
    inside = False
    for line in stderr.splitlines():
        if line.startswith("Target: "):
            triple = line[len("Target: "):].strip()
        elif line.startswith("#include <...> search starts here"):
            inside = True
        elif line.startswith("End of search list"):
            inside = False
        elif inside and line.strip():
            # A macOS driver marks a framework directory; it is no include root.
            text = line.strip().removesuffix(" (framework directory)")
            dirs.append(str((cwd / text).resolve()))
    return triple, dirs


def parse_defines(stdout: str) -> list[tuple[str, str]]:
    """Read the ``#define`` lines of ``gcc -dM -E`` as (name with parameters, value).

    Left out: the macros that clang defines itself (``_CLANG_OWNED_MACROS``)
    and the macros of types that libclang cannot parse
    (``_UNPARSABLE_TYPE_MACROS``).
    """
    macros: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        match = _DEFINE_LINE.match(line)
        if match is None or match["name"].startswith(_CLANG_OWNED_MACROS + _UNPARSABLE_TYPE_MACROS):
            continue
        macros.append((match["name"] + (match["params"] or ""), match["value"] or ""))
    return macros


#: (compiler path, mtime, size, language, flags, working directory)
_QueryKey = tuple[str, int, int, str, tuple[str, ...], str]

#: Successful answers, keyed by the identity of the binary.
_QUERY_CACHE: dict[_QueryKey, DriverInfo] = {}

#: Failed queries and the time of the failure.  A failure is kept for a short
#: time only: without it, a compiler that always fails (a clang installed as
#: ``cc``) is queried twice for each unit — measured 37 ms a pair, thus 74 s
#: for a build of 2000 units.  Kept for good, a timeout under load would hold
#: for the life of an MCP server.
_FAILED_QUERIES: dict[_QueryKey, float] = {}
_FAILURE_TTL_S = 300.0


def clear_query_cache() -> None:
    """Forget every driver answer and failure (for tests, and after a toolchain update)."""
    _QUERY_CACHE.clear()
    _FAILED_QUERIES.clear()


def query_gcc_driver(compiler: Path, language: str, flags: tuple[str, ...], cwd: Path) -> DriverInfo | None:
    """Ask the GCC driver *compiler* for its triple, system directories and macros.

    *language* is ``"c"`` or ``"cpp"``, because the two have different
    directories (libstdc++) and different macros.  This function runs
    *compiler* in *cwd*, the directory of the entry, with *flags* as the
    build gives them: the same run as the build, thus a relative path in a
    flag means the same file.  Measured: a specs file of the build named
    ``-specs=my.specs`` stopped the query with "cannot read spec file" when
    the query ran in the directory of the compiler.

    The answer is kept per (path, mtime, size, language, flags, cwd) for
    the life of the process.  A toolchain update changes the binary, thus a
    long MCP server process asks again.  An edit of a file that a flag names
    (a specs file, a sysroot) does not change the key: a long MCP server
    process sees it after a restart of the client.  An index run is a
    process of its own and always asks again.  WHY not a stamp of those
    files: to find them, fw-context would have to repeat the search of GCC
    (``-B`` directories, the sysroot, the directories of the toolchain),
    which is a guess.  *cwd* is in the key, because a relative path in a
    flag depends on it; the measured builds name one to a few directories,
    thus few more queries.

    Returns None when the binary is not a GCC driver, does not run, or gives
    an answer that this function cannot read.  When *cwd* does not exist,
    the build directory is gone, and the query does not run (see
    ``_warn_missing_build_directory``).
    """
    try:
        status = compiler.stat()
    except OSError:
        return None
    if not cwd.is_dir():
        _warn_missing_build_directory(cwd)
        return None
    key = (str(compiler), status.st_mtime_ns, status.st_size, language, flags, str(cwd))
    if key in _QUERY_CACHE:
        return _QUERY_CACHE[key]
    failed_at = _FAILED_QUERIES.get(key)
    if failed_at is not None and time.monotonic() - failed_at < _FAILURE_TTL_S:
        return None
    info = _run_query(compiler, language, flags, cwd)
    if info is None:
        _FAILED_QUERIES[key] = time.monotonic()
    else:
        _FAILED_QUERIES.pop(key, None)
        _QUERY_CACHE[key] = info
    return info


def _run_query(compiler: Path, language: str, flags: tuple[str, ...], cwd: Path) -> DriverInfo | None:
    lang = "c++" if language == "cpp" else "c"
    base = [str(compiler), *flags, "-x", lang]
    try:
        verbose = _run_driver([*base, "-E", "-v", "-o", os.devnull, "-"], cwd)
        defines = _run_driver([*base, "-dM", "-E", "-"], cwd)
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warning("Cannot query the compiler %s for its system headers: %s", compiler, error)
        return None
    if verbose.returncode != 0 or defines.returncode != 0:
        log.warning(
            "The compiler %s did not answer the system-header query (exit %d/%d): %s",
            compiler, verbose.returncode, defines.returncode, (verbose.stderr or defines.stderr)[-300:],
        )
        return None
    triple, dirs = parse_verbose_output(verbose.stderr, cwd)
    macros = parse_defines(defines.stdout)
    names = {name for name, _ in macros}
    # A clang that is installed as `cc` or `gcc` also answers both queries.
    # Its directories belong to ITS resource dir, which is not the one of
    # the libclang that parses, thus only a real GCC is used.
    if "__GNUC__" not in names or "__clang__" in names or not triple:
        log.debug("The compiler %s is not a GCC driver; its answer is not used", compiler)
        return None
    return DriverInfo(triple=triple, include_dirs=tuple(dirs), macros=tuple(macros))


def _run_driver(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one driver query.

    An argv list and no shell.  The input is an empty stdin (``-``), which
    works on every host — Windows has no /dev/null.  *cwd* is the directory
    of the entry, in which the build runs the same compiler.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=_QUERY_TIMEOUT_S, check=False,
        stdin=subprocess.DEVNULL, cwd=str(cwd),
    )


@lru_cache(maxsize=64)
def libclang_supports(triple: str) -> bool:
    """Say if the libclang of this process can parse for the target *triple*.

    WHY a probe and not a list of names: the list missed ``avr``, because
    ``avr-g++`` gives the triple ``avr`` and the list held the prefix
    ``avr-``.  The probe asks the library that does the parse.
    """
    from clang import cindex

    try:
        cindex.Index.create().parse(
            "probe.c", args=[f"--target={triple}"], unsaved_files=[("probe.c", "")],
        )
    except cindex.TranslationUnitLoadError:
        return False
    return True


def clang_resource_include() -> str | None:
    """Give the clang compiler headers of the libclang version, or None (see ``_clang_resource``).

    WHY: the libclang wheel has no resource directory, thus the compiler
    headers came from the internal include directory of GCC, whose
    intrinsic headers are written for GCC builtins: the ``arm_acle.h`` of
    ARM GCC 12 gave 2574 errors in one STM32 build.  The headers must be
    of the libclang major version: newer ones use types that libclang does
    not know.  The lookup runs on each call (no cache), thus a long MCP
    process does not keep a directory that an upgrade removed.
    """
    from fw_context_mcp.indexer._clang_resource import find_resource_include, libclang_major

    found = find_resource_include()
    if found is None:
        _warn_no_resource(libclang_major())
        return None
    return str(found.include)


def _system_dirs(info: DriverInfo) -> tuple[list[str], list[str]]:
    """Give the system directories of *info*, with the GCC internal one replaced by clang's.

    Returns the directories and the replaced GCC directories.  A replaced
    directory goes to ``-idirafter`` (searched last), because it also holds
    headers that clang has not: ``stdfix.h`` and ``gcov.h`` of GCC 12 and
    8.4.  Measured: the ``-idirafter`` changed no error count on STM32,
    ESP32, ESP-IDF and PlatformIO builds.  Without a clang resource
    directory, the directories stay as the driver gives them.
    """
    resource = clang_resource_include()
    if resource is None:
        return list(info.include_dirs), []
    dirs: list[str] = []
    replaced: list[str] = []
    for directory in info.include_dirs:
        if _GCC_INTERNAL_INCLUDE.search(directory):
            dirs.append(resource)
            replaced.append(directory)
        else:
            dirs.append(directory)
    return dirs, replaced


@cache
def _warn_no_resource(major: int | None) -> None:
    log.warning(
        "No clang %s compiler headers are installed, thus a GCC build parses with the "
        "compiler headers of GCC, and some of them do not parse in libclang (arm_acle.h). "
        "Run `fw-context doctor --fix` to install them.",
        major if major is not None else "(unknown version)",
    )


def proxy_target(info: DriverInfo) -> tuple[str, list[str]] | None:
    """Choose a target that libclang supports and that has the type sizes of *info*.

    Returns the triple and the flags that make its data model equal to the
    real one, or None when no x86 data model matches (for example a 16-bit
    ``int``).

    The flags, measured against xtensa-esp32-elf-gcc 8.4.0 with 17
    ``_Static_assert`` checks that the real compiler passes:

    - ``-mlong-double-64``: i386 has a 12-byte ``long double``.
    - ``-malign-double``: i386 aligns a ``double`` and a ``long long`` in a
      struct to 4 bytes, and an embedded ABI aligns them to 8.
    """
    if info.macro("__SIZEOF_INT__") != "4":
        return None
    pointer = info.macro("__SIZEOF_POINTER__")
    if pointer == "4":
        triple, flags = "i386-unknown-elf", ["-malign-double"]
    elif pointer == "8" and info.macro("__SIZEOF_LONG__") == "8":
        triple, flags = "x86_64-unknown-elf", []
    else:
        return None
    long_double = {"8": "-mlong-double-64", "12": "-mlong-double-80", "16": "-mlong-double-128"}
    size = info.macro("__SIZEOF_LONG_DOUBLE__")
    if size in long_double:
        flags.append(long_double[size])
    return triple, flags


def libclang_args(info: DriverInfo, args: Sequence[str]) -> list[str]:
    """Build the libclang flags for one unit from the driver answer and the unit flags.

    *args* are the unit flags after ``normalize_args`` without a target.
    The result is:

    ``--target=<T> -nostdinc -undef <data-model flags> -ferror-limit=0 -D<macros> <args> -isystem <dirs>``

    - ``-nostdinc`` removes the host directories, which are the fault this
      module removes.
    - ``-undef`` removes the macros of the parsing target; the macros of
      the driver replace them.  They come before *args*, thus a ``-D`` or
      ``-U`` of the unit still wins.
    - ``-ferror-limit=0``: libclang stops at 20 errors with a fatal error,
      and the declarations after that point never reach the AST.  A header
      of a newer GCC can give that many errors in libclang.
    - The ``-isystem`` directories come last and in the driver order, thus
      an ``-I`` of the unit is searched first, as GCC does.  The internal
      include directory of GCC is replaced with the one of clang when a
      clang is installed (see ``clang_resource_include``), at the same
      place in the order, as the clang driver does with a GCC toolchain.
    """
    model: list[str] = []
    if libclang_supports(info.triple):
        target = info.triple
        unit_args = list(args)
    else:
        proxy = proxy_target(info)
        if proxy is None:
            _warn_no_proxy(info.triple)
            target = ""
        else:
            target, model = proxy
            _log_proxy_once(info.triple, target)
        # The -m flags belong to the real target, which the parse does not use.
        unit_args = [a for a in args if not a.startswith("-m")]
    result = [f"--target={target}"] if target else []
    result += ["-nostdinc", "-undef", *model, "-ferror-limit=0"]
    if info.macro("__SIZEOF_WCHAR_T__") == "2":
        result.append("-fshort-wchar")
    if info.macro("__cpp_sized_deallocation") is not None:
        # GCC turns sized deallocation on from C++14, and libstdc++ then
        # calls the sized operator delete.  clang 18 has it off by default,
        # and each such call was an error ("selects non-usual deallocation
        # function"): 55 in one ESP-IDF build with GCC 13.
        result.append("-fsized-deallocation")
    result.append("-funsigned-char" if info.macro("__CHAR_UNSIGNED__") is not None else "-fsigned-char")
    result += [f"-D{name}={value}" for name, value in info.macros]
    dirs, replaced = _system_dirs(info)
    if replaced and info.macro("__ARM_ARCH") is not None and info.macro("__ARM_ACLE") is None:
        # clang's arm_acle.h needs the macro that clang itself predefines.
        result.append(f"-D__ARM_ACLE={_CLANG_ARM_ACLE}")
    result += unit_args
    for directory in dirs:
        result += ["-isystem", directory]
    for directory in replaced:
        result += ["-idirafter", directory]
    return result


@cache
def _warn_missing_build_directory(directory: Path) -> None:
    """Say once that the build directory of compile_commands.json is gone.

    WHY no query in another directory: a relative flag would then name
    another file, and the answer would be a guess.  The build of the project
    makes the directory again.
    """
    log.warning(
        "The build directory %s of compile_commands.json does not exist, thus fw-context "
        "cannot ask the compiler for its system headers; the units of that directory get "
        "no answer of the compiler. Run 'fw-context index --build'.",
        directory,
    )


@cache
def _warn_path_lookup(token: str, found: str) -> None:
    log.warning(
        "compile_commands.json names the compiler %s without a directory; fw-context found %s "
        "through PATH. An environment with another PATH gives other flags and another build.",
        token, found,
    )


@cache
def _warn_no_proxy(triple: str) -> None:
    log.warning(
        "libclang cannot parse for %s, and no proxy target has its type sizes; "
        "the parse uses the host target with the headers and macros of %s",
        triple, triple,
    )


@cache
def _log_proxy_once(triple: str, proxy: str) -> None:
    log.info("libclang has no backend for %s; the parse uses %s, which has the same type sizes", triple, proxy)


#: The first word of a ``command`` field when it needs no shell parsing: a
#: word with no quote and no backslash, or one quoted word with no
#: backslash.  The separators are those of ``shlex`` in POSIX mode.
_SIMPLE_FIRST_WORD = re.compile(r"""[ \t\r\n]*(?:"([^"\\]*)"|'([^']*)'|([^ \t\r\n"'\\]+))(?:[ \t\r\n]|\Z)""")


def compiler_token(entry: Mapping[str, object]) -> str:
    """Give the first word of an entry (the compiler), or "" when the entry names none.

    WHY not always ``shlex.split``: a ``command`` field of a large build
    holds hundreds of flags, and the split of the whole line costs more than
    the rest of the work on the entry.  Only the first word is necessary.
    The regex gives the same word as ``shlex`` for the simple forms; a form
    with an escape or a joined quote goes to ``shlex``, which can raise
    ``ValueError`` on an unclosed quote.
    """
    arguments = entry.get("arguments")
    if isinstance(arguments, list) and arguments:
        return str(arguments[0])
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return ""
    simple = _SIMPLE_FIRST_WORD.match(command)
    if simple:
        return next(group for group in simple.groups() if group is not None)
    return shlex.split(command)[0]
