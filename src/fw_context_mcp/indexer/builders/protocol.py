"""Build system protocol — shared interface for all build system backends.

Defines the ``BuildSystem`` protocol class and ``BuildIssue`` dataclass.
Each concrete builder (PlatformIO, Zephyr, Mbed OS, …) implements this
interface so the index orchestration layer can treat them uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..build import BuildConfig


@dataclass
class BuildIssue:
    """A problem detected during build-artifact validation.

    Attributes:
        severity: ``"error"`` (blocks indexing) or ``"warning"`` (advisory).
        category: Machine-readable tag — ``"missing_compile_commands"``,
            ``"incomplete_compile_commands"``, etc.
        message: Human-readable description of the issue.
        auto_fixable: Whether ``auto_fix()`` can resolve this issue.
        fix_hint: Brief description of the fix, shown to the user.
    """

    severity: str  # "error" | "warning"
    category: str
    message: str
    auto_fixable: bool = False
    fix_hint: str | None = None


class BuildSystem(Protocol):
    """Interface for a build system backend.

    Each concrete class represents one build system (PlatformIO, Zephyr,
    Mbed OS, ESP-IDF, …).  Classmethods are used for detection so the
    registry can discover the active system without instantiating every
    backend.
    """

    # ── Class-level metadata ──

    name: str
    """Human-readable name shown in logs and status output."""

    config_key: str
    """Key used in ``[build] system = "..."`` config and registry lookups."""

    # ── Classmethods (called without an instance) ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        """Return True if this build system's markers are present in *project_root*."""
        ...

    # ── Instance methods (require configuration) ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Run the build and return the path to ``compile_commands.json``."""
        ...

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """Check build artifacts for completeness and correctness."""
        ...

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        """Attempt to fix a single ``BuildIssue``. Returns True on success."""
        ...

    def required_tools(self) -> list[str]:
        """Return the CLI tool names required by this build system."""
        ...

    # ── Optional: automatic background build ──
    # Not every backend implements the method below.  The concrete classes
    # are structural implementations of this Protocol, not subclasses, thus
    # a default here would never reach them.  Ask through
    # ``builders.background_build_safe`` instead, which treats a missing
    # method as "no" — the safe answer, because it stops fw-context from
    # starting a build that could collide with the one in the IDE.

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Tell whether fw-context may run this build on its own.

        fw-context starts a build by itself when it finds a source file that
        compile_commands.json does not cover.  That build runs while the user
        works, possibly while an IDE builds the same project, and fw-context
        cannot lock the build of the IDE — the IDE knows nothing about it.

        Answer True only when one of these holds:

        * The backend writes every artifact under
          ``cfg.isolated_build_dir``, thus the two builds cannot meet.
        * The backend compiles nothing (a converter, or a dry run), thus
          there is no artifact to corrupt.

        Answer False otherwise, and for a backend that cannot build at all.
        *cfg* is a parameter because the answer can depend on it: the
        makefile backend compiles only when ``make_dry_run`` is off.
        """
        ...

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return path patterns for build-output directories.

        Patterns are relative to *project_root* and used to identify
        generated/transient files that should not trigger staleness
        (e.g. ``["BUILD/"]`` for Mbed OS, ``[".pio/"]`` for PlatformIO,
        ``["build/", "cmake-build-"]`` for CMake).

        An empty list means no build-directory filtering is needed.
        """
        ...

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return LIKE patterns for the vendor and SDK trees INSIDE the project.

        Answers one question: which in-tree paths hold code that the team does
        not own?  A path outside project_root needs no pattern — it is vendor by
        position.  Build output needs no pattern either: it has
        get_build_dir_patterns(), and generated code counts as project code.

        Return an empty list when the build system mandates no in-tree vendor
        directory.  A guess is worse than nothing here: a wrong pattern hides
        the team's own code from a project_only query, and it makes the
        staleness check trust a tree that the team edits.

        *units* are this build's translation units, or None on the query side.
        A builder that needs the compiler flags reads them from *units*.  With
        None it must use the environment and the project files, so it stays
        usable before the units are parsed.
        """
        return []

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return the linker scripts of this build, or an empty list.

        The linker script defines symbols that no translation unit defines —
        the initial stack pointer of a vector table, the boundaries of
        `.data` and `.bss` — and it holds the memory map.  It is an INPUT to
        the linker, thus `compile_commands.json` never names it, and each
        build system keeps it somewhere else.

        Answer with a path only when the BUILD names it.  A path built from
        a pattern that looks right is a guess, and a wrong script puts a
        wrong memory map in front of a user who cannot tell.  An empty list
        is the correct answer for a build system that records nothing, and
        `builders._linker` holds the mechanisms that more than one backend
        shares.

        The return type is a list because one build can pass several
        scripts: ESP-IDF splits its script into about ten files, and Zephyr
        passes a final script and a pre-pass script.

        *compile_commands* is this build's compile_commands.json, whose
        directory is the build directory for a CMake backend.  *variant*
        names the build variant, which a backend needs when it keeps one
        output directory per variant.  *units* are this build's translation
        units, which a backend reads when the compiler flags name the
        output tree — mbed-tools writes one tree per toolchain and profile,
        and the flags say which one this build used.

        Ask through ``builders.linker_scripts``, which treats a missing
        method as "no script".  The concrete classes implement this
        Protocol structurally and not by inheritance, thus a default here
        would never reach them.
        """
        return []

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        """Auto-detect build environment paths for this build system.

        Returns a dict with keys ``python`` and ``activate``.
        Values are resolved absolute paths (as strings), or ``None``
        when not detected.

        Called by ``fw-context init`` — results are written to
        ``.fw-context/local.toml``.

        Default implementation returns no detections — builders that
        need a specific Python interpreter or shell activation script
        should override this.
        """
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        """Human-readable instructions for manual environment setup.

        Shown by ``fw-context init`` when ``detect_environment()``
        returns no results and ``required_tools()`` are not on PATH.
        """
        return ""
