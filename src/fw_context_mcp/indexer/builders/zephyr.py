"""Zephyr build system — detection, build, and validation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ...config.settings import Config

_ZEPHYR_MARKERS = ["west.yml", "zephyr"]


class ZephyrBuildSystem:
    """Zephyr RTOS build system (``west build``)."""

    name: str = "Zephyr"
    config_key: str = "zephyr"
    markers: list[str] = ["west.yml", "zephyr"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _ZEPHYR_MARKERS)

    # ── Build ──

    def build(self, project_root: Path, cfg: Config) -> Path:
        from ..build import _build_zephyr

        return _build_zephyr(project_root, cfg.build)

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        # CMake + GCC always emits .d files — nothing to configure.
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["west"]


# Register
registry.register(ZephyrBuildSystem)
