"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ...config.settings import Config

_PIO_MARKERS = ["platformio.ini"]


class PlatformIOBuildSystem:
    """PlatformIO build system (``pio run --target compiledb``)."""

    name: str = "PlatformIO"
    config_key: str = "platformio"
    markers: list[str] = ["platformio.ini"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _PIO_MARKERS)

    # ── Build ──

    def build(self, project_root: Path, cfg: Config) -> Path:
        from ..build import _build_platformio

        return _build_platformio(project_root, cfg.build)

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        """Ensure PlatformIO generates ``.d`` files via ``-MMD``.

        Delegates to the existing ``_ensure_platformio_dep_tracking`` helper
        in ``cli.py`` until Phase 2 moves it here.
        """
        from ...cli import _ensure_platformio_dep_tracking

        # The existing function prints its own output; run it and return a summary.
        _ensure_platformio_dep_tracking(project_root, fix=fix)
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["pio"]


# Register
registry.register(PlatformIOBuildSystem)
