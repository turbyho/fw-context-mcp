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
