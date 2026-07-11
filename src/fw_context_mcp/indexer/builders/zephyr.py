"""Zephyr build system — detection, build, and validation."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

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

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``west build``."""
        if not shutil.which("west"):
            raise RuntimeError(
                "west is required for Zephyr builds.  Install the Zephyr SDK and west tool."
            )

        if not cfg.board:
            raise RuntimeError(
                "Zephyr requires a board name.  Set it in .fw-context/config.toml:\n"
                "  [build]\n  board = \"your_board\""
            )

        build_dir = project_root / "build"

        cmd: list[str] = [
            "west", "build",
            "-b", cfg.board,
            "-d", str(build_dir),
        ]

        if cfg.clean:
            cmd.append("--pristine")
        cmd.append("--")
        cmd.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")

        log.info("zephyr build: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=project_root)

        if result.returncode != 0:
            raise RuntimeError(f"west build failed with exit code {result.returncode}")

        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                "compile_commands.json not found in build directory. "
                "Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled."
            )

        # Copy to project root for consistency
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        # CMake + GCC always emits .d files — nothing to configure.
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        issues: list[BuildIssue] = []
        # Zephyr via CMake: CMAKE_EXPORT_COMPILE_COMMANDS is enabled so
        # compile_commands.json is complete.  No extra builder-specific checks.
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["west"]


# Register
registry.register(ZephyrBuildSystem)
