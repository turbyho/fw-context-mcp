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

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return path patterns for build-output directories.

        Patterns are relative to *project_root* and used to identify
        generated/transient files that should not trigger staleness
        (e.g. ``["BUILD/"]`` for Mbed OS, ``[".pio/"]`` for PlatformIO,
        ``["build/", "cmake-build-"]`` for CMake).

        An empty list means no build-directory filtering is needed.
        """
        ...
