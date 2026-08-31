"""IAR EWARM build system — convert ``.ewp`` to compile_commands.json.

Uses ``keil2clangd`` to parse the IAR project file (XML) and generate
``compile_commands.json`` **without building** — no IAR installation needed.
Only the project structure, include paths, defines, and toolchain info are
extracted from the project file.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class IARBuildSystem:
    """IAR EWARM — compile_commands.json via ``keil2clangd`` conversion.

    Does NOT build the project — only parses the ``.ewp`` file.
    Requires ``keil2clangd`` (``pip install keil2clangd``).

    WHY keil2clangd (not bear + iarbuild): IAR's command-line build tools
    (iarbuild.exe) are Windows-only and don't integrate with bear's
    LD_PRELOAD interception.  keil2clangd parses the .ewp XML project file
    to extract compiler flags, include paths, and defines without running
    the compiler — it works cross-platform and doesn't require the IAR
    toolchain to be installed.
    """

    name: str = "IAR EWARM"
    config_key: str = "iar-ewarm"
    markers: list[str] = [".ewp", ".eww"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return bool(list(root.glob("*.ewp"))) or bool(list(root.glob("*.eww")))

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

        if cfg.python:
            keil_prefix: list[str] = [cfg.python, "-m", "keil2clangd"]
        elif not shutil.which("keil2clangd"):
            raise RuntimeError(
                "keil2clangd is required for IAR EWARM projects.\n"
                "Install it:\n"
                "  pip install keil2clangd\n"
                "Or generate compile_commands.json manually and run:\n"
                "  fw-context index compile_commands.json"
            )
        else:
            keil_prefix = ["keil2clangd"]

        # Find the .ewp file
        if cfg.iar_project:
            proj_path = root / cfg.iar_project
        else:
            candidates = list(root.glob("*.ewp"))
            if not candidates:
                raise RuntimeError(
                    "No .ewp file found in project root.\n"
                    "Set iar_project in .fw-context/config.toml:\n"
                    '  [build]\n  iar_project = "Project.ewp"'
                )
            proj_path = candidates[0]

        if not proj_path.exists():
            raise RuntimeError(f"IAR project file not found: {proj_path}")

        cmd: list[str] = keil_prefix + [str(proj_path)]

        if cfg.iar_target:
            cmd += ["-t", cfg.iar_target]

        if cfg.toolchain_path:
            cmd += ["--toolchain-bin", cfg.toolchain_path]

        if cfg.toolchain_prefix:
            cmd += ["--toolchain-prefix", cfg.toolchain_prefix]

        cc_path = cc_output_path(root)
        cmd += ["-o", str(cc_path)]

        log.info("iar convert: %s", " ".join(cmd))
        run_build_command(cmd, cwd=root, description="iar convert", build_cfg=cfg)

        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — keil2clangd may have failed silently")

        log.info("IAR convert produced %s", cc_path)
        return cc_path

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["keil2clangd"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "Install keil2clangd:\n"
            "  pip install keil2clangd"
        )

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — the backend converts an .ewp and compiles nothing."""
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
        """Return nothing: IAR uses a linker configuration file.

        ilink takes an `.icf` file, whose grammar is not the grammar of GNU
        ld.  `indexer.linker_script` would read it wrong, and a wrong
        memory map is worse than none.  This backend detects only.
        """
        return []

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return []

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return no pattern — IAR EWARM keeps its device support outside the project.

        The header files and the libraries come from the EWARM installation
        directory.  A path outside project_root is vendor by position and
        needs no pattern.
        """
        return []


# Register
registry.register(IARBuildSystem)
