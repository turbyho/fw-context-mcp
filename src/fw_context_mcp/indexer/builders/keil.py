"""Keil MDK build system — convert ``.uvprojx`` to compile_commands.json.

Uses ``keil2clangd`` to parse the Keil project file (XML) and generate
``compile_commands.json`` **without building** — no Keil installation needed.
Only the project structure, include paths, defines, and toolchain info are
extracted from the project file.
"""

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


class KeilBuildSystem:
    """Keil MDK — compile_commands.json via ``keil2clangd`` conversion.

    Does NOT build the project — only parses the ``.uvprojx`` file.
    Requires ``keil2clangd`` (``pip install keil2clangd``).
    """

    name: str = "Keil MDK"
    config_key: str = "keil-mdk"
    markers: list[str] = [".uvprojx"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return bool(list(root.glob("*.uvprojx")))

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Delegates to convert() — Keil projects don't need a build."""
        return self.convert(project_root, cfg)

    def convert(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json from ``.uvprojx`` via keil2clangd.

        Requires ``keil2clangd`` to be installed.  When ``keil_project``
        is not set in config, the first ``*.uvprojx`` in *project_root*
        is used.
        """
        root = project_root.resolve()

        if not shutil.which("keil2clangd"):
            raise RuntimeError(
                "keil2clangd is required for Keil MDK projects.\n"
                "Install it:\n"
                "  pip install keil2clangd\n"
                "Or generate compile_commands.json manually and run:\n"
                "  fw-context index compile_commands.json"
            )

        # Find the .uvprojx file
        if cfg.keil_project:
            proj_path = root / cfg.keil_project
        else:
            candidates = list(root.glob("*.uvprojx"))
            if not candidates:
                raise RuntimeError(
                    "No .uvprojx file found in project root.\n"
                    "Set keil_project in .fw-context/config.toml:\n"
                    '  [build]\n  keil_project = "Project.uvprojx"'
                )
            proj_path = candidates[0]

        if not proj_path.exists():
            raise RuntimeError(f"Keil project file not found: {proj_path}")

        cmd: list[str] = ["keil2clangd", str(proj_path)]

        if cfg.keil_target:
            cmd += ["-t", cfg.keil_target]

        if cfg.keil_cmsis_path:
            cmd += ["--cmsis", cfg.keil_cmsis_path]

        if cfg.toolchain_path:
            cmd += ["--toolchain-bin", cfg.toolchain_path]

        if cfg.toolchain_prefix:
            cmd += ["--toolchain-prefix", cfg.toolchain_prefix]

        cc_path = root / "compile_commands.json"
        cmd += ["-o", str(cc_path)]

        log.info("keil convert: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=root)

        if result.returncode != 0:
            raise RuntimeError(
                f"keil2clangd failed with exit code {result.returncode}.\n"
                "Check that keil_project, keil_cmsis_path, and toolchain_path are correct."
            )

        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — keil2clangd may have failed silently")

        log.info("Keil convert produced %s", cc_path)
        return cc_path

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["keil2clangd"]

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return []


# Register
registry.register(KeilBuildSystem)
