"""Tier 3/4 stub builders — detection without automated build.

These builders recognise proprietary/legacy build systems and provide
helpful instructions for generating ``compile_commands.json`` manually.
They cannot build automatically — the user must provide a
``compile_commands.json`` or a custom ``[build] command`` in config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class STM32CubeIDEStub:
    """STM32CubeIDE (``.cproject``, ``.project``).

    Eclipse-based IDE with a built-in JSON Compilation Database generator.
    Go to Project Properties → C/C++ Build → Builder Settings →
    Generate JSON Compilation Database.
    """

    name: str = "STM32CubeIDE"
    config_key: str = "stm32cubeide"
    markers: list[str] = [".cproject", ".project"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / ".cproject").exists() or (root / ".project").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "STM32CubeIDE detected, but automatic build is not supported.\n"
            "Enable JSON Compilation Database in CubeIDE:\n"
            "  Project Properties → C/C++ Build → Builder Settings →\n"
            "  ✓ Generate JSON Compilation Database\n"
            "Then run 'fw-context index' with the generated file."
        )

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return []


class TICCSStub:
    """TI Code Composer Studio (``.projectspec``).

    Eclipse-based IDE. Similar to STM32CubeIDE — enable the built-in
    JSON Compilation Database generator in Project Settings.
    """

    name: str = "TI Code Composer Studio"
    config_key: str = "ti-ccs"
    markers: list[str] = [".projectspec"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / ".projectspec").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "TI Code Composer Studio detected, but automatic build is not supported.\n"
            "Enable JSON Compilation Database in CCS Project Settings, or\n"
            "use bear to wrap a headless build:\n"
            "  bear -- eclipse -nosplash -application com.ti.ccstudio.apps.projectBuild ..."
        )

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return []


# Register stub builders
registry.register(STM32CubeIDEStub)
registry.register(TICCSStub)
