"""Arduino CLI build system — detection, build, and validation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class ArduinoBuildSystem:
    """Arduino CLI build system (``arduino-cli compile --export-compile-commands``).

    Detected by ``.ino`` files in the project root or a ``sketch.yaml``.
    """

    name: str = "Arduino"
    config_key: str = "arduino"
    markers: list[str] = ["sketch.yaml"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        # sketch.yaml is the definitive marker (Arduino CLI 1.0+)
        if (root / "sketch.yaml").exists():
            return True
        # Legacy: .ino file in project root
        if list(root.glob("*.ino")):
            return True
        return False

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``arduino-cli compile --export-compile-commands``."""
        if not shutil.which("arduino-cli"):
            raise RuntimeError(
                "arduino-cli is required for Arduino builds.  Install it:\n"
                "  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
            )

        if not cfg.fqbn:
            raise RuntimeError(
                "Arduino requires a board FQBN.  Set it in .fw-context/config.toml:\n"
                "  [build]\n  fqbn = \"arduino:avr:uno\""
            )

        # --only-compilation-database is the flag in arduino-cli 1.x;
        # --export-compile-commands is the renamed flag in newer versions.
        # Detect which one is supported by inspecting the help output.
        help_result = subprocess.run(
            ["arduino-cli", "compile", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        export_flag = (
            "--export-compile-commands"
            if "--export-compile-commands" in help_result.stdout
            else "--only-compilation-database"
        )

        cmd: list[str] = [
            "arduino-cli", "compile",
            "--fqbn", cfg.fqbn,
            export_flag,
            "--build-path", str(project_root / "build"),
        ]

        if cfg.clean:
            build_dir = project_root / "build"
            if build_dir.exists():
                shutil.rmtree(build_dir)

        log.info("arduino build: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=project_root)

        if result.returncode != 0:
            raise RuntimeError(f"arduino-cli compile failed with exit code {result.returncode}")

        cc_in_build = project_root / "build" / "compile_commands.json"
        if not cc_in_build.exists():
            # Arduino CLI sometimes puts it in project root
            cc_in_root = project_root / "compile_commands.json"
            if cc_in_root.exists():
                return cc_in_root
            raise RuntimeError(
                "compile_commands.json not generated. "
                "Ensure arduino-cli supports --only-compilation-database or --export-compile-commands."
            )

        # Copy to project root for consistency
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        # GCC always emits .d files with Arduino.
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["arduino-cli"]


# Register
registry.register(ArduinoBuildSystem)
