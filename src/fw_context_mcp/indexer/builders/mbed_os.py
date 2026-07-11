"""Mbed OS build system — detection, build, and validation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ...config.settings import Config

_MBED_MARKERS = [".mbed", "mbed-os", "mbed_app.json"]


class MbedOSBuildSystem:
    """Mbed OS 5/6 build system (``mbed compile`` via ``bear``)."""

    name: str = "Mbed OS"
    config_key: str = "mbed-os"
    markers: list[str] = [".mbed", "mbed-os", "mbed_app.json"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _MBED_MARKERS)

    # ── Build ──

    def build(self, project_root: Path, cfg: Config) -> Path:
        from ..build import _build_mbed_os

        return _build_mbed_os(project_root, cfg.build)

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        # GCC always emits .d files with Mbed OS — nothing to configure.
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["bear", "mbed"]


# Register
registry.register(MbedOSBuildSystem)
