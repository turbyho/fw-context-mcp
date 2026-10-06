"""Arduino CLI build system — detection, build, and validation."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, run_build_command

from ..build_layout import BuildLayout
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

        build_dir = BuildLayout(project_root).out_dir(cfg.variant_name)
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
        run_build_command(
            base_cmd + ["--only-compilation-database"],
            cwd=project_root,
            description="arduino-cli compile --only-compilation-database",
            build_cfg=cfg,
        )

        # The database stays in the build directory when arduino-cli writes
        # it there.  An arduino-cli that writes it into the sketch directory
        # gets a copy in the build directory, so the index never reads a file
        # that the next build of the user can replace.
        target_cc = build_dir / "compile_commands.json"
        if not target_cc.exists():
            cc_in_root = project_root / "compile_commands.json"
            if not cc_in_root.exists():
                raise RuntimeError(
                    "compile_commands.json not generated. Ensure arduino-cli supports --only-compilation-database."
                )
            target_cc = cc_output_path(project_root, cfg)
            shutil.copy2(cc_in_root, target_cc)
            log.info("Copied %s → %s", cc_in_root, target_cc)

        # ── Pass 2: real compile to produce .d dependency files ──
        log.info("arduino build (compile): %s", " ".join(base_cmd))
        try:
            run_build_command(base_cmd, cwd=project_root, description="arduino-cli compile (real build for .d files)", build_cfg=cfg)
        except RuntimeError:
            # Real compile failed — but we already have the compilation
            # database.  Warn and continue (the indexer can still work,
            # it just won't have .d files for incremental reindexing).
            log.warning("arduino-cli real compile failed — .d files may be missing")

        return target_cc

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — ``--build-path`` puts every artifact in the output directory of fw-context."""
        return True

    # ── Build dir patterns ──

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return nothing: the Arduino build records no link command.

        arduino-cli compiles through a temporary build directory and keeps
        no artifact that names the script of the core it linked.  No
        Arduino project is available to measure, thus this backend answers
        with nothing rather than with a path from a pattern.
        """
        return []

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return no pattern — Arduino has no in-tree vendor directory.

        The Arduino CLI keeps the cores and the libraries in the sketchbook
        directory, which is outside the project.  An in-tree ``libraries/``
        folder is a team convention, not a rule of the build system, so a
        pattern for it would hide code the team owns.
        """
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

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return ""


# Register
registry.register(ArduinoBuildSystem)
