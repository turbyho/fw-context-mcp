"""Find the link inputs of a PlatformIO build through `pio run -t envdump`.

SCons runs the link of a PlatformIO build and writes no file that records
the link command: no ninja file, no `link.txt`, no response file.  The
`envdump` target prints the SCons construction environment of each
environment of the project instead.  Two of its variables make the link
command:

* `LINKFLAGS` holds the `-T`, `--default-script` and `--defsym` options.
* `LIBPATH` holds the `-L` directories, in which `ld` finds a script that
  `-T` names without a directory.

Measured on an STM32 and an ESP32 project: a forced relink with `pio run -v`
printed a link line whose options are the `LINKFLAGS` tokens, one for one.
Thus these tokens are what the linker receives, and not a guess.

The dump is a `pprint` of a Python dict, not JSON.  Most values are SCons
objects, which have no literal form.  This module reads only the values it
needs, and only when each one is a literal.  A value that is not a literal
gives nothing, because `_linker` forbids an answer the build did not state.

The build backend reads the dump once, in `build()`, and keeps the result
in a sidecar file in the fw-context build directory.  WHY not run `envdump`
from the reader: the reader gets no `BuildConfig`, thus it knows neither the
PlatformIO Python nor the isolated build directory, and each `pio run` also
runs the `extra_scripts` of the project.

The sidecar holds one entry for each pair of build variant and SHA-256 of
the compilation database.  WHY a pair: the variants of one project build
before the first one is indexed, and each variant has its own database.
WHY the hash: a build that stops after the sidecar write and before the
database is published leaves an entry that no database matches, and the
entry of the published database stays valid.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from fw_context_mcp.utils import clear_dead_temporaries, owner_token

from . import _link_command
from ._linker import _output_paths

log = logging.getLogger(__name__)

SIDECAR_NAME = "platformio_link.json"

# Change this number when the meaning of a sidecar field changes.  A reader
# that finds another number uses nothing, and the next build writes the file
# again.  Format 3 adds `unknown`: format 2 dropped a token that did not
# expand, and its entries cannot tell a harmless one from a lost script.
SIDECAR_FORMAT = 3

# The sidecar keeps this many entries, the newest ones.  A project with more
# variants than this loses the link record of the oldest variant, which
# gives no memory map for it, not a wrong one.
_MAX_ENTRIES = 32

# `pio run` prints one such line before the output of each environment.
_ENV_HEADER = re.compile(r"^Processing (\S+) \(", re.MULTILINE)

# A terminal colour sequence.  `PLATFORMIO_NO_ANSI` turns them off, and this
# pattern removes one that a wrapper adds anyway.  Measured: with
# `PLATFORMIO_FORCE_ANSI=true` each line of the dump starts with `\x1b[0m`.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# SCons substitutes `$NAME` and `${NAME}`.  Other forms, such as
# `${TEMPFILE(...)}` or `${_concat(...)}`, call a function, and this module
# does not evaluate them.
_VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")

# Longest chain of variables that expand to other variables.  The measured
# chain is `BUILD_DIR -> PROJECT_BUILD_DIR, PIOENV`, two levels.  The limit
# stops a cycle.
_MAX_EXPANSION_DEPTH = 8

@dataclass(frozen=True)
class EnvLink:
    """The link inputs of one PlatformIO environment, with variables expanded.

    Attributes:
        linkflags: The `LINKFLAGS` tokens.  A token that holds a variable
            this module cannot expand is None.  It stays in the list,
            because `-T` and its value are two tokens, and a removed token
            would join `-T` to the wrong value.  Such a token is harmless
            only when it cannot name a link input, see `unknown`.
        libpath: The `LIBPATH` directories, in their order.  An entry that
            does not expand is None, and the search stops there: `ld` looks
            in that directory, and this module does not know its name.
        cwd: The directory the link runs in, `PROJECT_DIR`.
        build_dir: `BUILD_DIR`, where the object files of this environment
            go.  The reader uses it to find which environment the
            compilation database belongs to.
        unknown: Why the link of this environment cannot be read, or ""
            when it can.  Examples: a token that did not expand and can be
            a script, a `LIBPATH` that is not a list of strings, a dump that
            holds no dict of this environment.  The environment stays in the
            result with this reason, and does not disappear from it: an
            environment that disappears lets `select_env` give the one that
            is left to units that belong to the lost one.
    """

    linkflags: list[str | None] = field(default_factory=list)
    libpath: list[str | None] = field(default_factory=list)
    cwd: str = ""
    build_dir: str = ""
    unknown: str = ""


# ── envdump ────────────────────────────────────────────────────────────────


def parse_envdump(text: str) -> dict[str, EnvLink]:
    """Return the link inputs of each environment in the output of `envdump`.

    Each environment that has a `Processing` line is in the result.  An
    environment whose link cannot be read has a reason in `EnvLink.unknown`,
    see there why it is not left out.
    """
    text = _ANSI.sub("", text)
    headers = list(_ENV_HEADER.finditer(text))
    result: dict[str, EnvLink] = {}
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        name = header.group(1)
        entries = _environment_dict(text[header.end():end], name)
        if entries is None:
            result[name] = EnvLink(unknown="the dump holds no dict of this environment")
            continue
        result[name] = _env_link(entries)
    return result


def _environment_dict(block: str, env: str) -> dict[str, str] | None:
    """Return the entries of the dump dict of *env* in *block*, or None.

    The dict of the environment is the LAST dict at the start of a line
    whose `PIOENV` is *env*.  An `extra_script` can print another dict
    first, for example `env.Dump()` before the platform adds its link
    options, and the first dict would then give a nearly empty `LINKFLAGS`.
    """
    found: dict[str, str] | None = None
    for match in re.finditer(r"^\{", block, re.MULTILINE):
        entries = _top_level_entries(block, match.start())
        if entries is not None and _literal(entries.get("PIOENV")) == env:
            found = entries
    return found


def _env_link(entries: dict[str, str]) -> EnvLink:
    """Return the link inputs from the raw entries of one environment.

    SCons always defines `LINKFLAGS`, thus a missing or unreadable value is
    an unknown link and not an empty one.  A missing `LIBPATH` is an empty
    list, and a `LIBPATH` that is not a list of strings is unknown: without
    it, a script that `-T` names with no directory is found in another one.
    """
    variables: dict[str, str] = {}
    for name, raw in entries.items():
        value = _literal(raw)
        if isinstance(value, str):
            variables[name] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            # SCons gives the decimal text of an int.  The PlatformIO core
            # defines UNIX_TIME=int(time()), and the Teensy platform puts
            # `--defsym=__rtc_localtime=$UNIX_TIME` in LINKFLAGS.  A bool
            # gives `True`, which no link option takes, thus it stays out.
            variables[name] = str(value)
    cwd = expand("$PROJECT_DIR", variables) or ""
    build_dir = expand("$BUILD_DIR", variables) or ""

    raw_flags = _string_list(entries.get("LINKFLAGS"))
    if raw_flags is None:
        return EnvLink(cwd=cwd, build_dir=build_dir, unknown="LINKFLAGS is missing or is not a list of strings")
    linkflags = [expand(token, variables, one_word=True) for token in raw_flags]
    raw_libpath = _string_list(entries.get("LIBPATH")) if "LIBPATH" in entries else []
    if raw_libpath is None:
        return EnvLink(linkflags=linkflags, cwd=cwd, build_dir=build_dir,
                       unknown="LIBPATH is not a list of strings")
    return EnvLink(
        linkflags=linkflags,
        libpath=[expand(directory, variables) for directory in raw_libpath],
        cwd=cwd,
        build_dir=build_dir,
        unknown=_link_command._lost_link_input(raw_flags, linkflags),
    )


def run_environments(config_json: str) -> list[str] | None:
    """Return the environments that `pio run` builds, or None.

    *config_json* is the output of `pio project config --json-output`: a
    list of `[section, [[option, value], ...]]`.  `pio run` with no `-e`
    builds the `default_envs` of the `platformio` section, or every
    `env:<name>` section when that option is not set.  None when the output
    has another shape.
    """
    try:
        sections = json.loads(config_json)
    except ValueError:
        return None
    if not isinstance(sections, list):
        return None
    names: list[str] = []
    default_envs: object = None
    for item in sections:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
            return None
        section, options = item
        if section.startswith("env:"):
            names.append(section[len("env:"):])
        elif section == "platformio" and isinstance(options, list):
            for option in options:
                if isinstance(option, list) and len(option) == 2 and option[0] == "default_envs":
                    default_envs = option[1]
    if default_envs:
        if isinstance(default_envs, str):
            default_envs = [name.strip() for name in default_envs.split(",") if name.strip()]
        if not (isinstance(default_envs, list) and all(isinstance(name, str) for name in default_envs)):
            return None
        return list(default_envs)
    return names


def _literal(raw: str | None) -> object:
    """Return the Python value of *raw*, or None when it is not a literal.

    The text goes in parentheses first.  `pprint` splits a long string into
    two adjacent literals, and below the top level it writes no parentheses
    around them.  Measured: a `PROJECT_BUILD_DIR` with a space in the path.
    Two adjacent literals in parentheses are one string, as in Python code.
    """
    if raw is None:
        return None
    try:
        return ast.literal_eval(f"({raw.strip()})")
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _string_list(raw: str | None) -> list[str] | None:
    """Return *raw* as a list of strings, or None.

    A string value is split on white space, which is what SCons does for a
    `CLVar` such as `LINKFLAGS`.
    """
    value = _literal(raw)
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list | tuple) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _top_level_entries(block: str, start: int) -> dict[str, str] | None:
    """Return the raw text of each key of the dict that opens at *start*.

    The scan follows the quotes and the brackets, thus a comma or a colon in
    a string or in a nested value does not end an entry.  A value is kept as
    text, because most values are SCons objects that `ast` cannot read.
    """
    entries: dict[str, str] = {}
    key: str | None = None
    piece_start = start + 1
    for index, char in _structural_chars(block, start + 1):
        if char == ":" and key is None:
            parsed = _literal(block[piece_start:index])
            key = parsed if isinstance(parsed, str) else None
            if key is None:
                return entries
            piece_start = index + 1
        elif char in ",}":
            if key is not None:
                entries[key] = block[piece_start:index]
            key = None
            piece_start = index + 1
            if char == "}":
                return entries
    return entries


def _structural_chars(text: str, start: int) -> Iterator[tuple[int, str]]:
    """Yield each `:`, `,` and `}` at the top level of a dict body.

    *start* is the offset after the opening brace.  A quoted string can hold
    a backslash escape, as `pprint` writes it.
    """
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "}":
            if depth == 0:
                yield index, char
                return
            depth -= 1
        elif char in ":," and depth == 0:
            yield index, char
        index += 1


def expand(
    token: str, variables: dict[str, str], depth: int = 0, *, one_word: bool = False,
) -> str | None:
    """Return *token* with each `$NAME` and `${NAME}` replaced, or None.

    Only a variable whose value is a string expands.  A missing variable, a
    variable whose value is a list or an object, a function call, and a
    chain deeper than `_MAX_EXPANSION_DEPTH` give None.  `$$` is a literal
    dollar sign in SCons.

    *one_word* is for a token of a command line, such as `LINKFLAGS`.  SCons
    splits the value of a variable there at white space and ignores quotes,
    and the shell then joins the quoted words again.  Thus `$EXTRA` with the
    value `-mcpu=m4 -Tx.ld` gives two arguments, and `-T"${BUILD_DIR}/x.ld"`
    with a space in `BUILD_DIR` gives one.  The expanded token must give
    exactly one shell word, or the result is None, and the caller decides
    if the lost token can matter.  A token with no variable is one argument
    as it is: SCons does not split an item of a list.  A directory of
    `LIBPATH` is not split, because SCons makes a directory node of each
    entry.
    """
    if "$" not in token:
        return token
    if depth > _MAX_EXPANSION_DEPTH:
        return None
    expanded_parts = []
    for part in token.split("$$"):
        pieces: list[str] = []
        last = 0
        for match in _VARIABLE.finditer(part):
            value = variables.get(match.group(1) or match.group(2))
            if value is None:
                return None
            inner = expand(value, variables, depth + 1)
            if inner is None:
                return None
            pieces.append(part[last:match.start()])
            pieces.append(inner)
            last = match.end()
        pieces.append(part[last:])
        text = "".join(pieces)
        if "$" in text:
            # A `$` that is not a variable name, such as `${TEMPFILE(`.
            return None
        expanded_parts.append(text)
    result = "$".join(expanded_parts)
    if one_word and _shell_word_count(result) != 1:
        return None
    return result


def _shell_word_count(text: str) -> int | None:
    """Return how many arguments the shell makes of *text*, or None when it cannot parse it."""
    words = _link_command.shell_words(text)
    return len(words) if words is not None else None


# ── Link of one environment ────────────────────────────────────────────────


def search_dirs(link: EnvLink) -> list[Path | None]:
    """Return the directories in which `ld` looks for a script of *link*, in its order.

    See `_link_command.search_dirs`: `LIBPATH` comes between the driver `-L`
    options and the `-Wl,` ones.
    """
    return _link_command.search_dirs(link.linkflags, link.libpath)


def resolve_scripts(link: EnvLink, project_root: Path) -> list[Path] | None:
    """Return the linker scripts that `ld` reads for *link*, or None.

    None when the link of the environment cannot be read (`EnvLink.unknown`),
    and in each case of `_link_command.resolve_link_scripts`.  The link runs
    in `PROJECT_DIR`, and in *project_root* when the dump does not state it.
    """
    if link.unknown:
        log.info("linker script: the PlatformIO link cannot be read: %s", link.unknown)
        return None
    cwd = Path(link.cwd) if link.cwd else project_root
    return _link_command.resolve_link_scripts(link.linkflags, link.libpath, cwd)


# ── Sidecar ────────────────────────────────────────────────────────────────


def sidecar_path(compile_commands_dir: Path) -> Path:
    """Return the path of the sidecar file in *compile_commands_dir*."""
    return compile_commands_dir / SIDECAR_NAME


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of the content of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _env_to_json(link: EnvLink) -> dict:
    return {
        "linkflags": link.linkflags,
        "libpath": link.libpath,
        "cwd": link.cwd,
        "build_dir": link.build_dir,
        "unknown": link.unknown,
    }


def _read_entries(target: Path) -> list[dict]:
    """Return the entries of the sidecar in *target*, or an empty list."""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.info("linker script: cannot read %s", target, exc_info=True)
        return []
    if not isinstance(payload, dict) or payload.get("format") != SIDECAR_FORMAT:
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def record_link(
    target: Path,
    variant: str,
    compile_commands: Path,
    envs: dict[str, EnvLink] | None,
) -> None:
    """Record *envs* for *variant* and the database *compile_commands*.

    None removes the entry of that pair.  A build whose dump failed must
    remove it: a change of the link alone keeps the hash of the database,
    and the old entry would give the old link to the new build.  When the
    file cannot be written, the function removes the whole file, which only
    costs the memory maps of the other variants until their next build.

    The write is atomic.  The temporary name starts with a dot and holds the
    owner token (see `utils.owner_token`), so that two processes never
    share it.
    """
    sha = file_sha256(compile_commands)
    entries = [
        entry for entry in _read_entries(target)
        if not (entry.get("variant") == variant and entry.get("cc_sha256") == sha)
    ]
    if envs is not None:
        entries.append({
            "variant": variant,
            "cc_sha256": sha,
            "envs": {name: _env_to_json(link) for name, link in sorted(envs.items())},
        })
    payload = {"format": SIDECAR_FORMAT, "entries": entries[-_MAX_ENTRIES:]}
    temporary = target.with_name(f".{target.stem}.{owner_token()}{target.suffix}")
    if target.parent.is_dir():
        # A build that SIGKILL stopped left its temporary file.
        clear_dead_temporaries(target.parent, target.stem, target.suffix)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        log.warning("Cannot write %s, thus it is removed: %s", target, exc)
        forget(target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.debug("cannot remove %s", temporary, exc_info=True)


def forget(target: Path) -> None:
    """Remove the sidecar file.  A failure goes to the log.

    A build that fw-context does not run through `build()`, such as a
    `[build] command`, calls this: its link is not recorded, and an old
    entry for the same database would describe another link.
    """
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Cannot remove %s: %s", target, exc)


def read_link(target: Path, variant: str, compile_commands: Path | None) -> dict[str, EnvLink] | None:
    """Return the environments that *target* records for this build, or None.

    None when the database or the entry is missing, or when a field has a
    wrong type.  Each case is a missing answer and not an error: the index
    run continues, and the stored memory map stays.

    One environment with a wrong field makes the whole entry None.  Without
    it, `select_env` sees one environment fewer, and with no unit that names
    an object file it gives the environment that is left.
    """
    if compile_commands is None:
        return None
    try:
        sha = file_sha256(compile_commands)
    except OSError:
        log.info("linker script: cannot read %s", compile_commands)
        return None
    entries = _read_entries(target)
    for entry in reversed(entries):
        if entry.get("variant") != variant or entry.get("cc_sha256") != sha:
            continue
        envs = entry.get("envs")
        if not isinstance(envs, dict):
            return None
        result: dict[str, EnvLink] = {}
        for name, raw in envs.items():
            link = _env_from_json(raw)
            if link is None:
                log.info("linker script: %s has a broken entry for %r, thus it is not used", target, name)
                return None
            result[name] = link
        return result
    log.info(
        "linker script: %s records no link for this %s — run 'fw-context index "
        "--build' to record it", SIDECAR_NAME, compile_commands.name,
    )
    return None


def _env_from_json(raw: object) -> EnvLink | None:
    """Return one sidecar environment, or None when a field has a wrong type."""
    if not isinstance(raw, dict):
        return None
    flags = raw.get("linkflags")
    libpath = raw.get("libpath")
    cwd = raw.get("cwd")
    build_dir = raw.get("build_dir")
    unknown = raw.get("unknown")
    if not (
        isinstance(flags, list)
        and all(item is None or isinstance(item, str) for item in flags)
        and isinstance(libpath, list)
        and all(item is None or isinstance(item, str) for item in libpath)
        and isinstance(cwd, str)
        and isinstance(build_dir, str)
        and isinstance(unknown, str)
    ):
        return None
    return EnvLink(linkflags=list(flags), libpath=list(libpath), cwd=cwd, build_dir=build_dir, unknown=unknown)


# ── Environment of the index ───────────────────────────────────────────────


def select_env(envs: dict[str, EnvLink], units: list | None, project_root: Path) -> str | None:
    """Return the environment whose object files the units name, or None.

    A build of fw-context records the one environment that its variant
    builds, but a sidecar can hold more: a database that the user gives
    explicitly, built for several environments in one `pio run`, holds the
    LAST environment only (measured with two).  The first environment, or
    `default_envs`, would then give the memory map of another chip.  The
    object file path of a unit is in the build directory of its
    environment, and that is the evidence here.

    Every object file that the units name must be in the build directory of
    one and the same environment.  With no unit that names an object file,
    a sidecar with one environment gives that environment.  An object file
    in no recorded build directory gives None: that unit comes from a build
    that the sidecar does not describe.
    """
    counts: Counter[str] = Counter()
    named = False
    outside = 0
    for unit in units or []:
        base = Path(getattr(unit, "directory", None) or project_root)
        for target in _output_paths(unit):
            named = True
            path = Path(target)
            if not path.is_absolute():
                path = base / path
            matched = [
                name for name, link in envs.items()
                if link.build_dir and _is_inside(path, Path(link.build_dir))
            ]
            counts.update(matched)
            outside += not matched
    if outside:
        log.info(
            "linker script: %d object file(s) are in no build directory of a "
            "recorded PlatformIO environment, thus none is used", outside,
        )
        return None
    if len(counts) == 1:
        return next(iter(counts))
    if not named and len(envs) == 1:
        return next(iter(envs))
    if counts:
        log.info(
            "linker script: the units name the build directories of %d "
            "PlatformIO environments (%s), thus none is used",
            len(counts), ", ".join(sorted(counts)),
        )
    return None


def _is_inside(path: Path, directory: Path) -> bool:
    """Say if *path* is in *directory*, with no access to the disk."""
    try:
        Path(os.path.normpath(path)).relative_to(os.path.normpath(directory))
    except ValueError:
        return False
    return True
