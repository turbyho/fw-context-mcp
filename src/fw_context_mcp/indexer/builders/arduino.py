"""Arduino CLI build system — detection, build, and validation."""

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
        """Generate compile_commands.json via ``arduino-cli compile``.

        Runs two passes:
        1. ``--only-compilation-database`` — dry-run that generates
           ``compile_commands.json`` (but no ``.o`` / ``.d`` files).
        2. Real compile (no special flags) — produces ``.d`` dependency
           files so the indexer can track header changes.
        """
        if not shutil.which("arduino-cli"):
            raise RuntimeError(
                "arduino-cli is required for Arduino builds.  Install it:\n"
                "  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
            )

        if not cfg.fqbn:
            raise RuntimeError(
                "Arduino requires a board FQBN.  Set it in .fw-context/config.toml:\n"
                '  [build]\n  fqbn = "arduino:avr:uno"'
            )

        build_dir = project_root / "build"
        if cfg.clean and build_dir.exists():
            shutil.rmtree(build_dir)

        build_path_flag = str(build_dir)
        base_cmd: list[str] = [
            "arduino-cli",
            "compile",
            "--fqbn",
            cfg.fqbn,
            "--build-path",
            build_path_flag,
        ]

        # ── Pass 1: dry-run to generate compile_commands.json ──
        log.info("arduino build (dry-run): %s --only-compilation-database", " ".join(base_cmd))
        dry_run = subprocess.run(
            base_cmd + ["--only-compilation-database"],
            cwd=project_root,
        )
        if dry_run.returncode != 0:
            raise RuntimeError(
                f"arduino-cli compile --only-compilation-database failed with exit code {dry_run.returncode}"
            )

        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            cc_in_root = project_root / "compile_commands.json"
            if cc_in_root.exists():
                cc_in_build = cc_in_root
            else:
                raise RuntimeError(
                    "compile_commands.json not generated. Ensure arduino-cli supports --only-compilation-database."
                )

        # Copy to project root for consistency BEFORE the real compile,
        # which may overwrite build/compile_commands.json (or not — but
        # without --only-compilation-database it typically won't).
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        # ── Pass 2: real compile to produce .d dependency files ──
        log.info("arduino build (compile): %s", " ".join(base_cmd))
        real = subprocess.run(base_cmd, cwd=project_root)
        if real.returncode != 0:
            # Real compile failed — but we already have the compilation
            # database.  Warn and continue (the indexer can still work,
            # it just won't have .d files for incremental reindexing).
            log.warning("arduino-cli real compile failed with exit code %d — .d files may be missing", real.returncode)

        return target_cc

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

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
