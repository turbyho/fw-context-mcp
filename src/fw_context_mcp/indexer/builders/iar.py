"""IAR EWARM build system — convert ``.ewp`` to compile_commands.json.

Uses ``keil2clangd`` to parse the IAR project file (XML) and generate
``compile_commands.json`` **without building** — no IAR installation needed.
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


class IARBuildSystem:
    """IAR EWARM — compile_commands.json via ``keil2clangd`` conversion.

    Does NOT build the project — only parses the ``.ewp`` file.
    Requires ``keil2clangd`` (``pip install keil2clangd``).
    """

    name: str = "IAR EWARM"
    config_key: str = "iar-ewarm"
    markers: list[str] = [".ewp", ".eww"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (
            bool(list(root.glob("*.ewp")))
            or bool(list(root.glob("*.eww")))
        )

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Delegates to convert() — IAR projects don't need a build."""
        return self.convert(project_root, cfg)

    def convert(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json from ``.ewp`` via keil2clangd.

        Requires ``keil2clangd`` to be installed.  When ``iar_project``
        is not set in config, the first ``*.ewp`` in *project_root*
        is used.
        """
        root = project_root.resolve()

        if not shutil.which("keil2clangd"):
            raise RuntimeError(
                "keil2clangd is required for IAR EWARM projects.\n"
                "Install it:\n"
                "  pip install keil2clangd\n"
                "Or generate compile_commands.json manually and run:\n"
                "  fw-context index compile_commands.json"
            )

        # Find the .ewp file
        if cfg.iar_project:
            proj_path = root / cfg.iar_project
        else:
            candidates = list(root.glob("*.ewp"))
            if not candidates:
                raise RuntimeError(
                    "No .ewp file found in project root.\n"
                    "Set iar_project in .fw-context/config.toml:\n"
                    "  [build]\n  iar_project = \"Project.ewp\""
                )
            proj_path = candidates[0]

        if not proj_path.exists():
            raise RuntimeError(f"IAR project file not found: {proj_path}")

        cmd: list[str] = ["keil2clangd", str(proj_path)]

        if cfg.iar_target:
            cmd += ["-t", cfg.iar_target]

        if cfg.toolchain_path:
            cmd += ["--toolchain-bin", cfg.toolchain_path]

        if cfg.toolchain_prefix:
            cmd += ["--toolchain-prefix", cfg.toolchain_prefix]

        cc_path = root / "compile_commands.json"
        cmd += ["-o", str(cc_path)]

        log.info("iar convert: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=root)

        if result.returncode != 0:
            raise RuntimeError(
                f"keil2clangd failed with exit code {result.returncode}.\n"
                "Check that iar_project, toolchain_path are correct."
            )

        if not cc_path.exists():
            raise RuntimeError(
                "compile_commands.json was not generated — keil2clangd may have failed silently"
            )

        log.info("IAR convert produced %s", cc_path)
        return cc_path

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["keil2clangd"]


# Register
registry.register(IARBuildSystem)
