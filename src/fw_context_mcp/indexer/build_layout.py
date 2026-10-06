"""Where fw-context puts the output of each build of a project.

Every build that fw-context runs writes into one tree that fw-context owns:

    <project>/.fw-context/build/
        .gitignore                  "*" — nothing here belongs in a commit
        <variant>/                  one run of the build system
            out/                    the output directory of the build system
            <sidecar files>         files that fw-context writes for this build

``<variant>`` is the name of a ``[[build.variants]]`` entry, or
``DEFAULT_VARIANT`` for a project without variants.  The build system owns
``out/``: a pristine build, a clean, or a PlatformIO checksum change can
delete it as a whole.  Thus a file that fw-context writes for a build sits
beside ``out/``, never in it.

WHY one tree that fw-context owns, and not the directory of the user:

* A build of the user (an IDE, ``pio run``, a CI script) writes into its own
  directory.  When fw-context reads from the same directory, that build
  changes the input of the index without a word.
* The check for a missing build becomes the same for each build system: the
  output directory of the build exists, or it does not.  The ``directory``
  field of ``compile_commands.json`` names the project root for PlatformIO,
  Mbed OS, Arduino and Makefile builds, thus a check of that field cannot
  see a deleted build of those systems.
* Two variants never share one output directory, thus they cannot overwrite
  each other.

A program that one run produces (a Zephyr sysbuild image, the ESP-IDF
bootloader) is a subdirectory of ``out/``, as the build system makes it.  The
backend says where its compilation databases are, see
``builders.output_compile_commands``.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The directory that holds every build of one project, relative to its root.
BUILD_ROOT_REL: Path = Path(".fw-context") / "build"

# The variant name of a project that declares no ``[[build.variants]]``.
DEFAULT_VARIANT: str = "default"

# The name of the output directory of the build system, in each variant.
OUT_DIR_NAME: str = "out"

# The file name of a compilation database.
COMPILE_COMMANDS_NAME: str = "compile_commands.json"

# Characters that a variant name cannot hold, because the name is a directory
# name on Linux, macOS and Windows.  "/" and "\" would add a level to the
# path, and Windows refuses the others in a file name.
_FORBIDDEN_CHARACTERS: frozenset[str] = frozenset('/\\:*?"<>|')

# Device names that Windows reserves in each directory, with or without an
# extension and in any case: a directory "nul" or "COM1.dev" cannot exist.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


class InvalidVariantName(ValueError):
    """A variant name that cannot be the name of a directory."""


def validate_variant_name(name: str) -> None:
    """Raise ``InvalidVariantName`` when *name* cannot be a variant directory.

    The rules come from the file systems that fw-context supports:

    * The name is not empty, and it is not ``.`` or ``..``.
    * The name holds no path separator and no character that Windows
      refuses in a file name.
    * The name holds no control character.
    * The name does not start with ``.``: ``.gitignore`` and the temporary
      files of an atomic write use that form in the same directory.
    * The name does not end with ``.`` or a space, and it is not a device
      name that Windows reserves (``CON``, ``NUL``, ``COM1`` …): Windows
      removes the end or refuses the name.
    """
    if not name:
        raise InvalidVariantName("a variant name cannot be empty")
    if name in {".", ".."} or name.startswith("."):
        raise InvalidVariantName(f"variant name {name!r} cannot start with '.'")
    bad = sorted({char for char in name if char in _FORBIDDEN_CHARACTERS or ord(char) < 32})
    if bad:
        shown = ", ".join(repr(char) for char in bad)
        raise InvalidVariantName(f"variant name {name!r} holds a character that a directory name cannot hold: {shown}")
    if name.endswith((".", " ")):
        raise InvalidVariantName(f"variant name {name!r} cannot end with '.' or a space")
    if name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise InvalidVariantName(f"variant name {name!r} is a device name that Windows reserves")


def check_variant_names(names: Iterable[str]) -> None:
    """Raise ``InvalidVariantName`` for a bad name or for two names of one directory.

    Two names that differ only in case are one directory on the
    case-insensitive file systems of macOS and Windows, thus the two
    variants would write into one output directory there.
    """
    seen: dict[str, str] = {}
    for name in names:
        validate_variant_name(name)
        key = name.casefold()
        if key in seen and seen[key] != name:
            raise InvalidVariantName(
                f"variant names {seen[key]!r} and {name!r} differ only in case, thus they "
                "share one directory on macOS and Windows"
            )
        seen[key] = name


@dataclass(frozen=True, slots=True)
class BuildLayout:
    """The build directories of the project at *project_root*.

    The methods only compute paths.  ``out_dir`` and ``variant_dir`` create
    nothing, because a reader asks for them too, and a reader must not make
    a directory that says "a build was here".
    """

    project_root: Path

    @property
    def root(self) -> Path:
        """Return ``<project>/.fw-context/build``."""
        return self.project_root / BUILD_ROOT_REL

    def variant_dir(self, variant: str) -> Path:
        """Return the directory of one build; "" names the build without variants.

        Raises ``InvalidVariantName`` for a name that cannot be a directory.
        """
        name = variant or DEFAULT_VARIANT
        validate_variant_name(name)
        return self.root / name

    def out_dir(self, variant: str) -> Path:
        """Return the output directory that the build system of *variant* writes."""
        return self.variant_dir(variant) / OUT_DIR_NAME

    def ensure_ignored(self) -> None:
        """Keep the build output out of the git status of the project.

        ``fw-context init`` writes ``**/.fw-context/*`` into the
        ``.gitignore`` of the project, but a project that was initialised
        before that rule, or by hand, can lack it.  A ``.gitignore`` INSIDE
        the build tree covers that case without a change to a file of the
        user.  ``*`` also hides this ``.gitignore`` itself.

        A failure costs only an untidy git status, thus it is logged and the
        build continues.
        """
        marker = self.root / ".gitignore"
        if marker.exists():
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("# Build output of fw-context. Not for committing.\n*\n", encoding="utf-8")
        except OSError:
            log.warning("Cannot write %s, thus git can show the build output as untracked", marker, exc_info=True)


# The build directory of a background build before this layout, relative to
# the project root.
_LEGACY_AUTOBUILD_REL: Path = Path(".fw-context") / "autobuild"

# Files and directories that fw-context wrote directly into the build root
# before this layout: the copies of each database, the PlatformIO sidecar,
# and the dependency files of the manual backend.
_LEGACY_SIDECAR_NAME = "platformio_link.json"
_LEGACY_DEPS_NAME = "deps"


def _is_legacy_database(path: Path) -> bool:
    """Say if *path* is a copy of a database in the build root, as fw-context wrote it before this layout.

    The copies were ``compile_commands.json`` and, for each variant and
    image, ``compile_commands.<variant>.<image>.json``.
    """
    name = path.name
    return name == COMPILE_COMMANDS_NAME or (name.startswith("compile_commands.") and name.endswith(".json"))


def _legacy_candidates(project_root: Path) -> list[Path]:
    """Return the paths of the layout before this one that are there.

    A directory in the build root is the directory of a variant when it
    holds ``out/``: then it stays, also when its name is ``deps``, also on a
    file system that ignores the case of a name.  The sidecar and the
    database copies are files; a directory of that name is a variant.
    """
    candidates: list[Path] = []
    autobuild = project_root / _LEGACY_AUTOBUILD_REL
    if autobuild.exists() or autobuild.is_symlink():
        candidates.append(autobuild)
    root = project_root / BUILD_ROOT_REL
    if not root.is_dir():
        return candidates
    candidates += sorted(p for p in root.iterdir() if p.is_file() and _is_legacy_database(p))
    sidecar = root / _LEGACY_SIDECAR_NAME
    if sidecar.is_file():
        candidates.append(sidecar)
    deps = root / _LEGACY_DEPS_NAME
    if deps.is_dir() and not deps.is_symlink() and not (deps / OUT_DIR_NAME).exists():
        candidates.append(deps)
    return candidates


def _protects(path: Path, keep: list[Path]) -> bool:
    """Say if *path* is a kept file, or a directory that holds one.

    ``os.path.samefile`` compares two paths that exist: a file system that
    ignores the case of a name (macOS, Windows) gives one file for two
    spellings, and ``Path.resolve`` does not fold the case.  A path that
    cannot be compared counts as kept: a doubt never removes a file.
    """
    for kept in keep:
        for candidate in (kept, *kept.parents):
            try:
                if candidate.exists() and path.exists():
                    if os.path.samefile(candidate, path):
                        return True
                elif candidate.resolve() == path.resolve():
                    return True
            except (OSError, RuntimeError):
                # RuntimeError: Path.resolve on a loop of symbolic links
                # (Python 3.11 and 3.12).
                return True
    return False


def has_legacy_output(project_root: Path) -> bool:
    """Say if the build output of an older fw-context is there, see :func:`remove_legacy_output`.

    A directory that cannot be read counts as one with old output, thus the
    removal runs and gives its warning.
    """
    try:
        return bool(_legacy_candidates(project_root))
    except OSError:
        return True


def remove_legacy_output(project_root: Path, keep: Iterable[Path] = ()) -> list[Path]:
    """Remove the build output that fw-context wrote before ``.fw-context/build/<variant>/out``.

    The paths are ``.fw-context/autobuild/``, the copies of each database in
    ``.fw-context/build/``, ``.fw-context/build/platformio_link.json`` and
    ``.fw-context/build/deps/``.  Nothing writes them since this layout, and
    an old copy of a database looks like a build that is there.

    *keep* holds the databases that must stay: each database that a build
    of the index reads, and the file that the user gives.  A path in
    *keep*, and a directory that holds one, stays.  The old index reads
    the old copies until a run indexes the build again, thus a copy goes
    only when no build of the index reads it any more.

    One log line for each removed path, because the user did not ask for
    the removal.  A path that cannot be read or removed gives a warning,
    and the run continues: the old output takes space, and it does not
    change an answer.  Returns the removed paths.
    """
    kept = list(keep)
    try:
        candidates = _legacy_candidates(project_root)
    except OSError as exc:
        log.warning("Cannot read the build output of an older fw-context in %s: %s", project_root, exc)
        return []

    removed: list[Path] = []
    for path in candidates:
        if _protects(path, kept):
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                # A symbolic link goes, and not what it names.
                path.unlink()
        except OSError as exc:
            log.warning("Cannot remove the build output of an older fw-context, %s: %s", path, exc)
            continue
        log.info("Removed the build output of an older fw-context: %s", path)
        removed.append(path)
    return removed
